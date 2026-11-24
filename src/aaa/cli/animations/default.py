"""默认启动横幅：6 行块状 `A` 标识 + 半块 `AAA` 字标，沿用 omp 的渐变旋转与高光扫过。"""

from typing import Callable, Iterator

from . import Frame, FrameContext

_GAP = "   "

# 6 行 × 12 列的块状 `A`：整块笔画在矮终端上也不糊，与 omp 的单字母 mark 同构。
_MARK = (
    "    ████    ",
    "   ██████   ",
    "  ███  ███  ",
    " ████  ████ ",
    "████████████",
    "███      ███",
)

# 半块字标，宽 14、高 3；与 omp 的 wordmark 同一套字符语言。
_WORDMARK = (
    "▄▀▀▄ ▄▀▀▄ ▄▀▀▄",
    "█▀▀█ █▀▀█ █▀▀█",
    "█  █ █  █ █  █",
)

# omp 同款三站渐变：品红 → 紫 → 青。
_GRADIENT_STOPS = (
    (248, 79, 204),
    (147, 98, 244),
    (0, 219, 228),
)
_SHINE_HALF_WIDTH = 0.18
_RESET = "\x1b[0m"
_DIM = "\x1b[90m"
_BOLD = "\x1b[1m"

_ICON_WIDTH = len(_MARK[0])
_WORDMARK_WIDTH = len(_WORDMARK[0])
_TEXT_OFFSET = _ICON_WIDTH + len(_GAP)
_WIDTH = _TEXT_OFFSET + max(_WORDMARK_WIDTH, 8)
_ROWS = len(_MARK)
_HEIGHT = _ROWS
_VERSION_ROW = _HEIGHT - 1
_WORDMARK_TOP = 0


def _gradient(t: float, shine: float = 0.0, shine_pos: float = 0.0) -> str:
    """斜向渐变取色；`t` 已按 omp 的规则归一，`shine` 强度 > 0 时向白色混合出高光带。"""
    seg = t * (len(_GRADIENT_STOPS) - 1)
    index = min(len(_GRADIENT_STOPS) - 2, int(seg))
    fraction = seg - index
    start, end = _GRADIENT_STOPS[index], _GRADIENT_STOPS[index + 1]
    red = start[0] + (end[0] - start[0]) * fraction
    green = start[1] + (end[1] - start[1]) * fraction
    blue = start[2] + (end[2] - start[2]) * fraction
    if shine > 0:
        distance = abs(t - shine_pos)
        intensity = max(0.0, 1.0 - distance / _SHINE_HALF_WIDTH) * shine
        if intensity > 0:
            red += (255 - red) * intensity
            green += (255 - green) * intensity
            blue += (255 - blue) * intensity
    return f"\x1b[38;2;{round(red)};{round(green)};{round(blue)}m"


def _paint_art(chars: str, y: int, phase: float, shine: float, shine_pos: float) -> str:
    """给一行图标上渐变；分母用图标自身格子数，与 omp 的 `Gw` 一致。"""
    out = []
    x_span = max(1, _ICON_WIDTH - 1)
    y_span = max(1, _HEIGHT - 1)
    for x, char in enumerate(chars):
        if char == " ":
            out.append(char)
            continue
        base = (x / x_span + y / y_span) / 2
        # 与 omp 相同：静止帧不取模，右下落点保持渐变终点而不是跳回起点。
        t = base if phase == 0 else (base + phase) % 1.0
        out.append(_gradient(t, shine, shine_pos) + char + _RESET)
    return "".join(out)


def _render(version: str, columns: int, phase: float, shine: float = 0.0, shine_pos: float = 0.0) -> str:
    margin = " " * max(0, (columns - _WIDTH) // 2)
    lines = []
    for row, icon in enumerate(_MARK):
        left = _paint_art(icon, row, phase, shine, shine_pos)
        right = ""
        if _WORDMARK_TOP <= row < _WORDMARK_TOP + len(_WORDMARK):
            # 与 omp 一致：只有图标走渐变，字标用加粗的终端前景色。
            right = f"{_BOLD}{_WORDMARK[row - _WORDMARK_TOP]}{_RESET}"
        elif row == _VERSION_ROW:
            right = f"{_DIM}v{version}{_RESET}"
        lines.append(margin + left + (_GAP + right if right else ""))
    return "\n".join(lines)


def _eased(progress: float) -> float:
    """omp 的 ease-out cubic：冲进静止帧时减速。"""
    return 1 - (1 - progress) ** 3


def _pose(progress: float) -> tuple[float, float, float]:
    """(渐变相位, 高光强度, 高光位置)；进度 1 恰好落回静止帧。"""
    eased = _eased(max(0.0, min(1.0, progress)))
    phase = (((1 - eased) * 2.5) % 1 + 1) % 1
    shine_pos = ((progress * 3) % 1 + 1) % 1
    shine = (1 - eased) ** 1.5
    return phase, shine, shine_pos


class _DefaultAnimation:
    min_columns = _WIDTH + 1
    min_rows = _HEIGHT + 1

    def frames(self, ctx: FrameContext) -> Iterator[Frame]:
        count = max(1, round(ctx.duration * 30))
        delay = ctx.duration / count
        for index in range(count):
            progress = (index + 1) / count
            phase, shine, shine_pos = _pose(progress)
            yield Frame(
                delay,
                lambda columns, phase=phase, shine=shine, pos=shine_pos: _render(
                    ctx.version, columns, phase, shine, pos
                ),
            )

    def static(self, ctx: FrameContext) -> Callable[[int], str]:
        return lambda columns: _render(ctx.version, columns, 0.0)


ANIMATION = _DefaultAnimation()
