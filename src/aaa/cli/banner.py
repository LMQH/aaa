"""启动横幅宿主：选择动画、判定空间、调度帧，然后交还编辑器。

动画本身在 `cli.animations`；这里不读键盘、不清屏，渲染失败也不阻断启动。
"""

import argparse
import asyncio
import logging
import math
import os
import sys
import time
import traceback
from bisect import bisect_right
from itertools import accumulate
from pathlib import Path
from typing import Optional

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.output import ColorDepth, Output, create_output
from prompt_toolkit.output.vt100 import Vt100_Output
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.utils import get_cwidth

from .animations import (
    DEFAULT,
    AnimationError,
    FrameContext,
    frames_of,
    limits,
    load,
    paint,
    static_of,
    version as package_version,
)
from .render import sanitize

logger = logging.getLogger(__name__)


def _plain_title(version: str, columns: Optional[int] = None) -> str:
    title = f"AAA  v{version}"
    if columns is None:
        return title
    title = title[:max(1, columns - 1)]
    return " " * max(0, (columns - len(title)) // 2) + title


def _fits(animation, columns: int, rows: int) -> bool:
    """动画自报的最小尺寸；不足时由宿主退化为单行标题。"""
    min_columns, min_rows = limits(animation)
    return columns >= min_columns and rows >= min_rows


def _width_ok(text: str, columns: int) -> bool:
    return all(get_cwidth(line) <= columns for line in sanitize(text).splitlines())


def _draw(output: Output, text: str, color_depth: ColorDepth) -> None:
    print_formatted_text(ANSI(text), output=output, color_depth=color_depth)


def _color_depth(output: Output) -> Optional[ColorDepth]:
    if sys.platform == "win32":
        from prompt_toolkit.output.win32 import Win32Output

        if isinstance(output, Win32Output):
            return None  # Legacy console attributes have only 16 colors.
    depth = output.get_default_color_depth()
    if depth in (ColorDepth.DEPTH_1_BIT, ColorDepth.DEPTH_4_BIT):
        return None
    if not isinstance(output, Vt100_Output):
        # The native Windows output already probes VT instead of relying on TERM.
        return depth

    term = os.environ.get("TERM", "")
    explicit = ColorDepth.from_env()
    direct = term.endswith("-direct") or os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    colors = None
    if term:
        try:
            import curses
        except ImportError:
            pass
        else:
            try:
                curses.setupterm(term)
            except curses.error:
                pass
            else:
                colors = curses.tigetnum("colors")
    if colors is not None and colors < 256:
        return None
    if colors is None and not (direct or term.endswith("-256color") or explicit):
        return None
    if explicit is not None:
        return explicit
    return ColorDepth.DEPTH_24_BIT if direct or (colors or 0) >= 1 << 24 else depth


def animate(duration: Optional[float] = None, animation=None, dirs=None) -> None:
    """用内联渲染器重绘横幅，不清除终端滚动历史；`dirs` 为项目级动画目录。"""
    name = DEFAULT
    if animation is None or duration is None:
        from ..config.settings import load_settings

        settings = load_settings()
        if animation is None:
            name = settings.get("ui.banner_animation")
        if duration is None:
            duration = float(settings.get("ui.banner_duration"))

    if animation is None:
        try:
            animation = load(name, dirs=dirs)
        except AnimationError as exc:
            logger.warning("启动动画 %s 不可用：%s", name, exc)
            try:
                animation = load(DEFAULT, dirs=dirs)
            except AnimationError:
                animation = None

    version = package_version()
    if animation is None:
        print(_plain_title(version))
        return
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        print(_plain_title(version))
        return
    output = create_output()
    color_depth = _color_depth(output)
    if color_depth is None:
        print(_plain_title(version))
        return

    if not math.isfinite(duration):
        duration = 0.0
    duration = max(0.0, duration)

    size = output.get_size()
    if not _fits(animation, size.columns, size.rows):
        print(_plain_title(version, size.columns))
        return

    ctx = FrameContext(columns=size.columns, rows=size.rows, duration=duration, version=version)
    try:
        static = static_of(animation, ctx)
        plan = list(frames_of(animation, ctx))
    except AnimationError as exc:
        logger.warning("启动动画 %s 不符合契约：%s", name, exc)
        print(_plain_title(version, size.columns))
        return
    if not plan:
        print(_plain_title(version, size.columns))
        return

    if duration <= 0:
        text = paint(static, size.columns)
        if not _width_ok(text, size.columns):
            logger.warning("启动动画 %s 输出了超宽行", name)
            print(_plain_title(version, size.columns))
            return
        # At the top-left cell, tmux archives the screen on the renderer's erase-down.
        # Start below a blank line so erasing the animation never archives a frame.
        output.write("\r\n")
        output.flush()
        _draw(output, text, color_depth)
        return

    # Keep the erase-down of the animation from archiving a frame in tmux.
    output.write("\r\n")
    output.flush()

    deadlines = tuple(accumulate(frame.delay for frame in plan))
    started = 0.0
    interrupted = False

    def fragments() -> ANSI:
        nonlocal interrupted
        viewport = output.get_size()
        if interrupted or not _fits(animation, viewport.columns, viewport.rows):
            interrupted = True
            return ANSI("")
        # Redraws are queued; choose the pose at draw time, not at invalidation time.
        index = bisect_right(deadlines, time.monotonic() - started)
        content = plan[index].content if index < len(plan) else static
        text = paint(content, viewport.columns)
        if not _width_ok(text, viewport.columns):
            logger.warning("启动动画 %s 输出了超宽行，已中止", name)
            interrupted = True
            return ANSI("")
        return ANSI(text)

    with create_pipe_input() as idle_input:
        app = Application(
            layout=Layout(Window(FormattedTextControl(fragments), dont_extend_height=True, wrap_lines=False)),
            input=idle_input, output=output, color_depth=color_depth, full_screen=False,
        )

        async def play() -> None:
            last_requested = 0
            while not interrupted:
                elapsed = time.monotonic() - started
                index = bisect_right(deadlines, elapsed)
                if index == len(deadlines):
                    break
                if index != last_requested:
                    last_requested = index
                    app.invalidate()
                await asyncio.sleep(max(0.0, deadlines[index] - elapsed))
            app.exit()

        def start_playback() -> None:
            nonlocal started
            started = time.monotonic()
            app.create_background_task(play())

        app.run(pre_run=start_playback)

    if interrupted:
        print(_plain_title(version, output.get_size().columns))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m aaa.cli.banner", description="预览启动动画")
    parser.add_argument("--animation", help="动画名，或动画文件路径")
    parser.add_argument("--duration", type=float, help="播放时长（秒），默认取配置")
    args = parser.parse_args(argv)
    animation = None
    if args.animation:
        path = Path(args.animation).expanduser()
        try:
            animation = load(path.stem, path) if path.is_file() else load(args.animation)
        except AnimationError:
            traceback.print_exc()
            return 1
    animate(duration=args.duration, animation=animation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
