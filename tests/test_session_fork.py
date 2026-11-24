"""会话分叉：副本只保留目标轮次之前的历史，原会话与用量账本不受影响。"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agno.agent.agent import Agent
from agno.db.sqlite import SqliteDb
from fixtures import FakeModel, text

from aaa.model.provider import parse_cost
from aaa.sessions.fork import fork_before, user_turns
from aaa.sessions.query import list_sessions
from aaa.usage import ledger

METRICS = {
    "input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500,
    "details": {"model": [{
        "id": "fake-model", "provider": "Fake", "input_tokens": 1000, "output_tokens": 500,
        "cache_read_tokens": 0, "total_tokens": 1500,
    }]},
}


class SnapshotModel(FakeModel):
    """记下每次调用当时看到的消息。FakeModel 存的是同一个列表对象，事后会被追加。"""

    def __init__(self, script):
        super().__init__(script)
        self.seen = []

    def invoke(self, **kwargs):
        self.seen.append([
            (message.role, message.content) for message in kwargs["messages"]
            if isinstance(message.content, str)
        ])
        return super().invoke(**kwargs)


class SessionForkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db = SqliteDb(db_file=str(self.dir / "sessions.db"))
        self.addCleanup(self.db.db_engine.dispose)
        self.model = SnapshotModel([text(f"ans{index}") for index in range(1, 7)])
        self.agent = Agent(
            model=self.model, db=self.db, add_history_to_context=True,
            instructions="SYS", markdown=False, telemetry=False,
        )
        self.runs = [
            self.agent.run(f"q{index}", session_id="s1", session_state={"todos": ["a"] * index})
            for index in (1, 2, 3)
        ]

    def _give_usage(self):
        """给库里的每个 run 补上用量；假模型不带 metrics。"""
        conn = sqlite3.connect(self.dir / "sessions.db")
        try:
            for run_id, raw in conn.execute("SELECT run_id, run_data FROM agno_runs").fetchall():
                data = json.loads(raw)
                data["metrics"] = METRICS
                data.setdefault("metadata", {})["channel"] = "fake"
                conn.execute("UPDATE agno_runs SET run_data = ? WHERE run_id = ?", (json.dumps(data), run_id))
            conn.commit()
        finally:
            conn.close()

    def test_turns_list_completed_user_turns_in_order(self):
        turns = user_turns(self.agent, "s1")
        self.assertEqual([(turn.number, turn.text) for turn in turns], [(1, "q1"), (2, "q2"), (3, "q3")])
        self.assertEqual([turn.run_id for turn in turns], [run.run_id for run in self.runs])

    def test_fork_keeps_earlier_turns_and_leaves_source_untouched(self):
        new_id = fork_before(self.agent, "s1", self.runs[1].run_id)
        self.assertEqual(len(self.db.get_runs(session_id=new_id)), 1)
        self.assertEqual(len(self.db.get_runs(session_id="s1")), 3)
        self.agent.run("edited", session_id=new_id)
        # 模型只看到目标轮次之前的对话，被放弃的两轮不在。
        self.assertEqual(self.model.seen[-1][1:], [("user", "q1"), ("assistant", "ans1"), ("user", "edited")])

    def test_fork_before_first_turn_starts_empty(self):
        new_id = fork_before(self.agent, "s1", self.runs[0].run_id)
        self.assertEqual(self.db.get_runs(session_id=new_id), [])
        self.agent.run("fresh", session_id=new_id)
        self.assertEqual(self.model.seen[-1][1:], [("user", "fresh")])

    def test_fork_restores_state_of_last_kept_turn(self):
        new_id = fork_before(self.agent, "s1", self.runs[1].run_id)
        forked = self.db.get_session(new_id, runs_limit=0)
        self.assertEqual(forked.session_data["session_state"], {"todos": ["a"]})
        self.assertEqual(forked.session_data["forked_from_session_id"], "s1")
        source = self.db.get_session("s1", runs_limit=0)
        self.assertEqual(source.session_data["session_state"], {"todos": ["a", "a", "a"]})

    def test_fork_before_unknown_run_changes_nothing(self):
        before = len(list_sessions(self.agent, limit=10))
        with self.assertRaises(ValueError):
            fork_before(self.agent, "s1", "missing")
        self.assertEqual(len(list_sessions(self.agent, limit=10)), before)

    def test_copied_turns_are_not_billed_twice(self):
        cost = parse_cost({"input": 0.3, "output": 1.2, "cache_read": 0})

        def price(channel, model_id):
            return cost

        usage_db = self.dir / "usage.db"
        self._give_usage()
        first = ledger.sync(price=price, sessions_db=self.dir / "sessions.db", usage_db=usage_db)
        self.assertEqual(first.added, 3)
        fork_before(self.agent, "s1", self.runs[1].run_id)
        self._give_usage()
        second = ledger.sync(price=price, sessions_db=self.dir / "sessions.db", usage_db=usage_db)
        self.assertEqual((second.added, second.updated), (0, 0))
        conn = sqlite3.connect(usage_db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0], 3)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
