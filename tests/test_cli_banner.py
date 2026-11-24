"""启动横幅：品牌尺寸、圆外默认背景、分阶段动效与动画插件。"""

import base64
import math
import re
import sys
import tempfile
import textwrap
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.utils import get_cwidth

from aaa.cli import animations, banner
from aaa.cli.animations import sharingan

ANSI = re.compile(r"\x1b\[[0-9;]*m")
CELL = re.compile(
    r"\x1b\[38;2;(\d+);(\d+);(\d+)m"
    r"(?:\x1b\[48;2;(\d+);(\d+);(\d+)m)?([▀▄])\x1b\[0m|."
)


def eye_pixels(frame, width=56):
    """读取图标区实际绘制的半格；未绘制的半格为 None。"""
    pixels = {}
    for row, line in enumerate(frame.splitlines()[:(width + 1) // 2]):
        for x, match in zip(range(width), CELL.finditer(line)):
            r, g, b, br, bg, bb, glyph = match.groups()
            fg = (int(r), int(g), int(b)) if r is not None else None
            back = (int(br), int(bg), int(bb)) if br is not None else None
            pixels[x, 2 * row] = back if glyph == "▄" else fg
            pixels[x, 2 * row + 1] = fg if glyph == "▄" else back
    return pixels



class BannerTest(unittest.TestCase):
    def test_assets_are_complete_56x56_frames(self) -> None:
        self.assertGreaterEqual(len(sharingan.EYES), 3)
        for name, height, data in sharingan.EYES:
            raw = zlib.decompress(base64.b64decode(data))
            self.assertEqual(len(raw), 56 * height * 3, name)
            self.assertEqual(height, 56, name)

    def test_final_banner_places_large_wordmark_beside_centered_eye(self) -> None:
        frame = sharingan.static_frame(columns=100, version="1.2.3")
        plain = ANSI.sub("", frame).splitlines()
        self.assertEqual(len(plain), 28)
        brand = [line[66:] for line in plain[8:19]]
        self.assertTrue(all("█" in line for line in brand[:8]))
        self.assertEqual(brand[7], "      ██       ██       ██")
        self.assertEqual(brand[8:], ["", "v1.2.3", "Amaterasu-Agno-Agent"])
        left = min(len(line) - len(line.lstrip()) for line in plain)
        right = max(map(len, plain))
        self.assertLessEqual(abs(left - (100 - right)), 1)

    def test_wordmark_wave_illuminates_the_three_letters_in_order(self) -> None:
        stroke_colors = re.compile(r"\x1b\[38;2;([\d;]+)m█")
        timeline = sharingan._timeline(2.0)
        base = sharingan.static_frame(version="test").splitlines()[12]
        colors = stroke_colors.findall(base)
        self.assertGreater(len(set(colors)), 3)
        for letter, progress in enumerate((0.25, 0.5, 0.75)):
            spec = min(timeline, key=lambda frame: abs(frame[1].get("wordmark_progress", 0.0) - progress))[1]
            wave = sharingan.render_frame(spec, version="test").splitlines()[12]
            moving = stroke_colors.findall(wave)
            self.assertEqual(ANSI.sub("", wave)[56:], ANSI.sub("", base)[56:])
            changed = {index // 8 for index, (before, after) in enumerate(zip(colors, moving)) if before != after}
            self.assertEqual(changed, {letter})

    def test_banner_duration_defaults_to_three_seconds_within_bounds(self) -> None:
        from aaa.config.settings import BY_PATH, Settings

        key = BY_PATH["ui.banner_duration"]
        self.assertEqual(key.default, 3.0)
        self.assertEqual((key.minimum, key.maximum), (0.3, 20.0))
        self.assertEqual(Settings().get("ui.banner_duration"), 3.0)

    def test_default_animation_is_the_omp_style_gradient_lockup(self) -> None:
        ctx = animations.FrameContext(columns=100, rows=30, duration=3.0, version="1.2.3")
        animation = animations.load(animations.DEFAULT)
        frames = list(animations.frames_of(animation, ctx))
        self.assertEqual(len(frames), 90)
        self.assertAlmostEqual(sum(frame.delay for frame in frames), 3.0, places=3)
        static = animations.paint(animations.static_of(animation, ctx), 100)
        first = animations.paint(frames[0].content, 100)
        last = animations.paint(frames[-1].content, 100)
        self.assertNotEqual(first, static)
        self.assertEqual(last, static)
        plain = ANSI.sub("", static).splitlines()
        self.assertEqual(plain[2].strip(), "..████..█████...   ▄▀▀▄ ▄▀▀▄ ▄▀▀▄")
        self.assertIn("v1.2.3", plain[6])
        self.assertIn("Amaterasu Agno Agent", plain[7])
        self.assertTrue(all(get_cwidth(re.sub(r"\x1b\[[0-9;]*m", "", line)) <= 100 for line in first.splitlines()))
        self.assertEqual(animations.limits(animation), (40, 12))
        self.assertTrue(banner._fits(animation, 40, 12))

    def test_narrow_or_short_terminal_degrades_to_the_plain_title(self) -> None:
        animation = sharingan.ANIMATION
        for columns, rows in ((85, 40), (100, 30), (40, 12)):
            with self.subTest(columns=columns, rows=rows):
                self.assertFalse(banner._fits(animation, columns, rows))
                title = banner._plain_title("1.2.3", columns)
                self.assertEqual(title.strip(), "AAA  v1.2.3")
                self.assertLessEqual(get_cwidth(title), columns)
        self.assertTrue(banner._fits(animation, 86, 31))

    def test_min_limits_match_the_single_line_fallback(self) -> None:
        self.assertEqual((sharingan.MIN_COLUMNS, sharingan.MIN_ROWS), (86, 31))

    def test_black_grows_out_of_the_tomoe_into_six_gaps(self) -> None:
        final_pixels = eye_pixels(sharingan.static_frame(stage=1))

        def frame(progress):
            return eye_pixels(sharingan.render_frame({"stage": 0, "morph": progress}))

        def at(pixels, radius, degrees):
            angle = math.radians(degrees)
            return pixels[(round(27.5 + radius * math.cos(angle)),
                           round(27.5 + radius * math.sin(angle)))][0]

        heads = (-60, 60, 180)
        start, early, late, settled = map(frame, (0.0, 0.30, 0.90, 0.99))
        for degrees in heads:                       # the tomoe themselves never move away
            self.assertLess(at(early, 17, degrees), 60)
        for degrees in (-30, 30, 90, 150, 210, 270):  # gaps have not eaten the petals yet
            self.assertGreater(at(early, 18, degrees), 90)
        agree = settled_agree = total = 0
        for y in range(56):                          # late frames follow the real art
            for x in range(56):
                if math.hypot(x - 27.5, y - 27.5) < 24:
                    total += 1
                    final_is_black = final_pixels[(x, y)][0] < 60
                    agree += (late[(x, y)][0] < 60) == final_is_black
                    # Settled frames average four sub-samples, so edge cells may blend.
                    settled_agree += (settled[(x, y)][0] < 60) == final_is_black
        self.assertGreater(agree / total, 0.97)
        self.assertGreater(settled_agree / total, 0.97)
        for pixels in (start, early, late):
            self.assertLess(pixels[27, 27][0], 25)

    def test_morph_uses_replaced_tomoe_art_at_the_same_size(self) -> None:
        spec = {"stage": 0, "morph": 0.30}
        with patch.object(sharingan, "EYES", sharingan.EYES.copy()):
            name, height, source = sharingan.EYES[0]
            pixels = eye_pixels(sharingan.render_frame(spec))
            self.assertLess(pixels[36, 13][0], 60)
            red = bytearray(zlib.decompress(base64.b64decode(source)))
            for y in range(6, 21):
                start = (y * 56 + 29) * 3
                red[start:start + 45] = bytes((210, 15, 12)) * 15
            replacement = base64.b64encode(zlib.compress(red)).decode("ascii")
            sharingan.EYES[0] = name, height, replacement
            pixels = eye_pixels(sharingan.render_frame(spec))
            self.assertGreater(pixels[36, 13][0], 150)

    def test_shape_near_the_handoff_matches_the_aligned_asset(self) -> None:
        pose = {"angle": math.radians(120)}
        early = eye_pixels(sharingan.render_frame({**pose, "stage": 0, "morph": 0.99}))
        settled = eye_pixels(sharingan.render_frame({**pose, "stage": 1}))
        compared = [(color, settled[point]) for point, color in early.items()
                    if color is not None and settled[point] is not None]
        self.assertGreater(len(compared), 2300)
        diffs = [abs(a[0] - b[0]) for a, b in compared]
        self.assertLessEqual(max(diffs), 90)                 # subpixel averaging at edges only
        self.assertLess(sum(diffs) / len(diffs), 20)

    def test_handoff_matches_asset_at_an_off_symmetry_angle(self) -> None:
        # 100° is not a multiple of the art's 120° symmetry, so a doubled rotation cannot hide here.
        angle = math.radians(100)
        for x, y in ((31, 40), (22, 33), (35, 18), (18, 20), (27, 44), (40, 27)):
            if math.hypot(x - 27.5, y - 27.5) >= 24:
                continue
            near = sharingan._eye_color({"stage": 0, "angle": angle, "morph": 0.999}, x, y)
            exact = sharingan._eye_color({"stage": 1, "angle": angle}, x, y)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(near, exact)), 12, (x, y))

    def test_spin_enters_morph_without_a_slowdown_and_hands_off_cleanly(self) -> None:
        frames = sharingan._timeline(2.0)
        first = next(i for i, (_, spec) in enumerate(frames) if "morph" in spec)
        self.assertAlmostEqual(math.degrees(frames[first - 1][1]["angle"]), 55.0)
        before = (frames[first - 1][1]["angle"] - frames[first - 2][1]["angle"]) / frames[first - 2][0]
        after = (frames[first][1]["angle"] - frames[first - 1][1]["angle"]) / frames[first - 1][0]
        self.assertGreater(after / before, 0.8)
        self.assertLess(after / before, 1.2)
        _, end = next(frame for frame in reversed(frames) if "morph" in frame[1])
        self.assertAlmostEqual(math.degrees(end["angle"]), 120.0)
        clean = {"stage": 1, "angle": end["angle"], "wordmark_progress": end["wordmark_progress"]}
        self.assertEqual(sharingan.render_frame(end), sharingan.render_frame(clean))

    def test_morph_gets_more_time_without_rushing_into_the_final_fade(self) -> None:
        frames = sharingan._timeline(2.0)
        self.assertAlmostEqual(sum(delay for delay, spec in frames if "morph" in spec), 0.80)
        self.assertAlmostEqual(sum(delay for delay, spec in frames if "mix" in spec), 0.44)
        first = next(i for i, (_, spec) in enumerate(frames) if "mix" in spec)
        before = (frames[first - 1][1]["angle"] - frames[first - 2][1]["angle"]) / frames[first - 2][0]
        after = (frames[first][1]["angle"] - frames[first - 1][1]["angle"]) / frames[first - 1][0]
        self.assertGreater(after / before, 0.8)
        self.assertLess(after / before, 1.2)

    def test_every_frame_leaves_corners_and_outside_half_cells_unpainted(self) -> None:
        for _, spec in sharingan._timeline(2.0):
            pixels = eye_pixels(sharingan.render_frame(spec))
            for point in ((0, 0), (0, 55), (55, 0), (55, 55), (20, 0), (20, 55)):
                self.assertIsNone(pixels[point], (spec, point, pixels[point]))
            for point in ((20, 1), (20, 54), (0, 27)):
                self.assertIsNotNone(pixels[point], (spec, point))

    def test_default_banner_preserves_original_colors_inside_circle(self) -> None:
        raw = zlib.decompress(base64.b64decode(sharingan.EYES[-1][2]))
        pixels = eye_pixels(sharingan.static_frame())
        self.assertEqual(len(pixels), 56 * 56)
        for (x, y), color in pixels.items():
            if math.hypot(x - 27.5, y - 27.5) <= 27.5:
                offset = (y * 56 + x) * 3
                self.assertEqual(color, tuple(raw[offset:offset + 3]), (x, y))

    def test_outer_ring_is_stationary_during_inner_motion(self) -> None:
        for stage in range(3):
            still = eye_pixels(sharingan.static_frame(stage, width=56), width=56)
            moving = eye_pixels(sharingan.render_frame({
                "stage": stage, "angle": 0.6, "scale": 1.05, "glow": 1.18,
            }, width=56), width=56)
            for (x, y), color in still.items():
                if color is not None and math.hypot(x - 27.5, y - 27.5) >= 25:
                    self.assertEqual(moving[x, y], color, (stage, x, y))
            raw = zlib.decompress(base64.b64decode(sharingan.EYES[stage][2]))
            for x, y in ((1, 27), (54, 27), (27, 1), (27, 54)):
                offset = (y * 56 + x) * 3
                self.assertEqual(still[x, y], tuple(raw[offset:offset + 3]))

    def test_half_turn_keeps_the_eye_centered(self) -> None:
        raw = bytearray(56 * 56 * 3)
        offset = (20 * 56 + 10) * 3
        raw[offset:offset + 3] = bytes((255, 0, 0))
        data = base64.b64encode(zlib.compress(raw)).decode()
        with patch.object(sharingan, "EYES", [("point", 56, data)]):
            pixels = eye_pixels(sharingan.render_frame({"stage": 0, "angle": math.pi}, width=56), width=56)
        self.assertEqual(pixels[45, 35], (255, 0, 0))
        self.assertEqual(pixels[44, 34], (0, 0, 0))

    def test_sampling_outside_image_does_not_repeat_edge_color(self) -> None:
        raw = bytearray(56 * 56 * 3)
        for y in range(56):
            for x in (0, 55):
                raw[(y * 56 + x) * 3] = 255
        data = base64.b64encode(zlib.compress(raw)).decode()
        with patch.object(sharingan, "EYES", [("edges", 56, data)]):
            pixels = eye_pixels(sharingan.render_frame({"stage": 0, "scale": 0.8}, width=56), width=56)
        self.assertEqual(pixels[3, 27], (0, 0, 0))
        self.assertEqual(pixels[52, 28], (0, 0, 0))

    def test_final_dissolve_keeps_turning_until_upright(self) -> None:
        timeline = sharingan._timeline(2.0)
        angles = [spec.get("angle", 0.0) for _, spec in timeline]
        self.assertTrue(all(prev < current for prev, current in zip(angles, angles[1:])))
        self.assertAlmostEqual(math.degrees(angles[-1]), 240.0)
        self.assertAlmostEqual(timeline[-1][1]["mix"]["angle"], math.tau)
        self.assertAlmostEqual(sum(delay for delay, _ in timeline), 2.0)
        end = sharingan.render_frame(timeline[-1][1], version="test")
        self.assertEqual(end, sharingan.static_frame(-1, version="test"))


class AnimationPluginTest(unittest.TestCase):
    def _write(self, root: Path, name: str, body: str) -> Path:
        path = root / f"{name}.py"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def test_user_directory_overrides_the_packaged_animation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "sharingan", "ANIMATION = lambda ctx: iter(())\n")
            found = animations.discover([root])
            self.assertEqual(found["sharingan"], root / "sharingan.py")
            self.assertIn(animations.DEFAULT, animations.discover())

    def test_bare_function_animation_renders_without_extra_declarations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "hello", """
                from aaa.cli.animations import Frame

                def frames(ctx):
                    yield Frame(0.5, "  Hello  ")
                    yield Frame(0.5, lambda columns: "x" * columns)

                ANIMATION = frames
            """)
            animation = animations.load("hello", path=root / "hello.py")
            ctx = animations.FrameContext(columns=20, rows=5, duration=1.0, version="t")
            self.assertEqual(animations.limits(animation), (0, 0))
            plan = list(animations.frames_of(animation, ctx))
            self.assertEqual([frame.delay for frame in plan], [0.5, 0.5])
            self.assertEqual(animations.paint(plan[0].content, 20), "  Hello  ")
            self.assertEqual(animations.paint(plan[1].content, 20), "x" * 20)
            self.assertEqual(
                animations.paint(animations.static_of(animation, ctx), 20), "  Hello  ")

    def test_broken_animation_raises_and_leaves_no_module_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "broken", "def (:\n")
            with self.assertRaises(animations.AnimationError):
                animations.load("broken", path=root / "broken.py")
            self.assertNotIn("aaa.cli.animations.broken", sys.modules)

    def test_animation_without_frames_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "empty", "ANIMATION = object()\n")
            with self.assertRaises(animations.AnimationError):
                animations.load("empty", path=root / "empty.py")

    def test_frames_must_be_frame_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, "wrong", "ANIMATION = lambda ctx: iter(['text'])\n")
            animation = animations.load("wrong", path=root / "wrong.py")
            ctx = animations.FrameContext(columns=20, rows=5, duration=1.0, version="t")
            with self.assertRaises(animations.AnimationError):
                list(animations.frames_of(animation, ctx))

    def test_default_setting_names_a_packaged_animation(self) -> None:
        from aaa.config.settings import BY_PATH

        self.assertEqual(BY_PATH["ui.banner_animation"].default, animations.DEFAULT)
        self.assertIn(animations.DEFAULT, animations.discover())

    def test_every_sharingan_frame_fits_the_declared_width(self) -> None:
        ctx = animations.FrameContext(columns=100, rows=36, duration=1.0, version="t")
        for frame in animations.frames_of(sharingan.ANIMATION, ctx):
            self.assertTrue(banner._width_ok(animations.paint(frame.content, 100), 100))
        static = animations.paint(animations.static_of(sharingan.ANIMATION, ctx), 100)
        self.assertTrue(banner._width_ok(static, 100))

    def test_sharingan_static_matches_the_resting_frame(self) -> None:
        ctx = animations.FrameContext(columns=100, rows=36, duration=0.0, version="1.2.3")
        self.assertEqual(
            animations.paint(animations.static_of(sharingan.ANIMATION, ctx), 100),
            sharingan.static_frame(version="1.2.3", columns=100),
        )
