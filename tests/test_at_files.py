"""`@文件` 附加：token 解析、图片与文本分流、降级与截断。"""

import os
import tempfile
import unittest
from pathlib import Path

from aaa.cli.app import expand_at_files


class AtFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, os.path.dirname(__file__) or ".")

    def _file(self, name: str, content: str) -> Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_text_file_is_appended_as_block(self) -> None:
        self._file("notes.md", "正文内容")
        text, images, notices = expand_at_files("看看 @notes.md 谢谢")
        self.assertIsNone(images)
        self.assertEqual(notices, [])
        self.assertIn("看看 @notes.md", text)
        self.assertIn('<file path="notes.md">', text)
        self.assertIn("正文内容", text)

    def test_image_suffix_goes_to_images_and_keeps_token(self) -> None:
        self._file("shot.png", "")
        text, images, notices = expand_at_files("看 @shot.png")
        self.assertEqual(text, "看 @shot.png")
        self.assertEqual(len(images or []), 1)
        self.assertEqual(notices, [])

    def test_missing_path_with_slash_notices_and_keeps_token(self) -> None:
        text, images, notices = expand_at_files("看 @dir/missing.md")
        self.assertEqual(text, "看 @dir/missing.md")
        self.assertIsNone(images)
        self.assertEqual(len(notices), 1)

    def test_bare_unknown_word_is_silently_kept(self) -> None:
        text, images, notices = expand_at_files("在 @channel 里说")
        self.assertEqual(text, "在 @channel 里说")
        self.assertIsNone(images)
        self.assertEqual(notices, [])

    def test_quoted_path_with_spaces(self) -> None:
        self._file("my notes.md", "内容")
        text, images, notices = expand_at_files('看 @"my notes.md"')
        self.assertEqual(notices, [])
        self.assertIn("内容", text)

    def test_large_file_is_truncated_with_marker(self) -> None:
        self._file("big.md", "字" * 100_500)
        text, images, notices = expand_at_files("看 @big.md")
        self.assertIn("已截断", text)
        self.assertEqual(notices, [])

    def test_directory_is_not_attached(self) -> None:
        (self.tmp / "sub").mkdir()
        text, images, notices = expand_at_files("看 @sub/")  # 补全对目录追加 `/`
        self.assertEqual(text, "看 @sub/")
        self.assertEqual(len(notices), 1)
        self.assertIn("目录", notices[0])

    def test_binary_file_is_not_attached(self) -> None:
        (self.tmp / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
        text, images, notices = expand_at_files("看 @blob.bin")
        self.assertEqual(text, "看 @blob.bin")
        self.assertEqual(len(notices), 1)

    def test_email_is_not_treated_as_file(self) -> None:
        text, images, notices = expand_at_files("联系 user@example.com 好吗")
        self.assertEqual(text, "联系 user@example.com 好吗")
        self.assertIsNone(images)
        self.assertEqual(notices, [])


if __name__ == "__main__":
    unittest.main()
