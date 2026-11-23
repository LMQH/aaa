"""极简启动横幅：像素 α 静置，只渐显 AAA 字标。"""

from typing import Callable, Iterator

from . import Frame, FrameContext

_ALPHA = (
    ".............##.",
    "............###.",
    "............##..",
    ".....####..###..",
    "...#######.##...",
    "..####..#####...",
    ".####....####...",
    ".###......###...",
    ".###......###...",
    ".###.....####...",
    "..###...######..",
    "..####.####.###.",
    "...#######...###",
    ".....####.....##",
)
_HALF_BLOCKS = {(".", "."): " ", ("#", "."): "▀", (".", "#"): "▄", ("#", "#"): "█"}
_ICON = tuple(
    "\x1b[38;2;147;115;220m"
    + "".join(_HALF_BLOCKS[top, bottom] for top, bottom in zip(upper, lower))
    + "\x1b[0m"
    for upper, lower in zip(_ALPHA[::2], _ALPHA[1::2])
)
_GAP = "   "
_PROJECT = "Amaterasu Agno Agent"
_TEXT_OFFSET = len(_ALPHA[0]) + len(_GAP)
_BRIGHTNESS = (64, 76, 88, 100, 112, 124, 136, 148)


def _render(version: str, columns: int, brightness: int) -> str:
    version = f"v{version}"
    width = _TEXT_OFFSET + max(len(_PROJECT), len(version))
    margin = " " * max(0, (columns - width) // 2)
    labels = (
        f"\x1b[1;38;2;{brightness};{brightness};{brightness}mAAA\x1b[0m",
        f"\x1b[90m{version}\x1b[0m",
        f"\x1b[90m{_PROJECT}\x1b[0m",
    )
    return "\n".join(
        margin + icon + (_GAP + labels[row - 2] if 2 <= row <= 4 else "")
        for row, icon in enumerate(_ICON)
    )


class _DefaultAnimation:
    min_columns = _TEXT_OFFSET + len(_PROJECT) + 1
    min_rows = len(_ICON) + 2

    def frames(self, ctx: FrameContext) -> Iterator[Frame]:
        delay = ctx.duration / len(_BRIGHTNESS)
        for brightness in _BRIGHTNESS:
            yield Frame(delay, lambda columns, brightness=brightness: _render(ctx.version, columns, brightness))

    def static(self, ctx: FrameContext) -> Callable[[int], str]:
        return lambda columns: _render(ctx.version, columns, _BRIGHTNESS[-1])


ANIMATION = _DefaultAnimation()
