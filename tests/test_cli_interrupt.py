"""打断并发出：取消确认后才发送，消息带上打断前的请求与已发生的工具调用。"""

import asyncio
import contextlib
import io
import threading
import unittest
from types import SimpleNamespace

from aaa.cli.controller import Controller, interrupt_message


class InterruptMessageTests(unittest.TestCase):
    def test_carries_request_tool_calls_and_instruction(self):
        calls = [
            {"name": "read_file", "args": {"file_path": "README.md"}, "call_id": "c1", "status": "完成"},
            {"name": "write_file", "args": None, "call_id": "c2", "status": "未完成"},
        ]
        message = interrupt_message("重构 parser", calls, "先只写方案")
        self.assertLess(message.index("打断前的请求：\n重构 parser"), message.index("新指示：\n先只写方案"))
        self.assertIn('- read_file {"file_path": "README.md"}（完成）', message)
        self.assertIn("- write_file（未完成）", message)
        self.assertIn("不在你的上下文里", message)

    def test_omits_sections_that_did_not_happen(self):
        message = interrupt_message(None, [], "换个方向")
        self.assertNotIn("打断前的请求", message)
        self.assertNotIn("工具调用", message)
        self.assertTrue(message.endswith("新指示：\n换个方向"))


class StopThenSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.agent = SimpleNamespace(cancel_run=lambda **kwargs: None)
        self.state = SimpleNamespace(runnable=self.agent, session_id="s1")
        self.dispatched = []
        self.changed = asyncio.Event()
        self.controller = Controller(
            self.state, asyncio.get_running_loop(), lambda summary: None,
            lambda text, queued: self.dispatched.append((text, queued)), self.changed.set,
        )
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    async def wait_until(self, predicate):
        async def wait():
            while not predicate():
                self.changed.clear()
                if predicate():
                    return
                await self.changed.wait()
        await asyncio.wait_for(wait(), 2)

    def blocking_worker(self, release, summary):
        def worker(emitter, handle):
            emitter.event("tool_start", tool="read_file", args={"file_path": "README.md"}, call_id="c1")
            emitter.event("tool_done", tool="read_file", duration=0.1, call_id="c1")
            emitter.event("tool_start", tool="write_file", args={"file_path": "a.py"}, call_id="c2")
            release.wait()
            return summary
        return worker

    async def test_confirmed_cancel_sends_composed_message(self):
        release = threading.Event()
        self.controller.start(self.blocking_worker(release, {"cancelled": True}), "重构 parser")
        await self.wait_until(lambda: len(self.controller.tool_calls) == 2)
        self.assertTrue(self.controller.stop_and_send("先只写方案"))
        self.assertEqual(self.dispatched, [])
        release.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        [(text, queued)] = self.dispatched
        self.assertTrue(queued)
        self.assertIn("打断前的请求：\n重构 parser", text)
        self.assertIn('read_file {"file_path": "README.md"}（完成）', text)
        self.assertIn('write_file {"file_path": "a.py"}（未完成）', text)
        self.assertTrue(text.endswith("新指示：\n先只写方案"))

    async def test_cancel_too_late_sends_plain_message(self):
        release = threading.Event()
        self.controller.start(self.blocking_worker(release, {}), "旧请求")
        await self.wait_until(lambda: len(self.controller.tool_calls) == 2)
        self.controller.stop_and_send("接着做")
        release.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertEqual(self.dispatched, [("接着做", True)])

    async def test_unconfirmed_cancel_keeps_message_paused_at_queue_front(self):
        release = threading.Event()
        self.controller.start(self.blocking_worker(release, {"error": True}), "旧请求")
        self.controller.enqueue("已排队的")
        self.controller.stop_and_send("打断后的话")
        release.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(list(self.controller.queue), [("打断后的话", "s1"), ("已排队的", "s1")])
        self.assertTrue(self.controller.queue_paused)

    async def test_exit_does_not_send_held_message(self):
        release = threading.Event()
        self.controller.start(self.blocking_worker(release, {"cancelled": True}), "旧请求")
        self.controller.stop_and_send("别发了")
        self.controller.request_exit()
        release.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertEqual(self.dispatched, [])
        self.assertEqual(list(self.controller.queue), [("别发了", "s1")])

    async def test_submitting_while_stopping_becomes_stop_then_send(self):
        release = threading.Event()
        self.controller.start(self.blocking_worker(release, {"cancelled": True}), "旧请求")
        self.controller.cancel()
        self.controller.enqueue("取消后提交的")
        self.assertEqual(list(self.controller.queue), [])
        release.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        [(text, queued)] = self.dispatched
        self.assertTrue(text.endswith("新指示：\n取消后提交的"))

    async def test_plain_cancel_and_idle_do_not_send(self):
        self.assertFalse(self.controller.stop_and_send("空闲时"))
        release = threading.Event()
        self.controller.start(self.blocking_worker(release, {"cancelled": True}), "旧请求")
        self.controller.cancel()
        release.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        self.assertEqual(self.dispatched, [])

    async def test_failed_tool_call_is_reported_as_failed(self):
        release = threading.Event()

        def worker(emitter, handle):
            emitter.event("tool_start", tool="run_shell_command", args={"args": ["ls"]}, call_id="c1")
            emitter.event("tool_error", tool="run_shell_command", duration=0.1, error="boom", call_id="c1")
            release.wait()
            return {"cancelled": True}

        self.controller.start(worker, "旧请求")
        await self.wait_until(
            lambda: self.controller.tool_calls and self.controller.tool_calls[0]["status"] != "未完成"
        )
        self.controller.stop_and_send("停")
        release.set()
        await asyncio.wait_for(self.controller.done.wait(), 2)
        [(text, _)] = self.dispatched
        self.assertIn('- run_shell_command {"args": ["ls"]}（出错）', text)


if __name__ == "__main__":
    unittest.main()
