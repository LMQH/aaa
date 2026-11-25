"""REPL 入口与命令行分派：交互走 REPL，非交互单轮执行后退出。"""

import asyncio
import io
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from contextlib import closing, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from agno.run.base import RunStatus
from prompt_toolkit import PromptSession
from prompt_toolkit.application import in_terminal, set_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, is_done
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    HSplit,
    VerticalAlign,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import create_output
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text

from .. import plugins
from .. import trust as trust_mod
from ..boundary.policy import MODES
from ..config.loader import ConfigError, load_config
from ..config.paths import ANIMATIONS_DIR, HISTORY_FILE
from ..config.settings import load_settings
from ..context.params import compact_threshold
from ..memory import MemoryEngineError, MemoryHost
from ..model.catalog import Ambiguous, Resolved
from ..runtime.logging_setup import setup_logging
from ..sessions.fork import fork_before, user_turns
from ..sessions.query import (
    ensure_session_name,
    find_session,
    latest_session_for_cwd,
    remember_session,
)
from ..todo_contract import normalize
from ..usage.cost import cost_of
from . import banner as banner_view
from . import picker
from . import report as report_panel
from .args import Options, parse_args
from .commands import configure, handle_command, show_memory_status
from .controller import Controller
from .git_status import read_status
from .key_input import create_cli_input, extended_keys, is_shift_enter
from .render import sanitize
from .run import (
    ALWAYS_ALLOW,
    HumanEmitter,
    JsonEmitter,
    PlainEmitter,
    stream_continue,
    stream_run,
)
from .state import AppState
from .tool_details import ToolDetails
from .ui import (
    COMMANDS,
    EDITOR_PROMPT,
    GLYPH,
    completion_rows,
    editor_status,
    ellipsize,
    reset_cursor_color,
    set_cursor_color,
    style_for,
    todo_lines,
)
from .usage_stats import note_used

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


AT_FILE_TOKEN = re.compile(r'(?<!\S)@(?:"([^"\n]+)"|([^\s"@]+))')
AT_FILE_CHAR_CAP = 100_000


def expand_at_files(text: str) -> tuple[str, Optional[List], List[str]]:
    """把消息里的 `@路径` 换成真实附件：图片进 images，文本文件以 `<file>` 块拼到消息尾部。

    对齐 omp 的 `@` 语义：token 以空白为界，支持 `"@带空格的路径"`；图片按后缀
    走图片通道，文本读取后截断到 10 万字符。路径不存在、是目录或读不了时
    原样保留；带 `/` 的缺失路径给一条提示。返回 (新文本, 附加的图片, 提示)。
    """
    from agno.media import Image

    images: List = []
    blocks: List[str] = []
    notices: List[str] = []

    def replace(match: "re.Match[str]") -> str:
        token, inner = match.group(0), match.group(1) or match.group(2)
        path = Path(inner).expanduser()
        if not path.is_file():
            if "/" in inner:
                reason = "目录不能附加" if path.is_dir() else "文件不存在"
                notices.append(f"@{inner}：{reason}，未附加")
            return token
        if path.suffix.lower() in IMAGE_SUFFIXES:
            try:
                images.append(Image(filepath=path))
            except Exception as exc:
                notices.append(f"@{inner}：图片读取失败（{exc}）")
            return token
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            notices.append(f"@{inner}：不是可读文本，未附加")
            return token
        if len(content) > AT_FILE_CHAR_CAP:
            size = path.stat().st_size
            content = content[:AT_FILE_CHAR_CAP] + f"\n…（已截断，完整文件 {size} 字节，可用 read 工具读取）"
        blocks.append(f'<file path="{path}">\n{content}\n</file>')
        return token

    text = AT_FILE_TOKEN.sub(replace, text)
    if blocks:
        text = text.rstrip() + "\n\n" + "\n\n".join(blocks)
    return text, (images or None), notices


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
    message, at_images, at_notices = expand_at_files(message)
    for notice in at_notices:
        print(f"@ 附件：{notice}", file=sys.stderr)
    if at_images:
        images = [*(images or []), *at_images]
    summary = stream_run(
        state.runnable, message, state.session_id, state.approved, emitter,
        user_id=_user_id(state), strategy=_approval_strategy(state),
        channel=state.provider,
        review_root=state.review_root(), images=images,
        memory=state.memory if isinstance(state.memory, MemoryHost) else None,
        tool_allows=getattr(state, "tool_allows", None),
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




APPROVAL_ARM_SECONDS = 0.5


class ApprovalSelection:
    """审批选择状态：新审批重置选中项，并给挂载保护期防止误确认。"""

    def __init__(self) -> None:
        self.index = 0
        self.current = None
        self.armed_until = 0.0

    def sync(self, approval, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        if approval is not self.current:
            self.current = approval
            self.index = 0
            self.armed_until = now + APPROVAL_ARM_SECONDS

    def move(self, delta: int, count: int = 2) -> None:
        self.index = (self.index + delta) % count

    def ready(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return now >= self.armed_until


def enter_action(buffer: Buffer, data: Optional[str], commands: Optional[set] = None) -> None:
    """Shift+Enter 只换行；补全按 omp 语义：选中过才接受，命令名随后提交。"""
    if is_shift_enter(data):
        buffer.insert_text("\n")
        return
    state = buffer.complete_state
    if state is not None and state.completions:
        current = state.current_completion
        if current is not None:
            buffer.apply_completion(current)
            selected = current.text or ""
            if commands is not None and selected.startswith("/") and selected[1:] in commands:
                buffer.validate_and_handle()
            return
        buffer.cancel_completion()
    buffer.validate_and_handle()


def approval_enter(buffer: Buffer, data: Optional[str], ready: bool, selected: bool, answer) -> None:
    """审批待决时的 Enter：Shift+Enter 只换行；草稿非空先拒绝并发送这条消息。"""
    if is_shift_enter(data):
        buffer.insert_text("\n")
        return
    if buffer.text.strip():
        answer(False)
        buffer.validate_and_handle()
        return
    if ready:
        answer(selected)


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
    if os.name == "nt":
        # Windows 按控制台代码页（cp1252/gbk 等）编码重定向输出，中文界面会直接抛错；
        # 控制台本身走 WriteConsoleW，不受影响。统一改用 UTF-8。
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError, OSError):
                pass
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
    trusted = trust_mod.ensure(project, interactive=options.interactive, always=options.trust_repo)

    try:
        state = AppState.start(config, settings, trusted=trusted)
    except MemoryEngineError as exc:
        print(f"{GLYPH['fail']} {exc}", file=sys.stderr)
        print("用 /memory（或 /memory engine <引擎>）重新选择记忆引擎后重启；也可以直接改 ~/.aaa/config.toml。", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    try:
        _run_application(state, options, project=project, trusted=trusted)
    finally:
        state.close_memory()
        clear_credentials(state.runnable)


def _completion_float(container):
    """找到 PromptSession 自带的补全浮层，换成自建渲染前先把它关掉。"""
    from prompt_toolkit.layout import FloatContainer
    from prompt_toolkit.layout.menus import CompletionsMenu, MultiColumnCompletionsMenu

    if isinstance(container, FloatContainer):
        for item in container.floats:
            content = item.content
            while isinstance(content, ConditionalContainer):
                content = content.content
            if isinstance(content, (CompletionsMenu, MultiColumnCompletionsMenu)):
                return item
    for child in container.get_children():
        found = _completion_float(child)
        if found is not None:
            return found
    return None


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
            mode=state.mode,
        ))

    def panel_lines() -> FormattedText:
        """扩展面板插槽：活动面板的渲染行；渲染异常降级为一行提示。"""
        registry = getattr(state, "extension_registry", None)
        name = getattr(registry, "active_panel", None)
        spec = registry.panel(name) if (registry is not None and name) else None
        if spec is None:
            return FormattedText([])
        _, model = state.selection()
        view = {
            "mode": state.mode,
            "model": model.id,
            "session_id": state.session_id,
            "cwd": os.getcwd(),
        }
        try:
            lines = list(spec.render(view))[:8]
        except Exception:
            logger.exception("扩展面板 %s 渲染失败", spec.name)
            lines = [f"面板 {spec.name} 渲染失败（详见日志）"]
        return FormattedText([("class:status.hint", f" {spec.title}│ {ln}") for ln in lines])

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
        approval_ui = ApprovalSelection()
        approval_choices = (("批准", True), ("本会话总是允许", ALWAYS_ALLOW), ("拒绝", False))

        def approval_active() -> bool:
            return controller is not None and controller.approval is not None

        approval_filter = Condition(approval_active)

        def sync_approval() -> None:
            approval_ui.sync(controller.approval if controller is not None else None)

        def approval_rows() -> FormattedText:
            sync_approval()
            approval = controller.approval
            name = sanitize(approval.name) if approval is not None else ""
            fragments = [("class:picker.title", f"审批 {name}")]
            fragments.append(("", "\n"))
            for index, (label, _) in enumerate(approval_choices):
                selected = index == approval_ui.index
                marker = GLYPH["pointer"] + " " if selected else "  "
                style = "class:picker.selected" if selected else "class:picker.description"
                fragments.append(("class:picker.pointer" if selected else "", marker))
                fragments.append((style, label))
                fragments.append(("", "   "))
            fragments.append(("", "\n"))
            fragments.append((
                "class:picker.hint",
                "↑↓ 选择 · Enter 确认 · Esc 拒绝",
            ))
            return FormattedText(fragments)

        @kb.add("up", filter=approval_filter, eager=True)
        def _(event) -> None:
            sync_approval()
            approval_ui.move(-1, len(approval_choices))

        @kb.add("down", filter=approval_filter, eager=True)
        def _(event) -> None:
            sync_approval()
            approval_ui.move(1, len(approval_choices))

        @kb.add("enter", filter=approval_filter, eager=True)
        def _(event) -> None:
            sync_approval()
            data = getattr(event.key_sequence[-1], "data", None) if event.key_sequence else None
            approval_enter(
                event.current_buffer,
                data,
                approval_ui.ready(),
                approval_choices[approval_ui.index][1],
                controller.answer,
            )

        @kb.add("escape", filter=approval_filter, eager=True)
        def _(event) -> None:
            controller.answer(False)

        @kb.add("c-c", filter=approval_filter, eager=True)
        def _(event) -> None:
            buffer = event.current_buffer
            if buffer.text:
                buffer.reset()
            else:
                controller.answer(False)

        @kb.add("pageup", filter=approval_filter, eager=True)
        @kb.add("pagedown", filter=approval_filter, eager=True)
        def _(event) -> None:
            """审批期间不把 PageUp/PageDown 交给历史，避免替换草稿。"""

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
                text = "↑↓ 选择 · Enter 确认 · Esc 拒绝"
            elif controller.busy:
                text = "Esc 打断并发送 · Enter 排队" if session.default_buffer.text.strip() else "Esc 打断"
            else:
                text = "Shift+Tab 切换权限"
            return FormattedText([("class:status.hint", " " + text)])

        def invalidate() -> None:
            session.app.invalidate()

        @kb.add("c-o", filter=~approval_filter)
        def _(event) -> None:
            if not tool_details.toggle():
                print(f"{GLYPH['dot']} 暂无工具详情。")
            elif tool_details.expanded and detail_height() == 0:
                tool_details.toggle()
                print(f"{GLYPH['dot']} 窗口太矮：收起草稿或放大窗口后再按 Ctrl-O。")
            event.app.invalidate()

        details_open = Condition(lambda: tool_details.expanded) & ~approval_filter

        @kb.add("pageup", filter=details_open)
        def _(event) -> None:
            tool_details.scroll(-1)
            event.app.invalidate()

        @kb.add("pagedown", filter=details_open)
        def _(event) -> None:
            tool_details.scroll(1)
            event.app.invalidate()

        @kb.add("escape", filter=~approval_filter)
        def _(event) -> None:
            if event.current_buffer.complete_state:
                event.current_buffer.cancel_completion()
            elif tool_details.expanded:
                tool_details.toggle()
                event.app.invalidate()
            elif controller.busy:
                draft = event.current_buffer.text
                if draft.strip() and controller.phase != "stopping":
                    controller.stop_and_send(draft)
                    event.current_buffer.reset()
                else:
                    controller.cancel()


        @kb.add("s-tab", filter=~approval_filter)
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

        @kb.add("c-j", filter=~approval_filter)
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @kb.add("enter", filter=~approval_filter)
        def _(event) -> None:
            data = getattr(event.key_sequence[-1], "data", None) if event.key_sequence else None
            known = {item[0][1:] for item in COMMANDS} | set(state.plugin_commands)
            enter_action(event.current_buffer, data, known)

        cleared: List[str] = []
        last_interrupt = [0.0]

        @kb.add("c-c", filter=~approval_filter)
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

        @kb.add("up", filter=~approval_filter)
        def _(event) -> None:
            buffer = event.current_buffer
            if buffer.complete_state:
                buffer.complete_previous()
                return
            if buffer.text or not cleared:
                buffer.history_backward()
                return
            buffer.insert_text(cleared.pop())

        @kb.add("c-g", filter=~approval_filter)
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

        @kb.add("escape", "up", filter=~approval_filter)
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

        @kb.add("c-d", filter=~approval_filter)
        def _(event) -> None:
            if event.current_buffer.text:
                event.current_buffer.delete()
            elif not controller.busy:
                event.app.exit(exception=EOFError())

        cli_output = create_output()
        depth = banner_view._color_depth(cli_output)
        if depth is not None:
            try:
                cli_output.default_color_depth = depth
            except AttributeError:
                pass
        session = PromptSession(
            input=terminal_input,
            output=cli_output,
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
        completion_open = Condition(lambda: session.default_buffer.complete_state is not None)
        builtin_menu = _completion_float(session.layout.container)
        if builtin_menu is not None:
            # 自建菜单替换自带浮层：需要指针、对齐列与滚动条。
            builtin_menu.content = Window(FormattedTextControl(lambda: FormattedText([])), height=0)

        def menu_rows():
            complete_state = session.default_buffer.complete_state
            if complete_state is None or not complete_state.completions:
                return []
            return completion_rows(
                complete_state.completions,
                complete_state.complete_index,
                session.app.output.get_size().columns,
                int(settings.get("ui.completion_rows")),
            )

        def menu_text():
            rows = menu_rows()
            fragments = []
            for index, row in enumerate(rows):
                fragments.extend(row)
                if index < len(rows) - 1:
                    fragments.append(("", "\n"))
            return FormattedText(fragments)

        @kb.add("pageup", filter=completion_open & ~details_open & ~approval_filter)
        def _(event) -> None:
            event.current_buffer.complete_previous(count=5)

        @kb.add("pagedown", filter=completion_open & ~details_open & ~approval_filter)
        def _(event) -> None:
            event.current_buffer.complete_next(count=5)

        def layout_rows() -> tuple[int, int]:
            size = session.app.output.get_size()
            above = 1 + bool(activity()) + (3 if controller and controller.approval else 0)
            # Leave a terminal line outside the active area; long drafts scroll in the editor.
            available = max(1, size.rows - above - 1 - len(menu_rows()))
            draft_rows = editor.content.preferred_height(
                max(1, size.columns), available, wrap_lines=True, get_line_prefix=editor.get_line_prefix,
            ) or 1
            return draft_rows, available - draft_rows

        session.app.before_render += lambda app: sync_approval()

        def pane_rows() -> int:
            return max(0, layout_rows()[1])

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
        approval_view = ConditionalContainer(
            Window(FormattedTextControl(approval_rows), height=3, wrap_lines=False),
            filter=~is_done & approval_filter,
        )
        panel_height = Condition(lambda: bool(panel_lines()))
        panel_view = ConditionalContainer(
            Window(
                FormattedTextControl(panel_lines),
                height=lambda: max(len(panel_lines()), 1), wrap_lines=False,
            ),
            filter=~is_done & panel_height,
        )
        status_view = ConditionalContainer(
            Window(
                FormattedTextControl(lambda: status_line(session.app.output.get_size().columns)),
                height=1, wrap_lines=False,
            ),
            filter=~is_done,
        )
        menu_view = ConditionalContainer(
            Window(FormattedTextControl(menu_text), height=lambda: len(menu_rows()), wrap_lines=False),
            filter=~is_done & completion_open,
        )
        session.layout.container = HSplit(
            [
                todo_view, detail_view, approval_view, activity_view, panel_view, status_view,
                session.layout.container, menu_view,
            ],
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
                    tool_allows=getattr(state, "tool_allows", None),
                )
                if not summary.get("error") and not summary.get("cancelled") and not handle["cancel_requested"]:
                    selected_channel, model = state.selection()
                    ensure_session_name(
                        agent, selected_channel, model, session_id, text, named,
                        GLYPH["title"], state.settings,
                    )
                return summary

            tool_details.clear()
            controller.start(worker, text)

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
                    tool_allows=getattr(state, "tool_allows", None),
                )

            tool_details.clear()
            controller.start(worker, kwargs.get("input"))


        def is_report(text: str) -> bool:
            """只读报告命令：输出进面板，不写滚动历史。"""
            if text in ("/help", "/todos", "/skills", "/insight", "/trust"):
                return True
            name = text[1:].split(None, 1)[0] if text.startswith("/") else ""
            return name == "usage"

        def run_report(text: str) -> bool:
            buffer = io.StringIO()
            state.report_lines = []
            try:
                with redirect_stdout(buffer):
                    handled = handle_command(state, text)
                collected = state.report_lines or []
            finally:
                state.report_lines = None
            lines = collected or buffer.getvalue().rstrip("\n").split("\n")
            if handled and lines and lines != [""] and sys.stdout.isatty():
                try:
                    report_panel.show(text, lines, settings.get("ui.theme"))
                    return handled
                except Exception:
                    logger.exception("report panel failed")
            for item in lines:
                print(item[0] if isinstance(item, tuple) else item)
            return handled

        prefill: List[str] = []

        def fork_command(text: str) -> None:
            """/fork：回到某一轮之前，当前会话分叉成新会话并切过去。"""
            argument = text[len("/fork"):].strip()
            number, message = None, ""
            if argument:
                parts = argument.split(None, 1)
                if not parts[0].isdigit():
                    print(f"用法：{GLYPH['prompt']} /fork [轮次] [新消息]")
                    return
                number = int(parts[0])
                message = parts[1].strip() if len(parts) > 1 else ""
            turns = user_turns(state.runnable, state.session_id)
            if not turns:
                print(f"{GLYPH['dot']} 本会话还没有可回到的轮次。")
                return
            if number is None:
                picked = picker.choose(
                    "回到哪一句之前",
                    [picker.Item(turn, f"{turn.number}  {' '.join(turn.text.split())}") for turn in turns],
                    subtitle="Enter 选择 · Esc 取消", theme=str(settings.get("ui.theme")),
                )
                if picked in (picker.BACK, picker.ABORT):
                    return
                turn = picked
            else:
                turn = next((item for item in turns if item.number == number), None)
                if turn is None:
                    print(f"{GLYPH['fail']} 没有第 {number} 轮；可用的轮次是 1–{turns[-1].number}。")
                    return
            if turn.number == 1:
                state.select_session(None)
                print(f"{GLYPH['ok']} 已新建会话 {state.session_id}，回到第 1 轮之前。")
            else:
                new_id = fork_before(state.runnable, state.session_id, turn.run_id, user_id=_user_id(state))
                state.select_session(find_session(state.runnable, new_id, state.settings))
                print(f"{GLYPH['ok']} 已分叉到会话 {state.session_id}，回到第 {turn.number} 轮之前；原会话保持不变。")
            if message:
                start_turn(message)
            else:
                prefill.append(turn.text)

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
                if is_report(text):
                    run_report(text)
                elif text == "/memory":
                    show_memory_status(state)
                elif not text.startswith("/") or (name in state.plugin_commands and name not in builtin):
                    controller.enqueue(text)
                else:
                    print(f"{GLYPH['dot']} 运行中不能执行 {text}，请空闲后重试。")
                return


            handled = run_report(text) if is_report(text) else handle_command(state, text)
            if handled:
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
            if text == "/fork" or text.startswith("/fork "):
                fork_command(text)
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
            text, at_images, at_notices = expand_at_files(text)
            for notice in at_notices:
                print(f"{GLYPH['dot']} {notice}")
            start_turn(text, at_images)

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
        draft = ""
        try:
            while not controller.shutdown:
                if pending is not None:
                    text, pending = pending, None
                    print(EDITOR_PROMPT + text)
                else:
                    try:
                        text = await session.prompt_async(
                            FormattedText([("class:prompt", EDITOR_PROMPT)]), default=draft,
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
                draft = prefill.pop(0) if prefill else ""
        finally:
            if controller.busy:
                controller.request_exit()
                await controller.done.wait()
            git_task.cancel()
            renderer.close()

    try:
        with extended_keys(), patch_stdout(), closing(create_cli_input()) as terminal_input:
            asyncio.run(interactive(terminal_input))
    finally:
        reset_cursor_color()
