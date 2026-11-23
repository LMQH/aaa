"""原生记忆引擎回归：装配身份、跨用户隔离、分页语义与配置键。

全部走真实 Agno SqliteDb（临时目录），不 mock SQL：条目落库、from_dict 还原
与超范围页返回 `([], 0)` 都是 Agno 原生行为，正是适配层要贴住的边界。
"""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agno.db.sqlite import SqliteDb
from agno.memory import UserMemory

from aaa.config import settings as settings_mod
from aaa.config.settings import Settings, load_settings, save
from aaa.memory import (
    MemoryEntry,
    MemoryEngineError,
    MemoryHost,
    MemoryTool,
    MemoryIdentity,
    engine_ids,
    get_engine,
    render_entries,
)
from aaa.memory import registry as registry_mod
from aaa.memory.agno import read_builtin


class FakeEngine:
    """外部引擎的最小替身：记录召回查询与观察事件。"""

    label = "FakeEngine"

    def __init__(self, hits=None, error=None, delay=0.0):
        self.hits = list(hits or [])
        self.error = error
        self.delay = delay
        self.events = []

    def recall(self, query):
        if self.error:
            raise self.error
        if self.delay:
            time.sleep(self.delay)
        return self.hits

    def observe(self, event):
        self.events.append(event)


def _memory(text: str, user_id: str, updated_at: int, topics=None) -> UserMemory:
    return UserMemory(
        memory=text, user_id=user_id, updated_at=updated_at, input=None, topics=topics
    )


class NativeMemoryReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aaa-memory-"))
        self.db = SqliteDb(db_file=str(self.tmp / "sessions.db"))
        self.addCleanup(self.db.db_engine.dispose)

    def _seed(self, user_id: str, count: int, base: int = 1_700_000_000) -> None:
        self.db.upsert_memories(
            [_memory(f"{user_id}-备忘{i}", user_id, base + i) for i in range(count)],
            preserve_updated_at=True,
        )


    def test_read_is_scoped_to_user_pool(self) -> None:
        self._seed("alice", 3)
        self._seed("bob", 1, base=1_700_000_100)
        rows, total = read_builtin(self.db, "alice", limit=10, page=1)
        self.assertEqual(total, 3)
        self.assertEqual([row.memory for row in rows], ["alice-备忘2", "alice-备忘1", "alice-备忘0"])
        rows, total = read_builtin(self.db, "bob", limit=10, page=1)
        self.assertEqual((total, rows[0].memory), (1, "bob-备忘0"))

    def test_read_paginates_newest_first(self) -> None:
        self._seed("alice", 3)
        rows, total = read_builtin(self.db, "alice", limit=2, page=1)
        self.assertEqual((total, len(rows)), (3, 2))
        rows, total = read_builtin(self.db, "alice", limit=2, page=2)
        self.assertEqual((total, len(rows)), (3, 1))
        # 原生超范围页返回 ([], 0)：该页无条目，不是整个池为空。
        rows, total = read_builtin(self.db, "alice", limit=2, page=3)
        self.assertEqual((rows, total), ([], 0))

    def test_read_rejects_unscoped_user(self) -> None:
        with self.assertRaises(ValueError):
            read_builtin(self.db, "", limit=10, page=1)


class MemoryEngineSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aaa-mem-cfg-"))
        patcher = mock.patch.object(settings_mod, "CONFIG_FILE", self.tmp / "config.toml")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_memory_defaults_to_off(self) -> None:
        self.assertEqual(load_settings().get("memory.source"), "off")

    def test_engine_and_list_limit_roundtrip_through_file(self) -> None:
        settings = Settings()
        settings.set("memory.source", "external:vendor-x")
        settings.set("memory.list_limit", 25)
        save(settings)
        loaded = load_settings()
        self.assertEqual(loaded.get("memory.source"), "external:vendor-x")
        self.assertEqual(loaded.get("memory.list_limit"), 25)
        self.assertEqual(loaded.warnings, [])

    def test_list_limit_bounds(self) -> None:
        settings = Settings()
        with self.assertRaises(ValueError):
            settings.set("memory.list_limit", 0)
        with self.assertRaises(ValueError):
            settings.set("memory.list_limit", 101)
        settings.set("memory.list_limit", 100)
        self.assertEqual(settings.get("memory.list_limit"), 100)



class EngineRegistryTest(unittest.TestCase):
    def test_local_file_plugin_is_discovered_and_loaded(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="aaa-mem-plugin-"))
        (tmp / "local-fake.py").write_text(
            "class Engine:\n"
            "    def __init__(self, data_dir): self.data_dir = data_dir\n"
            "    def recall(self, query): return []\n"
            "ENGINE = Engine\n",
            encoding="utf-8",
        )
        with mock.patch.object(registry_mod, "MEMORY_ENGINES_DIR", tmp):
            self.assertIn("local-fake", engine_ids())
            engine = get_engine("local-fake", tmp / "data")
        self.assertEqual(engine.data_dir, tmp / "data" / "local-fake")
        self.assertTrue(engine.data_dir.is_dir())

    def test_entry_point_plugin_is_discovered_and_loaded(self) -> None:
        class Point:
            name = "entry-fake"

            def load(self):
                return lambda data_dir: FakeEngine([MemoryEntry(str(data_dir))])

        with mock.patch.object(registry_mod, "_entry_points", return_value=(Point(),)):
            self.assertIn("entry-fake", engine_ids())
            with tempfile.TemporaryDirectory() as directory:
                engine = get_engine("entry-fake", Path(directory))
                self.assertEqual(engine.recall(None)[0].memory, str(Path(directory) / "entry-fake"))


class MemoryHostTest(unittest.TestCase):
    def _settings(self, **values):
        return Settings(values)

    def test_render_dedupes_and_respects_budget(self) -> None:
        entries = [
            MemoryEntry(memory="同一句"),
            MemoryEntry(memory="同一句"),
            MemoryEntry(memory="另一句"),
        ]
        block = render_entries(entries, 200)
        self.assertEqual(block.count("同一句"), 1)
        self.assertIn("另一句", block)
        self.assertEqual(render_entries([], 200), "")

    def test_recall_caps_results_even_if_engine_ignores_limit(self) -> None:
        engine = FakeEngine([MemoryEntry("first match"), MemoryEntry("over limit")])
        host = MemoryHost(engine, "fake", self._settings(**{"memory.recall_limit": 1}))
        block = host.recall("q", identity=MemoryIdentity("u", "aaa-main"))
        self.assertIn("first match", block)
        self.assertNotIn("over limit", block)

    def test_recall_failure_and_timeout_degrade_to_empty(self) -> None:
        for error in (RuntimeError("boom"), SystemExit(1)):
            with self.subTest(error=type(error).__name__):
                host = MemoryHost(FakeEngine(error=error), "fake", self._settings())
                self.assertEqual(host.recall("q", identity=MemoryIdentity("u", "aaa-main")), "")
        slow = FakeEngine(delay=0.3)
        host = MemoryHost(slow, "fake", self._settings(**{"memory.recall_timeout": 0.1}))
        self.assertEqual(host.recall("q", identity=MemoryIdentity("u", "aaa-main")), "")

    def test_observe_runs_once_per_run(self) -> None:
        engine = FakeEngine()
        host = MemoryHost(engine, "fake", self._settings())
        run_context = SimpleNamespace(run_id="r1", session_id="s1", user_id="u1")
        run_output = SimpleNamespace(
            run_id="r1", session_id="s1",
            input=SimpleNamespace(input_content_string=lambda: "q"),
            get_content_as_string=lambda: "a",
        )
        host.observe_hook(run_output, run_context)
        host.observe_hook(run_output, run_context)
        self.assertEqual(len(engine.events), 1)
        self.assertEqual(engine.events[0].query, "q")
        self.assertEqual(engine.events[0].answer, "a")

    def test_tool_contract_is_validated_and_declared(self) -> None:
        calls = []
        engine = SimpleNamespace(
            tools=lambda: [SimpleNamespace(name="search_notes", description="搜索记忆",
                                           parameters={"type": "object"}, read_only=True)],
            call_tool=lambda name, args, ctx: calls.append((name, args, ctx)) or "search_notes ok",
        )
        good = SimpleNamespace(name="search_notes", description="搜索记忆",
                               parameters={"type": "object"}, read_only=True)
        duplicate = SimpleNamespace(name="search_notes", description="重复")
        bad_name = SimpleNamespace(name="bad name!", description="非法")
        host = MemoryHost(engine, "fake", self._settings())
        self.assertEqual(host.tool_specs()[0].name, "search_notes")
        self.assertEqual(host.confirm_tool_names(), frozenset())
        self.assertEqual(host.call_tool("search_notes", {"q": "蓝"}, MemoryIdentity("u", "aaa-main", "s", "r")), "search_notes ok")
        self.assertEqual(calls[-1][0], "search_notes")
        with self.assertRaises(MemoryEngineError):
            MemoryHost(SimpleNamespace(tools=lambda: [good, duplicate], call_tool=lambda *a: "ok"),
                       "fake", self._settings())
        with self.assertRaises(MemoryEngineError):
            MemoryHost(SimpleNamespace(tools=lambda: [bad_name], call_tool=lambda *a: "ok"),
                       "fake", self._settings())
        with self.assertRaises(MemoryEngineError):
            MemoryHost(SimpleNamespace(tools=lambda: [good]), "fake", self._settings())

    def test_tool_declarations_fail_closed_on_bad_fields(self) -> None:
        reserved = SimpleNamespace(name="read_result", description="保留名", read_only=True)
        non_bool = SimpleNamespace(name="remember", description="写入", read_only="false")
        non_bool_confirmation = SimpleNamespace(name="remember", requires_confirmation="false")
        bad_params = SimpleNamespace(name="remember", description="写入", read_only=True,
                                     parameters='{"type": "object"}')
        for spec in (reserved, non_bool, non_bool_confirmation, bad_params):
            with self.subTest(name=spec.name):
                with self.assertRaises(MemoryEngineError):
                    MemoryHost(SimpleNamespace(tools=lambda s=spec: [s], call_tool=lambda *a: "ok"),
                               "fake", self._settings())

    def test_call_tool_degrades_to_error_text(self) -> None:
        broken = SimpleNamespace(tools=lambda: [], call_tool=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
        host = MemoryHost(broken, "fake", self._settings())
        self.assertEqual(host.call_tool("search_notes", {}, MemoryIdentity("u", "aaa-main", "s", "r")), "Error: RuntimeError")
        slow = SimpleNamespace(tools=lambda: [], call_tool=lambda *a: time.sleep(0.3))
        host = MemoryHost(slow, "fake", self._settings(**{"memory.tool_timeout": 0.1}))
        self.assertEqual(host.call_tool("search_notes", {}, MemoryIdentity("u", "aaa-main", "s", "r")), "Error: TimeoutError")

    def test_prompt_block_is_optional_and_sanitized(self) -> None:
        self.assertEqual(MemoryHost(FakeEngine(), "fake", self._settings()).prompt_block(), "")
        host = MemoryHost(SimpleNamespace(prompt_block=lambda: "  先检索再回答  "), "fake", self._settings())
        self.assertEqual(host.prompt_block(), "先检索再回答")
        broken = MemoryHost(SimpleNamespace(prompt_block=lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
                            "fake", self._settings())
        self.assertEqual(broken.prompt_block(), "")
