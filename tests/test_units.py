"""离线回归：全部不联网。假模型驱动真实 Agent 循环，其余测模块契约。

运行 `./tests/run.sh`；需要真实渠道时再跑 `./tests/run.sh e2e`。
"""

import contextlib
import io
import json
import os
import re
import signal
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agno.agent import Agent
from agno.compaction import Compaction
from agno.db.sqlite import SqliteDb
from agno.tools import Toolkit
from fixtures import FakeModel, text, tool_call
from prompt_toolkit.document import Document

from aaa import plugins
from aaa.boundary.danger import danger_reason
from aaa.boundary.policy import (
    APPROVAL_ALWAYS,
    APPROVAL_DANGEROUS,
    MODE_FULL,
    MODE_READ,
    MODE_WRITE,
    Isolation,
    ModePolicy,
    is_external_tool,
    needs_approval,
    resolve_policy,
    signature,
)
from aaa.cli import commands as commands_mod
from aaa.cli import usage_stats
from aaa.cli.commands import _print_usage, handle_command
from aaa.cli.completer import SlashCompleter
from aaa.cli.diff import change_diff
from aaa.cli.run import approve_requirements, stream_continue, stream_run
from aaa.cli.state import AppState
from aaa.cli.ui import display_width
from aaa.config import settings as settings_mod
from aaa.config.loader import normalize_config
from aaa.config.settings import Settings, load_settings, save
from aaa.context.params import compact_threshold, compaction_budget, history_runs
from aaa.model.catalog import Ambiguous, NotFound, from_config
from aaa.model.provider import parse_cost
from aaa.runtime.agent import tools_for_mode
from aaa.sessions import index
from aaa.sessions.query import list_sessions
from aaa.tools.assemble import build_tools, tool_names
from aaa.tools.builtin.powershell import PowerShellTools, find_powershell
from aaa.tools.builtin.processes import ProcessTools
from aaa.usage import ledger, report
from aaa.usage.cost import cost_of, multiplier


def output_of_usage(data) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _print_usage(data)
    return buffer.getvalue()


def tools_for_test(mode: str) -> list:
    policy = resolve_policy(mode)
    return build_tools(
        policy.capabilities,
        root=policy.isolation.file_root,
        confirm_tools=policy.confirm_tools,
    )


def sample_config() -> dict:
    return normalize_config(
        {
            "version": 2,
            "default": {"provider": "a", "model": "m1"},
            "providers": {
                "a": {
                    "base_url": "https://a.example/v1",
                    "api_key": "sk-a",
                    "models": [{"id": "m1", "context_window": 1000}],
                },
                "b": {
                    "base_url": "https://b.example/v1",
                    "api_key": "sk-b",
                    "models": [
                        {"id": "m1", "context_window": 2000},
                        {"id": "m2", "context_window": 2000},
                        {"id": "m3", "context_window": 2000},
                    ],
                },
            },
        },
        Settings(),
    )


class CatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = from_config(sample_config(), Settings())

    def test_resolve_three_outcomes(self) -> None:
        self.assertIsInstance(self.catalog.resolve("m1"), Ambiguous)
        self.assertEqual(self.catalog.resolve("b/m1").ref.provider, "b")
        self.assertEqual(self.catalog.resolve("m2").ref.provider, "b")
        self.assertIsInstance(self.catalog.resolve("nope"), NotFound)

    def test_scope_and_fallbacks(self) -> None:
        self.assertEqual(len(self.catalog.scope(["a/*"])), 1)
        self.assertEqual(len(self.catalog.scope(["*/m1"])), 2)
        self.assertEqual(len(self.catalog.scope([])), 4)
        self.assertEqual(self.catalog.fallbacks("a", "m1"), [])
        self.assertEqual([m.id for m in self.catalog.fallbacks("b", "m1")], ["m2", "m3"])


class BoundaryTest(unittest.TestCase):
    def test_tool_visibility_per_mode(self) -> None:
        read_only = set(tool_names(tools_for_test(MODE_READ)))
        write = set(tool_names(tools_for_test(MODE_WRITE)))
        full = set(tool_names(tools_for_test(MODE_FULL)))

        self.assertIn("grep", read_only)
        self.assertNotIn("write_file", read_only)
        self.assertNotIn("run_shell_command", read_only)
        self.assertIn("write_file", write)
        self.assertIn("job_output", write)
        self.assertNotIn("delete_file", write)
        self.assertIn("delete_file", full)

    def test_shell_confirmation_only_in_workspace_write(self) -> None:
        expected = {MODE_READ: False, MODE_WRITE: True, MODE_FULL: False}
        for mode, wants in expected.items():
            for tool in build_tools(
                resolve_policy(mode).capabilities,
                root=Path.cwd(),
                confirm_tools=resolve_policy(mode).confirm_tools,
            ):
                for name, function in (getattr(tool, "functions", None) or {}).items():
                    if name == "run_shell_command":
                        self.assertEqual(
                            bool(getattr(function, "requires_confirmation", False)), wants, mode
                        )


class PolicyTest(unittest.TestCase):
    def test_declarations_and_isolation(self) -> None:
        base = Path("/tmp/base")
        read = resolve_policy(MODE_READ, base)
        self.assertEqual(read.capabilities, frozenset({"read", "list", "grep"}))
        self.assertEqual(read.confirm_tools, frozenset())
        self.assertEqual(read.isolation.file_root, base)

        write = resolve_policy(MODE_WRITE, base)
        self.assertIn("shell", write.capabilities)
        self.assertIn("run_shell_command", write.confirm_tools)

        full = resolve_policy(MODE_FULL, base)
        self.assertIn("delete", full.capabilities)
        self.assertEqual(full.isolation.file_root, Path("/"))
        self.assertEqual(full.isolation.command_cwd, base)

        with self.assertRaises(KeyError):
            resolve_policy("nope", base)

    def test_signature_is_key_order_independent(self) -> None:
        self.assertEqual(
            signature("run_shell_command", {"args": ["ls"], "tail": 100}),
            signature("run_shell_command", {"tail": 100, "args": ["ls"]}),
        )
        self.assertNotEqual(signature("tool", {"a": 1}), signature("tool", {"a": 2}))

    def test_policy_source_is_replaceable(self) -> None:
        sandbox = Path("/tmp/sandbox-root")

        def sandbox_policy(mode: str, base=None) -> ModePolicy:
            return ModePolicy(frozenset({"read", "list"}), frozenset(), Isolation(sandbox, sandbox))

        names = set(tool_names(tools_for_mode(MODE_WRITE, Settings(), sandbox_policy)))
        self.assertIn("read_file", names)
        self.assertNotIn("run_shell_command", names)


class DiffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="aaa-diff-"))

    def test_write_new_and_existing(self) -> None:
        self.assertIn("新建 a.txt", change_diff("write_file", {"path": "a.txt", "content": "x"}, self.root))
        (self.root / "a.txt").write_text("old\n", encoding="utf-8")
        diff = change_diff("write_file", {"path": "a.txt", "content": "new\n"}, self.root)
        self.assertIn("-old", diff)
        self.assertIn("+new", diff)

    def test_edit_hit_miss_and_replace_all(self) -> None:
        (self.root / "f.txt").write_text("aa\nbb\naa\n", encoding="utf-8")
        hit = change_diff("edit_file", {"path": "f.txt", "old_str": "bb", "new_str": "cc"}, self.root)
        self.assertIn("-bb", hit)
        self.assertIn("+cc", hit)
        # 多处命中且未开 replace_all：工具会失败，不预览。
        self.assertIsNone(change_diff("edit_file", {"path": "f.txt", "old_str": "aa", "new_str": "z"}, self.root))
        replaced = change_diff(
            "edit_file", {"path": "f.txt", "old_str": "aa", "new_str": "z", "replace_all": True}, self.root
        )
        self.assertEqual(replaced.count("+z"), 2)
        self.assertIsNone(change_diff("edit_file", {"path": "gone.txt", "old_str": "a", "new_str": "b"}, self.root))
        self.assertIsNone(change_diff("edit_file", {"path": "f.txt", "old_str": "no-hit", "new_str": "b"}, self.root))

    def test_move_and_invalid(self) -> None:
        (self.root / "a").write_text("x", encoding="utf-8")
        self.assertEqual(
            change_diff("move_file", {"src": "a", "dst": "b"}, self.root), "移动 a → b"
        )
        self.assertEqual(
            change_diff("move_file", {"src": "a", "dst": "b", "overwrite": True}, self.root),
            "移动 a → b（覆盖已有目标）",
        )
        # src 不存在：工具会失败，不预览。
        self.assertIsNone(change_diff("move_file", {"src": "ghost", "dst": "b"}, self.root))
        self.assertIsNone(change_diff("run_shell_command", {"args": ["ls"]}, self.root))
        self.assertIsNone(change_diff("write_file", {"path": ""}, self.root))
        self.assertIsNone(change_diff("write_file", {"path": "x", "content": 123}, self.root))


class ApprovalTest(unittest.TestCase):
    class FakeRequirement:
        def __init__(self, name, args):
            self.tool_execution = type("Tool", (), {"tool_name": name, "tool_args": args})()
            self.outcome = None

        def confirm(self):
            self.outcome = "confirmed"

        def reject(self, reason):
            self.outcome = f"rejected:{reason}"

    class FakeEmitter:
        def __init__(self, answer):
            self.answer, self.asked = answer, []

        def ask_approval(self, name, args, diff=None):
            self.asked.append((name, args, diff))
            return self.answer

    def test_same_signature_asked_once_and_rejection_reported(self) -> None:
        approved: set[str] = set()
        emitter = self.FakeEmitter(True)
        first = self.FakeRequirement("run_shell_command", {"args": ["ls"]})
        second = self.FakeRequirement("run_shell_command", {"args": ["ls"]})
        approve_requirements([first, second], approved, emitter)
        self.assertEqual([first.outcome, second.outcome], ["confirmed", "confirmed"])
        self.assertEqual(len(emitter.asked), 1)

        rejected = self.FakeRequirement("run_shell_command", {"args": ["rm"]})
        approve_requirements([rejected], set(), self.FakeEmitter(False))
        self.assertEqual(rejected.outcome, "rejected:用户拒绝")

    def test_dangerous_strategy_lets_safe_commands_run_without_asking(self) -> None:
        emitter = self.FakeEmitter(False)  # 非交互：只可能拒绝
        safe = self.FakeRequirement("run_shell_command", {"args": ["ls", "-la"]})
        approve_requirements([safe], set(), emitter, APPROVAL_DANGEROUS)
        self.assertEqual(safe.outcome, "confirmed")
        self.assertEqual(emitter.asked, [])

    def test_dangerous_strategy_still_asks_for_dangerous_commands(self) -> None:
        emitter = self.FakeEmitter(False)
        stopped = self.FakeRequirement("run_shell_command", {"args": ["rm", "-rf", "/"]})
        approve_requirements([stopped], set(), emitter, APPROVAL_DANGEROUS)
        self.assertEqual(stopped.outcome, "rejected:用户拒绝")
        self.assertEqual(len(emitter.asked), 1)

    def test_file_tools_reviewed_with_diff_even_under_dangerous(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="aaa-review-"))
        (root / "f.txt").write_text("old line\n", encoding="utf-8")
        # dangerous 策略不自动放行文件改动；diff 进入询问。
        emitter = self.FakeEmitter(True)
        req = self.FakeRequirement("edit_file", {"path": "f.txt", "old_str": "old", "new_str": "new"})
        approve_requirements([req], set(), emitter, APPROVAL_DANGEROUS, review_root=root)
        self.assertEqual(req.outcome, "confirmed")
        name, args, diff = emitter.asked[0]
        self.assertEqual(name, "edit_file")
        self.assertIn("-old line", diff)
        self.assertIn("+new line", diff)

        # 拒绝：改动不执行（HITL 层已保证），签名不进已批准集合。
        rejected = self.FakeRequirement("write_file", {"path": "g.txt", "content": "x"})
        approve_requirements([rejected], set(), self.FakeEmitter(False), APPROVAL_ALWAYS, review_root=root)
        self.assertEqual(rejected.outcome, "rejected:用户拒绝")

        # 无法预览（文件缺失）→ diff 为 None，仍询问。
        missing = self.FakeRequirement("edit_file", {"path": "nope.txt", "old_str": "a", "new_str": "b"})
        emitter2 = self.FakeEmitter(True)
        approve_requirements([missing], set(), emitter2, APPROVAL_ALWAYS, review_root=root)
        self.assertEqual(emitter2.asked[0][2], None)
        self.assertEqual(missing.outcome, "confirmed")

    def test_needs_approval_reads_only_command_tools(self) -> None:
        self.assertTrue(needs_approval("run_shell_command", {"args": ["ls"]}, APPROVAL_ALWAYS))
        self.assertFalse(needs_approval("run_shell_command", {"args": ["ls"]}, APPROVAL_DANGEROUS))
        self.assertTrue(
            needs_approval("run_powershell", {"command": "Remove-Item x"}, APPROVAL_DANGEROUS)
        )
        self.assertEqual(
            danger_reason("run_shell_command", {"args": ["git", "push", "--force", "o", "m"]}),
            "覆盖远端历史或丢弃本地改动",
        )
        self.assertIsNone(danger_reason("run_shell_command", {"args": ["git", "push", "o", "m"]}))
        self.assertIsNone(danger_reason("read_file", {"path": "/etc/passwd"}))
        with self.assertRaises(KeyError):
            needs_approval("run_shell_command", {"args": ["ls"]}, "never")


class ToolsTest(unittest.TestCase):
    def test_grep_registered_without_legacy_search(self) -> None:
        for mode in (MODE_READ, MODE_WRITE, MODE_FULL):
            names = set(tool_names(tools_for_test(mode)))
            self.assertIn("grep", names)
            self.assertNotIn("search_content", names)

    def test_powershell_registered_only_when_available(self) -> None:
        names = set(tool_names(tools_for_test(MODE_WRITE)))
        self.assertEqual("run_powershell" in names, find_powershell() is not None)
        self.assertIsNone(find_powershell("/nonexistent/powershell"))

    def test_powershell_passes_command_as_arguments(self) -> None:
        output = PowerShellTools(executable="/bin/echo").run_powershell("Write-Output hi")
        for flag in ("-NoProfile", "-NonInteractive", "-Command", "Write-Output hi"):
            self.assertIn(flag, output)


class ShellJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="aaa-jobs-"))
        self.tools = ProcessTools(base_dir=self.work)

    def tearDown(self) -> None:
        for job in list(self.tools._jobs.values()):
            if job.process.poll() is None:
                try:
                    os.killpg(job.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            job.process.wait(timeout=5)
        self.tools._jobs.clear()

    def _job_id(self, text_value: str) -> str:
        match = re.search(r"job ([0-9a-f]{8}) started", text_value)
        self.assertIsNotNone(match, text_value)
        return match.group(1)

    def _wait_finished(self, job_id: str, timeout: float = 10.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            output = self.tools.job_output(job_id)
            if "finished" in output:
                return output
            time.sleep(0.1)
        self.fail(f"job {job_id} 未结束")

    def test_background_job_reports_output_and_exit(self) -> None:
        job_id = self._job_id(self.tools.run_shell_command(["echo", "job-ok"], background=True))
        output = self._wait_finished(job_id)
        self.assertIn("exit 0", output)
        self.assertIn("job-ok", output)

        bad = self._job_id(
            self.tools.run_shell_command(["sh", "-c", "echo bad >&2; exit 3"], background=True)
        )
        self.assertIn("exit 3", self._wait_finished(bad))

    def test_kill_and_illegal_id(self) -> None:
        job_id = self._job_id(self.tools.run_shell_command(["sleep", "30"], background=True))
        self.assertIn("running", self.tools.job_output(job_id))
        self.assertIn("已终止", self.tools.job_kill(job_id))
        self.assertIn("finished", self._wait_finished(job_id))
        self.assertIn("未知 job id", self.tools.job_output("../etc"))

    def test_job_state_stays_in_process(self) -> None:
        job_id = self._job_id(self.tools.run_shell_command(["sleep", "30"], background=True))
        self.assertIn("running", self.tools.job_output(job_id))
        # 后台任务不落盘：工作目录里没有 jobs/、meta.json、output.log。
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertIn("已终止", self.tools.job_kill(job_id))


class SessionIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Path(tempfile.mkdtemp(prefix="aaa-idx-")) / "sessions.db"

    def test_latest_per_cwd_and_sequence(self) -> None:
        self.assertIsNone(index.latest(self.db, "/w"))
        index.remember(self.db, "/w", "aaaa1111")
        index.remember(self.db, "/w", "bbbb2222")
        index.remember(self.db, "/x", "cccc3333")
        self.assertEqual(index.latest(self.db, "/w"), "bbbb2222")
        self.assertEqual(index.latest(self.db, "/x"), "cccc3333")
        conn = sqlite3.connect(self.db)
        seq = conn.execute("SELECT seq FROM aaa_cwd_session WHERE cwd = '/w'").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(seq, 2)

    def test_forget_only_matching_session(self) -> None:
        index.remember(self.db, "/w", "aaaa1111")
        index.forget(self.db, "/w", "bbbb2222")  # 不匹配：不动
        self.assertEqual(index.latest(self.db, "/w"), "aaaa1111")
        index.forget(self.db, "/w", "aaaa1111")
        self.assertIsNone(index.latest(self.db, "/w"))


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aaa-cfg-")) / "config.toml"
        self._patch = mock.patch.object(settings_mod, "CONFIG_FILE", self.tmp)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()

    def test_defaults_and_validation(self) -> None:
        self.assertEqual(Settings().get("tools.shell_timeout"), 120)
        bad = Settings({"tools.shell_timeout": "abc"})
        self.assertEqual(bad.get("tools.shell_timeout"), 120)
        self.assertTrue(bad.warnings)
        unknown = Settings({"nope.key": 1})
        self.assertTrue(any("未知配置项" in warning for warning in unknown.warnings))

    def test_command_approval_defaults_and_choices(self) -> None:
        self.assertEqual(Settings().get("boundary.command_approval"), APPROVAL_ALWAYS)
        accepted = Settings({"boundary.command_approval": APPROVAL_DANGEROUS})
        self.assertEqual(accepted.get("boundary.command_approval"), APPROVAL_DANGEROUS)
        self.assertFalse(accepted.warnings)
        rejected = Settings({"boundary.command_approval": "never"})
        self.assertEqual(rejected.get("boundary.command_approval"), APPROVAL_ALWAYS)
        self.assertTrue(any("boundary.command_approval" in w for w in rejected.warnings))

    def test_broken_file_falls_back_and_roundtrip(self) -> None:
        self.tmp.write_text("not = [valid\n", encoding="utf-8")
        broken = load_settings()
        self.assertEqual(broken.get("tools.shell_timeout"), 120)
        self.assertTrue(broken.warnings)

        fresh = Settings()
        fresh.set("tools.shell_timeout", 42)
        save(fresh)
        self.assertEqual(load_settings().get("tools.shell_timeout"), 42)

    def test_legacy_memory_keys_migrate_without_warnings(self) -> None:
        self.tmp.write_text(
            '[memory]\nenabled = true\nengine = "agno-native"\nuser_id = "u"\n',
            encoding="utf-8",
        )
        migrated = load_settings()
        self.assertEqual(migrated.get("memory.source"), "builtin")
        self.assertFalse(any("memory.enabled" in w or "memory.engine" in w for w in migrated.warnings))

        self.tmp.write_text('[memory]\nenabled = false\nengine = "agno-native"\n', encoding="utf-8")
        self.assertEqual(load_settings().get("memory.source"), "off")

        self.tmp.write_text('[memory]\nenabled = true\nengine = "external:miniviking"\n', encoding="utf-8")
        self.assertEqual(load_settings().get("memory.source"), "external:miniviking")

        self.tmp.write_text('[memory]\nenabled = true\nengine = "miniviking"\n', encoding="utf-8")
        self.assertEqual(load_settings().get("memory.source"), "external:miniviking")

    def test_explicit_source_wins_over_legacy_keys(self) -> None:
        self.tmp.write_text(
            '[memory]\nsource = "off"\nenabled = true\nengine = "agno-native"\n',
            encoding="utf-8",
        )
        migrated = load_settings()
        self.assertEqual(migrated.get("memory.source"), "off")
        self.assertFalse(any("memory." in w and "未知" in w for w in migrated.warnings))


class SocksProxyTest(unittest.TestCase):
    def test_socks_proxy_backend_present(self) -> None:
        """缺 socksio 时 httpx 会抛 ImportError；连接失败才是预期（本地 9 端口无服务）。"""
        import httpx

        with self.assertRaises(Exception) as ctx:
            httpx.get("http://127.0.0.1:9/", proxy="socks5://127.0.0.1:9", timeout=0.3)
        self.assertNotIn("socksio", str(ctx.exception))


class ContextParamsTest(unittest.TestCase):
    def test_values_and_threshold(self) -> None:
        s = Settings({"context.history_runs": 7, "context.compaction_budget": 1234})
        self.assertEqual(history_runs(s), 7)
        self.assertEqual(compaction_budget(s), 1234)

        s = Settings({"context.threshold_ratio": 0.85, "context.threshold_reserve": 16384})
        self.assertEqual(compact_threshold(1_000_000, s), 850_000)
        self.assertEqual(compact_threshold(20_000, s), 3_616)
        self.assertGreaterEqual(compact_threshold(10, Settings({"context.threshold_ratio": 0.01})), 1)


def echo(value: str) -> str:
    """原样回显。

    Args:
        value: 要回显的文本。
    """
    return value


class ConfirmToolkit(Toolkit):
    def __init__(self) -> None:
        super().__init__(
            name="confirm_toolkit", tools=[self.touch], requires_confirmation_tools=["touch"]
        )

    def touch(self, value: str = "x") -> str:
        """写下一个值。

        Args:
            value: 要写入的值。
        """
        return f"touched:{value}"


class AgentLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Path(tempfile.mkdtemp(prefix="aaa-loop-")) / "sessions.db"
        self._agents: list[Agent] = []

    def tearDown(self) -> None:
        for agent in self._agents:
            agent.db.db_engine.dispose()

    def make(self, script, tools) -> Agent:
        agent = Agent(
            model=FakeModel(script), tools=tools, db=SqliteDb(db_file=str(self.db)), markdown=False
        )
        self._agents.append(agent)
        return agent

    def test_tool_roundtrip(self) -> None:
        agent = self.make([tool_call("echo", {"value": "hi"}), text("done")], [echo])
        output = agent.run("go", session_id="s1")
        self.assertEqual(output.content, "done")
        self.assertEqual([t.result for t in output.tools or [] if t.tool_name == "echo"], ["hi"])

    def test_hitl_confirm_and_reject(self) -> None:
        agent = self.make(
            [tool_call("touch", {"value": "v1"}), text("after-confirm")], [ConfirmToolkit()]
        )
        paused = agent.run("go", session_id="s1")
        requirement = paused.requirements[0]
        requirement.confirm()
        resumed = agent.continue_run(
            run_response=paused, requirements=[requirement], session_id="s1"
        )
        self.assertEqual(resumed.content, "after-confirm")
        self.assertEqual(
            [t.result for t in resumed.tools or [] if t.tool_name == "touch"], ["touched:v1"]
        )

        reject_agent = self.make(
            [tool_call("touch", {"value": "v2"}), text("after-reject")], [ConfirmToolkit()]
        )
        paused = reject_agent.run("go", session_id="s2")
        requirement = paused.requirements[0]
        requirement.reject("用户拒绝")
        resumed = reject_agent.continue_run(
            run_response=paused, requirements=[requirement], session_id="s2"
        )
        self.assertEqual(resumed.content, "after-reject")
        tool_messages = [
            m.content
            for m in reject_agent.model.invocations[-1]["messages"]
            if getattr(m, "role", None) == "tool"
        ]
        self.assertIn("用户拒绝", tool_messages)

    def test_stream_run_uses_approval_strategy(self) -> None:
        class StubEmitter:
            def __init__(self, answer: bool) -> None:
                self.answer, self.asked = answer, []

            def event(self, kind: str, **data) -> None:
                pass

            def ask_approval(self, name, args) -> bool:
                self.asked.append(name)
                return self.answer

            def close(self) -> None:
                pass

        for strategy, answer, expected_asks in (
            (APPROVAL_DANGEROUS, False, 0),
            (APPROVAL_ALWAYS, True, 1),
        ):
            agent = self.make(
                [tool_call("touch", {"value": "v1"}), text("after")], [ConfirmToolkit()]
            )
            emitter = StubEmitter(answer)
            summary = stream_run(
                agent, "go", f"s-{strategy}", set(), emitter, strategy=strategy
            )
            self.assertEqual(summary["content"].strip(), "after")
            self.assertEqual(len(emitter.asked), expected_asks, strategy)

    class Stub:
        def event(self, kind, **data):
            pass
        def ask_approval(self, name, args, diff=None):
            return True
        def close(self):
            pass

    def test_stream_continue_regenerate_and_fork(self) -> None:
        # regenerate：重建最后一轮回复。
        agent = self.make([text("one"), text("one-v2")], [])
        first = agent.run("go", session_id="s-c")
        summary = stream_continue(
            agent, "s-c", first.run_id, regenerate=True, emitter=self.Stub(), approved=set()
        )
        self.assertEqual(summary["content"].strip(), "one-v2")
        # 重建时不再回放旧回复：模型收到的 user 序列只有原输入。
        users = [m.content for m in agent.model.invocations[-1]["messages"] if m.role == "user"]
        self.assertEqual(users, ["go"])

        # fork + 新文本：原 run 保留，新 run 从原输入处重发。
        agent2 = self.make([text("one"), text("edited")], [])
        first2 = agent2.run("go", session_id="s-f")
        summary = stream_continue(
            agent2, "s-f", first2.run_id, input="go-edited", fork=True,
            emitter=self.Stub(), approved=set(),
        )
        self.assertEqual(summary["content"].strip(), "edited")
        users = [m.content for m in agent2.model.invocations[-1]["messages"] if m.role == "user"]
        self.assertEqual(users, ["go-edited"])
        runs = agent2.db.get_runs(session_id="s-f") or []
        self.assertEqual(len(runs), 2)

    def test_edit_forks_from_last_user_and_resumes_hitl(self) -> None:
        # 编辑重发只带新文本；分叉后的 HITL 暂停仍能按原生 run_id 续跑。
        agent = self.make(
            [text("one"), tool_call("touch", {"value": "v"}), text("after")], [ConfirmToolkit()]
        )
        first = agent.run("go", session_id="s-edit")
        self.assertEqual(first.content.strip(), "one")

        class Yes:
            def __init__(self):
                self.asked = []

            def event(self, kind, **data):
                pass

            def ask_approval(self, name, args, diff=None):
                self.asked.append(name)
                return True

            def close(self):
                pass

        emitter = Yes()
        summary = stream_continue(
            agent, "s-edit", first.run_id, input="go-edited", fork=True,
            emitter=emitter, approved=set(),
        )
        self.assertEqual(emitter.asked, ["touch"])
        self.assertEqual(summary["content"].strip(), "after")
        fork_users = [m.content for m in agent.model.invocations[1]["messages"] if m.role == "user"]
        self.assertEqual(fork_users, ["go-edited"])

    def test_write_file_hitl_with_diff_review(self) -> None:
        from agno.tools.workspace import Workspace

        root = Path(tempfile.mkdtemp(prefix="aaa-ws-"))
        ws = Workspace(root=root, allowed=[], confirm=["write"])
        agent = self.make([tool_call("write_file", {"path": "n.txt", "content": "hi"}), text("done")], [ws])
        paused = agent.run("go", session_id="s-ws")
        requirements = paused.requirements or []
        self.assertEqual(len(requirements), 1)

        class Yes:
            def ask_approval(self, name, args, diff=None):
                self.diff = diff
                return True
            def event(self, kind, **data):
                pass
            def close(self):
                pass

        emitter = Yes()
        approve_requirements(requirements, set(), emitter, review_root=root)
        self.assertIn("新建 n.txt", emitter.diff)
        agent.continue_run(
            run_response=paused, requirements=requirements, session_id="s-ws"
        )
        self.assertEqual((root / "n.txt").read_text(encoding="utf-8"), "hi")

        # 拒绝：文件不落盘。
        root2 = Path(tempfile.mkdtemp(prefix="aaa-ws2-"))
        ws2 = Workspace(root=root2, allowed=[], confirm=["write"])
        agent2 = self.make([tool_call("write_file", {"path": "n.txt", "content": "hi"}), text("ok")], [ws2])
        paused2 = agent2.run("go", session_id="s-ws2")
        req2 = paused2.requirements or []
        class No:
            def ask_approval(self, name, args, diff=None):
                return False
            def event(self, kind, **data):
                pass
            def close(self):
                pass
        approve_requirements(req2, set(), No(), review_root=root2)
        agent2.continue_run(run_response=paused2, requirements=req2, session_id="s-ws2")
        self.assertFalse((root2 / "n.txt").exists())

class ExportCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Path(tempfile.mkdtemp(prefix="aaa-export-")) / "sessions.db"
        self._agents: list[Agent] = []

    def tearDown(self) -> None:
        for agent in self._agents:
            agent.db.db_engine.dispose()

    @staticmethod
    def _menu_fake(answer):
        """questionary 菜单替身：application.run 返回预置答案。"""
        return SimpleNamespace(
            application=SimpleNamespace(key_bindings=None, run=mock.Mock(return_value=answer))
        )

    def test_session_bare_menu_rename_via_delegation(self) -> None:
        agent = self._agent([text("回答")])
        agent.run("问题", session_id="s-m")
        state = self._state(agent, "s-m")
        fakes = iter([self._menu_fake("rename"), self._menu_fake("改过的标题")])
        buf = io.StringIO()
        with mock.patch.object(commands_mod.questionary, "select", side_effect=lambda *a, **k: next(fakes)), \
                mock.patch.object(commands_mod.questionary, "text", side_effect=lambda *a, **k: next(fakes)), \
                contextlib.redirect_stdout(buf):
            self.assertTrue(handle_command(state, "/session"))
        self.assertIn("已改名", buf.getvalue())
        sessions = {s.session_id: s for s in list_sessions(agent, 10)}
        self.assertEqual((sessions["s-m"].session_data or {}).get("session_name"), "改过的标题")

    def test_session_bare_menu_cancel_changes_nothing(self) -> None:
        agent = self._agent([text("回答")])
        agent.run("问题", session_id="s-c")
        state = self._state(agent, "s-c")
        buf = io.StringIO()
        with mock.patch.object(commands_mod.questionary, "select",
                               return_value=self._menu_fake(None)), \
                contextlib.redirect_stdout(buf):
            self.assertTrue(handle_command(state, "/session"))
        # 菜单取消静默返回：不打印、不改动。
        self.assertEqual(buf.getvalue(), "")

    def test_export_bare_confirms_prefilled_path_and_cancel(self) -> None:
        agent = self._agent([text("回答")])
        agent.run("问题", session_id="s-e2")
        out_dir = Path(tempfile.mkdtemp(prefix="aaa-export2-"))
        state = self._state(agent, "s-e2")
        target = out_dir / "chosen.md"
        buf = io.StringIO()
        with mock.patch.object(commands_mod.questionary, "text",
                               return_value=self._menu_fake(str(target))), \
                contextlib.redirect_stdout(buf):
            self.assertTrue(handle_command(state, "/export"))
        self.assertTrue(target.is_file())
        self.assertIn("已导出", buf.getvalue())
        (out_dir / "chosen.md").unlink()
        with mock.patch.object(commands_mod.questionary, "text",
                               return_value=self._menu_fake(None)), \
                contextlib.redirect_stdout(buf):
            self.assertTrue(handle_command(state, "/export"))
        self.assertIn("已取消", buf.getvalue())
        self.assertFalse(target.exists())

    def _state(self, agent: Agent, session_id: str) -> AppState:
        self._agents.append(agent)
        return AppState(
            config={}, settings=None, catalog=None, provider="p", model_id="m",
            mode=MODE_WRITE, runnable=agent,
            session_id=session_id, completer=None,
            context_tokens=1234,
        )

    def _agent(self, script) -> Agent:
        agent = Agent(
            model=FakeModel(script),
            compaction=Compaction(compact_at_tokens=100_000, uncompacted_runs=3),
            db=SqliteDb(db_file=str(self.db)), markdown=False,
            add_history_to_context=True, num_history_runs=3,
        )
        self._agents.append(agent)
        return agent

    def test_export_writes_markdown_transcript(self) -> None:
        import contextlib as _cl
        import io as _io

        agent = self._agent([text("回答一"), text("回答二")])
        agent.run("问题一", session_id="s-e")
        agent.run("问题二", session_id="s-e")
        out_dir = Path(tempfile.mkdtemp(prefix="aaa-export-"))
        state = self._state(agent, "s-e")
        buf = _io.StringIO()
        with _cl.redirect_stdout(buf):
            self.assertTrue(handle_command(state, f"/export {out_dir / 'out.md'}"))
        text_out = (out_dir / "out.md").read_text(encoding="utf-8")
        self.assertIn("问题一", text_out)
        self.assertIn("回答二", text_out)
        self.assertIn("## user", text_out)
        self.assertIn("已导出", buf.getvalue())


class PluginsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aaa-plugins-"))

    def test_discover_commands_filters_names(self) -> None:
        d = self.tmp / "commands"
        d.mkdir()
        (d / "review.md").write_text("审查 $ARGUMENTS", encoding="utf-8")
        (d / "Bad Name.md").write_text("x", encoding="utf-8")  # 非法命令名：跳过
        cmds = plugins.discover_commands([d])
        self.assertEqual([c.name for c in cmds], ["review"])
        self.assertEqual(cmds[0].template, "审查 $ARGUMENTS")

    def test_discover_skills_nested_and_frontmatter(self) -> None:
        d = self.tmp / "skills"
        (d / "alpha").mkdir(parents=True)
        (d / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: 第一个技能\n---\n正文A", encoding="utf-8"
        )
        (d / "cat" / "beta").mkdir(parents=True)
        (d / "cat" / "beta" / "SKILL.md").write_text(
            "---\nname: beta\ndescription: 嵌套技能\n---\n正文B", encoding="utf-8"
        )
        skills = plugins.discover_skills([d])
        self.assertEqual([s.name for s in skills], ["alpha", "beta"])
        self.assertEqual(plugins.skill_body(skills[0].path), "正文A")

    def test_mcp_discovery_and_toggle(self) -> None:
        d = self.tmp / "extensions"
        ext = d / "toy"
        ext.mkdir(parents=True)
        (ext / "extension.toml").write_text(
            '[mcp]\ncommand = "python toy.py"\n', encoding="utf-8"
        )
        (d / "empty").mkdir()
        exts = plugins.discover_mcp([d])
        self.assertEqual([e.name for e in exts], ["toy"])
        self.assertTrue(exts[0].enabled)
        self.assertFalse(plugins.toggle_extension(ext))
        self.assertEqual([e.name for e in plugins.discover_mcp([d]) if e.enabled], [])
        self.assertTrue(plugins.toggle_extension(ext))

    def test_skill_tool_known_and_unknown(self) -> None:
        d = self.tmp / "skills"
        (d / "alpha").mkdir(parents=True)
        (d / "alpha" / "SKILL.md").write_text("---\nname: alpha\n---\n正文A", encoding="utf-8")
        tool = plugins.make_skill_tool(plugins.discover_skills([d]))
        self.assertEqual(tool("alpha"), "正文A")
        self.assertIn("未知技能", tool("nope"))

    def test_skill_references_cannot_read_outside_the_reference_directory(self) -> None:
        skill = self.tmp / "skills" / "alpha"
        references = skill / "references"
        references.mkdir(parents=True)
        (skill / "SKILL.md").write_text("技能正文", encoding="utf-8")
        (references / "guide.md").write_text("公开指南", encoding="utf-8")
        private = skill / "private.md"
        private.write_text("目录外内容", encoding="utf-8")
        tool = plugins.make_skill_tool(plugins.discover_skills([skill.parent]))
        self.assertEqual(tool("alpha", "guide.md"), "公开指南")
        self.assertIn("必须是单个", tool("alpha", "../private.md"))
        if os.name != "nt":
            (references / "alias.md").symlink_to(private)
            self.assertIn("无法读取", tool("alpha", "alias.md"))

    @unittest.skipIf(os.name == "nt", "Windows 创建符号链接可能需要额外权限")
    def test_linked_reference_root_is_neither_a_resource_nor_a_second_skill(self) -> None:
        skill = self.tmp / "skills" / "alpha"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("技能正文", encoding="utf-8")
        (skill / "private.md").write_text("目录外内容", encoding="utf-8")
        (skill / "references").symlink_to(".", target_is_directory=True)
        skills = plugins.discover_skills([skill.parent])
        self.assertEqual([item.name for item in skills], ["alpha"])
        self.assertIn("无法读取", plugins.make_skill_tool(skills)("alpha", "private.md"))

    @unittest.skipIf(os.name == "nt", "Windows 创建符号链接可能需要额外权限")
    def test_body_link_cannot_escape_at_discovery_or_read_time(self) -> None:
        skill = self.tmp / "skills" / "alpha"
        skill.mkdir(parents=True)
        private = self.tmp / "private.md"
        private.write_text("目录外内容", encoding="utf-8")
        body = skill / "SKILL.md"
        body.write_text("技能正文", encoding="utf-8")
        tool = plugins.make_skill_tool(plugins.discover_skills([skill.parent]))
        body.unlink()
        body.symlink_to(private)
        self.assertEqual(plugins.discover_skills([skill.parent]), [])
        self.assertIn("无法读取", tool("alpha"))

    def test_is_external_tool(self) -> None:
        self.assertTrue(is_external_tool("mcp_echo"))
        self.assertTrue(is_external_tool("create_plugin"))
        self.assertFalse(is_external_tool("echo"))
        self.assertFalse(is_external_tool("run_shell_command"))

    def test_create_tools_validate_and_write(self) -> None:

        home = Path(tempfile.mkdtemp(prefix="aaa-inst-"))
        with mock.patch.object(plugins, "COMMANDS_DIR_DEFAULT", home / "commands"), \
             mock.patch.object(plugins, "EXTENSIONS_DIR_DEFAULT", home / "extensions"), \
             mock.patch.object(plugins, "SKILLS_ROOT_DEFAULT", home / ".agents" / "skills"):
            create_plugin, create_skill = plugins.make_create_tools()
            self.assertTrue(create_plugin.requires_confirmation)
            self.assertTrue(create_skill.requires_confirmation)
            self.assertIn("未知类型", create_plugin.entrypoint(kind="nope", name="x", content="y"))
            self.assertIn("名称不合法", create_plugin.entrypoint(kind="command", name="Bad!", content="y"))
            self.assertIn("创建失败", create_plugin.entrypoint(kind="extension", name="ok", content="hello"))
            self.assertIn("[mcp]", create_plugin.entrypoint(kind="extension", name="ok", content="plain = true"))
            ok = create_plugin.entrypoint(kind="command", name="review", content="审查 $ARGUMENTS")
            self.assertIn("已创建 command:review", ok)
            self.assertEqual(
                (home / "commands" / "review.md").read_text(encoding="utf-8"),
                "审查 $ARGUMENTS",
            )

            skill_md = "---\nname: myskill\ndescription: 什么时候读我\n---\n方法论正文"
            self.assertIn("名称不合法", create_skill.entrypoint(name="Bad!", content=skill_md))
            self.assertIn("name", create_skill.entrypoint(name="myskill", content="无frontmatter"))
            self.assertIn("description", create_skill.entrypoint(
                name="myskill", content="---\nname: myskill\n---\n正文"))
            self.assertIn("已创建 skill:myskill", create_skill.entrypoint(name="myskill", content=skill_md))
            self.assertIn("已存在", create_skill.entrypoint(name="myskill", content=skill_md))
            self.assertIn("已创建", create_skill.entrypoint(name="myskill", content=skill_md, force=True))
            self.assertEqual(
                (home / ".agents" / "skills" / "myskill" / "SKILL.md").read_text(encoding="utf-8"),
                skill_md,
            )

    def test_builtin_skill_discovered(self) -> None:
        skills = plugins.discover_skills([plugins.BUILTIN_SKILLS_DIR])
        self.assertIn("create-extension", [s.name for s in skills])
        description = next(s.description for s in skills if s.name == "create-extension")
        self.assertTrue(description.startswith("为 aaa 创建扩展"), description)


class McpBridgeTest(unittest.TestCase):
    """MCP 真连接：本地 toy stdio server（零网络，mcp/fastmcp 已在依赖里）。"""

    SERVER = str(Path(__file__).parent.parent / "tests" / "toy_mcp_server.py")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aaa-mcp-"))

    def test_open_bridge_and_call(self) -> None:
        exts = [plugins.McpExtension(
            name="toy", path=self.tmp / "toy",
            spec={"command": f"{sys.executable} {self.SERVER}"}, enabled=True,
        )]
        functions, failed = plugins.open_mcp_tools(exts, timeout=30)
        self.assertEqual(failed, [])
        names = [f.name for f in functions]
        self.assertEqual(names, ["mcp_echo"])
        self.assertTrue(all(f.requires_confirmation for f in functions))
        entrypoint = functions[0].entrypoint
        original = entrypoint  # entrypoint 已是同步桥；直接调用验证跨 loop 结果
        result = original(**{"text": "hi"})
        self.assertEqual(getattr(result, "content", result), "echo:hi")

    def test_open_failure_reported(self) -> None:
        exts = [plugins.McpExtension(
            name="bad", path=self.tmp / "bad",
            spec={"command": f"{sys.executable} -c 'import sys; sys.exit(1)'"}, enabled=True,
        )]
        functions, failed = plugins.open_mcp_tools(exts, timeout=10)
        self.assertEqual(functions, [])
        self.assertEqual(failed, ["bad"])


class UsageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aaa-usage-"))
        self.sessions = self.tmp / "sessions.db"
        self.usage = self.tmp / "usage.db"
        self._create_sessions()

    def _create_sessions(self) -> None:
        conn = sqlite3.connect(self.sessions)
        conn.executescript(
            """
            CREATE TABLE agno_runs (
                run_id TEXT PRIMARY KEY, session_id TEXT, status TEXT,
                created_at INTEGER, updated_at INTEGER, run_data JSON
            );
            CREATE TABLE aaa_cwd_session (cwd TEXT PRIMARY KEY, session_id TEXT, seq INTEGER);
            """
        )
        conn.commit()
        conn.close()

    def _add_run(self, run_id, created_at, updated_at=None, channel="deepseek",
                 model="deepseek-flash", inp=1000, out=500, read=0, status="COMPLETED",
                 role="model", session_id="s1", cwd="/w"):
        metrics = {
            "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": read, "total_tokens": inp + out,
            "details": {role: [{"id": model, "provider": "OpenAI", "input_tokens": inp,
                                "output_tokens": out, "cache_read_tokens": read,
                                "total_tokens": inp + out}]},
        }
        data = {"run_id": run_id, "model": model, "metrics": metrics,
                "metadata": {"channel": channel} if channel else {}}
        conn = sqlite3.connect(self.sessions)
        conn.execute(
            "INSERT OR REPLACE INTO agno_runs VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, session_id, status, created_at, updated_at or created_at,
             json.dumps(data)),
        )
        conn.execute(
            "INSERT OR REPLACE INTO aaa_cwd_session VALUES (?, ?, ?)", (cwd, session_id, 1)
        )
        conn.commit()
        conn.close()

    def _price(self, channel, model):
        return parse_cost({"input": 0.3, "output": 1.2, "cache_read": 0.006})

    def test_cost_math_and_peak_windows(self) -> None:
        cost = parse_cost({
            "input": 0.3, "output": 1.2, "cache_read": 0.006, "cache_write": 0,
            "off_peak": {"multiplier": 0.5, "windows": [["22:00", "02:00"]]},
        })
        day = datetime(2026, 11, 18, 12, 0)
        night = datetime(2026, 11, 18, 23, 30)
        early = datetime(2026, 11, 18, 1, 59)
        self.assertAlmostEqual(cost_of(cost, 1_000_000, 0, 0, 0, day), 0.3)
        self.assertAlmostEqual(cost_of(cost, 1_000_000, 0, 0, 0, night), 0.15)
        self.assertAlmostEqual(cost_of(cost, 1_000_000, 0, 0, 0, early), 0.15)
        self.assertAlmostEqual(cost_of(cost, 0, 0, 0, 0, day), 0.0)
        self.assertIsNone(cost_of(None, 1000, 0, 0, 0, day))
        self.assertEqual(multiplier(cost, datetime(2026, 11, 18, 2, 0)), 1.0)

    def test_invalid_cost_is_rejected(self) -> None:
        for bad in (
            {"input": -1, "output": 1},
            {"input": "x", "output": 1},
            {"output": 1},
            {"input": 1, "output": 1, "off_peak": {"multiplier": 2}},
            {"input": 1, "output": 1, "off_peak": {"multiplier": 0.5, "windows": [["25:00", "01:00"]]}},
        ):
            with self.assertRaises(ValueError):
                parse_cost(bad)
        config = normalize_config(
            {"version": 2, "providers": {"a": {
                "base_url": "https://x", "api_key": "k",
                "models": [{"id": "m1", "cost": {"input": -1, "output": 1}}]}}},
            Settings(),
        )
        self.assertNotIn("cost", config["providers"]["a"]["models"][0])

    def test_sync_is_idempotent_and_tracks_updates(self) -> None:
        ts = int(datetime(2026, 11, 18, 12, 0).timestamp())
        self._add_run("r1", ts)
        first = ledger.sync(price=self._price, sessions_db=self.sessions, usage_db=self.usage)
        self.assertEqual((first.added, first.updated), (1, 0))

        again = ledger.sync(price=self._price, sessions_db=self.sessions, usage_db=self.usage)
        self.assertEqual((again.added, again.updated), (0, 0))

        self._add_run("r1", ts, updated_at=ts + 5, inp=3000)
        third = ledger.sync(price=self._price, sessions_db=self.sessions, usage_db=self.usage)
        self.assertEqual((third.added, third.updated), (0, 1))

        data = report.report("day", now=datetime(2026, 11, 18, 13, 0), usage_db=self.usage)
        self.assertEqual(len(data.rows), 1)
        self.assertEqual(data.rows[0].channel, "deepseek")
        self.assertEqual(data.rows[0].input_tokens, 3000)
        self.assertEqual(data.rows[0].requests, 1)

    def test_paused_runs_are_not_ingested(self) -> None:
        ts = int(datetime(2026, 11, 18, 12, 0).timestamp())
        self._add_run("paused", ts, status="PAUSED")
        result = ledger.sync(price=self._price, sessions_db=self.sessions, usage_db=self.usage)
        self.assertEqual(result.touched, 0)

    def test_unpriced_rows_are_backfilled_once(self) -> None:
        ts = int(datetime(2026, 11, 18, 12, 0).timestamp())
        self._add_run("r1", ts)
        first = ledger.sync(sessions_db=self.sessions, usage_db=self.usage)
        self.assertEqual(first.added, 1)
        data = report.report("day", now=datetime(2026, 11, 18, 13, 0), usage_db=self.usage)
        self.assertIsNone(data.rows[0].cost)
        self.assertEqual(data.unpriced, 1)

        second = ledger.sync(price=self._price, sessions_db=self.sessions, usage_db=self.usage)
        self.assertEqual(second.backfilled, 1)
        data = report.report("day", now=datetime(2026, 11, 18, 13, 0), usage_db=self.usage)
        self.assertAlmostEqual(data.rows[0].cost, 1000 / 1e6 * 0.3 + 500 / 1e6 * 1.2)
        self.assertEqual(data.unpriced, 0)

        third = ledger.sync(price=self._price, sessions_db=self.sessions, usage_db=self.usage)
        self.assertEqual(third.backfilled, 0)

    def test_unknown_channel_stays_unpriced(self) -> None:
        ts = int(datetime(2026, 11, 18, 12, 0).timestamp())
        self._add_run("r1", ts, channel=None)
        ledger.sync(price=self._price, sessions_db=self.sessions, usage_db=self.usage)
        data = report.report("day", now=datetime(2026, 11, 18, 13, 0), usage_db=self.usage)
        self.assertIsNone(data.rows[0].channel)
        self.assertIsNone(data.rows[0].cost)

    def test_report_sorts_by_tokens_and_windows_by_period(self) -> None:
        base = datetime(2026, 11, 18, 12, 0)
        ts = int(base.timestamp())
        self._add_run("r1", ts, channel="a", model="big", inp=9000, out=1000)
        self._add_run("r2", ts, channel="a", model="small", inp=100, out=100)
        self._add_run("r3", ts, channel="b", model="mid", inp=2000, out=0, role="compaction_model")
        ledger.sync(price=self._price, sessions_db=self.sessions, usage_db=self.usage)

        day = report.report("day", now=base, usage_db=self.usage)
        self.assertEqual([row.model for row in day.rows], ["big", "mid", "small"])
        self.assertEqual(day.tokens, 12_200)

        start, end = report.window("week", base)
        self.assertEqual((start.weekday(), start.hour), (0, 0))
        start, _ = report.window("month", base)
        self.assertEqual(start.day, 1)

        older = int(datetime(2026, 11, 10, 12, 0).timestamp())
        self._add_run("r4", older, channel="a", model="old", inp=5000, out=0)
        ledger.sync(price=self._price, sessions_db=self.sessions, usage_db=self.usage)
        later = report.report("day", now=datetime(2026, 11, 18, 23, 0), usage_db=self.usage)
        self.assertNotIn("old", [row.model for row in later.rows])

    def test_usage_table_aligns_with_cjk(self) -> None:
        data = report.Report(
            period="day",
            start=datetime(2026, 11, 18, 0, 0),
            end=datetime(2026, 11, 18, 12, 0),
            rows=[
                report.Row(channel="deepseek", model="deepseek-flash",
                           input_tokens=128_000, output_tokens=42_000,
                           cache_read_tokens=980_000, cost=0.09468, requests=2),
                report.Row(channel=None, model="mystery", input_tokens=900,
                           output_tokens=100, unpriced=1, requests=1),
            ],
        )
        lines = [line for line in output_of_usage(data).splitlines() if line.strip()]
        table = lines[1:-1]  # 去掉标题与「未定价」提示
        widths = {display_width(line) for line in table}
        self.assertEqual(len(widths), 1, output_of_usage(data))

    def test_usage_renderer_hides_zero_cache_column(self) -> None:
        data = report.Report(
            period="day",
            start=datetime(2026, 11, 18, 0, 0),
            end=datetime(2026, 11, 18, 12, 0),
            rows=[report.Row(channel="deepseek", model="deepseek-flash",
                             input_tokens=128_000, output_tokens=42_000,
                             cost=0.087, requests=3)],
        )
        self.assertIn("deepseek", output_of_usage(data))
        self.assertNotIn("缓存读", output_of_usage(data))
        self.assertIn("$0.087", output_of_usage(data))


class UsageStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="aaa-cmdstats-"))
        self.file = self.root / "command_usage.json"
        patcher = mock.patch.object(usage_stats, "COMMAND_USAGE_FILE", self.file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_note_and_order_by_frequency_then_alphabet(self) -> None:
        usage_stats.note_used("memory")
        usage_stats.note_used("memory")
        usage_stats.note_used("model")
        self.assertEqual(usage_stats.read_counts(), {"memory": 2, "model": 1})
        names = ["/config", "/memory", "/model", "/session"]
        # 次数降序优先；没出现过的按字母 A→Z。
        self.assertEqual(usage_stats.ordered(names), ["/memory", "/model", "/config", "/session"])

    def test_missing_or_corrupt_file_falls_back_to_alphabet(self) -> None:
        self.assertEqual(usage_stats.ordered(["/z", "/a"]), ["/a", "/z"])
        self.file.write_text("not json", encoding="utf-8")
        self.assertEqual(usage_stats.ordered(["/z", "/a"]), ["/a", "/z"])
        self.assertEqual(usage_stats.read_counts(), {})

    def test_completer_orders_by_usage(self) -> None:
        usage_stats.note_used("session")
        completer = SlashCompleter([], Settings())
        texts = [c.text for c in completer.get_completions(Document("/", len("/")), None)]
        self.assertEqual(texts[0], "/session")
        self.assertEqual(texts[1:], sorted(texts[1:]))

    def test_note_used_ignores_write_failures(self) -> None:
        readonly = self.root / "ro.json"
        readonly.write_text("{}", encoding="utf-8")
        readonly.chmod(0o444)
        with mock.patch.object(usage_stats, "COMMAND_USAGE_FILE", readonly):
            usage_stats.note_used("memory")  # 不抛异常
        readonly.chmod(0o644)


if __name__ == "__main__":
    unittest.main()
