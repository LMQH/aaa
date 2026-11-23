"""REPL 入口与命令行分派：交互走 REPL，非交互单轮执行后退出。"""

import asyncio
import json
import logging
import os
import shlex
import sys
import subprocess
import tempfile
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agno.run.base import RunStatus
from prompt_toolkit import PromptSession
from prompt_toolkit.application import in_terminal, set_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition, has_focus, is_done
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.layout import ConditionalContainer, Dimension, HSplit, VerticalAlign, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.shortcuts import print_formatted_text

from ..boundary.policy import MODES
from ..config.loader import ConfigError, load_config
from ..config.paths import ANIMATIONS_DIR, HISTORY_FILE
from ..config.settings import load_settings
from ..context.params import compact_threshold
from ..memory import MemoryEngineError, MemoryHost
from ..model.catalog import Ambiguous, Resolved
from ..runtime.logging_setup import setup_logging
from ..usage.cost import cost_of
from ..sessions.query import ensure_session_name, find_session, latest_session_for_cwd, remember_session
from .args import Options, parse_args
from .controller import Controller
from .git_status import read_status
from .key_input import create_cli_input, is_shift_enter
from . import banner as banner_view
from .. import plugins, trust as trust_mod
from ..todo_contract import normalize
from .commands import configure, handle_command, show_memory_status
from .run import HumanEmitter, JsonEmitter, PlainEmitter, stream_continue, stream_run
from .state import AppState
from .tool_details import ToolDetails
from .usage_stats import note_used
from .ui import (
    COMMANDS, EDITOR_PROMPT, GLYPH, editor_status, ellipsize,
    reset_cursor_color, set_cursor_color, style_for, todo_lines,
)

# 退出码：0 成功，1 运行失败/会话不存在，2 用法/配置问题，130 被中断。
logger = logging.getLogger(__name__)
EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 130


def apply_options(state: AppState, options: Options) -> None:
    """把启动参数落到状态上：模型、权限模式、续接的会话。"""
    changed = False
    if options.model:
        result = state.catalog.resolve(options.model)
        if isinstance(result, Ambiguous):
            names = "、".join(ref.display for ref in result.candidates)
            _fail(f"模型引用有歧义：{options.model} 命中 {names}，请加渠道前缀。")
        if not isinstance(result, Resolved):
            _fail(f"没有匹配的模型：{options.model}")
        state.provider, state.model_id = result.ref.provider, result.ref.model_id
        changed = True
    if options.permission and options.permission != state.mode:
        state.mode = options.permission
        changed = True
    if changed:
        try:
            state.rebuild_runnable()
        except ValueError as exc:
            _fail(str(exc))

    if options.continue_session:
        session = latest_session_for_cwd(state.runnable, str(Path.cwd()), state.settings)
        if session is None:
            _fail(f"当前目录没有可续聊的 Agent 会话：{Path.cwd()}")
        state.select_session(session)
    elif options.resume:
        session = find_session(state.runnable, options.resume, state.settings)
        if session is None:
            _fail(f"没有匹配的会话：{options.resume}")
        state.select_session(session)


def _user_id(state: AppState) -> str:
    return str(state.settings.get("memory.user_id"))


def _approval_strategy(state: AppState) -> str:
    return str(state.settings.get("boundary.command_approval"))


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def read_message(options: Options) -> Optional[str]:
    """非交互模式的消息：位置参数优先，其次 stdin。"""
    if options.message:
        return options.message
    if sys.stdin.isatty():
        return None
    return sys.stdin.read().strip() or None


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def load_images(paths: List[str]) -> Optional[List]:
    """把本地图片路径转成 Agno Image；路径无效直接报用法错误。"""
    from agno.media import Image

    out: List = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            print(f"图片不存在或格式不支持：{raw}", file=sys.stderr)
            sys.exit(2)
        out.append(Image(filepath=path))
    return out or None


def run_once(
    state: AppState, message: str, output: str, allow_shell: bool = False,
    images: Optional[List] = None,
) -> int:
    """非交互执行一轮，按输出形态渲染并给出退出码。"""
    if output == "json":
        emitter: PlainEmitter | JsonEmitter = JsonEmitter(allow_shell=allow_shell)
    else:
        emitter = PlainEmitter(allow_shell=allow_shell)
    remember_session(str(Path.cwd()), state.session_id)
    summary = stream_run(
        state.runnable, message, state.session_id, state.approved, emitter,
        user_id=_user_id(state), strategy=_approval_strategy(state),
        channel=state.provider,
        review_root=state.review_root(), images=images,
        memory=state.memory if isinstance(state.memory, MemoryHost) else None,
    )
    if output == "json":
        print(json.dumps({
            "type": "result",
            "session_id": state.session_id,
            "content": summary.get("content", ""),
            "tokens": summary.get("tokens", 0),
            "duration_s": round(float(summary.get("duration", 0) or 0), 3),
            "error": bool(summary.get("error")),
            "cancelled": bool(summary.get("cancelled")),
        }, ensure_ascii=False), flush=True)
    if summary.get("cancelled") or summary.get("interrupted"):
        return EXIT_CANCELLED
    return EXIT_RUN_FAILED if summary.get("error") else EXIT_OK




def enter_action(buffer: Buffer, data: Optional[str]) -> None:
    """Shift+Enter 只插入换行；其他回车按提交处理。"""
    if is_shift_enter(data):
        buffer.insert_text("\n")
    else:
        buffer.validate_and_handle()


def approval_pane(kb, get_controller):
    """审批必须先显式切换焦点，普通草稿中的按键不会授权工具。"""
    buffer = Buffer()
    control = BufferControl(buffer=buffer)
    active = Condition(lambda: get_controller() is not None and get_controller().approval is not None)
    focused = active & has_focus(buffer)

    @kb.add("f2", filter=active)
    def focus_approval(event):
        event.app.layout.focus(control)

    @kb.add("y", filter=focused)
    def approve(event):
        get_controller().answer(True)

    @kb.add("n", filter=focused)
    @kb.add("enter", filter=focused)
    @kb.add("escape", filter=focused, eager=True)
    def reject(event):
        get_controller().answer(False)

    label = Window(
        FormattedTextControl(lambda: f"审批 {get_controller().approval.name}：F2 聚焦，y 批准 / n 或 Esc 拒绝"),
        height=1,
    )
    return ConditionalContainer(HSplit([label, Window(control, height=1)]), filter=active & ~is_done), control


def last_completed_run_id(runnable, session_id):
    runs = runnable.db.get_runs(
        session_id=session_id, status=RunStatus.completed, sort_by="run_index", sort_order="desc", limit=1,
    )
    return runs[0].run_id if runs else None


def clear_credentials(runnable) -> None:
    """退出前清空 Agent 模型持有的密钥。"""
    if runnable.model is not None:
        runnable.model.api_key = None


def main(argv: Optional[List[str]] = None) -> None:
    settings = load_settings()
    for warning in settings.warnings:
        print(warning, file=sys.stderr)
    options = parse_args(str(settings.get("boundary.default_mode")), argv)
    setup_logging(settings)

    # 首次启动或配置不完整：交互模式进入向导，非交互模式直接报错。
    # 配置文件存在却读不出来时一律报错退出，绝不进向导（保存会覆盖原文件）。
    try:
        config = load_config(settings)
    except ConfigError as exc:
        print(f"{GLYPH['fail']} {exc}", file=sys.stderr)
        print("修好配置文件后再启动；程序不会覆盖它。", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    if config is None:
        if not options.interactive:
            print("尚未配置渠道，先运行 aaa 进入配置向导。", file=sys.stderr)
            raise SystemExit(EXIT_USAGE)
        print("首次启动，先完成渠道配置。")
        try:
            config = configure(None, settings)
        except (EOFError, KeyboardInterrupt):
            print("\n已取消配置。")
            return
        if config is None:
            print("未完成配置，退出。")
            return

    project = plugins.project_dirs() if settings.get("project.enabled") else None
    trusted = trust_mod.ensure(project, interactive=options.interactive)

    try:
        state = AppState.start(config, settings)
    except MemoryEngineError as exc:
        print(f"{GLYPH['fail']} {exc}", file=sys.stderr)
        print("用 /memory（或 /memory engine <引擎>）重新选择记忆引擎后重启；也可以直接改 ~/.aaa/config.toml。", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    try:
        _run_application(state, options, project=project, trusted=trusted)
    finally:
        state.close_memory()
        clear_credentials(state.runnable)


def _run_application(
    state: AppState, options: Options, *, project=None, trusted=frozenset()
) -> None:
    settings = state.settings
    apply_options(state, options)

    if options.interactive:
        # 与 omp 对齐：进入 REPL 前清掉当前屏幕与滚动历史；非 TTY 不写控制序列。
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[2J\x1b[3J\x1b[H")
            sys.stdout.flush()
        # 许多终端不回 CPR 响应；内联布局不依赖它，默认关闭探测（用户可显式设 0）。
        os.environ.setdefault("PROMPT_TOOLKIT_NO_CPR", "1")

    if options.interactive and state.project_note:
        print(f"{GLYPH['dot']} {state.project_note}")

    if not options.interactive:
        message = read_message(options)
        if not message:
            raise SystemExit("非交互模式需要提示词：aaa -p \"...\" 或从 stdin 传入。")
        images = load_images(options.images)
        sys.exit(run_once(state, message, options.output, options.allow_shell, images=images))

    # 当前会话状态，只由交互循环更新。
    hud: Dict = {"cost": None, "items": [], "git": None}
    cwd = os.getcwd()
    home = str(Path.home())
    if cwd == home or cwd.startswith(home + os.sep):
        cwd = "~" + cwd[len(home):]

    def status_line(width: int) -> FormattedText:
        channel, model = state.selection()
        window = state.window()
        return FormattedText(editor_status(
            width,
            logo=str(settings.get("ui.logo") or "α"),
            model=f"{model.id} ({channel.name or channel.id})",
            cwd=ellipsize(cwd, int(settings.get("ui.toolbar_width"))),
            git=hud["git"],
            cost=hud["cost"],
            tokens=state.context_tokens,
            window=window,
            compact_at=compact_threshold(window, settings) if state.context_tokens is not None else 0,
            bar_min=int(settings.get("ui.bar_min_width")),
            bar_max=int(settings.get("ui.bar_max_width")),
        ))

    if sys.stdout.isatty() and settings.get("ui.banner_enabled"):
        try:
            duration = float(settings.get("ui.banner_duration")) if settings.get("ui.banner_animate") else 0.0
            animation_dirs = None
            if project is not None and "animations" in trusted:
                animation_dirs = (project["animations"], ANIMATIONS_DIR)
            banner_view.animate(duration=duration, dirs=animation_dirs)
        except Exception:
            logger.exception("banner rendering failed")
    print("输入 /help 查看命令。")
    set_cursor_color("#7fd1ff")
    named: set = set()

    async def interactive(terminal_input) -> None:
        kb = KeyBindings()
        controller = None
        tool_details = ToolDetails()
        pane, approval_control = approval_pane(kb, lambda: controller)

        def activity() -> str:
            if controller is None:
                return ""
            parts = []
            if controller.phase == "stopping":
                parts.append("正在停止…")
            elif controller.busy and controller.approval is None:
                parts.append(f"{GLYPH['working']} 运行中…")
            if controller.queue:
                parts.append(f"排队 {len(controller.queue)}")
            return " · ".join(parts)

        def editor_hint() -> FormattedText:
            if is_done() or controller is None or controller.phase == "stopping":
                return FormattedText([])
            if controller.approval is not None:
                text = "y 批准 / n 或 Esc 拒绝" if has_focus(approval_control.buffer)() else "F2 聚焦审批"
            elif controller.busy:
                text = "Esc 打断"
            else:
                text = f"Shift+Tab 切换权限 · {state.mode}"
            return FormattedText([("class:status.hint", " " + text)])

        def invalidate() -> None:
            if controller.approval is None and session.layout.current_control is approval_control:
                session.layout.focus(session.default_buffer)
            session.app.invalidate()

        @kb.add("c-o", filter=~has_focus(approval_control.buffer))
        def _(event) -> None:
            if not tool_details.toggle():
                print(f"{GLYPH['dot']} 暂无工具详情。")
            elif tool_details.expanded and detail_height() == 0:
                tool_details.toggle()
                print(f"{GLYPH['dot']} 窗口太矮：收起草稿或放大窗口后再按 Ctrl-O。")
            event.app.invalidate()

        details_open = Condition(lambda: tool_details.expanded) & ~has_focus(approval_control.buffer)

        @kb.add("pageup", filter=details_open)
        def _(event) -> None:
            tool_details.scroll(-1)
            event.app.invalidate()

        @kb.add("pagedown", filter=details_open)
        def _(event) -> None:
            tool_details.scroll(1)
            event.app.invalidate()

        @kb.add("escape", filter=~has_focus(approval_control.buffer))
        def _(event) -> None:
            if event.current_buffer.complete_state:
                event.current_buffer.cancel_completion()
            elif tool_details.expanded:
                tool_details.toggle()
                event.app.invalidate()
            elif controller.approval is not None:
                print(f"{GLYPH['dot']} 审批待处理；按 F2 聚焦后可拒绝。")
            elif controller.busy:
                controller.cancel()


        @kb.add("s-tab")
        def _(event) -> None:
            if controller.busy:
                print(f"{GLYPH['dot']} 运行中不能切换权限模式，请空闲后重试。")
                return
            if not state.scope():
                return
            modes = list(MODES)
            try:
                state.set_mode(modes[(modes.index(state.mode) + 1) % len(modes)])
            except ValueError as exc:
                print(f"{GLYPH['fail']} {exc}")
                return
            event.app.invalidate()

        @kb.add("c-j", filter=~has_focus(approval_control.buffer))
        @kb.add("escape", "enter", filter=~has_focus(approval_control.buffer))
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("enter", filter=~has_focus(approval_control.buffer))
        def _(event) -> None:
            data = getattr(event.key_sequence[-1], "data", None) if event.key_sequence else None
            enter_action(event.current_buffer, data)

        cleared: List[str] = []
        last_interrupt = [0.0]

        @kb.add("c-c", filter=~has_focus(approval_control.buffer))
        def _(event) -> None:
            """Ctrl-C：清草稿可召回；900ms 内再次按下才退出。"""
            buffer = event.current_buffer
            now = time.monotonic()
            window = float(settings.get("ui.exit_confirm_window"))
            if buffer.text:
                cleared.append(buffer.text)
                buffer.reset()
                print(f"{GLYPH['dot']} 已清空草稿；按 Up 取回")
                last_interrupt[0] = 0.0
                return
            if now - last_interrupt[0] <= window:
                event.app.exit(exception=EOFError())
                return
            last_interrupt[0] = now
            print(f"{GLYPH['dot']} 再按一次 Ctrl-C 退出")

        @kb.add("up", filter=~has_focus(approval_control.buffer))
        def _(event) -> None:
            if event.current_buffer.text or not cleared:
                event.current_buffer.history_backward()
                return
            event.current_buffer.insert_text(cleared.pop())

        @kb.add("c-g", filter=~has_focus(approval_control.buffer))
        def _(event) -> None:
            buffer = event.current_buffer
            original = buffer.text

            async def edit_draft() -> None:
                path = None
                try:
                    fd, path = tempfile.mkstemp(suffix=".md")
                    with os.fdopen(fd, "w", encoding="utf-8") as file:
                        file.write(original)
                    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
                    async with in_terminal():
                        result = await asyncio.to_thread(subprocess.run, [*editor, path], check=False)
                    if result.returncode:
                        print(f"{GLYPH['fail']} 编辑器退出码 {result.returncode}，草稿未改变。")
                        return
                    with open(path, encoding="utf-8") as file:
                        buffer.text = file.read()
                except Exception as exc:
                    print(f"{GLYPH['fail']} 编辑器失败：{exc}；草稿未改变。")
                finally:
                    if path is not None:
                        try:
                            os.unlink(path)
                        except OSError:
                            logger.exception("removing editor temporary file failed")

            asyncio.create_task(edit_draft())

        @kb.add("escape", "up", filter=~has_focus(approval_control.buffer))
        def _(event) -> None:
            if event.current_buffer.text:
                print(f"{GLYPH['dot']} 请先处理当前草稿。")
            elif controller.queue:
                text = controller.take_queued(state.session_id)
                if text is None:
                    owners = ", ".join(dict.fromkeys(owner for _, owner in controller.queue))
                    print(f"{GLYPH['dot']} 队列属于会话 {owners}；切回所属会话后按 Alt+Up 取回。")
                else:
                    event.current_buffer.insert_text(text)

        @kb.add("c-d", filter=~has_focus(approval_control.buffer))
        def _(event) -> None:
            if event.current_buffer.text:
                event.current_buffer.delete()
            elif not controller.busy:
                event.app.exit(exception=EOFError())

        session = PromptSession(
            input=terminal_input,
            history=FileHistory(str(HISTORY_FILE)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=state.completer,
            complete_while_typing=True,
            reserve_space_for_menu=0,
            style=style_for(settings.get("ui.theme")),
            prompt_continuation=lambda width, *_: " " * width,
            rprompt=editor_hint,
            key_bindings=kb,
        )
        editor = session.layout.current_window

        def layout_rows() -> tuple[int, int]:
            size = session.app.output.get_size()
            above = 1 + bool(activity()) + (2 if controller and controller.approval else 0)
            # Leave a terminal line outside the active area; long drafts scroll in the editor.
            available = max(1, size.rows - above - 1)
            draft_rows = editor.content.preferred_height(
                max(1, size.columns), available, wrap_lines=True, get_line_prefix=editor.get_line_prefix,
            ) or 1
            return draft_rows, available - draft_rows

        def reserve_completion_rows(app) -> None:
            draft_rows, remaining = layout_rows()
            completions = session.default_buffer.complete_state
            # Reserve room only while the floated completion menu is visible.
            menu_rows = (
                min(len(completions.completions), (remaining + 1) // 2)
                if completions and app.layout.has_focus(session.default_buffer) else 0
            )
            session.reserve_space_for_menu = draft_rows + menu_rows if menu_rows else 0

        session.app.before_render += reserve_completion_rows

        def pane_rows() -> int:
            draft_rows, remaining = layout_rows()
            return max(0, remaining - max(0, session.reserve_space_for_menu - draft_rows))

        def detail_height():
            if not tool_details.expanded:
                return 0
            available = min(int(settings.get("ui.tool_detail_lines")), pane_rows())
            return available if available >= 3 else 0

        def visible_todos():
            lines = todo_lines(hud["items"], session.app.output.get_size().columns)
            budget = max(0, pane_rows() - detail_height())
            if len(lines) > budget:
                if not budget:
                    return []
                current = next((line for line in lines if line[0] == "class:todo.in_progress"), None)
                if budget == 1:
                    lines = [current or lines[0]]
                else:
                    kept = lines[:budget - 1]
                    if current and current not in kept:
                        kept[-1] = current
                    lines = [*kept, ("class:todo.pending", ellipsize("… /todos 查看全部", session.app.output.get_size().columns))]
            return lines

        def editor_height() -> Dimension:
            if is_done():
                return Dimension()
            draft_rows, remaining = layout_rows()
            maximum = draft_rows + remaining - detail_height() - len(visible_todos())
            return Dimension(min=min(session.reserve_space_for_menu, maximum), max=maximum)

        editor.height = editor_height

        def formatted_lines(lines):
            return FormattedText([
                (style, text + ("\n" if index < len(lines) - 1 else ""))
                for index, (style, text) in enumerate(lines)
            ])

        todo_view = ConditionalContainer(
            Window(FormattedTextControl(lambda: formatted_lines(visible_todos())), height=lambda: len(visible_todos())),
            filter=~is_done & Condition(lambda: bool(visible_todos())),
        )
        detail_view = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: formatted_lines(tool_details.render(
                    session.app.output.get_size().columns, detail_height(),
                ))),
                height=detail_height,
                wrap_lines=False,
            ),
            filter=~is_done & Condition(lambda: bool(detail_height())),
        )
        activity_view = ConditionalContainer(
            Window(FormattedTextControl(activity, style="class:status.activity"), height=1, wrap_lines=False),
            filter=~is_done & Condition(lambda: bool(activity())),
        )
        status_view = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: status_line(session.app.output.get_size().columns)),
                height=1, wrap_lines=False,
            ),
            filter=~is_done,
        )
        session.layout.container = HSplit(
            [todo_view, detail_view, pane, activity_view, status_view, session.layout.container],
            align=VerticalAlign.TOP,
        )

        def refresh_hud(summary: Dict) -> None:
            if summary.get("context_tokens"):
                state.context_tokens = summary["context_tokens"]
            usage = summary.get("usage")
            if usage is not None:
                price = state.catalog.cost(state.provider, state.model_id)
                amount = cost_of(
                    price, usage[0], usage[1], usage[2], usage[3], datetime.now().astimezone()
                )
                if amount is not None:
                    hud["cost"] = (hud["cost"] or 0.0) + amount
            try:
                state_data = state.runnable.get_session_state(session_id=state.session_id)
                hud["items"] = normalize((state_data or {}).get("todos"))
            except Exception:
                logger.exception("loading session todos failed")

        def start_turn(text: str, images: Optional[List] = None) -> None:
            agent, session_id = state.runnable, state.session_id
            user_id, strategy = _user_id(state), _approval_strategy(state)
            channel, root = state.provider, state.review_root()

            def worker(emitter, handle):
                remember_session(str(Path.cwd()), session_id)
                summary = stream_run(
                    agent, text, session_id, state.approved, emitter=emitter,
                    user_id=user_id, strategy=strategy, channel=channel,
                    review_root=root, handle=handle, images=images,
                    memory=state.memory if isinstance(state.memory, MemoryHost) else None,
                )
                if not summary.get("error") and not summary.get("cancelled") and not handle["cancel_requested"]:
                    selected_channel, model = state.selection()
                    ensure_session_name(
                        agent, selected_channel, model, session_id, text, named,
                        GLYPH["title"], state.settings,
                    )
                return summary

            tool_details.clear()
            controller.start(worker)

        def start_continue(kwargs: Dict) -> None:
            agent, session_id = state.runnable, state.session_id
            user_id, strategy, root = _user_id(state), _approval_strategy(state), state.review_root()

            def worker(emitter, handle):
                return stream_continue(
                    agent, session_id, kwargs["run_id"],
                    input=kwargs.get("input"), fork=kwargs.get("fork", False),
                    regenerate=kwargs.get("regenerate", False), approved=state.approved,
                    emitter=emitter, user_id=user_id, strategy=strategy,
                    review_root=root, handle=handle, channel=state.provider,
                    memory=state.memory if isinstance(state.memory, MemoryHost) else None,
                )

            tool_details.clear()
            controller.start(worker)


        def dispatch(text: str, from_queue: bool = False) -> None:
            text = text.strip()
            if not text:
                return
            name = text[1:].split(None, 1)[0] if text.startswith("/") and text[1:].strip() else ""
            builtin = {item[0][1:] for item in COMMANDS}
            # 用户主动敲下的命令记一次使用；排队重放与菜单内部转派不重复计。
            if name and not from_queue and (name in builtin or name in state.plugin_commands):
                note_used(name)
            if text == "/exit":
                controller.request_exit()
                return
            if controller.busy:
                if text == "/todos":
                    handle_command(state, "/todos")
                elif text in ("/help", "/skills") or text == "/usage" or text.startswith("/usage "):
                    handle_command(state, text)
                elif text == "/memory":
                    show_memory_status(state)
                elif text == "/insight":
                    handle_command(state, text)
                elif text == "/trust":
                    handle_command(state, text)
                elif not text.startswith("/") or (name in state.plugin_commands and name not in builtin):
                    controller.enqueue(text)
                else:
                    print(f"{GLYPH['dot']} 运行中不能执行 {text}，请空闲后重试。")
                return


            if handle_command(state, text):
                session.completer = state.completer
                return
            if name in state.plugin_commands:
                _, _, argument = text.partition(" ")
                prompt = state.plugin_commands[name].template.replace("$ARGUMENTS", argument.strip())
                start_turn(prompt)
                return
            if text.startswith("/image "):
                raw = text[len("/image "):].strip()
                try:
                    images = load_images([raw])
                except SystemExit:
                    return
                start_turn(f"（用户发来一张图片：{Path(raw).name}，请查看并回应。）", images)
                return
            if text == "/redo" or text == "/edit" or text.startswith("/edit "):
                new_text = text[len("/edit"):].strip() if text != "/redo" else None
                if text != "/redo" and not new_text:
                    print(f"用法：{GLYPH['prompt']} /edit <新的消息>")
                    return
                last = last_completed_run_id(state.runnable, state.session_id)
                if last is None:
                    print(f"{GLYPH['fail']} 本会话没有已完成、可操作的轮次；暂停中的运行不会被续跑。")
                    return
                start_continue({"run_id": last, "regenerate": True} if text == "/redo"
                               else {"run_id": last, "input": new_text, "fork": True})
                return
            start_turn(text)

        class AppRenderer(HumanEmitter):
            """事件先更新 HUD，再按统一渲染输出。"""

            def _write(self, text: str) -> None:
                # Only renderer-owned ANSI reaches the formatted terminal sink.
                with set_app(session.app):
                    print_formatted_text(ANSI(text), end="", output=session.app.output)

            def event(self, kind: str, **data) -> None:
                tool_details.update(kind, **data)
                if kind == "tool_done" and str(data.get("tool", "")).startswith("todo") and isinstance(data.get("result"), str):
                    try:
                        payload = json.loads(data["result"])
                        if payload.get("ok") and isinstance(payload.get("todos"), list):
                            hud["items"] = normalize(payload["todos"])
                    except (ValueError, TypeError):
                        pass
                if kind != "model_start":
                    super().event(kind, **data)

        def reload_session_hud() -> None:
            tool_details.clear()
            try:
                data = state.runnable.get_session_state(session_id=state.session_id)
                hud["items"] = normalize((data or {}).get("todos"))
            except Exception:
                logger.exception("loading session todos failed")
            hud["cost"] = None

        reload_session_hud()
        renderer = AppRenderer()
        controller = Controller(
            state, asyncio.get_running_loop(), refresh_hud, dispatch, invalidate, render=renderer
        )
        async def git_loop() -> None:
            interval = float(settings.get("ui.git_interval"))
            timeout = float(settings.get("ui.git_timeout"))
            try:
                while not controller.shutdown:
                    hud["git"] = await read_status(Path.cwd(), timeout)
                    controller.redraw()
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return

        git_task = asyncio.create_task(git_loop())
        pending = options.message
        try:
            while not controller.shutdown:
                if pending is not None:
                    text, pending = pending, None
                    print(EDITOR_PROMPT + text)
                else:
                    try:
                        text = await session.prompt_async(
                            FormattedText([("class:prompt", EDITOR_PROMPT)])
                        )
                    except KeyboardInterrupt:
                        continue
                    except EOFError:
                        controller.request_exit()
                        break
                before_session = state.session_id
                try:
                    dispatch(text)
                except Exception as exc:
                    logger.exception("command dispatch failed")
                    print(f"{GLYPH['fail']} 命令失败：{exc}")
                if state.session_id != before_session:
                    reload_session_hud()
        finally:
            if controller.busy:
                controller.request_exit()
                await controller.done.wait()
            git_task.cancel()
            renderer.close()

    try:
        with patch_stdout(), closing(create_cli_input()) as terminal_input:
            asyncio.run(interactive(terminal_input))
    finally:
        reset_cursor_color()
