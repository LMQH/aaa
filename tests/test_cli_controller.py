import asyncio
import contextlib
import io
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agno.run.base import RunStatus
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import has_focus
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput

from aaa.cli.app import EXIT_CANCELLED, approval_pane, last_completed_run_id, run_once
from aaa.cli.controller import Controller


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agent = SimpleNamespace(cancel_run=lambda **kwargs: None)
        self.state = SimpleNamespace(runnable=self.agent, session_id="s1")
        self.dispatched = []
        self.changed = asyncio.Event()
        self.controller = Controller(
            self.state, asyncio.get_running_loop(), lambda summary: None,
            lambda text, queued: self.dispatched.append((text, queued)), self.changed.set,
        )

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                self.changed.clear()
                if predicate():
                    return
                await self.changed.wait()
        await asyncio.wait_for(wait(), 2)

    async def test_success_dispatches_fifo_without_losing_remaining_items(self):
        release = threading.Event()
        self.controller.start(lambda emitter, handle: (release.wait(), {})[1])
        self.controller.enqueue("first")
        self.controller.enqueue("/custom second")
        release.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertEqual(self.dispatched, [("first", True)])
        self.assertEqual(list(self.controller.queue), [("/custom second", "s1")])

    async def test_failure_and_unconfirmed_cancellation_pause_queue(self):
        for summary in ({"error": True}, {"interrupted": True}, {}):
            release = threading.Event()
            self.controller.start(lambda emitter, handle: (release.wait(), summary)[1])
            self.controller.enqueue("keep")
            if not summary:
                self.controller.cancel()
            release.set()
            await asyncio.wait_for(self.controller.done.wait(), 2)
            self.assertEqual(self.dispatched, [])
            self.assertEqual(self.controller.queue.pop()[0], "keep")

    async def test_cancel_before_run_id_retains_intent_and_exit_waits(self):
        started = threading.Event()
        release = threading.Event()
        seen = []

        def worker(emitter, handle):
            started.set()
            release.wait()
            seen.append(handle.copy())
            return {"error": True}

        self.controller.start(worker)
        await asyncio.to_thread(started.wait)
        self.controller.request_exit()
        self.assertFalse(self.controller.done.is_set())
        release.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertEqual(seen, [{"run_id": None, "cancel_requested": True}])

    async def test_approval_is_answered_on_ui_loop_and_cancel_rejects(self):
        answers = []

        def worker(emitter, handle):
            answers.append(emitter.ask_approval("write_file", {"path": "a"}))
            return {}

        self.controller.start(worker)
        await self.wait_until(lambda: self.controller.approval is not None)
        self.controller.cancel()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertIsNone(self.controller.approval)
        self.assertEqual(self.controller.phase, "idle")
        self.assertEqual(answers, [False])

    async def test_native_cancel_failure_is_visible_and_not_reported_as_cancelled(self):
        release = threading.Event()
        def fail(**kwargs):
            raise RuntimeError("cannot cancel")
        self.agent.cancel_run = fail
        self.controller.start(lambda emitter, handle: (release.wait(), {"error": True})[1])
        self.controller.handle["run_id"] = "r1"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.controller.cancel()
            release.set()
            await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertIn("取消请求失败", output.getvalue())
        self.assertNotIn("已取消", output.getvalue())

    async def test_worker_error_is_visible(self):
        def worker(emitter, handle):
            raise RuntimeError("broken model")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.controller.start(worker)
            await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertIn("broken model", output.getvalue())

    async def test_old_approval_is_rejected_not_left_waiting(self):
        from aaa.cli.controller import Approval
        request = Approval("write_file", {}, None, threading.Event())
        self.controller.deliver(-1, "approval", request)
        self.assertTrue(request.ready.is_set())
        self.assertFalse(request.answer)

    async def test_paused_queue_stays_paused_during_new_run_and_can_be_recovered_by_owner(self):
        gate = threading.Event()
        self.controller.start(lambda emitter, handle: (gate.wait(), {"error": True})[1])
        self.controller.enqueue("old")
        gate.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertTrue(self.controller.queue_paused)
        self.state.session_id = "s2"
        executed = []
        self.controller.start(lambda emitter, handle: executed.append("new") or {})
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertEqual(executed, ["new"])
        self.assertEqual(self.dispatched, [])
        self.assertIsNone(self.controller.take_queued("s2"))
        self.assertEqual(list(self.controller.queue), [("old", "s1")])
        self.assertEqual(self.controller.take_queued("s1"), "old")
        self.assertFalse(self.controller.queue)

    async def test_shutdown_never_dispatches_queued_input(self):
        gate = threading.Event()
        self.controller.start(lambda emitter, handle: (gate.wait(), {})[1])
        self.controller.enqueue("queued")
        self.controller.request_exit()
        gate.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(list(self.controller.queue), [("queued", "s1")])

    async def test_old_token_and_wrong_session_events_cannot_override_current_run(self):
        from aaa.cli.controller import Approval
        gate = threading.Event()
        self.controller.start(lambda emitter, handle: (gate.wait(), {})[1])
        token = self.controller.token
        self.controller.token += 1
        request = Approval("danger", {}, None, threading.Event())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.controller.deliver(token, "content", {"text": "stale"})
            self.controller.deliver(token, "approval", request)
            self.controller.deliver(self.controller.token, "content", {"text": "wrong-session"}, "s2")
        self.assertEqual(output.getvalue(), "")
        self.assertTrue(request.ready.is_set())
        self.assertIsNone(self.controller.approval)
        self.controller.token = token
        gate.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)

    async def test_approval_presentation_failure_rejects_and_releases_worker(self):
        replies = []
        self.controller.invalidate = lambda: (_ for _ in ()).throw(RuntimeError("no display"))
        self.controller.start(lambda emitter, handle: replies.append(emitter.ask_approval("shell", {"cmd": "x"})) or {})
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertEqual(replies, [False])
        self.assertFalse(self.controller.busy)

    async def test_approval_displays_complete_dangerous_arguments(self):
        tail = "dangerous-tail"
        replies = []
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.controller.start(lambda emitter, handle: replies.append(
                emitter.ask_approval("shell", {"command": "x" * 200 + tail})
            ) or {})
            await self.wait_until(lambda: self.controller.approval is not None)
            self.controller.answer(False)
            await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertIn(tail, output.getvalue())
        self.assertEqual(replies, [False])

    async def test_changed_session_rejects_pending_approval_event(self):
        from aaa.cli.controller import Approval
        gate = threading.Event()
        self.controller.start(lambda emitter, handle: (gate.wait(), {})[1])
        request = Approval("shell", {}, None, threading.Event())
        self.state.session_id = "s2"
        self.controller.deliver(self.controller.token, "approval", request, "s1")
        self.assertTrue(request.ready.is_set())
        self.assertFalse(request.answer)
        self.assertIsNone(self.controller.approval)
        gate.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)

    async def test_refresh_and_redraw_errors_do_not_prevent_completion(self):
        def fail(*args):
            raise RuntimeError("ui failure")
        self.controller.refresh = fail
        self.controller.invalidate = fail
        self.controller.start(lambda emitter, handle: {})
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertFalse(self.controller.busy)


class ApprovalFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_draft_keys_do_not_approve_and_focused_key_does(self):
        ready, submitted, answered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        replies, submissions = [], []
        state = SimpleNamespace(approval=SimpleNamespace(name="shell"))

        def answer(value):
            replies.append(value)
            answered.set()
        state.answer = answer
        kb = KeyBindings()
        pane, approval = approval_pane(kb, lambda: state)
        draft = Buffer()
        draft_control = BufferControl(buffer=draft)

        @kb.add("enter", filter=has_focus(draft))
        def submit(event):
            submissions.append(draft.text)
            submitted.set()

        with create_pipe_input() as pipe:
            app = Application(
                layout=Layout(HSplit([pane, Window(draft_control)]), focused_element=draft_control),
                key_bindings=kb, input=pipe, output=DummyOutput(), full_screen=False,
            )
            app.pre_run_callables.append(ready.set)
            task = asyncio.create_task(app.run_async())
            try:
                await asyncio.wait_for(ready.wait(), 2)
                pipe.send_text("y\r")
                await asyncio.wait_for(submitted.wait(), 2)
                self.assertEqual(submissions, ["y"])
                self.assertEqual(replies, [])
                self.assertEqual(draft.text, "y")
                pipe.send_text("\x1bOQy")
                await asyncio.wait_for(answered.wait(), 2)
                self.assertEqual(replies, [True])
                self.assertEqual(draft.text, "y")
            finally:
                app.exit()
                await task


class CompletedRunTests(unittest.TestCase):
    def test_paused_run_is_not_selected_for_redo_or_edit(self):
        runs = [
            SimpleNamespace(run_id="paused", status=RunStatus.paused),
            SimpleNamespace(run_id="complete", status=RunStatus.completed),
        ]

        def get_runs(**kwargs):
            return [run for run in runs if run.status == kwargs["status"]][:kwargs["limit"]]

        agent = SimpleNamespace(db=SimpleNamespace(get_runs=get_runs))
        self.assertEqual(last_completed_run_id(agent, "s1"), "complete")
        runs.pop()
        self.assertIsNone(last_completed_run_id(agent, "s1"))


class NoninteractiveTests(unittest.TestCase):
    def test_interrupted_run_exits_130_without_new_json_field(self):
        state = SimpleNamespace(
            runnable=object(), session_id="s1", approved=set(), provider="p",
            settings=SimpleNamespace(get=lambda key: "value"), review_root=lambda: None,
            memory=None,
        )
        output = io.StringIO()
        with patch("aaa.cli.app.remember_session"), patch("aaa.cli.app.stream_run", return_value={"interrupted": True}), contextlib.redirect_stdout(output):
            code = run_once(state, "hello", "json")
        self.assertEqual(code, EXIT_CANCELLED)
        self.assertNotIn('"interrupted"', output.getvalue())
        self.assertIn('"cancelled": false', output.getvalue())
