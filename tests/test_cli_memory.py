"""`/memory` 的行为：状态与引擎菜单、空闲入口与补全。

真实原生 SqliteDb（内存库）+ 假 AppState；不发模型调用、不联网。
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agno.db.schemas.memory import UserMemory
from agno.db.sqlite import SqliteDb
from fixtures import FakeModel, text
from prompt_toolkit.document import Document

from aaa.cli import commands as commands_mod
from aaa.cli import state as state_mod
from aaa.cli.commands import handle_command
from aaa.cli.completer import SlashCompleter
from aaa.cli.ui import print_help
from aaa.config import settings as settings_mod
from aaa.config.settings import Settings
from aaa.memory import MemoryEntry, MemoryHost, MemoryIdentity
from aaa.memory import registry as registry_mod
from aaa.runtime import agent as agent_mod

BASE = 1_700_000_000


def completions(text: str):
    """取补全结果：Document 供文本，complete_event 在补全器中未使用。"""
    completer = SlashCompleter([], Settings())
    return list(completer.get_completions(Document(text, len(text)), None))


class MemoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.db = SqliteDb(db_url="sqlite:///:memory:")
        self.addCleanup(self.db.db_engine.dispose)

    def seed(self, user_id: str, *bodies: str) -> list:
        """按递增 created_at 落库；插入分支令 updated_at=created_at，可验证倒序。"""
        ids = []
        for offset, body in enumerate(bodies):
            item = UserMemory(memory=body, user_id=user_id, created_at=BASE + offset)
            self.db.upsert_user_memory(item)
            ids.append(item.memory_id)
        return ids

    def state(self, *, memory="builtin", user_id="u1", list_limit=10, db=None):
        settings = Settings({"memory.user_id": user_id, "memory.list_limit": list_limit})
        agent = SimpleNamespace(db=db or self.db)
        return SimpleNamespace(settings=settings, runnable=agent, memory=memory)

    @staticmethod
    def _menu_fake(answer):
        return SimpleNamespace(
            application=SimpleNamespace(key_bindings=None, run=mock.Mock(return_value=answer))
        )

    def run_command(self, state, text: str, *, action_choice=None, engine_choice=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if text == "/memory":
                # /memory 先弹操作菜单（action_choice，None 视为取消），
                # 选「切换引擎」后 engine_choice 指定引擎（None 视为取消）。
                with mock.patch.object(commands_mod.questionary, "select",
                                       return_value=self._menu_fake(action_choice)), \
                        mock.patch.object(commands_mod, "_ask_engine_choice",
                                          return_value=engine_choice):
                    handled = handle_command(state, text)
            else:
                handled = handle_command(state, text)
        return handled, buf.getvalue()


class StatusViewTest(MemoryFixture):
    def test_status_shows_current_source_and_count(self) -> None:
        self.seed("u1", "m-one", "m-two")
        handled, out = self.run_command(self.state(user_id="u1"), "/memory")
        self.assertTrue(handled)
        self.assertIn("记忆引擎：内置", out)
        self.assertIn("2 条", out)

    def test_empty_or_missing_table_reports_empty_without_add_hint(self) -> None:
        handled, out = self.run_command(self.state(), "/memory")
        self.assertTrue(handled)
        self.assertIn("0 条", out)
        self.assertNotIn("add", out)

    def test_off_never_reads_a_memory_pool(self) -> None:
        state = self.state(memory=None, db=object())
        handled, out = self.run_command(state, "/memory")
        self.assertTrue(handled)
        # 操作菜单取消是静默的：只输出关闭状态，不读任何池。
        self.assertEqual(out.strip(), "记忆已关闭。")


class PoolIsolationTest(MemoryFixture):
    def test_views_show_only_configured_user_pool(self) -> None:
        self.seed("u1", "mine-one", "mine-two")
        self.seed("u2", "theirs")
        state = self.state(user_id="u1")
        _, status = self.run_command(state, "/memory")
        self.assertIn("2 条", status)
        self.assertNotIn("theirs", status)

    def test_external_data_comes_only_from_active_instance(self) -> None:
        self.seed("u1", "native-only")
        def read(identity, *, limit, page):
            return [MemoryEntry(f"{identity.user_id}-{identity.session_id}-{identity.run_id}-external")], 1

        engine = SimpleNamespace(read=mock.Mock(side_effect=read))
        state = self.state()
        state.memory = MemoryHost(engine, "running", state.settings)
        self.addCleanup(state.memory.close)
        state.session_id = "s1"
        state.settings.set("memory.source", "external:next-start")
        _, status = self.run_command(state, "/memory")
        self.assertIn("外置：running", status)
        self.assertEqual([call.args[0] for call in engine.read.call_args_list],
                         [MemoryIdentity("u1", "aaa-main", "s1")])
        state.session_id = None
        self.run_command(state, "/memory")
        self.assertEqual(engine.read.call_args.args[0], MemoryIdentity("u1", "aaa-main"))


class CommandBoundaryTest(MemoryFixture):
    def test_unknown_subcommand_handled_never_forwarded(self) -> None:
        handled, out = self.run_command(self.state(), "/memory foo bar")
        self.assertTrue(handled)
        self.assertIn("未知子命令", out)

    def test_read_error_caught_at_boundary(self) -> None:
        state = self.state()
        with mock.patch.object(commands_mod, "read_builtin", side_effect=RuntimeError("db boom")):
            handled, out = self.run_command(state, "/memory")
            self.assertTrue(handled)
            self.assertIn("记忆命令失败", out)
            self.assertIn("db boom", out)


class EngineSelectorTest(MemoryFixture):
    def setUp(self) -> None:
        super().setUp()
        self.config = Path(tempfile.mkdtemp(prefix="aaa-mem-cfg-")) / "config.toml"
        patcher = mock.patch.object(settings_mod, "CONFIG_FILE", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_saving_off_keeps_current_data_until_restart(self) -> None:
        state = self.state()
        self.seed("u1", "body")
        handled, out = self.run_command(state, "/memory", action_choice="engine", engine_choice="off")
        self.assertTrue(handled)
        self.assertIn("重启后生效", out)
        self.assertIn('source = "off"', self.config.read_text(encoding="utf-8"))

    def test_direct_engine_pass_saves_without_menu(self) -> None:
        state = self.state()
        handled, out = self.run_command(state, "/memory engine builtin")
        self.assertTrue(handled)
        self.assertIn("已保存：内置", out)
        self.assertIn('source = "builtin"', self.config.read_text(encoding="utf-8"))

    def test_unknown_engine_rejected_without_mutation(self) -> None:
        state = self.state(user_id="u1")
        handled, out = self.run_command(state, "/memory engine vendor-x")
        self.assertTrue(handled)
        self.assertIn("未知记忆引擎", out)
        self.assertEqual(state.settings.get("memory.source"), "off")
        self.assertFalse(self.config.exists())

    def test_picker_can_select_builtin(self) -> None:
        state = self.state(memory=None)
        handled, out = self.run_command(state, "/memory", action_choice="engine", engine_choice="builtin")
        self.assertTrue(handled)
        self.assertIn("已保存：内置", out)
        self.assertIn('source = "builtin"', self.config.read_text(encoding="utf-8"))

    def test_picker_cancel_and_interrupt_keep_configuration(self) -> None:
        state = self.state(user_id="u1")
        handled, out = self.run_command(state, "/memory", action_choice="engine", engine_choice=None)
        self.assertTrue(handled)
        self.assertIn("已取消", out)
        self.assertEqual(state.settings.get("memory.source"), "off")
        self.assertFalse(self.config.exists())
        # 中断（KeyboardInterrupt/EOF）在选择器内归一为取消，同样不改配置。
        question = SimpleNamespace(application=SimpleNamespace(
            key_bindings=None, run=mock.Mock(side_effect=KeyboardInterrupt),
        ))
        answers = iter([self._menu_fake("engine"), question])
        buf = io.StringIO()
        with mock.patch.object(commands_mod.questionary, "select",
                               side_effect=lambda *a, **k: next(answers)), \
                contextlib.redirect_stdout(buf):
            handled, out = handle_command(state, "/memory"), buf.getvalue()
        self.assertTrue(handled)
        self.assertIn("已取消", out)
        self.assertFalse(self.config.exists())

    def test_save_failure_reports_and_keeps_previous_value(self) -> None:
        state = self.state(user_id="u1")
        with mock.patch.object(commands_mod, "save_settings", side_effect=OSError("disk full")):
            handled, out = self.run_command(state, "/memory", action_choice="engine", engine_choice="builtin")
        self.assertTrue(handled)
        self.assertIn("保存失败", out)
        self.assertEqual(state.settings.get("memory.source"), "off")
        self.assertFalse(self.config.exists())


    def test_discovered_external_engine_is_saved_without_loading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin = Path(directory)
            (plugin / "fake-engine.py").write_text("raise RuntimeError('must not import')\n")
            with mock.patch.object(registry_mod, "MEMORY_ENGINES_DIR", plugin):
                handled, out = self.run_command(self.state(), "/memory",
                                                action_choice="engine",
                                                engine_choice="external:fake-engine")
        self.assertTrue(handled)
        self.assertIn("重启后生效", out)
        self.assertIn('source = "external:fake-engine"', self.config.read_text(encoding="utf-8"))


class EngineConfigTest(MemoryFixture):
    """`/memory config` 的宿主校验与引擎落盘；引擎用带 specs 的假实例。"""

    SPECS = [
        {"key": "limits.search_limit", "section": "limits", "type": "int",
         "current": 50, "minimum": 1, "maximum": 200, "choices": None,
         "secret": False, "description": "检索候选上限"},
        {"key": "llm.api_key", "section": "llm", "type": "str",
         "current": "", "minimum": None, "maximum": None, "choices": None,
         "secret": True, "description": "蒸馏端点密钥"},
    ]

    def state_with_specs(self, apply_mock=None):
        engine = SimpleNamespace(config_specs=lambda: list(self.SPECS),
                                 apply_config=apply_mock or mock.Mock(return_value=""))
        state = self.state(memory=None)
        state.session_id = "s-cfg"
        state.memory = MemoryHost(engine, "fake", state.settings)
        self.addCleanup(state.memory.close)
        return state

    def test_builtin_reports_no_engine_config(self) -> None:
        handled, out = self.run_command(self.state(), "/memory config")
        self.assertTrue(handled)
        self.assertIn("内置记忆没有引擎级配置", out)

    def test_direct_apply_converts_and_saves(self) -> None:
        apply_mock = mock.Mock(return_value="")
        state = self.state_with_specs(apply_mock)
        handled, out = self.run_command(state, "/memory config limits.search_limit 80")
        self.assertTrue(handled)
        self.assertIn("已保存：limits.search_limit = 80", out)
        self.assertIn("重启", out)
        apply_mock.assert_called_once_with({"limits.search_limit": 80})

    def test_direct_view_shows_current_and_range(self) -> None:
        state = self.state_with_specs()
        handled, out = self.run_command(state, "/memory config limits.search_limit")
        self.assertTrue(handled)
        self.assertIn("检索候选上限", out)
        self.assertIn("当前：50", out)
        self.assertIn("≥ 1", out)
        self.assertIn("≤ 200", out)

    def test_direct_invalid_value_never_applies(self) -> None:
        apply_mock = mock.Mock(return_value="")
        state = self.state_with_specs(apply_mock)
        handled, out = self.run_command(state, "/memory config limits.search_limit abc")
        self.assertTrue(handled)
        self.assertIn("需要整数", out)
        apply_mock.assert_not_called()

    def test_direct_out_of_range_rejected(self) -> None:
        apply_mock = mock.Mock(return_value="")
        state = self.state_with_specs(apply_mock)
        handled, out = self.run_command(state, "/memory config limits.search_limit 500")
        self.assertTrue(handled)
        self.assertIn("不能大于 200", out)
        apply_mock.assert_not_called()

    def test_unknown_key_reported(self) -> None:
        state = self.state_with_specs()
        handled, out = self.run_command(state, "/memory config nope.x 1")
        self.assertTrue(handled)
        self.assertIn("未知配置键", out)

    def test_secret_current_is_masked(self) -> None:
        state = self.state_with_specs()
        handled, out = self.run_command(state, "/memory config llm.api_key")
        self.assertTrue(handled)
        self.assertIn("当前：", out)
        self.assertNotIn("sk-real", out)

    def test_menu_routes_config_action(self) -> None:
        apply_mock = mock.Mock(return_value="")
        state = self.state_with_specs(apply_mock)
        selects = iter([self._menu_fake("config"), self._menu_fake("limits"),
                        self._menu_fake("limits.search_limit")])
        buf = io.StringIO()
        with mock.patch.object(commands_mod.questionary, "select",
                               side_effect=lambda *a, **k: next(selects)), \
                mock.patch.object(commands_mod.questionary, "text",
                                  return_value=self._menu_fake("80")), \
                contextlib.redirect_stdout(buf):
            self.assertTrue(handle_command(state, "/memory"))
        self.assertIn("已保存：limits.search_limit = 80", buf.getvalue())
        apply_mock.assert_called_once_with({"limits.search_limit": 80})


class MemoryStartupTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(registry_mod, "MEMORY_ENGINES_DIR", self.root))
        self.enterContext(mock.patch.object(agent_mod, "DATA_DIR", self.root))
        self.enterContext(mock.patch.object(agent_mod, "DB_FILE", self.root / "sessions.db"))
        self.enterContext(mock.patch.object(state_mod.plugins, "discover_mcp", return_value=[]))
        catalog = SimpleNamespace(
            scope=lambda _: [], build=lambda *_: FakeModel([text("回答")]), fallbacks=lambda *_: [],
            require=lambda *_: (None, SimpleNamespace(context_window=100_000)),
        )
        self.enterContext(mock.patch.object(state_mod, "from_config", return_value=catalog))
        self.config = {"default": {"provider": "test", "model": "test"}}

    def test_external_instance_survives_rebuild_and_closes_at_exit(self):
        (self.root / "external-test.py").write_text(
            "from aaa.memory import MemoryEntry\n"
            "class Engine:\n"
            "    def __init__(self, path):\n"
            "        self.path = path\n"
            "        (path / 'started').touch(exist_ok=False)\n"
            "    def recall(self, query): return [MemoryEntry('外置记忆')]\n"
            "    def close(self): (self.path / 'closed').touch()\n"
            "ENGINE = Engine\n",
            encoding="utf-8",
        )
        settings = Settings({"memory.source": "external:external-test", "memory.data_root": str(self.root / "data")})
        state = state_mod.AppState.start(self.config, settings)
        self.addCleanup(state.runnable.db.db_engine.dispose)
        self.addCleanup(state.close_memory)
        settings.set("memory.source", "builtin")
        state.rebuild_runnable()
        self.addCleanup(state.runnable.db.db_engine.dispose)
        directory = self.root / "data" / "external-test"
        self.assertTrue((directory / "started").exists())
        self.assertFalse((directory / "closed").exists())
        state.runnable.run("问题")
        request = state.runnable.model.invocations[0]
        self.assertIn("外置记忆", str(request["messages"]))
        self.assertNotIn("update_user_memory", {item["function"]["name"] for item in request["tools"]})
        state.close_memory()
        self.assertTrue((directory / "closed").exists())

    def test_broken_external_starts_with_memory_off_and_keeps_selection(self):
        (self.root / "broken.py").write_text("raise RuntimeError('broken')\n", encoding="utf-8")
        settings = Settings({"memory.source": "external:broken"})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            state = state_mod.AppState.start(self.config, settings)
        self.addCleanup(state.runnable.db.db_engine.dispose)
        self.assertIn("以记忆关闭状态启动", err.getvalue())
        self.assertEqual(settings.get("memory.source"), "external:broken")
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(commands_mod.questionary, "select",
                                  return_value=SimpleNamespace(
                                      application=SimpleNamespace(key_bindings=None,
                                                                  run=mock.Mock(return_value=None)))):
            handle_command(state, "/memory")
        self.assertIn("记忆已关闭。", out.getvalue())
        self.assertEqual(state.runnable.run("问题").content, "回答")

    def test_invalid_engine_is_closed_before_running_off(self):
        (self.root / "invalid.py").write_text(
            "class Engine:\n"
            "    def __init__(self, path): self.path = path\n"
            "    def close(self): (self.path / 'closed').touch()\n"
            "ENGINE = Engine\n",
            encoding="utf-8",
        )
        settings = Settings({"memory.source": "external:invalid", "memory.data_root": str(self.root / "data")})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            state = state_mod.AppState.start(self.config, settings)
        self.addCleanup(state.runnable.db.db_engine.dispose)
        self.assertTrue((self.root / "data" / "invalid" / "closed").is_file())
        self.assertIn("以记忆关闭状态启动", err.getvalue())
        self.assertIsNone(state.runnable.memory_manager)
        self.assertEqual(settings.get("memory.source"), "external:invalid")


class CompletionTest(unittest.TestCase):
    def test_root_command_listed(self) -> None:
        texts = [c.text for c in completions("/mem")]
        self.assertIn("/memory", texts)

    def test_subcommand_and_engine_id_completions(self) -> None:
        self.assertEqual({c.text for c in completions("/memory ")}, {"engine", "config", "embedding"})
        self.assertEqual({c.text for c in completions("/memory e")}, {"engine", "embedding"})
        with mock.patch.object(registry_mod, "MEMORY_ENGINES_DIR", Path("/nonexistent")), \
                mock.patch.object(registry_mod, "_entry_points", return_value=()):
            self.assertEqual({c.text for c in completions("/memory engine ")}, {"builtin", "off"})

    def test_help_lists_memory_entry(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_help()
        self.assertIn("/memory", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
