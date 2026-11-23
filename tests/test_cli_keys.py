"""输入键位的可观察行为：Shift+Enter 只换行，Enter 只提交一次。"""

import unittest

from prompt_toolkit.buffer import Buffer

from aaa.cli.app import enter_action
from aaa.cli.key_input import SHIFT_ENTER_SEQUENCES, is_shift_enter


class EnterActionTest(unittest.TestCase):
    def _buffer(self, accepted):
        return Buffer(
            accept_handler=lambda buffer: accepted.append(buffer.text) or True,
            multiline=False,
        )

    def test_shift_enter_inserts_newline_without_accepting(self) -> None:
        accepted = []
        buffer = self._buffer(accepted)
        for sequence in SHIFT_ENTER_SEQUENCES:
            self.assertTrue(is_shift_enter(sequence), sequence)
            buffer.text = "a"
            buffer.cursor_position = len(buffer.text)
            enter_action(buffer, sequence)
            self.assertEqual(buffer.text, "a\n", sequence)
        self.assertEqual(accepted, [])

    def test_plain_enter_accepts_once(self) -> None:
        accepted = []
        buffer = self._buffer(accepted)
        buffer.text = "hello"
        enter_action(buffer, "\r")
        self.assertEqual(accepted, ["hello"])
        self.assertFalse(is_shift_enter("\r"))
        self.assertFalse(is_shift_enter(None))


if __name__ == "__main__":
    unittest.main()
