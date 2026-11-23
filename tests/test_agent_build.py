"""Agent 原生装配回归：工具声明、提示词文件、技能披露和记忆。

通过离线模型观察 Agno 实际收到的消息与工具，不连接模型服务。
记忆相关：manager 必须绑同一份 model/db，关闭时不初始化，未知引擎即使关闭也在
构造期报错，原生记忆跨重建仍可见且按 user_id 隔离注入。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional
from unittest import mock

from agno.memory import UserMemory
from agno.tools.function import Function
from fixtures import FakeModel, text, tool_call

from aaa.cli.run import stream_continue
from aaa.config.settings import Settings
from aaa.memory import (
    MemoryEngineError,
    MemoryEntry,
    MemoryHost,
    MemoryIdentity,
    MemoryTool,
)
from aaa.memory.agno import read_builtin
from aaa.plugins import BUILTIN_SKILLS_DIR, discover_skills
from aaa.runtime import agent as agent_mod
from aaa.runtime import prompt


def _plugin_tool() -> Function:
    def echo(text: str) -> str:
        return text

    return Function(name="mcp_echo", description="回显", entrypoint=echo)


class FakeExternalEngine:
    """外部引擎替身：不回写存储，只记录召回与观察。"""

    label = "FakeExternalEngine"

    def __init__(self, hits=(), error=None):
        self.hits = list(hits)
        self.error = error
        self.queries = []
        self.events = []

    def recall(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.hits

    def observe(self, event):
        self.events.append(event)


class ToolEngine(FakeExternalEngine):
    """声明工具的外置替身：记录名称、参数与调用上下文。"""

    def __init__(self, specs, error=None):
        super().__init__()
        self.specs = list(specs)
        self.tool_error = error
        self.calls = []

    def tools(self):
        return self.specs

    def call_tool(self, name, arguments, context):
        self.calls.append((name, dict(arguments or {}), context))
        if self.tool_error:
            raise self.tool_error
        return f"{name} ok"

    def prompt_block(self):
        return "先检索记忆再回答"


class BuildAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aaa-build-"))
        patcher_db = mock.patch.object(agent_mod, "DB_FILE", self.tmp / "sessions.db")
        patcher_dir = mock.patch.object(agent_mod, "DATA_DIR", self.tmp)
        patcher_db.start()
        patcher_dir.start()
        self.addCleanup(patcher_db.stop)
        self.addCleanup(patcher_dir.stop)
        self.enterContext(mock.patch.object(prompt, "SYSTEM_PROMPT_FILE", self.tmp / "SYSTEM.md"))
        self.enterContext(mock.patch.object(prompt, "APPEND_SYSTEM_PROMPT_FILE", self.tmp / "APPEND_SYSTEM.md"))

    def _build(self, settings: Optional[Settings] = None, script: Optional[List] = None, *, skills=(), memory=None):
        """构造一个真实 Agent：模型按脚本回放，数据库落在临时目录。"""
        model = FakeModel(script or [])
        catalog = SimpleNamespace(
            require=lambda provider, model_id: (None, SimpleNamespace(context_window=100000)),
            build=lambda provider, model_id: model,
            fallbacks=lambda provider, model_id: [],
        )
        agent = agent_mod.build_agent(
            catalog, "ch", "m1", "read-only", settings or Settings(), extra_tools=[_plugin_tool()],
            skills=skills, memory=memory,
        )
        self.addCleanup(agent.db.db_engine.dispose)
        return agent

    def test_plugin_tool_is_declared_without_copying_its_name_into_system_prompt(self) -> None:
        agent = self._build(script=[text("好")])
        agent.run("你好")
        request = agent.model.invocations[0]
        self.assertIn("mcp_echo", {item["function"]["name"] for item in request["tools"]})
        system = next(message.content for message in request["messages"] if message.role == "system")
        self.assertIn("你是运行在用户终端里的 aaa 助手", system)
        self.assertNotIn("mcp_echo", system)

    def test_user_prompt_keeps_native_context_and_uses_skill_references_on_demand(self) -> None:
        (self.tmp / "SYSTEM.md").write_text("自定义基础指令", encoding="utf-8")
        (self.tmp / "APPEND_SYSTEM.md").write_text("用户追加指令", encoding="utf-8")
        skills = discover_skills([BUILTIN_SKILLS_DIR])
        agent = self._build(skills=skills, script=[
            tool_call("load_skill", {"name": "create-extension"}),
            tool_call("load_skill", {"name": "create-extension", "reference": "animations.md"}),
            text("已查阅"),
        ])
        agent.run("怎样自定义开场动画？")
        requests = agent.model.invocations
        system = next(message.content for message in requests[0]["messages"] if message.role == "system")
        self.assertIn("自定义基础指令\n\n用户追加指令", system)
        self.assertNotIn("你是运行在用户终端里的 aaa 助手", system)
        self.assertIn("create-extension", system)
        self.assertIn("为 aaa 创建扩展或插件", system)
        self.assertNotIn("animations.md", system)
        self.assertNotIn("get_skill_script", {item["function"]["name"] for item in requests[0]["tools"]})
        results = [message.content for message in requests[-1]["messages"] if message.role == "tool"]
        self.assertIn("animations.md", results[0])
        self.assertIn("ANIMATION = frames", results[1])

    def test_manager_uses_original_model_and_db(self) -> None:
        agent = self._build(memory="builtin")
        manager = agent.memory_manager
        self.assertIsNotNone(manager)
        self.assertIs(manager.model, agent.model)
        self.assertIs(manager.db, agent.db)
        self.assertTrue(manager.delete_memories)
        self.assertTrue(manager.clear_memories)

    def test_memory_is_off_by_default(self) -> None:
        agent = self._build(script=[text("好")])
        agent.db.upsert_user_memory(UserMemory(memory="旧内置条目", user_id="alice"))
        agent.run("你好", user_id="alice")
        request = agent.model.invocations[0]
        self.assertNotIn("旧内置条目", str(request["messages"]))
        self.assertNotIn("update_user_memory", {item["function"]["name"] for item in request["tools"]})

    def test_memory_survives_rebuild_and_injection(self) -> None:
        first = self._build(script=[text("好")], memory="builtin")
        first.run("你好", user_id="alice")
        memory_id = first.memory_manager.add_user_memory(
            UserMemory(memory="alice 喜欢蓝色", user_id="alice"), user_id="alice"
        )
        self.assertTrue(memory_id)

        second = self._build(script=[text("收到"), text("好的")], memory="builtin")
        second.run("在吗", user_id="alice")
        second.run("你是谁", user_id="bob")
        self.assertIn("alice 喜欢蓝色", str(second.model.invocations[0]))
        self.assertNotIn("alice 喜欢蓝色", str(second.model.invocations[1]))

        rows, total = read_builtin(second.db, "alice", limit=10, page=1)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].memory, "alice 喜欢蓝色")


    def _build_external(self, engine, script):
        settings = Settings()
        return self._build(settings, script=script, memory=MemoryHost(engine, "external", settings))

    def test_external_engine_injects_into_system_prompt_only(self) -> None:
        engine = FakeExternalEngine([MemoryEntry(memory="alice 喜欢蓝色")])
        agent = self._build_external(engine, [text("好"), text("收到")])
        agent.db.upsert_user_memory(UserMemory(memory="旧内置条目", user_id="u1"))
        agent.run("Q1", session_id="s1", user_id="u1")
        agent.run("Q2", session_id="s1", user_id="u1")

        self.assertIsNone(agent.memory_manager)
        self.assertFalse(agent.enable_agentic_memory)
        first = agent.model.invocations[0]["messages"]
        self.assertNotIn("旧内置条目", str(first))
        self.assertNotIn("update_user_memory", {
            item["function"]["name"] for item in agent.model.invocations[0]["tools"]
        })
        system = [str(m.content) for m in first if m.role == "system"]
        self.assertTrue(any("alice 喜欢蓝色" in content for content in system))
        self.assertEqual([str(m.content) for m in first if m.role == "user"], ["Q1"])

        second = agent.model.invocations[1]["messages"]
        history = [str(m.content) for m in second if m.role != "system"]
        self.assertFalse(any("recalled_memories" in content for content in history))
        self.assertEqual([query.identity.user_id for query in engine.queries], ["u1", "u1"])
        self.assertEqual([event.query for event in engine.events], ["Q1", "Q2"])

    def test_external_recall_failure_keeps_the_run(self) -> None:
        engine = FakeExternalEngine(error=RuntimeError("engine down"))
        agent = self._build_external(engine, [text("好")])
        result = agent.run("Q1", session_id="s1", user_id="u1")
        self.assertEqual(result.content, "好")
        system = str(agent.model.invocations[0]["messages"][0].content)
        self.assertNotIn("recalled_memories", system)

    def _tool_host(self, engine):
        return MemoryHost(engine, "tools", Settings())

    def test_external_tools_are_registered_and_dispatched(self) -> None:
        tool = MemoryTool(
            name="search_notes", description="搜索记忆",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            read_only=True,
        )
        engine = ToolEngine([tool])
        host = self._tool_host(engine)
        self.addCleanup(host.close)
        agent = self._build(
            script=[tool_call("search_notes", {"q": "蓝色"}), text("查到了")],
            memory=host,
        )
        result = agent.run("找找蓝色", session_id="s1", user_id="u1")

        request = agent.model.invocations[0]
        self.assertIn("search_notes", {item["function"]["name"] for item in request["tools"]})
        system = next(m.content for m in request["messages"] if m.role == "system")
        self.assertIn("- search_notes: 搜索记忆", system)
        self.assertIn("先检索记忆再回答", system)
        self.assertIn("你是运行在用户终端里的 aaa 助手", system)  # 宿主模板未被引擎替换

        self.assertEqual(result.content, "查到了")
        name, args, context = engine.calls[0]
        self.assertEqual((name, args), ("search_notes", {"q": "蓝色"}))
        self.assertTrue(result.run_id)
        expected = MemoryIdentity("u1", "aaa-main", "s1", result.run_id)
        self.assertEqual(context, expected)
        self.assertEqual(engine.queries[0].identity, expected)
        self.assertEqual(engine.events[0].identity, expected)
        tool_text = "".join(str(m.content) for m in agent.model.invocations[1]["messages"] if m.role == "tool")
        self.assertIn("search_notes ok", tool_text)

        rebuilt = self._build(script=[text("继续")], memory=host)
        next_run = rebuilt.run("下一轮", session_id="s1", user_id="u1")
        self.assertNotEqual(next_run.run_id, result.run_id)
        self.assertEqual(engine.queries[-1].identity, MemoryIdentity("u1", "aaa-main", "s1", next_run.run_id))

    def test_replay_tool_and_observation_use_the_new_run_id(self) -> None:
        engine = ToolEngine([MemoryTool("search_notes")])
        host = self._tool_host(engine)
        self.addCleanup(host.close)
        agent = self._build(
            script=[text("原回答"), tool_call("search_notes", {}), text("新回答")], memory=host,
        )
        original = agent.run("原问题", session_id="s1", user_id="u1")
        result = stream_continue(
            agent, "s1", original.run_id, fork=True, input="新问题",
            user_id="u1", memory=host, emitter=mock.Mock(),
        )
        self.assertEqual(result["content"], "新回答")
        self.assertEqual(len(engine.queries), 1)
        identity = engine.events[-1].identity
        self.assertNotEqual(identity.run_id, original.run_id)
        self.assertEqual(engine.calls[-1][2], identity)

    def test_replay_with_blank_user_does_not_inherit_another_memory_owner(self) -> None:
        engine = ToolEngine([MemoryTool("search_notes")])
        host = MemoryHost(engine, "tools", Settings({"memory.user_id": ""}))
        self.addCleanup(host.close)
        agent = self._build(
            script=[text("原回答"), tool_call("search_notes", {}), text("新回答")], memory=host,
        )
        original = agent.run("原问题", session_id="s1", user_id="alice")
        with self.assertLogs("aaa", level="ERROR"):
            result = stream_continue(
                agent, "s1", original.run_id, fork=True, input="新问题",
                user_id="", memory=host, emitter=mock.Mock(),
            )
        self.assertTrue(result["error"])
        self.assertEqual(engine.calls, [])
        self.assertEqual(len(engine.events), 1)

    def test_external_tool_confirmation_is_independent_of_read_only(self) -> None:
        write = MemoryTool(name="remember_note", requires_confirmation=False)
        read = MemoryTool(name="read_private_note", read_only=True, requires_confirmation=True)
        engine = ToolEngine([write, read])
        host = self._tool_host(engine)
        self.addCleanup(host.close)
        agent = self._build(
            script=[tool_call(write.name, {}), tool_call(read.name, {}), text("完成")],
            memory=host,
        )
        paused = agent.run("处理记忆", session_id="s1", user_id="u1")
        self.assertEqual([name for name, _, _ in engine.calls], [write.name])
        (requirement,) = paused.requirements
        self.assertEqual(requirement.tool_execution.tool_name, read.name)
        requirement.reject("用户拒绝")
        result = agent.continue_run(run_response=paused, requirements=[requirement], session_id="s1")
        self.assertEqual(result.content, "完成")
        self.assertEqual([name for name, _, _ in engine.calls], [write.name])

    def test_external_tool_errors_become_error_text(self) -> None:
        tool = MemoryTool(name="search_notes", description="搜索", read_only=True)
        engine = ToolEngine([tool], error=RuntimeError("boom"))
        agent = self._build(
            script=[tool_call("search_notes", {}), text("收到")],
            memory=self._tool_host(engine),
        )
        agent.run("查找", session_id="s1", user_id="u1")
        tool_text = "".join(str(m.content) for m in agent.model.invocations[1]["messages"] if m.role == "tool")
        self.assertIn("Error: RuntimeError", tool_text)

    def test_memory_tool_name_conflict_refuses_assembly(self) -> None:
        tool = MemoryTool(name="read_file", description="与内置工具冲突")
        engine = ToolEngine([tool])
        with self.assertRaises(MemoryEngineError):
            self._build(memory=self._tool_host(engine))


if __name__ == "__main__":
    unittest.main()
