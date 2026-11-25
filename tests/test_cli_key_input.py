"""Exercise modified Enter through the actual VT100 parser, not raw handler calls."""

import os
import unittest
from unittest import mock

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys

from aaa.cli.app import approval_enter, enter_action
from aaa.cli.key_input import (
    ALT_ENTER_SEQUENCES,
    SHIFT_ENTER_SEQUENCES,
    ModifiedEnter,
    extended_keys,
)


def parse(chunks):
    events = []
    native = Vt100Parser(events.append)
    adapter = ModifiedEnter()
    for chunk in chunks:
        native.feed(adapter.feed(chunk))
    native.feed(adapter.flush())
    native.flush()
    return events


class ModifiedEnterTest(unittest.TestCase):
    def test_every_split_inserts_one_newline_and_plain_enter_submits_once(self):
        for sequence in SHIFT_ENTER_SEQUENCES:
            for split in range(len(sequence) + 1):
                with self.subTest(sequence=sequence, split=split):
                    accepted = []
                    buffer = Buffer(accept_handler=lambda value: accepted.append(value.text) or True)
                    buffer.text = 'before'
                    buffer.cursor_position = len(buffer.text)
                    for event in parse([sequence[:split], sequence[split:] + 'after\r']):
                        if event.key == Keys.ControlM:
                            enter_action(buffer, event.data)
                        else:
                            buffer.insert_text(event.data)
                    self.assertEqual(accepted, ['before\nafter'])

    def test_release_does_not_add_a_second_newline(self):
        events = parse(['\x1b[13;2:1u', '\x1b[13;2:3u'])
        self.assertEqual([event.key for event in events], [Keys.ControlM])

    def test_literal_sequences_inside_paste_are_unchanged_across_splits(self):
        payload = '原文\n\x1b[13;2u\x1b[13;2:3u\t尾部'
        wire = '\x1b[200~' + payload + '\x1b[201~\x1b[13;2u'
        for split in range(len(wire) + 1):
            with self.subTest(split=split):
                events = parse([wire[:split], wire[split:]])
                self.assertEqual([event.key for event in events], [Keys.BracketedPaste, Keys.ControlM])
                self.assertEqual(events[0].data, payload)

    def test_alt_enter_in_one_read_becomes_the_marker_and_split_reads_do_not(self):
        for sequence in ALT_ENTER_SEQUENCES:
            events = parse([sequence])
            self.assertEqual([event.key for event in events], [Keys.ControlM], repr(sequence))
            self.assertEqual(events[0].data, SHIFT_ENTER_SEQUENCES[0])
            split_events = parse([sequence[:1], sequence[1:]])
            self.assertEqual([event.key for event in split_events[:1]], [Keys.Escape], repr(sequence))
            self.assertEqual(len(split_events), 2, repr(sequence))

    def test_lone_escape_still_flushes_and_plain_enter_stays_enter(self):
        self.assertEqual([event.key for event in parse(['\x1b'])], [Keys.Escape])
        events = parse(['a\r'])
        self.assertEqual([event.key for event in events], ['a', Keys.ControlM])
        self.assertEqual(events[-1].data, '\r')


class ExtendedKeysTest(unittest.TestCase):
    """modifyOtherKeys / CSI-u 序列必须还原成既有绑定认得的按键。"""

    def assert_keys(self, wire, expected):
        for split in range(len(wire) + 1):
            with self.subTest(wire=repr(wire), split=split):
                self.assertEqual([event.key for event in parse([wire[:split], wire[split:]])], expected)

    def test_ctrl_letters_keep_firing_existing_bindings(self):
        cases = {
            '\x1b[27;5;99~': Keys.ControlC,
            '\x1b[99;5u': Keys.ControlC,
            '\x1b[27;5;106~': Keys.ControlJ,
            '\x1b[106;5u': Keys.ControlJ,
            '\x1b[27;5;111~': Keys.ControlO,
            '\x1b[27;5;103~': Keys.ControlG,
            '\x1b[27;5;100~': Keys.ControlD,
        }
        for wire, key in cases.items():
            self.assert_keys(wire, [key])

    def test_modified_enter_becomes_the_newline_marker(self):
        for wire in ('\x1b[27;2;13~', '\x1b[13;2u', '\x1b[27;3;13~', '\x1b[27;5;13~'):
            self.assert_keys(wire, [Keys.ControlM])
            self.assertTrue(all(event.data == SHIFT_ENTER_SEQUENCES[0] for event in parse([wire])))

    def test_shift_letter_types_uppercase_without_escape_garbage(self):
        for wire in ('\x1b[27;2;65~', '\x1b[97;2u'):
            events = parse([wire])
            self.assertEqual([event.key for event in events], ['A'], wire)
            self.assertEqual(events[0].data, 'A', wire)

    def test_modified_tab_and_backspace_keep_their_bindings(self):
        self.assert_keys('\x1b[27;2;9~', [Keys.BackTab])
        self.assert_keys('\x1b[27;1;9~', [Keys.ControlI])
        self.assert_keys('\x1b[27;5;127~', [Keys.ControlH])

    def test_traditional_function_keys_pass_through_untouched(self):
        for wire in ('\x1b[1;5A', '\x1b[3;5~', '\x1b[1;2H'):
            with self.subTest(wire=wire):
                self.assertEqual([event.key for event in parse([wire])], [ANSI_SEQUENCES[wire]])

    def test_unknown_csi_u_does_not_leak_literal_text(self):
        self.assertEqual(parse(['\x1b[57441u']), [])

    def test_caps_lock_bit_does_not_break_ctrl_keys(self):
        self.assert_keys('\x1b[99;69u', [Keys.ControlC])

    def test_ctrl_space_is_not_a_literal_space(self):
        self.assert_keys('\x1b[27;5;32~', [Keys.ControlAt])
        events = parse(['\x1b[27;2;32~'])
        self.assertEqual([event.key for event in events], [' '])

    def test_backspace_codepoint_8_and_alt_prefix(self):
        self.assert_keys('\x1b[27;2;8~', [Keys.ControlH])
        self.assert_keys('\x1b[27;3;8~', [Keys.Escape, Keys.ControlH])
        self.assert_keys('\x1b[27;3;127~', [Keys.Escape, Keys.ControlH])

    def test_ctrl_punctuation_still_means_escape(self):
        self.assert_keys('\x1b[27;5;91~', [Keys.Escape])
        self.assert_keys('\x1b[27;5;51~', [Keys.Escape])
        self.assert_keys('\x1b[27;5;64~', [Keys.ControlAt])
        self.assert_keys('\x1b[27;5;92~', [ANSI_SEQUENCES['\x1c']])

    def test_ctrl_shift_tab_does_not_toggle_permissions(self):
        self.assert_keys('\x1b[27;6;9~', [Keys.ControlI])
        self.assert_keys('\x1b[27;2;9~', [Keys.BackTab])

    def test_super_and_malformed_csi_u_are_swallowed(self):
        for wire in ('\x1b[97;9u', '\x1b[99;13u', '\x1b[97;2;65u', '\x1b[97:65;2u'):
            with self.subTest(wire=wire):
                self.assertEqual(parse([wire]), [])

    def test_escape_then_enter_across_reads_is_not_alt_enter(self):
        for chunk, enter in (('\r', Keys.ControlM), ('\n', Keys.ControlJ)):
            adapter = ModifiedEnter()
            events = []
            native = Vt100Parser(events.append)
            native.feed(adapter.feed('\x1b'))
            native.feed(adapter.feed(chunk))
            native.feed(adapter.flush())
            native.flush()
            self.assertEqual([event.key for event in events], [Keys.Escape, enter])
            self.assertNotEqual(events[-1].data, SHIFT_ENTER_SEQUENCES[0])


class ApprovalEnterTest(unittest.TestCase):
    """审批待决时 Shift+Enter 只能换行，不得批准，也不得发送草稿。"""

    def _buffer(self, accepted):
        return Buffer(accept_handler=lambda buffer: accepted.append(buffer.text) or True)

    def test_shift_enter_inserts_newline_and_answers_nothing(self):
        for sequence in SHIFT_ENTER_SEQUENCES:
            for text in ('', 'draft'):
                answers = []
                accepted = []
                buffer = self._buffer(accepted)
                buffer.text = text
                buffer.cursor_position = len(text)
                approval_enter(buffer, sequence, True, True, answers.append)
                self.assertEqual(answers, [], sequence)
                self.assertEqual(accepted, [], sequence)
                self.assertEqual(buffer.text, text + '\n', sequence)

    def test_plain_enter_confirms_selected_choice(self):
        answers = []
        buffer = self._buffer([])
        approval_enter(buffer, '\r', True, True, answers.append)
        self.assertEqual(answers, [True])
        approval_enter(buffer, '\r', False, True, answers.append)
        self.assertEqual(answers, [True])

    def test_plain_enter_with_draft_refuses_and_sends(self):
        answers = []
        accepted = []
        buffer = self._buffer(accepted)
        buffer.text = 'hello'
        approval_enter(buffer, '\r', True, True, answers.append)
        self.assertEqual(answers, [False])
        self.assertEqual(accepted, ['hello'])


@unittest.skipIf(os.name == "nt", "modifyOtherKeys 只在 POSIX 终端启用，Windows 走 Win32 输入")
class ExtendedKeysToggleTest(unittest.TestCase):
    def test_enable_and_restore_only_on_supported_terminal(self):
        class Stream:
            def __init__(self):
                self.written = ''

            def isatty(self):
                return True

            def write(self, text):
                self.written += text

            def flush(self):
                pass

        stream = Stream()
        with mock.patch.dict(os.environ, {'TERM_PROGRAM': 'iTerm.app'}, clear=True):
            with extended_keys(stream):
                self.assertEqual(stream.written, '\x1b[>4;2m')
        self.assertEqual(stream.written, '\x1b[>4;2m\x1b[>4m')

        stream = Stream()
        with mock.patch.dict(os.environ, {'TERM_PROGRAM': 'Apple_Terminal'}, clear=True):
            with extended_keys(stream) as enabled:
                self.assertFalse(enabled)
        self.assertEqual(stream.written, '')

    def test_restore_runs_when_session_raises(self):
        class Stream:
            def __init__(self):
                self.written = ''

            def isatty(self):
                return True

            def write(self, text):
                self.written += text

            def flush(self):
                pass

        stream = Stream()
        with mock.patch.dict(os.environ, {'TERM_PROGRAM': 'iTerm.app'}, clear=True):
            with self.assertRaises(RuntimeError):
                with extended_keys(stream):
                    raise RuntimeError('boom')
        self.assertEqual(stream.written, '\x1b[>4;2m\x1b[>4m')

    def test_non_tty_is_left_untouched(self):
        class Stream:
            def __init__(self):
                self.written = ''

            def isatty(self):
                return False

            def write(self, text):
                self.written += text

            def flush(self):
                pass

        stream = Stream()
        with mock.patch.dict(os.environ, {'TERM_PROGRAM': 'iTerm.app'}, clear=True):
            with extended_keys(stream) as enabled:
                self.assertFalse(enabled)
        self.assertEqual(stream.written, '')


class TmuxExtendedKeysHintTest(unittest.TestCase):
    """默认配置的 tmux 把 Shift+Enter 压成回车：启动时点名提示怎么修。"""

    _IN_TMUX = {'TMUX': '/tmp/tmux,1,0', 'TERM': 'tmux-256color'}

    def test_off_in_tmux_gives_fix_hint(self):
        from aaa.cli.key_input import tmux_extended_keys_hint

        shown = mock.Mock(stdout='extended-keys off\n')
        with mock.patch('aaa.cli.key_input.subprocess.run', return_value=shown) as run:
            hint = tmux_extended_keys_hint(dict(self._IN_TMUX))
        self.assertIsNotNone(hint)
        self.assertIn('set -s extended-keys on', hint)
        self.assertEqual(run.call_args.args[0], ['tmux', 'show-options', '-s', 'extended-keys'])

    def test_on_in_tmux_is_silent(self):
        from aaa.cli.key_input import tmux_extended_keys_hint

        with mock.patch('aaa.cli.key_input.subprocess.run', return_value=mock.Mock(stdout='extended-keys on\n')):
            self.assertIsNone(tmux_extended_keys_hint(dict(self._IN_TMUX)))

    def test_outside_tmux_does_not_shell_out(self):
        from aaa.cli.key_input import tmux_extended_keys_hint

        with mock.patch('aaa.cli.key_input.subprocess.run') as run:
            self.assertIsNone(tmux_extended_keys_hint({'TERM_PROGRAM': 'iTerm.app'}))
            self.assertIsNone(tmux_extended_keys_hint({'TMUX': '/tmp/tmux,1,0', 'TERM_PROGRAM': 'iTerm.app'}))
        run.assert_not_called()

    def test_tmux_missing_or_failing_is_silent(self):
        import subprocess

        from aaa.cli.key_input import tmux_extended_keys_hint

        with mock.patch('aaa.cli.key_input.subprocess.run', side_effect=OSError('no tmux')):
            self.assertIsNone(tmux_extended_keys_hint(dict(self._IN_TMUX)))
        with mock.patch('aaa.cli.key_input.subprocess.run', side_effect=subprocess.TimeoutExpired('tmux', 2)):
            self.assertIsNone(tmux_extended_keys_hint(dict(self._IN_TMUX)))
