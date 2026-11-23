"""Offline lifecycle checks for the public CLI stream entry points."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from agno.models.response import ToolExecution
from agno.run.agent import (
    RunCancelledEvent,
    RunCompletedEvent,
    RunContentEvent,
    RunPausedEvent,
    RunStartedEvent,
    ToolCallCompletedEvent,
    ToolCallErrorEvent,
    ToolCallStartedEvent,
)

from aaa.cli.run import JsonEmitter, approve_requirements, stream_continue, stream_run


class Emitter:
    def __init__(self):
        self.events = []
        self.closed = False

    def event(self, kind, **data):
        self.events.append((kind, data))

    def ask_approval(self, name, args, diff=None):
        return False

    def close(self):
        self.closed = True


class Agent:
    def __init__(self, events, cancel_error=None):
        self.events = events
        self.cancel_error = cancel_error
        self.cancelled = []
        self.calls = []

    def run(self, text, **kwargs):
        self.calls.append(("run", text, kwargs))
        return iter(self.events)

    def continue_run(self, **kwargs):
        self.calls.append(("continue", kwargs))
        return iter(self.events)

    def cancel_run(self, run_id):
        self.cancelled.append(run_id)
        if self.cancel_error:
            raise self.cancel_error
        return False  # Agno can store intent even if registration is not yet visible.


class LifecycleTests(unittest.TestCase):
    def test_cancel_before_first_id_uses_native_event_not_completed(self):
        handle = {"run_id": None, "cancel_requested": True}
        agent = Agent([
            RunStartedEvent(run_id="r1"),
            RunContentEvent(run_id="r1", content="partial"),
            RunCancelledEvent(run_id="r1"),
            RunCompletedEvent(run_id="r1"),
        ])
        emitter = Emitter()
        result = stream_run(agent, "hello", "s1", emitter=emitter, user_id="u1", handle=handle)
        self.assertEqual(handle["run_id"], "r1")
        self.assertEqual(agent.cancelled, ["r1"])
        self.assertEqual(agent.calls[0][2]["user_id"], "u1")
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["content"], "partial")
        self.assertTrue(any(kind == "warning" and "未确认保存" in data["message"]
                            for kind, data in emitter.events))
        self.assertTrue(emitter.closed)

    def test_late_cancel_does_not_invent_cancelled_terminal(self):
        handle = {"run_id": None, "cancel_requested": False}
        agent = Agent([])

        def events():
            yield RunStartedEvent(run_id="r2")
            self.assertEqual(handle["run_id"], "r2")
            handle["cancel_requested"] = True
            agent.cancel_run(run_id=handle["run_id"])
            yield RunContentEvent(run_id="r2", content="visible")

        agent.events = events()
        emitter = Emitter()
        result = stream_run(agent, "hello", "s", emitter=emitter, handle=handle)
        self.assertEqual(agent.cancelled, ["r2"])
        self.assertTrue(result["error"])
        self.assertFalse(result.get("cancelled", False))
        self.assertIn("未收到运行终态", [d["message"] for k, d in emitter.events if k == "error"][0])

    def test_completion_after_request_remains_completion(self):
        handle = {"run_id": None, "cancel_requested": True}
        agent = Agent([RunCompletedEvent(run_id="r")])
        result = stream_run(agent, "hello", "s", emitter=Emitter(), handle=handle)
        self.assertEqual(agent.cancelled, ["r"])
        self.assertFalse(result.get("cancelled", False))
        self.assertFalse(result.get("error", False))

    def test_cancel_failure_is_visible_and_not_cancellation(self):
        handle = {"run_id": None, "cancel_requested": True}
        agent = Agent([RunStartedEvent(run_id="r")], RuntimeError("cancel failed"))
        emitter = Emitter()
        result = stream_run(agent, "hello", "s", emitter=emitter, handle=handle)
        self.assertTrue(result["error"])
        self.assertFalse(result.get("cancelled", False))
        self.assertIn(("error", {"message": "cancel failed"}), emitter.events)
        self.assertTrue(emitter.closed)

    def test_cancel_before_approval_does_not_continue_or_approve(self):
        handle = {"run_id": None, "cancel_requested": True}
        agent = Agent([RunPausedEvent(run_id="r", requirements=[])])
        result = stream_run(agent, "hello", "s", emitter=Emitter(), handle=handle)
        self.assertEqual(agent.cancelled, ["r"])
        self.assertEqual(len(agent.calls), 1)
        self.assertTrue(result["error"])
        self.assertFalse(result.get("cancelled", False))

    def test_cancel_during_approval_does_not_resume(self):
        handle = {"run_id": None, "cancel_requested": False}
        agent = Agent([])

        class Requirement:
            tool_execution = ToolExecution(tool_name="run_shell_command", tool_args={"command": "echo hi"})

            def confirm(self):
                raise AssertionError("cancelled approval must not be confirmed")

            def reject(self, reason):
                self.reason = reason

        class CancellingEmitter(Emitter):
            def ask_approval(self, name, args, diff=None):
                handle["cancel_requested"] = True
                agent.cancel_run(run_id=handle["run_id"])
                return False

        agent.events = [RunPausedEvent(run_id="r", requirements=[Requirement()])]
        result = stream_run(agent, "hello", "s", emitter=CancellingEmitter(), handle=handle)
        self.assertEqual(agent.cancelled, ["r"])
        self.assertEqual(len(agent.calls), 1)
        self.assertTrue(result["error"])
        self.assertFalse(result.get("cancelled", False))

    def test_continue_uses_new_event_id_and_preserves_identity(self):
        handle = {"run_id": None, "cancel_requested": True}
        agent = Agent([RunCancelledEvent(run_id="new")])
        result = stream_continue(agent, "s", "old", regenerate=True, user_id="user",
                                 emitter=Emitter(), handle=handle)
        self.assertEqual(agent.cancelled, ["new"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(agent.calls[0][1]["run_id"], "old")
        self.assertEqual(agent.calls[0][1]["user_id"], "user")

    def test_continue_propagates_channel_through_hitl_resume(self):
        class PausingAgent(Agent):
            def continue_run(self, **kwargs):
                self.calls.append(("continue", kwargs))
                if len(self.calls) == 1:
                    return iter([RunPausedEvent(run_id="new", requirements=[])])
                return iter([RunCompletedEvent(run_id="new")])

        for options in ({"regenerate": True}, {"fork": True, "input": "revised"}):
            with self.subTest(options=options):
                agent = PausingAgent([])
                result = stream_continue(agent, "session", "old", emitter=Emitter(),
                                         user_id="user", channel="provider", **options)
                self.assertFalse(result.get("error", False))
                self.assertEqual(len(agent.calls), 2)
                for _, kwargs in agent.calls:
                    self.assertEqual(kwargs["metadata"], {"channel": "provider"})
                    self.assertEqual(kwargs["session_id"], "session")
                    self.assertEqual(kwargs["user_id"], "user")
                self.assertEqual(agent.calls[1][1]["run_id"], "new")

    def test_continue_without_channel_preserves_initial_arguments(self):
        agent = Agent([RunCompletedEvent(run_id="new")])
        stream_continue(agent, "session", "old", emitter=Emitter())
        self.assertNotIn("metadata", agent.calls[0][1])

    def test_edit_truncates_at_last_user_message(self):
        class Run:
            messages = [
                SimpleNamespace(role="user", content="old"),
                SimpleNamespace(role="assistant", content="answer"),
            ]

        agent = Agent([RunCompletedEvent(run_id="new")])
        agent.db = SimpleNamespace(get_run=lambda run_id: Run())
        stream_continue(agent, "s", "old", input="new text", fork=True, emitter=Emitter())
        self.assertEqual(agent.calls[0][1]["continue_from"], 0)

    def test_redo_does_not_force_truncation(self):
        def explode(run_id):
            raise AssertionError("regenerate must not load the run")

        agent = Agent([RunCompletedEvent(run_id="new")])
        agent.db = SimpleNamespace(get_run=explode)
        stream_continue(agent, "s", "old", regenerate=True, emitter=Emitter())
        self.assertNotIn("continue_from", agent.calls[0][1])

    def test_initial_invocation_failure_emits_error_and_closes(self):
        class FailingAgent(Agent):
            def run(self, text, **kwargs):
                raise RuntimeError("run start failed")

            def continue_run(self, **kwargs):
                raise RuntimeError("continue start failed")

        for invoke, message in (
            (lambda agent, emitter: stream_run(agent, "hi", "s", emitter=emitter), "run start failed"),
            (lambda agent, emitter: stream_continue(agent, "s", "old", emitter=emitter),
             "continue start failed"),
        ):
            with self.subTest(message=message):
                emitter = Emitter()
                result = invoke(FailingAgent([]), emitter)
                self.assertTrue(result["error"])
                self.assertEqual(result["content"], "")
                self.assertIn(("error", {"message": message}), emitter.events)
                self.assertTrue(emitter.closed)

    def test_initial_run_failure_produces_json_result_and_failure_exit(self):
        from aaa.cli.app import run_once

        class FailingAgent(Agent):
            def run(self, text, **kwargs):
                raise RuntimeError("start failed")

        state = SimpleNamespace(
            runnable=FailingAgent([]), session_id="session", approved=set(),
            settings={"memory.user_id": "user", "boundary.command_approval": "always"},
            provider="provider", review_root=lambda: None, memory=None,
        )
        out = io.StringIO()
        with patch("aaa.cli.app.remember_session"), contextlib.redirect_stdout(out):
            code = run_once(state, "hi", "json")
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(code, 1)
        self.assertEqual(records[0], {"type": "error", "message": "start failed"})
        self.assertEqual(records[-1], {
            "type": "result", "session_id": "session", "content": "", "tokens": 0,
            "duration_s": 0.0, "error": True, "cancelled": False,
        })

    def test_tool_projection_distinguishes_calls_and_preserves_json_protocol(self):
        first = ToolExecution(tool_call_id="one", tool_name="read", tool_args={"path": "a"}, result="A")
        second = ToolExecution(tool_call_id="two", tool_name="read", tool_args={"path": "b"})
        events = [ToolCallStartedEvent(run_id="r", tool=first),
                  ToolCallStartedEvent(run_id="r", tool=second),
                  ToolCallCompletedEvent(run_id="r", tool=first),
                  ToolCallErrorEvent(run_id="r", tool=second, error="denied"),
                  RunCompletedEvent(run_id="r")]
        emitter = Emitter()
        stream_run(Agent(events), "hello", "s", emitter=emitter)
        projected = [(kind, data) for kind, data in emitter.events if kind.startswith("tool_")]
        self.assertEqual([data["call_id"] for _, data in projected], ["one", "two", "one", "two"])
        self.assertEqual(projected[2][1]["result"], "A")
        self.assertEqual(projected[3][1]["error"], "denied")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stream_run(Agent(events), "hello", "s", emitter=JsonEmitter())
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([set(record) for record in records], [
            {"type", "tool", "args"}, {"type", "tool", "args"},
            {"type", "tool", "duration_s"}, {"type", "tool", "duration_s", "error"},
        ])


class MemoryToolApprovalTest(unittest.TestCase):
    """引擎显式请求的工具确认不由命令审批策略自动放行。"""

    def _memory_host(self, *specs):
        from aaa.config.settings import Settings
        from aaa.memory import MemoryHost

        engine = SimpleNamespace(tools=lambda: list(specs), call_tool=lambda *a: "ok")
        return MemoryHost(engine, "fake", Settings())

    def test_declared_confirm_tool_asks_even_under_dangerous_strategy(self):
        from aaa.memory import MemoryTool

        requirement = SimpleNamespace(
            tool_execution=ToolExecution(tool_name="forget", tool_args={"what": "blue"}),
            confirm=mock.Mock(), reject=mock.Mock(),
        )
        approved = set()
        host = self._memory_host(MemoryTool(name="forget", description="删除", requires_confirmation=True))
        self.addCleanup(host.close)
        emitter = SimpleNamespace(ask_approval=mock.Mock(return_value=True))
        approve_requirements([requirement], approved, emitter, strategy="dangerous", memory=host)
        emitter.ask_approval.assert_called_once()
        requirement.confirm.assert_called_once()
        self.assertIn("forget:{\"what\": \"blue\"}", approved)

    def test_registry_covers_memory_tools_without_threaded_host(self):
        """漏传宿主时，注册表兜底：声明需要确认的工具仍会被询问。"""
        from aaa.memory import MemoryTool

        host = self._memory_host(MemoryTool(name="forget", description="删除", requires_confirmation=True))
        self.addCleanup(host.close)
        requirement = SimpleNamespace(
            tool_execution=ToolExecution(tool_name="forget", tool_args={"what": "blue"}),
            confirm=mock.Mock(), reject=mock.Mock(),
        )
        emitter = SimpleNamespace(ask_approval=mock.Mock(return_value=True))
        approve_requirements([requirement], set(), emitter, strategy="dangerous")
        emitter.ask_approval.assert_called_once()
        requirement.confirm.assert_called_once()

    def test_continue_path_gates_declared_memory_tools(self):
        """/edit、/redo 走 stream_continue：暂停要求必须先过确认门。"""
        from aaa.memory import MemoryTool

        requirement = SimpleNamespace(
            tool_execution=ToolExecution(tool_name="forget", tool_args={"what": "blue"}),
            confirm=mock.Mock(), reject=mock.Mock(),
        )
        host = self._memory_host(MemoryTool(name="forget", description="删除", requires_confirmation=True))
        self.addCleanup(host.close)
        def events():
            yield RunStartedEvent(run_id="r")
            yield RunPausedEvent(run_id="r", requirements=[requirement])
            yield RunCompletedEvent(run_id="r")

        agent = Agent([])
        agent.events = events()

        class ApprovingEmitter(Emitter):
            def ask_approval(self, name, args, diff=None):
                return True

        result = stream_continue(
            agent, "s", "r", fork=True, emitter=ApprovingEmitter(), strategy="dangerous", memory=host,
        )
        # 一次开启分叉（暂停于确认门），一次批准后续跑。
        emitter_calls = [c for c in agent.calls if c[0] == "continue"]
        self.assertEqual(len(emitter_calls), 2)
        self.assertFalse(result.get("error"))

        requirement_rejecting = SimpleNamespace(
            tool_execution=ToolExecution(tool_name="forget", tool_args={"what": "red"}),
            confirm=mock.Mock(), reject=mock.Mock(),
        )
        def rejecting_events():
            yield RunStartedEvent(run_id="r2")
            yield RunPausedEvent(run_id="r2", requirements=[requirement_rejecting])
            yield RunCompletedEvent(run_id="r2")

        agent = Agent([])
        agent.events = rejecting_events()
        result = stream_continue(
            agent, "s", "r2", fork=True, emitter=Emitter(), strategy="dangerous", memory=host,
        )
        requirement_rejecting.reject.assert_called_once()
        self.assertEqual(result["content"], "")

    def test_replayed_resolved_requirement_does_not_crash(self):
        """/redo 重放已决议要求：批准时的重复决议不再抛错，跳过并继续。"""
        requirement = SimpleNamespace(
            tool_execution=ToolExecution(tool_name="write_file", tool_args={"path": "a"}),
            confirm=mock.Mock(side_effect=ValueError("This requirement does not require confirmation")),
            reject=mock.Mock(side_effect=ValueError("This requirement does not require confirmation")),
        )
        emitter = SimpleNamespace(ask_approval=mock.Mock(return_value=True))
        approve_requirements([requirement], set(), emitter, strategy="always")
        emitter.ask_approval.assert_called_once()
        requirement.confirm.assert_called_once()
        requirement.reject.assert_not_called()

    def test_startup_exits_cleanly_on_memory_engine_error(self):
        """重名等记忆装配失败：干净报错退出（2），不吐 traceback。"""
        from aaa.cli import state as state_mod
        from aaa.cli.app import EXIT_USAGE, main
        from aaa.config.settings import Settings

        settings = Settings({"memory.source": "off"})
        config = {"default": {"provider": "probe", "model": "probe-model"}}
        catalog = SimpleNamespace(
            require=lambda p, m: (None, SimpleNamespace(context_window=100000)),
            scope=lambda *_: [],
            build=lambda p, m: FakeModel([text("ok")]),
            fallbacks=lambda p, m: [],
        )

        def build_with_conflict(*args, **kwargs):
            from aaa.memory import MemoryHost, MemoryTool
            from aaa.runtime.agent import build_agent

            host = MemoryHost(
                SimpleNamespace(
                    tools=lambda: [MemoryTool(name="read_file", description="冲突", read_only=True)],
                    call_tool=lambda *a: "ok",
                    recall=lambda query: [],
                ),
                "conflict", settings,
            )
            kwargs["memory"] = host
            return build_agent(*args, **kwargs)

        # 直接走 main 的失败路径：AppState.start 抛 MemoryEngineError → exit 2
        err = io.StringIO()
        with contextlib.redirect_stderr(err), \
                mock.patch("aaa.cli.app.load_config", return_value=config), \
                mock.patch.object(state_mod, "from_config", return_value=catalog), \
                mock.patch.object(state_mod, "build_agent", side_effect=build_with_conflict):
            with self.assertRaises(SystemExit) as ctx:
                main(["-p", "hi"])
        self.assertEqual(ctx.exception.code, EXIT_USAGE)
        self.assertIn("记忆工具与现有工具重名", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
