"""Exercise modified Enter through the actual VT100 parser, not raw handler calls."""

import unittest

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys

from aaa.cli.app import enter_action
from aaa.cli.key_input import ModifiedEnter, SHIFT_ENTER_SEQUENCES


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

    def test_lone_escape_flushes_and_alt_enter_remains_distinct(self):
        self.assertEqual([event.key for event in parse(['\x1b'])], [Keys.Escape])
        events = parse(['\x1b', '\r'])
        self.assertEqual([event.key for event in events], [Keys.Escape, Keys.ControlM])
        self.assertEqual(events[-1].data, '\r')
