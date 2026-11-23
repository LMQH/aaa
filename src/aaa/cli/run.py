"""一轮运行的流式输出、HITL 审批与事件格式化。

事件循环与渲染分开：`HumanEmitter` 是交互 REPL，`PlainEmitter` / `JsonEmitter`
给非交互模式用，三者共用同一套事件与审批逻辑。
"""

import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Set
import questionary
from agno.agent import Agent
from agno.run import agent as agent_events

from ..boundary.policy import (
    APPROVAL_ALWAYS, REVIEW_FILE_TOOLS, is_external_tool, needs_approval, signature,
)
from ..boundary.danger import danger_reason
from ..memory import MemoryHost
from ..runtime.delegation import SAVE_TOOL, parent_run_id, save_approvals
from .diff import change_diff
from .render import MarkdownStream, render_diff, sanitize
from .ui import GLYPH, UI_STYLE, StatusLine

# 运行期异常落 ~/.aaa/logs/aaa.log（logging_setup 给 "aaa" 挂了文件 handler）。
logger = logging.getLogger("aaa")


def format_tool(name: str, args) -> str:
    """把工具调用压成一行，参数过长时截断。"""
    if not args:
        return str(name)
    text = json.dumps(args, ensure_ascii=False)
    if len(text) > 80:
        text = text[:77] + "..."
    return f"{name} {text}"


def _usage_of(metrics) -> Optional[tuple]:
    """从 RunMetrics.details 汇总四类 token；没有明细时返回 None。"""
    details = getattr(metrics, "details", None)
    if not details:
        return None
    totals = [0, 0, 0, 0]
    for entries in details.values():
        for m in entries or []:
            totals[0] += getattr(m, "input_tokens", 0) or 0
            totals[1] += getattr(m, "output_tokens", 0) or 0
            totals[2] += getattr(m, "cache_read_tokens", 0) or 0
            totals[3] += getattr(m, "cache_write_tokens", 0) or 0
    return tuple(totals)


def _result_summary(result) -> str:
    """工具结果的第一行摘要；空结果返回空串。"""
    text = str(result or "").strip()
    if not text:
        return ""
    first = text.splitlines()[0]
    return first[:120] + ("…" if len(first) > 120 else "")


def _tool_duration(tool, started: Optional[float]) -> float:
    """优先用 Agno 记录的耗时，没有则用本地计时。"""
    metrics = getattr(tool, "metrics", None)
    duration = getattr(metrics, "duration", None) if metrics is not None else None
    if duration:
        return float(duration)
    return time.monotonic() - started if started else 0.0


class HumanEmitter:
    """交互渲染：状态行、流式正文、工具行。"""

    def __init__(self) -> None:
        self.status = StatusLine()
        self.line_open = False
        self.colors = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.markdown = MarkdownStream(color=self.colors)

    def _write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def _flush_markdown(self) -> None:
        tail = self.markdown.close()
        if tail:
            self._write(("" if self.line_open else "\n") + tail)
            self.line_open = True

    def _line(self, text: str) -> None:
        self.status.clear()
        self._flush_markdown()
        self._write(("\n" if self.line_open else "") + sanitize(text) + "\n")
        self.line_open = False

    def event(self, kind: str, **data) -> None:
        if kind == "model_start":
            self.status.update(f"  \x1b[2m{GLYPH['working']} Working…\x1b[0m")
        elif kind == "content":
            self.status.clear()
            rendered = self.markdown.feed(sanitize(str(data["text"])))
            if rendered:
                self._write(("" if self.line_open else "\n") + rendered)
                self.line_open = True
        elif kind == "tool_start":
            line = f"  {GLYPH['dot']} {format_tool(data['tool'], data.get('args'))}"
            self._line(f"\x1b[2m{line}\x1b[0m" if self.colors else line)
        elif kind == "tool_done":
            summary = _result_summary(data.get("result"))
            suffix = f" · {summary}" if summary else ""
            mark = GLYPH["fail"] if data.get("error") else GLYPH["ok"]
            self._line(f"  {mark} {data['tool']} {data['duration']:.1f}s{suffix}")
        elif kind == "tool_error":
            self._line(f"  {GLYPH['fail']} {data['tool']} {data['duration']:.1f}s · {data.get('error') or 'error'}")
        elif kind == "error":
            self._line(f"  {GLYPH['fail']} {data.get('message') or 'run error'}")
        elif kind == "warning":
            self._line(f"  {GLYPH['dot']} {data['message']}")
        elif kind == "cancelled":
            self._line(f"  {GLYPH['fail']} 已取消")

    def preview_approval(self, name: str, args, diff: Optional[str] = None) -> None:
        """审批预览：diff 着色后原样展示；真正的回答由 Controller 的独立焦点处理。"""
        if diff is not None:
            self._line("  " + GLYPH["dot"] + " 改动预览")
            body = render_diff(sanitize(diff), color=self.colors)
            self._write("".join(f"  {line}\n" for line in body.splitlines()) + "\n")
        detail = sanitize(json.dumps(args, ensure_ascii=False, default=str)) if args is not None else ""
        self._line(f"  ? {name} {detail}".rstrip())

    def ask_approval(self, name: str, args, diff: Optional[str] = None) -> bool:
        self.preview_approval(name, args, diff)
        try:
            return bool(questionary.confirm("允许执行？", default=False, style=UI_STYLE).ask())
        except (EOFError, KeyboardInterrupt):
            return False

    def close(self) -> None:
        self.status.clear()
        self._flush_markdown()
        if self.line_open:
            self._write("\n")
            self.line_open = False


class PlainEmitter:
    """非交互正文输出：只把助手回复写到 stdout，工具活动进日志。"""

    def __init__(self, allow_shell: bool = False) -> None:
        self.allow_shell = allow_shell

    def event(self, kind: str, **data) -> None:
        if kind == "content":
            sys.stdout.write(sanitize(str(data["text"])))
            sys.stdout.flush()
        elif kind == "tool_error":
            logger.warning("tool failed: %s · %s", data["tool"], data.get("error"))
        elif kind == "error":
            logger.error("run error: %s", data.get("message"))
            print(f"run error: {data.get('message') or 'run error'}", file=sys.stderr)

    def ask_approval(self, name: str, args, diff: Optional[str] = None) -> bool:
        if self.allow_shell and name == "run_shell_command":
            logger.info("非交互模式按 --allow-shell 放行工具 %s", name)
            return True
        logger.warning("非交互模式无法确认工具 %s，已拒绝", name)
        return False

    def close(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()


class JsonEmitter:
    """非交互结构化输出：每行一个事件，末行由调用方补 result。"""

    def __init__(self, allow_shell: bool = False) -> None:
        self.allow_shell = allow_shell

    def event(self, kind: str, **data) -> None:
        record = {"type": kind}
        for key, value in data.items():
            if key == "tool":
                continue
            if key == "duration":
                record["duration_s"] = round(float(value), 3)
            else:
                record[key] = value
        if "tool" in data:
            record["tool"] = data["tool"]
        print(json.dumps(record, ensure_ascii=False), flush=True)

    def ask_approval(self, name: str, args, diff: Optional[str] = None) -> bool:
        if self.allow_shell and name == "run_shell_command":
            print(json.dumps(
                {"type": "confirm_approved", "tool": name, "args": args}, ensure_ascii=False
            ), flush=True)
            return True
        record = {"type": "confirm_required", "tool": name, "args": args}
        if diff is not None:
            record["diff"] = diff
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return False

    def close(self) -> None:
        pass


def approve_requirements(
    requirements, approved: Set[str], emitter, strategy: str = APPROVAL_ALWAYS,
    review_root=None,
    memory: Optional[MemoryHost] = None,
) -> None:
    """逐个处理 HITL 要求。

    已批准过的同签名直接放行；文件改动工具走内容审阅（diff 预览），且不受
    `dangerous` 策略自动放行影响——每次改动都要过目；外置记忆工具由引擎
    声明确认，声明需要确认的同样不自动放行；命令类工具按策略处理，`dangerous` 下
    未命中危险规则的一次调用直接放行（记日志）；其余交给渲染器。
    """
    for requirement in requirements:
        tool = getattr(requirement, "tool_execution", None)
        name = getattr(tool, "tool_name", "tool")
        args = getattr(tool, "tool_args", None)
        key = signature(name, args)

        def settle(approved_flag: bool) -> bool:
            """确认或拒绝；Agno 对已决议要求的重复决议会抛错，/redo 重放旧要求时跳过。"""
            try:
                if approved_flag:
                    requirement.confirm()
                else:
                    requirement.reject("用户拒绝")
                return True
            except ValueError as exc:
                logger.info("跳过已决议的审批要求：%s", exc)
                return False

        if name == SAVE_TOOL:
            grants = save_approvals.get()
            if grants is not None and emitter.ask_approval(name, args):
                if settle(True):
                    # 同一批出现多个相同保存要求时按批准次数逐次消费。
                    grants[key] = grants.get(key, 0) + 1
            else:
                settle(False)
            continue
        if key in approved:
            settle(True)
            continue
        if is_external_tool(name):
            # 第三方 MCP 工具：外部信任面，每次询问；审批策略不自动放行。
            if emitter.ask_approval(name, args):
                approved.add(key)
                settle(True)
            else:
                settle(False)
            continue
        confirm_names = (
            memory.confirm_tool_names()
            if memory is not None
            else MemoryHost.registered_confirm_names()
        )
        if name in confirm_names:
            # 引擎显式请求确认；宿主实例漏传时按注册表兜底。
            if emitter.ask_approval(name, args):
                approved.add(key)
                settle(True)
            else:
                settle(False)
            continue
        if name in REVIEW_FILE_TOOLS:
            diff = change_diff(name, args, review_root) if review_root is not None else None
            if emitter.ask_approval(name, args, diff=diff):
                approved.add(key)
                settle(True)
            else:
                settle(False)
            continue
        if not needs_approval(name, args, strategy):
            logger.info(
                "审批策略 %s：自动放行 %s（%s）", strategy, name, danger_reason(name, args) or "未命中危险规则"
            )
            settle(True)
            continue
        if emitter.ask_approval(name, args):
            approved.add(key)
            settle(True)
        else:
            settle(False)


def stream_run(
    agent: Agent,
    text: str,
    session_id: str,
    approved: Optional[Set[str]] = None,
    emitter=None,
    user_id: Optional[str] = None,
    strategy: str = APPROVAL_ALWAYS,
    channel: str = "",
    review_root=None,
    handle: Optional[Dict] = None,
    images: Optional[List] = None,
    memory: Optional[MemoryHost] = None,
) -> Dict:
    """流式执行一轮；处理 Agno HITL 暂停并在批准后继续。

    `user_id` 决定记忆归属；不传时 Agno 落到 `"default"`。
    `strategy` 决定「需要确认的工具」这一次要不要真的问人；默认每条都问。
    `channel` 写进 run metadata，供用量账本按 (渠道, 模型) 计价——Agno 自己只记
    provider 类名，无法区分同一模型的多个渠道。
    """
    render = emitter or HumanEmitter()
    approved = approved if approved is not None else set()
    metadata = {"channel": channel} if channel else None
    if memory is not None:
        user_id = user_id or memory.user_id
    return _drain(
        agent,
        lambda: agent.run(
            text, session_id=session_id, user_id=user_id, stream=True, stream_events=True,
            metadata=metadata, images=images,
        ),
        session_id, approved, render, user_id, strategy, review_root, handle, metadata=metadata,
        memory=memory,
    )


def _edit_boundary(agent: Agent, run_id: str) -> Optional[int]:
    """`/edit` 的截断点：该 run 最后一条用户消息的下标，旧回答不进入新上下文。"""
    db = getattr(agent, "db", None)
    if db is None:
        return None
    try:
        run = db.get_run(run_id)
    except Exception:
        logger.exception("loading edit target failed")
        return None
    messages = getattr(run, "messages", None) or []
    for index in range(len(messages) - 1, -1, -1):
        if getattr(messages[index], "role", None) == "user":
            return index
    logger.warning("edit target has no user message; appending instead of replacing")
    return None


def stream_continue(
    agent: Agent,
    session_id: str,
    run_id: str,
    *,
    input: Optional[str] = None,
    fork: bool = False,
    regenerate: bool = False,
    approved: Optional[Set[str]] = None,
    emitter=None,
    user_id: Optional[str] = None,
    strategy: str = APPROVAL_ALWAYS,
    review_root=None,
    handle: Optional[Dict] = None,
    channel: str = "",
    memory: Optional[MemoryHost] = None,
) -> Dict:
    """从某个既有 run 分叉/重生成：`/edit`（fork+新文本）与 `/redo`（regenerate）共用。

    fork 且带新文本时，从原 run 最后一条用户消息处截断再追加，旧回答不进入新上下文；
    regenerate=True 由 Agno 自行推导重建边界。
    """
    render = emitter or HumanEmitter()
    approved = approved if approved is not None else set()
    if memory is not None:
        user_id = user_id or memory.user_id
    metadata = {"channel": channel} if channel else None
    continue_from = _edit_boundary(agent, run_id) if fork and input else None
    return _drain(
        agent,
        lambda: agent.continue_run(
            run_id=run_id,
            input=input,
            fork=fork,
            regenerate=regenerate,
            session_id=session_id,
            user_id=user_id,
            stream=True,
            stream_events=True,
            **({"metadata": metadata} if metadata is not None else {}),
            **({"continue_from": continue_from} if continue_from is not None else {}),
        ),
        session_id, approved, render, user_id, strategy, review_root, handle, metadata=metadata,
        memory=memory,
    )


def _drain(
    agent: Agent,
    start_events,
    session_id: str,
    approved: Set[str],
    render,
    user_id: Optional[str],
    strategy: str,
    review_root,
    handle: Optional[Dict],
    metadata: Optional[Dict] = None,
    memory: Optional[MemoryHost] = None,
) -> Dict:
    """消费 Agno 事件；取消仅在收到原生取消事件后确认。"""
    tool_start: Dict[str, float] = {}
    summary: Dict = {}
    memory_run_token = None
    run_id: Optional[str] = None
    pieces: list = []
    completed = False
    save_token = save_approvals.set({})
    parent_token = parent_run_id.set(None)
    try:
        events = start_events()
        while True:
            paused = None
            for event in events:
                if run_id is None:
                    run_id = getattr(event, "run_id", None)
                    if run_id is not None:
                        parent_run_id.set(run_id)
                    if memory is not None and isinstance(event, agent_events.RunContinuedEvent):
                        # Agno 分叉的工具上下文可能还带原 run id；事件给出实际执行的 id。
                        memory_run_token = memory.continued_run_id.set(run_id)
                    if run_id is not None and handle is not None:
                        # 供 REPL 的 Esc 打断直调 cancel_run。
                        handle["run_id"] = run_id
                        if handle.get("cancel_requested"):
                            agent.cancel_run(run_id=run_id)
                if isinstance(event, agent_events.ModelRequestStartedEvent):
                    render.event("model_start")
                elif isinstance(event, agent_events.RunContentEvent) and event.content:
                    pieces.append(str(event.content))
                    render.event("content", text=str(event.content))
                elif isinstance(event, (
                    agent_events.ToolCallStartedEvent, agent_events.ToolCallCompletedEvent, agent_events.ToolCallErrorEvent,
                )) and event.tool:
                    tool = event.tool
                    name = getattr(tool, "tool_name", None) or "tool"
                    call_id = getattr(tool, "tool_call_id", None)
                    key = call_id or name
                    private = {} if isinstance(render, (PlainEmitter, JsonEmitter)) else {"call_id": call_id}
                    if isinstance(event, agent_events.ToolCallStartedEvent):
                        tool_start[key] = time.monotonic()
                        render.event("tool_start", tool=name, args=getattr(tool, "tool_args", None), **private)
                    elif isinstance(event, agent_events.ToolCallErrorEvent):
                        if private:
                            private["result"] = getattr(tool, "result", None)
                        render.event(
                            "tool_error", tool=name, duration=_tool_duration(tool, tool_start.pop(key, None)),
                            error=event.error, **private,
                        )
                    else:
                        if private:
                            private["result"] = getattr(tool, "result", None)
                            if private["result"] is None:
                                private["result"] = getattr(event, "content", None)
                            private["error"] = getattr(tool, "tool_call_error", None)
                        render.event(
                            "tool_done", tool=name, duration=_tool_duration(tool, tool_start.pop(key, None)),
                            **private,
                        )
                elif isinstance(event, agent_events.ModelRequestCompletedEvent):
                    if getattr(event, "input_tokens", None):
                        summary["context_tokens"] = event.input_tokens
                elif isinstance(event, agent_events.RunPausedEvent):
                    paused = event
                    break
                elif isinstance(event, agent_events.RunCompletedEvent):
                    if not summary.get("cancelled"):
                        completed = True
                    metrics = getattr(event, "metrics", None)
                    if metrics is not None:
                        summary["tokens"] = getattr(metrics, "total_tokens", 0) or 0
                        summary["duration"] = getattr(metrics, "duration", 0) or 0
                        usage = _usage_of(metrics)
                        if usage is not None:
                            summary["usage"] = usage
                elif isinstance(event, agent_events.RunErrorEvent):
                    logger.error("run error: %s", event.content or "run error")
                    summary["error"] = True
                    render.event("error", message=event.content)
                elif isinstance(event, agent_events.RunCancelledEvent):
                    logger.warning("run cancelled")
                    summary["cancelled"] = True
                    render.event("cancelled")

            if paused is None:
                break

            if handle is not None and handle.get("cancel_requested"):
                summary["error"] = True
                render.event("error", message="运行在审批时暂停；已请求取消，但尚未确认取消完成")
                break

            requirements = paused.requirements or []
            approve_requirements(requirements, approved, render, strategy, review_root=review_root,
                                 memory=memory)
            if handle is not None and handle.get("cancel_requested"):
                summary["error"] = True
                render.event("error", message="运行在审批时暂停；已请求取消，但尚未确认取消完成")
                break
            events = agent.continue_run(
                run_id=paused.run_id,
                requirements=requirements,
                # 从 run_id 继续必须带 session_id，否则 Agno 直接拒绝。
                session_id=session_id,
                user_id=user_id,
                stream=True,
                stream_events=True,
                metadata=metadata,
            )
    except KeyboardInterrupt:
        if handle is not None:
            handle["cancel_requested"] = True
        if run_id:
            try:
                agent.cancel_run(run_id=run_id)
            except Exception as exc:
                logger.exception("cancel request failed")
                render.event("error", message=f"取消请求失败：{exc}")
        summary["error"] = True
        summary["interrupted"] = True
        render.event("error", message="已中断事件消费；无法确认运行已取消")
    except Exception as exc:
        logger.exception("run failed")
        summary["error"] = True
        render.event("error", message=str(exc))
    finally:
        save_approvals.reset(save_token)
        parent_run_id.reset(parent_token)
        if memory is not None and memory_run_token is not None:
            memory.continued_run_id.reset(memory_run_token)
        if handle is not None and handle.get("cancel_requested") and not (
            summary.get("cancelled") or summary.get("error") or completed
        ):
            summary["error"] = True
            render.event("error", message="已请求取消，但未收到运行终态")
        if not completed and not summary.get("cancelled") and not summary.get("error"):
            # 流自然结束却没有终态事件：不能当作成功，也不能自动派发队列。
            summary["error"] = True
            render.event("error", message="运行未收到终态事件；结果不可确认")
        if pieces and (summary.get("cancelled") or summary.get("interrupted") or (
            handle is not None and handle.get("cancel_requested") and not completed
        )):
            message = "已输出的部分内容未确认保存，不能保证下一轮可见"
            if isinstance(render, (PlainEmitter, JsonEmitter)):
                print(message, file=sys.stderr)
            else:
                render.event("warning", message=message)
        try:
            render.close()
        except Exception:
            logger.exception("render close failed")
    summary["content"] = "".join(pieces)
    return summary
