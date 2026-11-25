"""扩展协议阶段 1：加载、模式裁剪、外部信任面、事件隔离与 Agent 接线。"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fixtures import FakeModel, text, tool_call

from aaa import plugins as plugins_mod
from aaa.boundary.policy import is_external_tool
from aaa.config.settings import Settings
from aaa.extensions import (
    ExtensionRegistry,
    discover_python_extensions,
    load_extensions,
    set_default_registry,
)
from aaa.extensions.bus import EventBus
from aaa.extensions.loader import tool_guard
from aaa.extensions.protocol import (
    Event,
    ExtensionContext,
    PromptBlockSpec,
    ToolDecision,
)
from aaa.runtime import agent as agent_mod

GOOD_EXT = '''
import os

PROTOCOL_VERSION = 1

def _record(event):
    path = os.environ["EXT_EVENT_FILE"]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(event.name + chr(10))

def _block(info):
    return f"扩展状态块（{info.mode}）"

def activate(ctx):
    ctx.register_tool(read_note, name="read_note", capability="read", confirm="always")
    ctx.register_tool(shell_helper, name="shell_helper", capability="shell", confirm="mode")
    ctx.register_tool(draft, name="draft", capability="ext/toy/draft", confirm="mode")
    ctx.register_tool(safe_peek, name="safe_peek", capability="ext/toy/peek", read_safe=True, confirm="mode")
    ctx.subscribe("run.completed", _record)
    ctx.subscribe("run.started", _record)
    ctx.register_prompt_block(name="status", render=_block)

def read_note(path: str) -> str:
    """读取便签内容。"""
    return "note"

def shell_helper(cmd: str) -> str:
    """执行辅助命令。"""
    return "shelled"

def draft(content: str) -> str:
    """写草稿。"""
    return "drafted"

def safe_peek(query: str) -> str:
    """只读窥探。"""
    return "peeked"
'''

BAD_EXT = '''
raise RuntimeError("boom")
'''

COMMAND_EXT = '''
def activate(ctx):
    ctx.register_command("demo", "扩展 $ARGUMENTS")
    ctx.register_command("onlyext", "扩展 $ARGUMENTS")
'''


def _make_ext(root: Path, name: str, body: str, *, kind: str = "python") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "extension.toml").write_text(f'[extension]\nkind = "{kind}"\n', encoding="utf-8")
    (d / "extension.py").write_text(body, encoding="utf-8")
    return d


class LoaderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.event_file = self.tmp / "events.log"
        patcher = mock.patch.dict(os.environ, {"EXT_EVENT_FILE": str(self.event_file)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _load(self, *names_body):
        for name, body in names_body:
            _make_ext(self.tmp, name, body)
        registry = ExtensionRegistry()
        load_extensions(discover_python_extensions([(self.tmp, "user")]), registry)
        return registry

    def test_discover_kind_and_dedupe(self) -> None:
        _make_ext(self.tmp, "toy", GOOD_EXT)
        _make_ext(self.tmp, "mcpish", GOOD_EXT, kind="mcp")
        (self.tmp / "plain").mkdir()
        (self.tmp / "plain" / "extension.toml").write_text("[extension]\nkind = 'python'\n", encoding="utf-8")
        found = discover_python_extensions([(self.tmp, "user")])
        self.assertEqual([e.name for e in found], ["toy"])
        self.assertEqual(found[0].source, "user")

    def test_load_activates_and_failure_isolates(self) -> None:
        registry = self._load(("toy", GOOD_EXT), ("bad", BAD_EXT))
        self.assertEqual(registry.loaded, ["toy"])
        self.assertEqual(registry.disabled, ["bad"])
        self.assertIn("bad", registry.bus.suspended())
        names = [spec.name for spec in registry.tools]
        self.assertEqual(
            names,
            ["ext_toy_read_note", "ext_toy_shell_helper", "ext_toy_draft", "ext_toy_safe_peek"],
        )

    def test_events_reach_subscriber(self) -> None:
        registry = self._load(("toy", GOOD_EXT))
        registry.bus.emit("run.completed", {"run_id": "r1"})
        self.assertIn("run.completed", self.event_file.read_text(encoding="utf-8"))


class ModeGatingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _make_ext(self.tmp, "toy", GOOD_EXT)
        self.registry = ExtensionRegistry()
        load_extensions(discover_python_extensions([(self.tmp, "user")]), self.registry)

    def test_read_only_only_read_safe_and_read_capability(self) -> None:
        names = {fn.name for fn in self.registry.tools_for({"read", "list", "grep"}, "read-only")}
        self.assertEqual(names, {"ext_toy_read_note", "ext_toy_safe_peek"})
        confirmed = {fn.name: fn.requires_confirmation for fn in self.registry.tools_for({"read", "list", "grep"}, "read-only")}
        self.assertTrue(confirmed["ext_toy_read_note"])
        # confirm="mode" 跟随模式确认名单：read-only 名单为空，不询问。
        self.assertFalse(confirmed["ext_toy_safe_peek"])

    def test_workspace_write_brings_shell_and_custom(self) -> None:
        caps = {"read", "list", "grep", "write", "edit", "move", "shell", "powershell"}
        names = {fn.name for fn in self.registry.tools_for(caps, "workspace-write")}
        self.assertEqual(
            names,
            {"ext_toy_read_note", "ext_toy_shell_helper", "ext_toy_draft", "ext_toy_safe_peek"},
        )
        confirmed = {fn.name: fn.requires_confirmation for fn in self.registry.tools_for(caps, "workspace-write")}
        self.assertTrue(confirmed["ext_toy_shell_helper"])
        self.assertTrue(confirmed["ext_toy_safe_peek"])

    def test_full_access_mode_confirm_is_free(self) -> None:
        caps = {"read", "list", "grep", "write", "edit", "move", "delete", "shell", "powershell"}
        confirmed = {fn.name: fn.requires_confirmation for fn in self.registry.tools_for(caps, "danger-full-access")}
        self.assertFalse(confirmed["ext_toy_shell_helper"])
        self.assertTrue(confirmed["ext_toy_read_note"])


class ExternalTrustTest(unittest.TestCase):
    def test_ext_prefix_is_external(self) -> None:
        self.assertTrue(is_external_tool("ext_toy_read_note"))
        self.assertTrue(is_external_tool("mcp_echo"))
        self.assertFalse(is_external_tool("read_file"))


class PanelTest(unittest.TestCase):
    def _make(self):
        registry = ExtensionRegistry()
        ctx = ExtensionContext(registry, registry.bus, "toy")
        ctx.register_panel(name="status", title="状态", render=lambda view: [f"mode={view['mode']}"])
        return registry, ctx

    def test_duplicate_panel_name_raises(self) -> None:
        registry, ctx = self._make()
        with self.assertRaises(ValueError):
            ctx.register_panel(name="status", title="重复", render=lambda view: [])

    def test_activate_and_off(self) -> None:
        registry, _ = self._make()
        self.assertFalse(registry.activate_panel("nope"))
        # 短名唯一时解析到完整名 `<扩展>/<名>`。
        self.assertTrue(registry.activate_panel("status"))
        self.assertEqual(registry.active_panel, "toy/status")
        self.assertTrue(registry.activate_panel(None))
        self.assertIsNone(registry.active_panel)

    def test_same_panel_name_in_other_extension_coexists(self) -> None:
        registry, ctx = self._make()
        other = ExtensionContext(registry, registry.bus, "tool")
        other.register_panel(name="status", title="另一个状态", render=lambda view: [])
        self.assertEqual(
            sorted(p.name for p in registry.panels), ["tool/status", "toy/status"],
        )
        # 短名有歧义：不能激活。
        self.assertFalse(registry.activate_panel("status"))
        self.assertTrue(registry.activate_panel("tool/status"))

    def test_render_receives_state_view(self) -> None:
        registry, _ = self._make()
        self.assertEqual(registry.panel("toy/status").render({"mode": "read-only"}), ["mode=read-only"])


class PanelCommandTest(unittest.TestCase):
    def _state(self):
        registry, _ = PanelTest()._make()
        return SimpleNamespace(extension_registry=registry, report_lines=None)

    def test_panel_command_lists_switches_and_off(self) -> None:
        import contextlib
        import io

        from aaa.cli.commands import handle_command

        state = self._state()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(handle_command(state, "/panel"))
        self.assertIn("已注册 1 个面板", buf.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(handle_command(state, "/panel status"))
        self.assertEqual(state.extension_registry.active_panel, "toy/status")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(handle_command(state, "/panel off"))
        self.assertIsNone(state.extension_registry.active_panel)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(handle_command(state, "/panel nope"))
        self.assertIsNone(state.extension_registry.active_panel)


class BusIsolationTest(unittest.TestCase):
    def test_handler_failures_suspend_whole_extension(self) -> None:
        bus = EventBus()
        seen = []
        ctx = ExtensionContext(SimpleNamespace(register_tool=lambda spec: None), bus, "noisy")

        def handler(event: Event) -> None:
            raise ValueError("boom")

        # 两个订阅同属 noisy：挂起按扩展生效，全部停止派发。
        ctx.subscribe("run.started", handler)
        ctx.subscribe("run.started", seen.append)
        for _ in range(3):
            bus.emit("run.started", {})
        self.assertEqual(len(seen), 2)
        self.assertIn("noisy", bus.suspended())
        bus.emit("run.started", {})
        self.assertEqual(len(seen), 2)


class AgentWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.event_file = self.tmp / "events.log"
        _make_ext(self.tmp, "toy", GOOD_EXT)
        self.registry = ExtensionRegistry()
        load_extensions(discover_python_extensions([(self.tmp, "user")]), self.registry)
        set_default_registry(self.registry)
        self.addCleanup(set_default_registry, None)
        patcher_db = mock.patch.object(agent_mod, "DB_FILE", self.tmp / "sessions.db")
        patcher_dir = mock.patch.object(agent_mod, "DATA_DIR", self.tmp)
        patcher_db.start()
        patcher_dir.start()
        self.addCleanup(patcher_db.stop)
        self.addCleanup(patcher_dir.stop)
        patcher_env = mock.patch.dict(os.environ, {"EXT_EVENT_FILE": str(self.event_file)})
        patcher_env.start()
        self.addCleanup(patcher_env.stop)

    def _build(self, mode: str, script=None):
        model = FakeModel(script or [text("好")])
        catalog = SimpleNamespace(
            require=lambda provider, model_id: (None, SimpleNamespace(context_window=100_000)),
            build=lambda provider, model_id: model,
            fallbacks=lambda provider, model_id: [],
        )
        agent = agent_mod.build_agent(
            catalog, "ch", "m1", mode, Settings(), extensions=self.registry,
        )
        self.addCleanup(agent.db.db_engine.dispose)
        return agent, model

    def test_ext_tools_follow_mode_in_request(self) -> None:
        agent, model = self._build("workspace-write")
        agent.run("你好")
        names = {item["function"]["name"] for item in model.invocations[0]["tools"]}
        self.assertIn("ext_toy_read_note", names)
        self.assertIn("ext_toy_shell_helper", names)

    def test_read_only_hides_shell_capability_ext_tools(self) -> None:
        agent, model = self._build("read-only")
        agent.run("你好")
        names = {item["function"]["name"] for item in model.invocations[0]["tools"]}
        self.assertIn("ext_toy_read_note", names)
        self.assertNotIn("ext_toy_shell_helper", names)
        self.assertNotIn("ext_toy_draft", names)

    def test_interceptor_blocks_tool_inside_agent_run(self) -> None:
        self.registry.register_interceptor(
            "toy", lambda call: ToolDecision.deny("禁止列目录") if call.name == "list_files" else None,
        )
        agent, model = self._build("read-only", [tool_call("list_files", {}), text("完成")])
        agent.run("你好")
        tool_results = [str(m.content) for m in model.invocations[-1]["messages"] if m.role == "tool"]
        self.assertTrue(any("已拒绝执行 list_files" in r for r in tool_results), tool_results)

    def test_run_lifecycle_events_emitted(self) -> None:
        agent, model = self._build("read-only")
        agent.run("你好")
        events = self.event_file.read_text(encoding="utf-8").splitlines()
        self.assertIn("run.started", events)
        self.assertIn("run.completed", events)

    def test_prompt_block_rendered_into_system_tail(self) -> None:
        agent, model = self._build("workspace-write")
        agent.run("你好")
        system = next(m.content for m in model.invocations[0]["messages"] if m.role == "system")
        self.assertIn('<extension_block source="toy/status">', system)
        self.assertIn("扩展状态块（workspace-write）", system)

    def test_prompt_block_cap_truncates_and_zero_disables(self) -> None:
        long_render = lambda info: "长" * 200  # noqa: E731
        self.registry.register_block(
            PromptBlockSpec(owner="toy", name="big", placement="system_tail", render=long_render)
        )
        truncated = self.registry.render_blocks("read-only", 60)
        self.assertIn("…(已截断)", truncated)
        self.assertNotIn("长" * 200, truncated)
        self.assertEqual(self.registry.render_blocks("read-only", 0), "")

    def test_prompt_block_cap_applies_in_agent_system(self) -> None:
        settings = Settings({"project.instructions_max_chars": 40})
        model = FakeModel([text("好")])
        catalog = SimpleNamespace(
            require=lambda provider, model_id: (None, SimpleNamespace(context_window=100_000)),
            build=lambda provider, model_id: model,
            fallbacks=lambda provider, model_id: [],
        )
        agent = agent_mod.build_agent(
            catalog, "ch", "m1", "read-only", settings, extensions=self.registry,
        )
        self.addCleanup(agent.db.db_engine.dispose)
        agent.run("你好")
        system = next(m.content for m in model.invocations[0]["messages"] if m.role == "system")
        self.assertIn("…(已截断)", system)


class SessionToolAllowTest(unittest.TestCase):
    """审批第三档「本会话总是允许」：按工具名放行，会话结束即失效。"""

    def _requirement(self, name, args, confirmed):
        return SimpleNamespace(
            tool_execution=SimpleNamespace(tool_name=name, tool_args=args),
            confirm=lambda: confirmed.append((name, args)),
            reject=lambda reason: None,
        )

    def _emitter(self, asked):
        from aaa.cli.run import ALWAYS_ALLOW

        return SimpleNamespace(
            ask_approval=lambda name, args, diff=None: asked.append((name, args)) or ALWAYS_ALLOW,
        )

    def test_always_allow_skips_asking_for_rest_of_session(self) -> None:
        from aaa.cli.run import approve_requirements

        confirmed, asked, allows, approved = [], [], set(), set()
        approve_requirements(
            [self._requirement("mcp_echo", {"x": 1}, confirmed)],
            approved, self._emitter(asked), tool_allows=allows,
        )
        self.assertEqual(asked, [("mcp_echo", {"x": 1})])
        self.assertEqual(allows, {"mcp_echo"})
        # 同工具、不同参数：本会话不再询问。
        approve_requirements(
            [self._requirement("mcp_echo", {"x": 2}, confirmed)],
            approved, self._emitter(asked), tool_allows=allows,
        )
        self.assertEqual(len(asked), 1)
        self.assertEqual(len(confirmed), 2)

    def test_file_review_tools_never_remember_always_allow(self) -> None:
        from aaa.cli.run import approve_requirements

        confirmed, asked, allows = [], [], set()
        approve_requirements(
            [self._requirement("write_file", {"path": "a"}, confirmed)],
            set(), self._emitter(asked), tool_allows=allows,
        )
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(allows, set())


class ToolGuardTest(unittest.TestCase):
    """工具调用前拦截器：只能收紧、异常即拒绝、三次失败挂起。"""

    def test_deny_short_circuits_and_allow_proceeds(self) -> None:
        registry = ExtensionRegistry()
        registry.register_interceptor(
            "toy", lambda call: ToolDecision.deny("危险命令") if "rm -rf" in str(call.args.get("cmd", "")) else None,
        )
        guard = tool_guard(registry, timeout=2.0)
        ran = []

        def inner(**arguments):
            ran.append(arguments)
            return "ok"

        self.assertEqual(guard(inner, {"cmd": "rm -rf /"}, "run_shell_command"),
                         "已拒绝执行 run_shell_command：危险命令")
        self.assertEqual(ran, [])
        self.assertEqual(guard(inner, {"cmd": "ls"}, "run_shell_command"), "ok")
        self.assertEqual(ran, [{"cmd": "ls"}])

    def test_exception_denies_then_suspends(self) -> None:
        registry = ExtensionRegistry()

        def boom(call):
            raise ValueError("boom")

        registry.register_interceptor("toy", boom)
        guard = tool_guard(registry, timeout=2.0)
        inner = lambda **arguments: "ok"  # noqa: E731
        for _ in range(3):
            self.assertIn("已拒绝执行", guard(inner, {}, "read_file"))
        self.assertIn("toy", registry.bus.suspended())
        # 挂起后该扩展的拦截器被跳过（挂起有日志可见，不是永久拒绝）。
        self.assertEqual(guard(inner, {}, "read_file"), "ok")

    def test_timeout_denies(self) -> None:
        import time

        registry = ExtensionRegistry()
        registry.register_interceptor("toy", lambda call: time.sleep(1.0))
        guard = tool_guard(registry, timeout=0.2)
        self.assertIn("已拒绝执行", guard(lambda **arguments: "ok", {}, "read_file"))


class ContextDebtTest(unittest.TestCase):
    """审定的接口面：settings 只读、log 带前缀、register_command 并入命令空间。"""

    def _ctx(self, settings=None):
        registry = ExtensionRegistry()
        return registry, ExtensionContext(registry, registry.bus, "toy", settings=settings)

    def test_settings_view_reads_key_table_and_hides_writes(self) -> None:
        settings = Settings()
        _, ctx = self._ctx(settings)
        self.assertEqual(ctx.settings.get("tools.shell_timeout"), settings.get("tools.shell_timeout"))
        self.assertEqual(ctx.settings.get("nope.unknown", "fallback"), "fallback")
        self.assertFalse(hasattr(ctx.settings, "set"))

    def test_settings_view_without_host_returns_default(self) -> None:
        _, ctx = self._ctx(None)
        self.assertEqual(ctx.settings.get("tools.shell_timeout", 1), 1)

    def test_log_uses_extension_prefix(self) -> None:
        _, ctx = self._ctx()
        self.assertEqual(ctx.log.name, "aaa.ext.toy")

    def test_register_command_validates_and_registers(self) -> None:
        registry, ctx = self._ctx()
        ctx.register_command("demo", "演示 $ARGUMENTS")
        self.assertEqual([(c.owner, c.name, c.template) for c in registry.commands],
                         [("toy", "demo", "演示 $ARGUMENTS")])
        with self.assertRaises(ValueError):
            ctx.register_command("Bad Name", "x")
        with self.assertRaises(ValueError):
            ctx.register_command("empty", "  ")
        with self.assertRaises(ValueError):
            ctx.register_command("demo", "重复")


class CommandWiringTest(unittest.TestCase):
    def test_extension_command_merges_and_file_command_wins(self) -> None:
        from fixtures import FakeModel, text

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / ".git").mkdir()
        _make_ext(root / ".aaa" / "extensions", "hi", COMMAND_EXT)
        (root / ".aaa" / "commands").mkdir(parents=True)
        (root / ".aaa" / "commands" / "demo.md").write_text("文件 $ARGUMENTS", encoding="utf-8")
        user_ext = root / "user_extensions"
        user_ext.mkdir()
        config = {"default": {"provider": "p", "model": "m"}}
        catalog = SimpleNamespace(
            require=lambda *_: (None, SimpleNamespace(context_window=100_000)),
            scope=lambda _: [], build=lambda *_: FakeModel([text("ok")]), fallbacks=lambda *_: [],
        )
        with mock.patch.object(plugins_mod, "EXTENSIONS_DIR_DEFAULT", user_ext), \
             mock.patch("aaa.cli.state.from_config", return_value=catalog), \
             mock.patch("aaa.cli.state.plugins.discover_mcp", return_value=[]), \
             mock.patch("aaa.runtime.agent.DATA_DIR", root), \
             mock.patch("aaa.runtime.agent.DB_FILE", root / "sessions.db"), \
             contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            from aaa.cli.state import AppState
            state = AppState.start(config, Settings(), trusted=frozenset({"extensions"}))
            try:
                commands = state.plugin_commands
                self.assertEqual(commands["onlyext"].template, "扩展 $ARGUMENTS")
                self.assertEqual(commands["onlyext"].source, "extension")
                # 文件模板优先，扩展同名命令不覆盖它。
                self.assertEqual(commands["demo"].template, "文件 $ARGUMENTS")
            finally:
                state.close_memory()


class LoadFailureVisibilityTest(unittest.TestCase):
    def test_failed_extensions_are_named_in_startup_note(self) -> None:
        from fixtures import FakeModel, text

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / ".git").mkdir()
        user_ext = root / "user_extensions"
        _make_ext(user_ext, "good", COMMAND_EXT)
        _make_ext(user_ext, "bad", BAD_EXT)
        catalog = SimpleNamespace(
            require=lambda *_: (None, SimpleNamespace(context_window=100_000)),
            scope=lambda _: [], build=lambda *_: FakeModel([text("ok")]), fallbacks=lambda *_: [],
        )
        with mock.patch.object(plugins_mod, "EXTENSIONS_DIR_DEFAULT", user_ext), \
             mock.patch("aaa.cli.state.from_config", return_value=catalog), \
             mock.patch("aaa.cli.state.plugins.discover_mcp", return_value=[]), \
             mock.patch("aaa.runtime.agent.DATA_DIR", root), \
             mock.patch("aaa.runtime.agent.DB_FILE", root / "sessions.db"), \
             contextlib.chdir(root), contextlib.redirect_stdout(io.StringIO()):
            from aaa.cli.state import AppState
            state = AppState.start({"default": {"provider": "p", "model": "m"}}, Settings())
            try:
                note = state.project_note
            finally:
                state.close_memory()
        # 失败必须在启动摘要里点名，不能只进日志。
        self.assertIn("加载失败 1 个：bad", note)
        self.assertIn("编程式扩展 1 个：good", note)


class SampleExtensionTest(unittest.TestCase):
    def test_shipped_example_loads_and_registers_all_mounts(self) -> None:
        example_root = Path(__file__).resolve().parents[1] / "docs" / "extending" / "examples"
        registry = ExtensionRegistry()
        load_extensions(discover_python_extensions([(example_root, "builtin")]), registry, Settings())
        self.assertIn("hello", registry.loaded)
        self.assertEqual([spec.name for spec in registry.tools], ["ext_hello_greet"])
        self.assertEqual([p.name for p in registry.panels], ["hello/demo"])
        self.assertEqual([c.name for c in registry.commands], ["hello"])
        self.assertEqual(len(registry.interceptors), 1)
        self.assertEqual(len(registry.blocks), 1)


if __name__ == "__main__":
    unittest.main()
