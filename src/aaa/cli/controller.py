"""UI-thread run coordination and the worker-to-UI emitter bridge."""

import asyncio
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from .run import format_tool
from .ui import GLYPH

logger = logging.getLogger(__name__)


@dataclass
class Approval:
    name: str
    args: object
    diff: Optional[str]
    ready: threading.Event
    answer: bool = False


def interrupt_message(original: Optional[str], calls: list, text: str) -> str:
    """停止后发送的消息：把打断前的请求与已发生的工具调用拼在新指示前面。

    被取消的轮次不进入模型上下文，模型只能从这条消息知道之前要做什么、已经做了什么。
    """
    parts = [
        "<interrupted_run>上一轮运行已被用户取消，它的内容不在你的上下文里。"
        "下面是打断前的情况，请依据最后的新指示决定如何继续。</interrupted_run>"
    ]
    if original:
        parts.append(f"打断前的请求：\n{original}")
    if calls:
        lines = [f"- {format_tool(call['name'], call['args'])}（{call['status']}）" for call in calls]
        parts.append("打断前已发生的工具调用（可能已经产生实际效果，未完成的调用结果未知）：\n" + "\n".join(lines))
    parts.append(f"新指示：\n{text}")
    return "\n\n".join(parts)


class InteractiveEmitter:
    """Workers publish events; only the UI loop renders and answers approvals."""

    def __init__(self, loop, deliver, token, session_id):
        self.loop, self.deliver, self.token, self.session_id = loop, deliver, token, session_id

    def event(self, kind, **data):
        self.loop.call_soon_threadsafe(self.deliver, self.token, kind, data, self.session_id)

    def ask_approval(self, name, args, diff=None):
        request = Approval(name, args, diff, threading.Event())
        try:
            self.loop.call_soon_threadsafe(self.deliver, self.token, "approval", request, self.session_id)
        except RuntimeError:
            logger.exception("approval delivery failed")
            return False
        request.ready.wait()
        return request.answer

    def close(self):
        self.loop.call_soon_threadsafe(self.deliver, self.token, "close", {}, self.session_id)


class Controller:
    """All state except the documented run handle is owned by the UI loop."""

    def __init__(self, state, loop, refresh: Callable, dispatch: Callable, invalidate: Callable, render=None):
        self.state, self.loop = state, loop
        self.render = render
        self.refresh, self.dispatch, self.invalidate = refresh, dispatch, invalidate
        self.queue = deque()
        self.queue_paused = False
        self.handle = None
        self.token = 0
        self.session_id = None
        self.phase = "idle"
        self.approval = None
        self.shutdown = False
        self.done = asyncio.Event()
        self.done.set()
        self.after_stop: Optional[tuple] = None
        self.current_text: Optional[str] = None
        self.tool_calls: list = []

    @property
    def busy(self):
        return self.handle is not None

    def redraw(self):
        try:
            self.invalidate()
        except Exception:
            logger.exception("interactive redraw failed")

    def start(self, worker, text=None):
        self.token += 1
        token = self.token
        handle = {"run_id": None, "cancel_requested": False}
        self.after_stop = None
        self.current_text = text
        self.tool_calls = []
        self.handle = handle
        self.session_id = self.state.session_id
        self.phase = "starting"
        self.done.clear()
        emitter = InteractiveEmitter(self.loop, self.deliver, token, self.session_id)
        self.loop.create_task(self._run(worker, emitter, handle, token))
        self.redraw()

    async def _run(self, worker, emitter, handle, token):
        summary = {"error": True}
        try:
            try:
                summary = await asyncio.to_thread(worker, emitter, handle)
            except Exception as exc:
                logger.exception("interactive run failed")
                print(f"{GLYPH['fail']} 运行失败：{exc}")
            if token != self.token:
                return
            self.answer(False)
            if self.state.session_id == self.session_id:
                try:
                    self.refresh(summary)
                except Exception:
                    logger.exception("refreshing run summary failed")
                    print(f"{GLYPH['fail']} 更新运行状态失败，请查看日志。")
        finally:
            if token == self.token:
                if summary.get("error") or summary.get("cancelled") or summary.get("interrupted") or handle["cancel_requested"]:
                    self.queue_paused = True
                self.answer(False)
                self.handle = None
                self.phase = "idle"
                self.done.set()
        held, self.after_stop = self.after_stop, None
        if held is not None:
            if self.shutdown or self.state.session_id != held[1]:
                self.queue.appendleft(held)
            elif summary.get("cancelled"):
                self._send_after_stop(interrupt_message(self.current_text, self.tool_calls, held[0]), held)
                return
            elif not (summary.get("error") or summary.get("interrupted")):
                # 取消来得太晚，运行已正常完成：按普通消息发出。
                self._send_after_stop(held[0], held)
                return
            else:
                # 取消未确认或运行出错：退回队首，沿用队列暂停规则。
                self.queue.appendleft(held)
        if summary.get("error") or summary.get("cancelled") or summary.get("interrupted") or handle["cancel_requested"]:
            self.queue_paused = True
            if self.queue:
                print(f"{GLYPH['dot']} 队列已暂停；切回所属会话后用 Alt+Up 取回。")
            return
        if not self.shutdown and not self.queue_paused and self.queue:
            text, session_id = self.queue.popleft()
            if session_id != self.state.session_id:
                self.queue.appendleft((text, session_id))
                self.queue_paused = True
                print(f"{GLYPH['fail']} 队列属于其他会话，已暂停。")
            else:
                try:
                    self.dispatch(text, True)
                except Exception as exc:
                    self.queue.appendleft((text, session_id))
                    self.queue_paused = True
                    logger.exception("queued command dispatch failed")
                    print(f"{GLYPH['fail']} 队列已暂停：{exc}")
            self.redraw()

    def deliver(self, token, kind, data, session_id=None):
        if (token != self.token or not self.busy or
                (session_id is not None and session_id != self.session_id) or
                self.state.session_id != self.session_id):
            if kind == "approval":
                data.ready.set()
            return
        if kind == "approval":
            if self.handle["cancel_requested"] or self.shutdown:
                data.ready.set()
                return
            try:
                if self.render is not None:
                    self.render.preview_approval(data.name, data.args, data.diff)
                    print("审批待处理：↑↓ 选择，Enter 确认，Esc 拒绝。")
                else:
                    if data.diff is not None:
                        print(f"{GLYPH['dot']} 改动预览\n{data.diff}")
                    detail = json.dumps(data.args, ensure_ascii=False, default=str) if data.args is not None else ""
                    print(f"? {data.name} {detail}\n审批待处理：↑↓ 选择，Enter 确认，Esc 拒绝。")
                self.approval = data
                self.phase = "approval"
                self.invalidate()
            except Exception:
                logger.exception("approval presentation failed; rejecting")
                self.approval = None
                self.phase = "running"
                data.ready.set()
            return
        elif kind == "close":
            if self.render is not None:
                self.render.close()
            else:
                print()
        else:
            self._track_tools(kind, data)
            if self.render is not None:
                self.render.event(kind, **data)
            elif kind == "content":
                print(str(data.get("text", "")), end="", flush=True)
            elif kind == "tool_start":
                print(f"\n  {GLYPH['dot']} {format_tool(data['tool'], data.get('args'))}")
            elif kind in ("tool_done", "tool_error"):
                mark = GLYPH['ok'] if kind == "tool_done" else GLYPH['fail']
                print(f"\n  {mark} {data['tool']} {data['duration']:.1f}s {data.get('error') or ''}")
            elif kind == "error":
                print(f"\n  {GLYPH['fail']} {data.get('message') or 'run error'}")
            elif kind == "cancelled":
                print(f"\n  {GLYPH['fail']} 已取消")
        if self.phase == "starting" and self.handle["run_id"]:
            self.phase = "running"
        self.redraw()

    def answer(self, answer):
        if self.approval is not None:
            request, self.approval = self.approval, None
            request.answer = answer
            request.ready.set()
            if self.phase == "approval":
                self.phase = "running"
            self.redraw()

    def cancel(self):
        if not self.busy or self.handle["cancel_requested"]:
            return
        self.handle["cancel_requested"] = True
        self.phase = "stopping"
        self.answer(False)
        run_id = self.handle["run_id"]
        if run_id:
            try:
                self.state.runnable.cancel_run(run_id=run_id)
            except Exception as exc:
                logger.exception("native cancellation request failed")
                print(f"{GLYPH['fail']} 取消请求失败：{exc}；仍在等待运行结束。")
        print(f"{GLYPH['dot']} 已请求停止，等待运行结束…")
        self.redraw()

    def request_exit(self):
        self.shutdown = True
        self.cancel()

    def _track_tools(self, kind, data):
        """记录本轮已发生的工具调用及状态，供停止后发送的消息说明打断前的进度。"""
        if kind == "tool_start":
            self.tool_calls.append({
                "name": str(data.get("tool", "")), "args": data.get("args"),
                "call_id": data.get("call_id"), "status": "未完成",
            })
        elif kind in ("tool_done", "tool_error"):
            call_id = data.get("call_id")
            for call in reversed(self.tool_calls):
                if call["status"] == "未完成" and call["name"] == str(data.get("tool", "")):
                    if call_id is not None and call["call_id"] not in (None, call_id):
                        continue
                    call["status"] = "出错" if kind == "tool_error" or data.get("error") else "完成"
                    break

    def _send_after_stop(self, text, held):
        """发送停止后发送的消息；派发失败则退回队首并暂停队列。"""
        try:
            self.dispatch(text, True)
        except Exception as exc:
            self.queue.appendleft(held)
            self.queue_paused = True
            logger.exception("stop-then-send dispatch failed")
            print(f"{GLYPH['fail']} 打断后的消息未能发送，已放回队列：{exc}")
        self.redraw()

    def stop_and_send(self, text):
        """登记「停止后发送」并请求取消；退出流程或空闲时返回 False。"""
        if not self.busy or self.shutdown:
            return False
        self.after_stop = (text, self.state.session_id)
        if self.handle["cancel_requested"]:
            print(f"{GLYPH['dot']} 运行确认取消后立即发送。")
        else:
            self.cancel()
            print(f"{GLYPH['dot']} 已请求打断，运行确认取消后立即发送。")
        self.redraw()
        return True

    def enqueue(self, text):
        if self.busy and self.phase == "stopping" and not self.shutdown:
            self.stop_and_send(text)
            return
        if not self.queue:
            self.queue_paused = False
        self.queue.append((text, self.state.session_id))
        print(f"{GLYPH['dot']} 已排队（当前 {len(self.queue)} 条）")
        self.redraw()

    def take_queued(self, session_id):
        for index in range(len(self.queue) - 1, -1, -1):
            text, owner = self.queue[index]
            if owner == session_id:
                del self.queue[index]
                if not self.queue:
                    self.queue_paused = False
                self.redraw()
                return text
        return None
