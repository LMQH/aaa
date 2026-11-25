"""终端图片显示：协议探测、编码、尺寸解析与路径识别。"""

import base64
import struct
import tempfile
import unittest
from pathlib import Path

from aaa.cli import images

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    + struct.pack(">I", 13) + b"IHDR"
    + struct.pack(">II", 800, 600)
    + b"\x08\x06\x00\x00\x00"
    + b"\x00" * 4
)
GIF_1x1 = b"GIF89a" + struct.pack("<HH", 32, 24) + b"\x00" * 8


class DetectTest(unittest.TestCase):
    def test_iterm_family(self) -> None:
        self.assertEqual(images.detect_protocol({"TERM_PROGRAM": "iTerm.app"}), images.ITERM2)
        self.assertEqual(images.detect_protocol({"TERM_PROGRAM": "WezTerm"}), images.ITERM2)

    def test_kitty_family(self) -> None:
        self.assertEqual(images.detect_protocol({"KITTY_WINDOW_ID": "1"}), images.KITTY)
        self.assertEqual(images.detect_protocol({"TERM": "xterm-kitty"}), images.KITTY)
        self.assertEqual(images.detect_protocol({"TERM": "xterm-ghostty"}), images.KITTY)

    def test_unsupported_or_tmux(self) -> None:
        self.assertIsNone(images.detect_protocol({"TERM": "xterm-256color"}))
        self.assertIsNone(images.detect_protocol({"TERM_PROGRAM": "Apple_Terminal"}))
        self.assertIsNone(images.detect_protocol({"TERM": "tmux-256color"}))


class EncodeTest(unittest.TestCase):
    def test_iterm2_wraps_base64_with_size_and_name(self) -> None:
        payload = PNG_1x1
        seq = images.encode_iterm2(payload, "图 1.png", 80)
        self.assertTrue(seq.startswith("\x1b]1337;File=inline=1;size="))
        self.assertIn("width=80", seq)
        self.assertTrue(seq.endswith("\x07"))
        body = seq.split(":", 1)[1].rsplit("\x07", 1)[0]
        self.assertEqual(base64.b64decode(body), payload)
        name = seq.split("name=", 1)[1].split(";", 1)[0]
        self.assertEqual(base64.b64decode(name).decode("utf-8"), "图 1.png")

    def test_kitty_chunks_roundtrip(self) -> None:
        payload = b"x" * 20000
        seq = images.encode_kitty(payload, 60)
        chunks = [part.split(";", 1)[1].rstrip("\x1b\\") for part in seq.split("\x1b_G")[1:]]
        self.assertGreater(len(chunks), 1)
        self.assertIn("m=1", seq)
        self.assertIn("m=0", seq)
        self.assertEqual(base64.b64decode("".join(chunks)), payload)
        self.assertIn("c=60", seq)
        self.assertIn("a=T", seq)


class DimensionsTest(unittest.TestCase):
    def test_png_and_gif(self) -> None:
        self.assertEqual(images.dimensions(PNG_1x1), (800, 600))
        self.assertEqual(images.dimensions(GIF_1x1), (32, 24))

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(images.dimensions(b"not an image"))

    def test_label_has_shape_and_size(self) -> None:
        text = images.label(Path("/tmp/pic.png"), PNG_1x1)
        self.assertIn("pic.png", text)
        self.assertIn("800x600", text)
        self.assertIn("KB", text)


class FindPathsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _file(self, name: str, size: int = 10) -> Path:
        path = self.tmp / name
        path.write_bytes(b"x" * size)
        return path

    def test_only_whole_line_paths(self) -> None:
        pic = self._file("a.png")
        text = f"日志开始\n{pic}\n输出：{pic} (inline)\n"
        self.assertEqual(images.find_image_paths(text), [pic])

    def test_ignores_missing_and_non_image(self) -> None:
        txt = self._file("note.txt")
        missing = self.tmp / "missing.png"
        self.assertEqual(images.find_image_paths(f"{txt}\n{missing}\n"), [])

    def test_max_bytes_filters_and_dedupes(self) -> None:
        small = self._file("small.png", 10)
        big = self._file("big.png", 5000)
        text = f"{small}\n{small}\n{big}\n"
        self.assertEqual(images.find_image_paths(text, max_bytes=1024), [small])


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.pic = self.tmp / "p.png"
        self.pic.write_bytes(PNG_1x1)

    def test_render_writes_protocol_sequence(self) -> None:
        written = []
        ok = images.render(self.pic, writer=written.append, columns=40, protocol=images.ITERM2)
        self.assertTrue(ok)
        self.assertTrue(written and written[0].startswith("\x1b]1337;"))

    def test_render_returns_false_without_protocol(self) -> None:
        written = []
        ok = images.render(self.pic, writer=written.append, columns=40, env={"TERM": "xterm-256color"})
        self.assertFalse(ok)
        self.assertEqual(written, [])


if __name__ == "__main__":
    unittest.main()
