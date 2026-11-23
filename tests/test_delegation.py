"""Offline native Agno round trips through aaa delegation tools."""

import json
import re
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event
from types import SimpleNamespace

from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.run import RunContext
from agno.run.cancel import is_cancelled
from fixtures import FakeModel, text, tool_call

from aaa.boundary.policy import resolve_policy
from aaa.cli.run import stream_run
from aaa.config.settings import Settings
from aaa.model.catalog import ModelRef, Resolved
from aaa.runtime.agent import tools_for_mode
from aaa.runtime.delegation import make_delegation_tools
from aaa.runtime.delegation_registry import DelegationRegistry


class Emitter:
    def __init__(self, grant=False):
        self.grant = grant
        self.asked = []

    def event(self, kind, **data):
        pass

    def close(self):
        pass

    def ask_approval(self, name, args, diff=None):
        self.asked.append((name, args))
        return self.grant


def bundle(name, entry=None, **deps):
    return {"entry": name, "definitions": {name: entry or actor(), **deps}}


def actor(model="worker", **kwargs):
    return dict(kind="agent", instructions="Do the task", model=model, **kwargs)


def register(spec):
    return tool_call("register_delegation", {"definition": spec})


def delegate(*refs):
    return tool_call("delegate_task", {"tasks": [{"ref": ref, "input": "Work"} for ref in refs]})


class DelegationTest(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.db = SqliteDb(db_file=str(self.root / "main.db"))
        self.addCleanup(self.db.db_engine.dispose)
        self.registry = DelegationRegistry(self.root / "saved")
        self.settings = Settings({"delegation.max_parallel": 2})

    def main(self, script, models, mode="read-only", registry=None):
        refs = [ModelRef("offline", name) for name in models]
        catalog = SimpleNamespace(build=lambda _, name: models[name](), resolve=lambda name, pool: next(
            (Resolved(ref) for ref in pool if name in (ref.model_id, ref.display)), None))
        return Agent(model=FakeModel([*script, text("parent done")]), db=self.db, telemetry=False,
                     tools=make_delegation_tools(registry or self.registry, catalog, refs, refs[0],
                                                 lambda: tools_for_mode(mode, self.settings,
                                                                        lambda m: resolve_policy(m, self.root)),
                                                 self.settings))

    def run_main(self, main, session="first", emitter=None, **kwargs):
        result = stream_run(main, "parent task", session, emitter=emitter or Emitter(), **kwargs)
        self.assertFalse(result.get("error"), result)
        messages = main.model.invocations[-1]["messages"]
        responses = []
        for m in messages:
            if m.role == "tool":
                try:
                    responses.append(json.loads(m.content))
                except json.JSONDecodeError:
                    responses.append(m.content)
        return responses

    def test_workflow_private_bundle_and_exact_human_save(self):
        spec = bundle("flow", {"kind": "workflow", "steps": [
            {"kind": "agent", "name": "check", "ref": "member"},
            {"kind": "condition", "name": "gate", "after": "check", "equals": "worker evidence",
             "then": [{"kind": "workflow", "name": "nested", "ref": "sub"}]},
        ]}, member=actor(), sub={"kind": "workflow", "steps": [{"kind": "agent", "name": "inside", "ref": "other"}]},
            other=actor("inside"))
        models = {"worker": lambda: FakeModel([text("worker evidence")]),
                  "inside": lambda: FakeModel([text("nested evidence")])}
        ask_save = tool_call("save_delegation", {"definition": spec})
        emitter = Emitter()
        output = self.run_main(self.main([register(spec), delegate("flow"), ask_save], models), emitter=emitter)
        self.assertEqual(output[1][0]["content"], "nested evidence")
        self.assertEqual(emitter.asked, [("save_delegation", {"definition": spec})])
        self.assertFalse((self.registry.path / "flow.json").exists())
        output = self.run_main(self.main([delegate("flow")], models), session="stranger")
        self.assertEqual(output[0][0]["status"], "error")
        changed = {**spec, "definitions": {**spec["definitions"], "member": actor(description="different")}}
        approving = Emitter(True)
        output = self.run_main(self.main([tool_call("save_delegation", {"definition": changed}), ask_save], models),
                               emitter=approving, strategy="dangerous")
        self.assertIn("不一致", output[0]["error"])
        self.assertEqual(approving.asked[-1], ("save_delegation", {"definition": spec}))
        self.assertEqual(json.loads((self.registry.path / "flow.json").read_text()), spec)
        restarted = DelegationRegistry(self.registry.path)
        output = self.run_main(self.main([tool_call("list_delegations"), delegate("flow", "member")], models,
                                         registry=restarted), session="new")
        self.assertEqual([row["ref"] for row in output[0]["entries"]], ["flow"])
        self.assertEqual([row["status"] for row in output[1]], ["completed", "error"])
        self.assertEqual(output[1][0]["content"], "nested evidence")

    def test_invalid_bundle_never_becomes_visible_and_parallel_steps_both_run(self):
        def step(name, ref):
            return {"kind": "agent", "name": name, "ref": ref}

        rejected = bundle("invalid", {"kind": "workflow", "steps": [step("work", "missing")]})
        valid = bundle("checks", {"kind": "workflow", "steps": [{
            "kind": "parallel", "name": "group", "steps": [step("first", "one"), step("second", "two")],
        }]}, one=actor("one"), two=actor("two"))
        models = {name: lambda name=name: FakeModel([text(f"evidence-{name}")]) for name in ("one", "two")}
        out = self.run_main(self.main([register(rejected), register(valid), tool_call("list_delegations"),
                                       delegate("checks")], models))
        self.assertIn("missing", out[0]["error"])
        self.assertEqual([entry["ref"] for entry in out[2]["entries"]], ["checks"])
        self.assertEqual(out[3][0]["status"], "completed")
        steps = out[3][0]["steps"][0]["steps"]
        self.assertEqual({(step["name"], step["content"]) for step in steps},
                         {("first", "evidence-one"), ("second", "evidence-two")})

    def test_four_native_team_modes(self):
        for mode in ("coordinate", "route", "broadcast", "tasks"):
            with self.subTest(mode=mode):
                calls = []

                class Member(FakeModel):
                    def invoke(model, **kwargs):
                        calls.append(model.marker)
                        return text(model.marker)

                def member(marker):
                    model = Member([])
                    model.marker = marker
                    return model

                class TasksLeader(FakeModel):
                    def invoke(model, **kwargs):
                        tools = [str(m.content) for m in kwargs["messages"] if m.role == "tool"]
                        if not tools:
                            return tool_call("create_task", {"title": "first"})
                        created = [re.search(r"Task created: \[([^]]+)]", value) for value in tools]
                        ids = [match[1] for match in created if match]
                        if len(tools) == 1:
                            return tool_call("create_task", {"title": "second", "depends_on": [ids[0]]})
                        if len(tools) == 2:
                            return tool_call("execute_task", {"task_id": ids[0], "member_id": "one"})
                        if len(tools) == 3:
                            self.assertIn("Result: one", tools[2])
                            return tool_call("update_task_status", {"task_id": ids[0], "status": "completed"})
                        if len(tools) == 4:
                            return tool_call("execute_task", {"task_id": ids[1], "member_id": "two"})
                        if len(tools) == 5:
                            self.assertIn("Result: two", tools[4])
                            return tool_call("mark_all_complete", {"summary": "tasks done"})
                        return text("tasks done")

                first = tool_call("delegate_task_to_members", {"task": "check"}) if mode == "broadcast" else tool_call(
                    "delegate_task_to_member", {"task": "check", "member_id": "one"})
                leader = (lambda: TasksLeader([])) if mode == "tasks" else (lambda: FakeModel([first, text("summary")]))
                spec = bundle(mode, dict(kind="team", instructions="Lead", model="lead", mode=mode, members=["one", "two"]),
                              one=actor("one"), two=actor("two"))
                out = self.run_main(self.main([register(spec), delegate(mode)], {
                    "lead": leader, "one": lambda: member("one"), "two": lambda: member("two")}), session=mode)
                self.assertEqual(out[-1][0]["status"], "completed", out)
                self.assertEqual(calls, ["one", "two"] if mode in {"broadcast", "tasks"} else ["one"])
                self.assertEqual(out[-1][0]["content"], {"route": "one", "tasks": "tasks done"}.get(mode, "summary"))

    def test_nested_confirm_action_is_refused_and_partial_batch_still_runs(self):
        target = self.root / "must-not-write"
        spec = bundle("guard", {"kind": "workflow", "steps": [{"kind": "agent", "name": "work", "ref": "member"}]},
                      member=actor(tools=["write_file"]))
        def model():
            return FakeModel([tool_call("write_file", {"path": str(target), "content": "unsafe"}), text("worker done")])
        emitter = Emitter(True)
        out = self.run_main(self.main([register(spec), delegate("missing", "guard")], {"worker": model}, "workspace-write"),
                            emitter=emitter)
        self.assertEqual([row["status"] for row in out[-1]], ["error", "completed"])
        self.assertIn("需要用户确认", out[-1][1]["refusals"][0])
        self.assertFalse(target.exists())
        self.assertEqual(emitter.asked, [])

    def test_batch_cancellation_reaches_active_children_and_leaves_queue_unstarted(self):
        entered = Barrier(3)
        release = Event()
        children = []

        def probe(run_context: RunContext):
            """Wait for release during the test."""
            children.append(run_context.run_id)
            entered.wait(4)
            self.assertTrue(release.wait(5))
            return "done"

        spec = bundle("held", actor(tools=["probe"]))
        ref = ModelRef("offline", "worker")
        catalog = SimpleNamespace(build=lambda *_: FakeModel([tool_call("probe"), text("child")]),
                                  resolve=lambda *_: Resolved(ref))
        main = Agent(model=FakeModel([register(spec), delegate("held", "held", "held"), text("parent")]), telemetry=False,
                     tools=make_delegation_tools(self.registry, catalog, [ref], ref, lambda: [probe], self.settings))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(main.run, "begin", run_id="main-cancel")
            try:
                entered.wait(4)
                self.assertTrue(main.cancel_run("main-cancel"))
                for _ in range(200):
                    if all(is_cancelled(run_id) for run_id in children):
                        break
                    release.wait(0.01)
                self.assertTrue(all(is_cancelled(run_id) for run_id in children))
                self.assertFalse(future.done())
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5).status.value, "CANCELLED")
        self.assertEqual(len(children), 2)
