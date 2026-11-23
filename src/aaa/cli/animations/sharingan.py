"""启动动画：圆形写轮眼的像素标识与三阶段动效。

只决定“画什么”；终端调度、空间降级与异常兜底由 `cli.banner` 宿主负责。
"""

import base64
import colorsys
import functools
import math
import zlib
from typing import Callable, Iterator, List, Optional, Tuple

from . import Frame, FrameContext, version as package_version

RESET = "\x1b[0m"

EYES: List[Tuple[str, int, str]] = [
    ('三勾玉写轮眼', 56, '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'),
    ('宇智波佐助 万花筒写轮眼', 56, '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'),
    ('宇智波佐助 永恒万花筒', 56, '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'),
]


CENTER = 27.5
# 56×56 素材的圆框位于 r≈26–27；外缘抗锯齿截止于画布内切圆。
_RING_INNER, _EDGE_INNER, _EDGE_OUTER = 25.0, 27.5, 28.0
_ICON_COLUMNS = 56
_GAP = "   "
_WORDMARK = tuple(" ".join([line] * 3) for line in (
    "  ▄██▄  ", " ▄████▄ ", "██    ██", "██    ██", "████████", "██    ██", "██    ██",
    "      ██",
))

RGB = Tuple[int, int, int]


@functools.lru_cache(maxsize=8)
def _raw(data: str) -> bytes:
    raw = bytearray(zlib.decompress(base64.b64decode(data)))
    for y in range(56):
        for x in range(56):
            if math.hypot(x - CENTER, y - CENTER) >= _EDGE_OUTER:
                offset = (y * 56 + x) * 3
                raw[offset:offset + 3] = b"\0\0\0"
    return bytes(raw)


def _mix(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def _pixel(raw: bytes, x: int, y: int) -> Tuple[int, int, int]:
    if not (0 <= x < 56 and 0 <= y < 56):
        return (0, 0, 0)
    offset = (y * 56 + x) * 3
    return tuple(raw[offset:offset + 3])  # type: ignore[return-value]


def _sample(raw: bytes, x: float, y: float, angle: float, scale: float) -> Tuple[int, int, int]:
    """双线性采样：目标像素按 angle/scale 反映射回源图。"""
    dx, dy = x - CENTER, y - CENTER
    ca, sa = math.cos(-angle), math.sin(-angle)
    sx = (dx * ca - dy * sa) / scale + CENTER
    sy = (dx * sa + dy * ca) / scale + CENTER
    x0, y0 = int(math.floor(sx)), int(math.floor(sy))
    fx, fy = sx - x0, sy - y0
    p00, p10 = _pixel(raw, x0, y0), _pixel(raw, x0 + 1, y0)
    p01, p11 = _pixel(raw, x0, y0 + 1), _pixel(raw, x0 + 1, y0 + 1)
    out = []
    for channel in range(3):
        top = p00[channel] * (1 - fx) + p10[channel] * fx
        bottom = p01[channel] * (1 - fx) + p11[channel] * fx
        out.append(round(top * (1 - fy) + bottom * fy))
    return tuple(out)  # type: ignore[return-value]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


@functools.lru_cache(maxsize=8)
def _black_layers(source: str, target: str) -> Tuple[List[List[bool]], List[List[bool]], List[List[int]]]:
    """Tomoe black, Mangekyo black, and each pixel's distance from the tomoe."""
    masks = []
    for data in (source, target):
        raw = _raw(data)
        masks.append([[(x - CENTER) ** 2 + (y - CENTER) ** 2 < _RING_INNER ** 2
                       and raw[(y * 56 + x) * 3] < 60 for x in range(56)] for y in range(56)])
    tomoe, final = masks
    distance = [[-1] * 56 for _ in range(56)]
    queue = [(x, y) for y in range(56) for x in range(56) if tomoe[y][x]]
    for x, y in queue:
        distance[y][x] = 0
    head = 0
    while head < len(queue):
        x, y = queue[head]
        head += 1
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < 56 and 0 <= ny < 56 and distance[ny][nx] < 0:
                distance[ny][nx] = distance[y][x] + 1
                queue.append((nx, ny))
    return tomoe, final, distance


@functools.lru_cache(maxsize=8)
def _red_profile(source: str) -> List[float]:
    """Red level by radius, taken from the three-tomoe art."""
    raw = _raw(source)
    totals, counts = [0.0] * 29, [0] * 29
    for y in range(56):
        for x in range(56):
            radius = int(math.hypot(x - CENTER, y - CENTER))
            value = raw[(y * 56 + x) * 3]
            if radius < 29 and value > 60:
                totals[radius] += value
                counts[radius] += 1
    profile = [total / count if count else 0.0 for total, count in zip(totals, counts)]
    for index in range(len(profile) - 2, -1, -1):
        if not profile[index]:
            profile[index] = profile[index + 1]
    return profile


def _coverage(mask: List[List[bool]], x: float, y: float) -> float:
    """Bilinear coverage of a boolean mask, so the moving edge gets real antialiasing."""
    x0, y0 = int(math.floor(x)), int(math.floor(y))
    if not (0 <= x0 < 55 and 0 <= y0 < 55):
        return 0.0
    fx, fy = x - x0, y - y0
    return (mask[y0][x0] * (1 - fx) * (1 - fy) + mask[y0][x0 + 1] * fx * (1 - fy)
            + mask[y0 + 1][x0] * (1 - fx) * fy + mask[y0 + 1][x0 + 1] * fx * fy)


def _spread(table: List[List[int]], x: float, y: float) -> float:
    """Bilinear distance from the tomoe, in pixels."""
    x0, y0 = int(math.floor(x)), int(math.floor(y))
    if not (0 <= x0 < 55 and 0 <= y0 < 55):
        return 0.0
    fx, fy = x - x0, y - y0
    return ((table[y0][x0] * (1 - fx) + table[y0][x0 + 1] * fx) * (1 - fy)
            + (table[y0 + 1][x0] * (1 - fx) + table[y0 + 1][x0 + 1] * fx) * fy)


def _morph_color(progress: float, distance: float, theta: float) -> RGB:
    """Black grows out of the tomoe into the Mangekyo gaps; red retreats into six petals."""
    source, target = EYES[0][2], EYES[1][2]
    tomoe, final, spread = _black_layers(source, target)
    x = CENTER + math.cos(theta) * distance
    y = CENTER + math.sin(theta) * distance
    grow = 26.0 * max(0.0, (progress - 0.10) / 0.90) ** 2
    profile = _red_profile(source)
    level = profile[min(len(profile) - 1, int(distance))]
    red = (round(level), round(level * 0.08), round(level * 0.06))

    reach = _clamp((grow - _spread(spread, x, y)) / 2 + 0.5)
    blackness = max(_coverage(tomoe, x, y), _coverage(final, x, y) * reach)
    color = _mix(red, (0, 0, 0), blackness)
    if progress > 0.78:
        # Hand the closing stretch to the real art; the two shapes already agree closely.
        settle = _ease(_clamp((progress - 0.78) / 0.17))
        sampled = _sample(_raw(target), x, y, 0.0, 1.0)
        color = _mix(color, sampled, settle)
    return color


def _eye_color(spec: dict, x: float, y: float) -> Optional[RGB]:
    distance = math.hypot(x - CENTER, y - CENTER)
    if distance >= _EDGE_OUTER:
        return None
    progress = spec.get("morph", 0.0)
    raw = _raw(EYES[1 if progress >= 1 else spec["stage"]][2])
    angle, scale = spec.get("angle", 0.0), spec.get("scale", 1.0)
    scaled = distance / max(scale, 1e-6)
    morphing = 0 < progress < 1 and scaled < _RING_INNER
    if morphing:
        color = _morph_color(progress, scaled, math.atan2(y - CENTER, x - CENTER) - angle)
    else:
        color = _sample(raw, x, y, angle, scale)
        if 0 < progress < 1:
            # The band outside the inner ring keeps its shape but still converges to the target.
            color = _mix(color, _sample(_raw(EYES[1][2]), x, y, angle, scale), _ease(_clamp(progress)))
    # 圆框留在源位置；只在靠近边缘时对固定圆框取样。
    rim_weight = max(0.0, min(1.0, distance - (_RING_INNER - 1)))
    rim = _sample(raw, x, y, 0.0, 1.0) if rim_weight else None
    if rim is not None and 0 < progress < 1:
        rim = _mix(rim, _sample(_raw(EYES[1][2]), x, y, 0.0, 1.0), _ease(_clamp(progress)))
    mix = spec.get("mix")
    if mix:
        other_raw = _raw(EYES[mix["stage"]][2])
        other = _sample(other_raw, x, y,
                        mix.get("angle", spec.get("angle", 0.0)), spec.get("scale", 1.0))
        color = _mix(color, other, mix["t"])
        if rim is not None:
            rim = _mix(rim, _sample(other_raw, x, y, 0.0, 1.0), mix["t"])
    glow = spec.get("glow", 1.0)
    if glow != 1.0:
        color = tuple(min(255, round(c * glow)) for c in color)  # type: ignore[assignment]
    if rim is not None:
        color = _mix(color, rim, rim_weight)
    opacity = spec.get("opacity", 1.0)
    if opacity != 1.0:
        color = tuple(round(c * opacity) for c in color)  # type: ignore[assignment]
    if distance > _EDGE_INNER:
        alpha = (_EDGE_OUTER - distance) / (_EDGE_OUTER - _EDGE_INNER)
        color = tuple(round(c * alpha) for c in color)
    return color


def _ansi_rgb(color: RGB, layer: int) -> str:
    return f"\x1b[{layer};2;{color[0]};{color[1]};{color[2]}m"


def _render_wordmark(progress: float) -> List[str]:
    """沿列推进一条亮色波；波峰进出字标时不改变静态渐变。"""
    width = len(_WORDMARK[0])
    radius = 5.0
    crest = (width - 1 + 2 * radius) * progress - radius
    saturation = [
        0.88 - 0.66 * _ease(max(0.0, 1.0 - abs(x - crest) / radius))
        for x in range(width)
    ]
    span = width - 1 + 2 * (len(_WORDMARK) - 1)
    rows = []
    for y, line in enumerate(_WORDMARK):
        cells = []
        for x, glyph in enumerate(line):
            if glyph == " ":
                cells.append(glyph)
                continue
            position = (x + 2 * y) / span
            red, green, blue = colorsys.hsv_to_rgb(0.88 - 0.38 * position, saturation[x], 1.0)
            cells.append(_ansi_rgb((round(255 * red), round(255 * green), round(255 * blue)), 38) + glyph)
        rows.append("".join(cells) + RESET)
    return rows


def _cell(upper: Optional[RGB], lower: Optional[RGB]) -> str:
    if upper is None:
        return " " if lower is None else _ansi_rgb(lower, 38) + "▄" + RESET
    top = _ansi_rgb(upper, 38)
    return top + (_ansi_rgb(lower, 48) if lower is not None else "") + "▀" + RESET


def _area_color(spec: dict, x: float, y: float, step: float) -> Optional[RGB]:
    if step == 1.0:
        return _eye_color(spec, x, y)
    if math.hypot(x - CENTER, y - CENTER) >= _EDGE_OUTER:
        return None
    # Supersample the source area so a one-pixel ring survives reduction.
    offset = step / 3
    red = green = blue = count = 0
    for sy in (y - offset, y, y + offset):
        for sx in (x - offset, x, x + offset):
            color = _eye_color(spec, sx, sy)
            if color is not None:
                r, g, b = color
                red += r
                green += g
                blue += b
                count += 1
    return round(red / count), round(green / count), round(blue / count)


def _render_eye(spec: dict, width: int) -> List[str]:
    step = 56 / width
    offsets = [(position + 0.5) * step - 0.5 for position in range(width)]
    if 0 < spec.get("morph", 0.0) < 1:
        # Hard threshold growth makes stair steps; average four sub-cell samples per pixel.
        quarter = step / 4
        def cell(x: float, y: float) -> Optional[RGB]:
            samples = [color for dx in (-quarter, quarter) for dy in (-quarter, quarter)
                       if (color := _eye_color(spec, x + dx, y + dy)) is not None]
            if not samples:
                return None
            return tuple(round(sum(s[i] for s in samples) / len(samples)) for i in range(3))  # type: ignore[return-value]
        pixels = [[cell(x, y) for x in offsets] for y in offsets]
    else:
        pixels = [[_area_color(spec, x, y, step) for x in offsets] for y in offsets]
    rows = []
    for y in range(0, width, 2):
        lower = pixels[y + 1] if y + 1 < width else [None] * width
        rows.append("".join(_cell(top, bottom) for top, bottom in zip(pixels[y], lower)))
    return rows


def render_frame(
    spec: dict, width: int = _ICON_COLUMNS, version: Optional[str] = None,
    *, columns: Optional[int] = None,
) -> str:
    """以终端显示列居中排布，圆外半格不绘制颜色。"""
    width = max(2, min(width, 56))
    version = version or package_version()
    brand = [*_WORDMARK, "", f"v{version}", "Amaterasu-Agno-Agent"]
    brand_width = max(map(len, brand))
    group_width = width + len(_GAP) + brand_width
    margin = " " * (max(0, columns - group_width) // 2) if columns is not None else ""
    brand[:len(_WORDMARK)] = _render_wordmark(spec.get("wordmark_progress", 0.0))
    eye = _render_eye(spec, width)
    height = max(len(eye), len(brand))
    eye_top = (height - len(eye)) // 2
    brand_top = (height - len(brand)) // 2
    return "\n".join(
        margin + (eye[row - eye_top] if eye_top <= row < eye_top + len(eye) else " " * width)
        + (_GAP + brand[row - brand_top] if brand_top <= row < brand_top + len(brand) else "")
        for row in range(height)
    )


def static_frame(
    stage: int = -1, width: int = _ICON_COLUMNS, version: Optional[str] = None,
    *, columns: Optional[int] = None,
) -> str:
    """静置画面：终态或指定阶段。"""
    index = len(EYES) - 1 if stage < 0 else stage % len(EYES)
    return render_frame({"stage": index}, width, version, columns=columns)


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


# 各阶段基准时长（秒）；实际播放按 ui.banner_duration 等比缩放。
_BASE = {"hold": 0.08, "spin": 0.38, "evolve": 0.80, "emerge": 0.30, "fade": 0.44}
_SPIN_FRAMES, _EVOLVE_FRAMES, _EMERGE_FRAMES, _FADE_FRAMES = 18, 32, 8, 14
_PLAYBACK_FPS = 18


def _step_specs(count: int, build) -> List[dict]:
    return [build((step + 1) / count) for step in range(count)]


def _turn(start: float, end: float, t: float, *, entering: float = 0.0, leaving: float = 0.0) -> float:
    """Interpolate degrees with matching angular velocities at a phase boundary."""
    rest = 1.0 - t
    return math.radians(start + (end - start) * _ease(t) + t * rest * (entering * rest - leaving * t))


def _timeline(duration: float) -> List[Tuple[float, dict]]:
    """(停留秒数, 帧规格)；动作：旋转 → 黑纹扩展、露出六瓣 → 脉冲 → 旋转淡化。"""
    scale = max(0.0, duration) / sum(_BASE.values())
    timeline: List[Tuple[float, dict]] = [(_BASE["hold"] * scale, {"stage": 0})]
    if scale <= 0:
        return timeline
    spin, evolve, emerge, fade = (max(0.0, _BASE[key] * scale) for key in ("spin", "evolve", "emerge", "fade"))

    seam_speed = 165.0  # degrees per second at nominal playback speed

    def frame_count(seconds: float, minimum: int) -> int:
        return max(minimum, math.ceil(seconds * _PLAYBACK_FPS))

    spin_frames = _step_specs(frame_count(spin, _SPIN_FRAMES), lambda t: {
        "stage": 0, "angle": _turn(0.0, 55.0, t, leaving=seam_speed * _BASE["spin"]),
    })
    evolve_frames = _step_specs(frame_count(evolve, _EVOLVE_FRAMES), lambda t: {
        "stage": 0, "angle": _turn(55.0, 120.0, t, entering=seam_speed * _BASE["evolve"]),
        "morph": t,
    })
    closing_start = evolve_frames[-1]["angle"]
    third_turn = math.radians(120.0)
    fade_end = math.tau - third_turn

    pulse_end = emerge / (emerge + fade)

    def closing_angle(t: float) -> float:
        return closing_start + (fade_end - closing_start) * _ease(t)

    def pulse_spec(t: float) -> dict:
        pulse = math.sin(math.pi * _ease(t))
        return {"stage": 1, "angle": closing_angle(pulse_end * t),
                "scale": 1.0 + 0.05 * pulse, "glow": 1.0 + 0.18 * pulse}

    def fade_spec(t: float) -> dict:
        angle = closing_angle(pulse_end + (1.0 - pulse_end) * t)
        # The incoming threefold motif has an equivalent pose one lobe ahead.
        return {"stage": 1, "angle": angle, "mix": {"stage": 2, "t": _ease(t), "angle": angle + third_turn}}

    emerge_frames = _step_specs(frame_count(emerge, _EMERGE_FRAMES), pulse_spec)
    fade_frames = _step_specs(frame_count(fade, _FADE_FRAMES), fade_spec)
    elapsed = timeline[0][0]
    for frames, seconds in ((spin_frames, spin), (evolve_frames, evolve),
                            (emerge_frames, emerge), (fade_frames, fade)):
        if not frames:
            continue
        delay = seconds / len(frames)
        for spec in frames:
            elapsed += delay
            spec["wordmark_progress"] = elapsed / duration
            timeline.append((delay, spec))
    return timeline


MIN_COLUMNS = _ICON_COLUMNS + len(_GAP) + max(map(len, _WORDMARK)) + 1
MIN_ROWS = max((_ICON_COLUMNS + 1) // 2, len(_WORDMARK) + 3) + 3


class Sharingan:
    """三勾玉 → 万花筒的三阶段启动动画。"""

    min_columns = MIN_COLUMNS
    min_rows = MIN_ROWS

    def frames(self, ctx: FrameContext) -> Iterator[Frame]:
        for delay, spec in _timeline(ctx.duration):
            yield Frame(delay, lambda columns, spec=spec: render_frame(
                spec, version=ctx.version, columns=columns))

    def static(self, ctx: FrameContext) -> Callable[[int], str]:
        return lambda columns: static_frame(version=ctx.version, columns=columns)


ANIMATION = Sharingan()

