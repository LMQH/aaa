"""MiniViking 回归：文件树、检索、LLM 蒸馏、资源导入与 aaa 接线。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aaa.config.settings import Settings
from aaa.memory import MemoryHost, MemoryIdentity
from aaa.runtime import agent as agent_mod, prompt
from fixtures import FakeModel, text, tool_call
from miniviking import ENGINE
from miniviking import config as config_module
from miniviking.config import Config, LLMSettings
from miniviking.engine import Engine
from miniviking.store import clean_rel, parse_uri, slug

TURN = {
    "memories": [{
        "title": "用户偏好深色主题",
        "abstract": "用户偏好深色主题",
        "overview": "用户在终端里偏好深色主题。",
        "body": "用户明确表示偏好深色主题。",
        "tags": ["偏好"],
        "keywords": ["深色主题", "配色"],
        "importance": 0.8,
    }],
    "session": {"summary": "用户偏好深色主题。", "topics": ["偏好"], "keywords": ["深色主题"]},
}

CHUNK = {
    "abstract": "发布流程摘要",
    "overview": "先跑离线回归，再打包。",
    "keywords": ["发布", "离线回归"],
    "tags": ["流程"],
}


class FakeChat:
    def __init__(self) -> None:
        self.turn = TURN
        self.chunk = CHUNK
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if "记忆整理器" in system:
            return json.dumps(self.turn, ensure_ascii=False)
        return json.dumps(self.chunk, ensure_ascii=False)


class EngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="miniviking-"))
        self.chat = FakeChat()
        self.enterContext(mock.patch("miniviking.engine.build_client", return_value=self.chat))
        self.enterContext(mock.patch("miniviking.engine.build_embedder", return_value=None))
        self.engine = Engine(self.tmp / "data", client=self.chat)
        self.addCleanup(self.engine.close)
        self.identity = MemoryIdentity(
            user_id="alice", agent_id="aaa-main", session_id="s1", run_id="r1",
        )

    def remember(self, title: str = "项目约定", content: str = "发布前必须跑离线回归。") -> str:
        return self.engine.call_tool(
            "viking_remember", {"title": title, "content": content}, self.identity,
        )


class StoreTest(EngineCase):
    def test_slug_keeps_unicode_and_rejects_traversal(self) -> None:
        self.assertEqual(slug("用户偏好"), "用户偏好")
        self.assertTrue(slug("a b").startswith("a_b-"))
        with self.assertRaises(ValueError):
            clean_rel("../etc/passwd")
        self.assertEqual(parse_uri("viking://alice/aaa-main/memories/x.md"),
                         ("alice", "aaa-main", "memories", "x.md"))

    def test_journal_is_idempotent_per_run(self) -> None:
        ns = self.engine.store.namespace("alice", "aaa-main")
        payload = {"query": "q", "answer": "a"}
        self.assertTrue(ns.write_journal("s1/r1.json", payload))
        self.assertFalse(ns.write_journal("s1/r1.json", payload))
        self.assertEqual(ns.read_journal("s1/r1.json"), payload)


class ToolTest(EngineCase):
    def test_remember_search_read_browse_forget(self) -> None:
        self.remember()
        hit = self.engine.call_tool(
            "viking_search", {"query": "离线回归的要求是什么"}, self.identity,
        )
        self.assertIn("项目约定", hit)
        self.assertIn("viking://alice/aaa-main/memories/", hit)
        uri = hit.split("[", 1)[1].split("]", 1)[0]
        abstract = self.engine.call_tool(
            "viking_read", {"target": uri, "level": "abstract"}, self.identity,
        )
        self.assertIn("发布前必须跑离线回归", abstract)
        root = self.engine.call_tool(
            "viking_browse", {"target": "viking://alice/aaa-main", "action": "list"}, self.identity,
        )
        self.assertIn("memories: 1 条", root)
        self.assertIn(uri, self.engine.call_tool(
            "viking_browse", {"target": "viking://alice/aaa-main/memories", "action": "tree"},
            self.identity,
        ))
        self.assertIn("已删除", self.engine.call_tool(
            "viking_forget", {"target": uri}, self.identity,
        ))
        self.assertEqual(self.engine.call_tool(
            "viking_search", {"query": "离线回归"}, self.identity,
        ), "没有命中。")

    def test_identity_cannot_come_from_arguments(self) -> None:
        result = self.engine.call_tool(
            "viking_search", {"query": "任意", "user_id": "bob"}, self.identity,
        )
        self.assertIn("身份由宿主提供", result)

    def test_memory_is_isolated_by_user(self) -> None:
        self.remember()
        bob = MemoryIdentity(user_id="bob", agent_id="aaa-main")
        self.assertEqual(self.engine.call_tool(
            "viking_search", {"query": "离线回归"}, bob,
        ), "没有命中。")
        self.assertEqual(self.engine.read(bob, limit=10, page=1)[1], 0)

    def test_read_pagination_uses_memory_entry_shape(self) -> None:
        self.remember("约定一", "第一条")
        self.remember("约定二", "第二条")
        entries, total = self.engine.read(self.identity, limit=1, page=1)
        self.assertEqual(total, 2)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].memory.startswith("[viking://"))


class DistillTest(EngineCase):
    def observe(self, run_id: str) -> None:
        identity = MemoryIdentity(
            user_id="alice", agent_id="aaa-main", session_id="s1", run_id=run_id,
        )
        self.engine.observe(SimpleNamespace(
            query="我喜欢深色主题", answer="已记住", identity=identity,
        ))
        self.assertTrue(self.engine.tasks.wait_idle(5))

    def test_observe_distills_once_and_is_idempotent(self) -> None:
        self.observe("r1")
        self.observe("r1")
        hits = self.engine.call_tool(
            "viking_search", {"query": "深色主题"}, self.identity,
        )
        self.assertIn("用户偏好深色主题", hits)
        memories = list((self.tmp / "data/vault/alice/aaa-main/memories").glob("*.md"))
        self.assertEqual(len(memories), 1)
        journal = list((self.tmp / "data/vault/alice/aaa-main/journal/s1").glob("*.json"))
        self.assertEqual(len(journal), 1)

    def test_observe_merges_same_title(self) -> None:
        self.observe("r1")
        self.chat.turn = {
            "memories": [{
                "title": "用户偏好深色主题",
                "abstract": "用户偏好深色主题，并禁用彩色输出",
                "overview": "用户偏好深色主题，且不希望彩色输出。",
                "body": "用户补充：输出关闭颜色。",
                "tags": ["偏好", "终端"],
                "keywords": ["深色主题", "无彩色"],
                "importance": 0.9,
            }],
            "session": TURN["session"],
        }
        self.observe("r2")
        memories = list((self.tmp / "data/vault/alice/aaa-main/memories").glob("*.md"))
        self.assertEqual(len(memories), 1)
        body = memories[0].read_text(encoding="utf-8")
        self.assertIn("输出关闭颜色", body)
        self.assertIn("深色主题", body)

    def test_without_model_uses_mechanical_memory(self) -> None:
        with mock.patch("miniviking.engine.build_client", return_value=None):
            engine = Engine(
                self.tmp / "plain",
                config=Config(llm=LLMSettings(inherit=False)),
            )
        self.addCleanup(engine.close)
        identity = MemoryIdentity(user_id="alice", agent_id="aaa-main", session_id="s", run_id="r")
        engine.observe(SimpleNamespace(query="记住这个决定", answer="采用文件树", identity=identity))
        self.assertTrue(engine.tasks.wait_idle(5))
        hits = engine.call_tool("viking_search", {"query": "采用文件树"}, identity)
        self.assertIn("采用文件树", hits)


class ResourceTest(EngineCase):
    def test_import_file_reports_task_and_indexes_chunks(self) -> None:
        source = self.tmp / "release.txt"
        source.write_text("发布前必须跑离线回归。\n\n回归通过后才能打包。", encoding="utf-8")
        result = self.engine.call_tool(
            "viking_add_resource", {"source": str(source)}, self.identity,
        )
        self.assertIn("已排队导入", result)
        task_uri = result.split("任务 ", 1)[1].strip()
        self.assertTrue(self.engine.tasks.wait_idle(5))
        status = self.engine.call_tool(
            "viking_read", {"target": task_uri}, self.identity,
        )
        self.assertIn("状态：done", status)
        self.assertIn("导入 1 个文件", status)
        hits = self.engine.call_tool(
            "viking_search", {"query": "离线回归", "scope": "resource"}, self.identity,
        )
        self.assertIn("chunk-001.md", hits)

    def test_import_directory_caps_and_lists(self) -> None:
        folder = self.tmp / "notes"
        (folder / "sub").mkdir(parents=True)
        (folder / "a.md").write_text("第一条笔记", encoding="utf-8")
        (folder / "sub" / "b.md").write_text("第二条笔记", encoding="utf-8")
        (folder / "blob.bin").write_bytes(b"\x00\x01")
        self.engine.call_tool("viking_add_resource", {"source": str(folder)}, self.identity)
        self.assertTrue(self.engine.tasks.wait_idle(5))
        browse = self.engine.call_tool(
            "viking_browse",
            {"target": "viking://alice/aaa-main/resources/notes", "action": "tree", "depth": 3},
            self.identity,
        )
        self.assertIn("files/001-a", browse)
        self.assertIn("files/002-b", browse)


class SkillTest(EngineCase):
    def test_add_skill_and_overwrite(self) -> None:
        args = {"name": "release", "content": "# 发布流程\n先跑离线回归。"}
        result = self.engine.call_tool("viking_add_skill", args, self.identity)
        self.assertIn("已写入技能", result)
        again = self.engine.call_tool("viking_add_skill", args, self.identity)
        self.assertIn("已存在", again)
        overwritten = self.engine.call_tool(
            "viking_add_skill", {**args, "overwrite": True}, self.identity,
        )
        self.assertIn("已写入技能", overwritten)
        read = self.engine.call_tool(
            "viking_read",
            {"target": "viking://alice/aaa-main/skills/release/SKILL.md", "level": "full"},
            self.identity,
        )
        self.assertIn("先跑离线回归", read)


class ConfigTest(unittest.TestCase):
    def test_invalid_config_falls_back(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="miniviking-config-"))
        (tmp / "config.toml").write_text("not = [toml", encoding="utf-8")
        loaded = config_module.load(tmp)
        self.assertEqual(loaded.limits.max_memories_per_turn, 6)
        self.assertTrue(loaded.llm.inherit)

    def test_limits_are_clamped(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="miniviking-config-"))
        (tmp / "config.toml").write_text(
            "[limits]\nmax_memories_per_turn = 99\nchunk_chars = 10\n", encoding="utf-8",
        )
        loaded = config_module.load(tmp)
        self.assertEqual(loaded.limits.max_memories_per_turn, 20)
        self.assertEqual(loaded.limits.chunk_chars, 500)


class HostContractTest(EngineCase):
    def test_host_sees_tools_read_and_recall(self) -> None:
        host = MemoryHost(ENGINE(self.tmp / "host"), "miniviking", Settings({"memory.user_id": "alice"}))
        self.addCleanup(host.close)
        self.assertEqual(len(host.tool_specs()), 7)
        self.assertEqual(
            host.confirm_tool_names(),
            frozenset({"viking_forget", "viking_add_resource", "viking_add_skill"}),
        )
        identity = host.identity(session_id="s1", run_id="r1")
        self.assertIn("已写入", host.call_tool(
            "viking_remember", {"title": "宿主约定", "content": "记忆由宿主注入"}, identity,
        ))
        block = host.recall("记忆由宿主注入", identity=identity)
        self.assertIn("<recalled_memories>", block)
        self.assertIn("viking://alice/aaa-main/memories/", block)
        entries, total = host.read(session_id="s1", limit=10, page=1)
        self.assertEqual(total, 1)
        self.assertEqual(entries[0].topics, ())


class AgentIntegrationTest(EngineCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(agent_mod, "DB_FILE", self.tmp / "sessions.db"))
        self.enterContext(mock.patch.object(agent_mod, "DATA_DIR", self.tmp))
        self.enterContext(mock.patch.object(prompt, "SYSTEM_PROMPT_FILE", self.tmp / "SYSTEM.md"))
        self.enterContext(mock.patch.object(
            prompt, "APPEND_SYSTEM_PROMPT_FILE", self.tmp / "APPEND_SYSTEM.md",
        ))

    def test_recall_is_injected_and_tools_dispatch(self) -> None:
        host = MemoryHost(ENGINE(self.tmp / "agent-data"), "miniviking", Settings({"memory.user_id": "alice"}))
        self.addCleanup(host.close)
        identity = host.identity(session_id="s1")
        host.call_tool(
            "viking_remember", {"title": "项目约定", "content": "发布前必须跑离线回归。"}, identity,
        )
        model = FakeModel([
            tool_call("viking_search", {"query": "离线回归的要求是什么"}),
            text("已检索"),
        ])
        catalog = SimpleNamespace(
            require=lambda provider, model_id: (None, SimpleNamespace(context_window=100_000)),
            build=lambda provider, model_id: model,
            fallbacks=lambda provider, model_id: [],
        )
        agent = agent_mod.build_agent(
            catalog, "ch", "m1", "read-only", Settings(), extra_tools=[], memory=host,
        )
        self.addCleanup(agent.db.db_engine.dispose)
        agent.run("离线回归的要求是什么", user_id="alice")
        system = next(
            message.content for message in model.invocations[0]["messages"]
            if message.role == "system"
        )
        self.assertIn("<recalled_memories>", system)
        self.assertIn("viking://alice/aaa-main/memories/", system)
        tool_messages = [
            message.content for message in model.invocations[-1]["messages"]
            if message.role == "tool"
        ]
        self.assertTrue(any("viking://" in str(item) for item in tool_messages))


class FakeEmbedder:
    """按主题词返回固定方向向量的嵌入替身：含“猫”→(1,0)，含“狗”→(0,1)。"""

    def __init__(self, name: str = "fake:embed") -> None:
        self._name = name
        self.calls = 0

    def name(self) -> str:
        return self._name

    def dimension(self) -> int:
        return 2

    def embed(self, texts) -> list[list[float]]:
        self.calls += 1
        vectors = []
        for text in texts:
            if "猫" in text:
                vector = [1.0, 0.0]
            elif "狗" in text:
                vector = [0.0, 1.0]
            else:
                vector = [0.5, 0.5]
            norm = sum(value * value for value in vector) ** 0.5 or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class EmbedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="miniviking-embed-"))
        self.chat = FakeChat()
        self.identity = MemoryIdentity(user_id="alice", agent_id="aaa-main", session_id="s1", run_id="r1")

    def mk_engine(self, embedder=None):
        with mock.patch("miniviking.engine.build_client", return_value=self.chat), \
                mock.patch("miniviking.engine.build_embedder", return_value=embedder):
            engine = Engine(self.tmp / "data", client=self.chat, embedder=embedder)
        self.addCleanup(engine.close)
        return engine

    def test_semantic_hit_without_keyword_overlap(self) -> None:
        engine = self.mk_engine(FakeEmbedder())
        engine.call_tool(
            "viking_remember",
            {"title": "猫的习性", "content": "猫咪喜欢晒太阳。"},
            self.identity,
        )
        self.assertTrue(engine.tasks.wait_idle(5))
        result = engine.call_tool("viking_search", {"query": "猫科动物"}, self.identity)
        self.assertIn("猫的习性", result)

    def test_hybrid_dedupes_same_uri(self) -> None:
        engine = self.mk_engine(FakeEmbedder())
        engine.call_tool(
            "viking_remember",
            {"title": "狗的饲养", "content": "狗需要每天遛弯。"},
            self.identity,
        )
        self.assertTrue(engine.tasks.wait_idle(5))
        result = engine.call_tool("viking_search", {"query": "狗 遛弯"}, self.identity)
        self.assertEqual(len(result.splitlines()), 1)
        self.assertIn("狗的饲养", result)

    def test_without_embedder_stays_keyword_only(self) -> None:
        engine = self.mk_engine(None)
        engine.call_tool(
            "viking_remember",
            {"title": "猫的习性", "content": "猫咪喜欢晒太阳。"},
            self.identity,
        )
        self.assertTrue(engine.tasks.wait_idle(5))
        self.assertEqual(
            engine.call_tool("viking_search", {"query": "猫科动物"}, self.identity),
            "没有命中。",
        )

    def test_query_embedding_is_cached(self) -> None:
        embedder = FakeEmbedder()
        engine = self.mk_engine(embedder)
        engine.call_tool(
            "viking_remember",
            {"title": "猫的习性", "content": "猫咪喜欢晒太阳。"},
            self.identity,
        )
        self.assertTrue(engine.tasks.wait_idle(5))
        before = embedder.calls
        engine.call_tool("viking_search", {"query": "猫科动物"}, self.identity)
        engine.call_tool("viking_search", {"query": "猫科动物"}, self.identity)
        self.assertEqual(embedder.calls, before + 1)

    def test_model_change_triggers_reembed(self) -> None:
        from miniviking.engine import _embed_text, _text_hash

        engine = self.mk_engine(FakeEmbedder("fake:A"))
        engine.call_tool(
            "viking_remember",
            {"title": "猫的习性", "content": "猫咪喜欢晒太阳。"},
            self.identity,
        )
        self.assertTrue(engine.tasks.wait_idle(5))
        engine.close()
        engine_b = self.mk_engine(FakeEmbedder("fake:B"))
        self.assertTrue(engine_b.tasks.wait_idle(5))
        node = next(
            node for node in engine_b.store.iter_nodes() if node.kind == "memories"
        )
        text_hash = _text_hash(_embed_text(node, engine_b.config.embedding.max_chars))
        self.assertTrue(engine_b.index.has_embedding(text_hash, "fake:B"))
        result = engine_b.call_tool("viking_search", {"query": "猫科动物"}, self.identity)
        self.assertIn("猫的习性", result)


    def test_vec_table_failure_degrades_to_keyword(self) -> None:
        embedder = FakeEmbedder()
        with mock.patch("miniviking.engine.build_client", return_value=self.chat), \
                mock.patch("miniviking.engine.build_embedder", return_value=embedder), \
                mock.patch("miniviking.engine.Index.ensure_vector_table", return_value=False):
            engine = Engine(self.tmp / "vecfail", client=self.chat, embedder=embedder)
        self.addCleanup(engine.close)
        self.assertIsNone(engine.embedder)
        engine.call_tool(
            "viking_remember",
            {"title": "猫的习性", "content": "猫咪喜欢晒太阳。"},
            self.identity,
        )
        self.assertTrue(engine.tasks.wait_idle(5))
        self.assertEqual(
            engine.call_tool("viking_search", {"query": "猫科动物"}, self.identity),
            "没有命中。",
        )


class EmbedderBuildTest(unittest.TestCase):
    def _settings(self, **overrides):
        from miniviking.config import EmbeddingSettings

        return EmbeddingSettings(**overrides)

    def test_disabled_returns_none(self) -> None:
        from miniviking.embed import build_embedder

        self.assertIsNone(build_embedder(self._settings(enable=False), Path("/tmp")))

    def test_local_backend_failure_degrades(self) -> None:
        from miniviking.embed import LocalBGE, build_embedder

        # 本地推理任何失败（依赖缺失、下载失败）都应降级为 None 并告警。
        with mock.patch.object(LocalBGE, "__init__", side_effect=RuntimeError("下载失败")), \
                self.assertLogs("aaa.memory.miniviking", level="WARNING"):
            self.assertIsNone(build_embedder(self._settings(), Path(tempfile.mkdtemp())))

    def test_logical_name_strips_org_prefix(self) -> None:
        from miniviking.embed import _logical_name

        self.assertEqual(_logical_name("BAAI/bge-m3"), "bge-m3")
        self.assertEqual(_logical_name("bge-small-zh-v1.5"), "bge-small-zh-v1.5")
        with self.assertRaises(ValueError):
            _logical_name("")

    def test_api_without_key_degrades(self) -> None:
        from miniviking.embed import build_embedder

        settings = self._settings(provider="api", model="BAAI/bge-m3", base_url="https://x/v1")
        self.assertIsNone(build_embedder(settings, Path("/tmp")))

    def test_api_with_key_constructs(self) -> None:
        from miniviking.embed import ApiBGE, build_embedder

        settings = self._settings(
            provider="api", model="BAAI/bge-m3", base_url="https://x/v1", api_key="k",
        )
        embedder = build_embedder(settings, Path("/tmp"))
        self.assertIsInstance(embedder, ApiBGE)
        self.assertEqual(embedder.name(), "api:https://x/v1#BAAI/bge-m3")


class EmbedInstallTest(unittest.TestCase):
    def _run(self, results):
        """results: 按调用顺序返回预设返回码；记录每次 argv。"""
        calls = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            code = results.pop(0) if results else 1
            return SimpleNamespace(returncode=code, stdout="", stderr="boom\n")

        return calls, runner

    def test_success_on_first_strategy(self) -> None:
        from miniviking.embed_install import PACKAGES, install

        with mock.patch("miniviking.embed_install._strategies") as strategies, \
                mock.patch("miniviking.embed_install._has_pip", return_value=False), \
                mock.patch("shutil.which", return_value="/usr/bin/uv"):
            strategies.return_value = [("uv", ["/usr/bin/uv", "pip", "install", *PACKAGES])]
            calls, runner = self._run([0])
            lines: list[str] = []
            self.assertTrue(install(lines.append, runner=runner))
        self.assertTrue(calls[0][0].endswith("uv"))
        self.assertNotIn("-i", calls[0])
        self.assertTrue(any("安装成功" in line for line in lines))

    def test_falls_back_to_mirror_index(self) -> None:
        from miniviking.embed_install import FALLBACK_INDEX, PACKAGES, install

        with mock.patch("miniviking.embed_install._strategies") as strategies, \
                mock.patch("miniviking.embed_install._has_pip", return_value=False), \
                mock.patch("shutil.which", return_value="/usr/bin/uv"):
            strategies.return_value = [("uv", ["/usr/bin/uv", "pip", "install", *PACKAGES])]
            calls, runner = self._run([1, 0])
            lines: list[str] = []
            self.assertTrue(install(lines.append, runner=runner))
        self.assertEqual(len(calls), 2)
        self.assertIn("-i", calls[1])
        self.assertIn(FALLBACK_INDEX, calls[1])
        self.assertTrue(any("安装成功" in line for line in lines))

    def test_all_attempts_fail_reports_manual_command(self) -> None:
        from miniviking.embed_install import install

        with mock.patch("miniviking.embed_install._strategies") as strategies, \
                mock.patch("shutil.which", return_value=None):
            strategies.return_value = []
            calls, runner = self._run([])
            lines: list[str] = []
            self.assertFalse(install(lines.append, runner=runner))
        self.assertEqual(calls, [])
        self.assertTrue(any("手动执行" in line for line in lines))


class CloseTest(unittest.TestCase):
    def test_close_stops_worker(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="miniviking-close-"))
        with mock.patch("miniviking.engine.build_client", return_value=FakeChat()), \
                mock.patch("miniviking.engine.build_embedder", return_value=None):
            engine = Engine(tmp / "data", client=FakeChat())
        engine.close()
        self.assertFalse(engine.tasks._thread.is_alive())
