"""Normalize modified Enter locally before prompt_toolkit's VT100 parser."""

import os
import re
import sys

from prompt_toolkit.input.defaults import create_input
from prompt_toolkit.input.vt100 import Vt100Input

SHIFT_ENTER_SEQUENCES = (
    "\x1b[27;2;13~",
    "\x1b[27;6;13~",
    "\x1b[13;2u",
    "\x1b[13;2:1u",
    "\x1b[13;2:2u",
)
_SHIFT_RELEASE = "\x1b[13;2:3u"
_PASTE_START, _PASTE_END = "\x1b[200~", "\x1b[201~"
_TOKENS = (*SHIFT_ENTER_SEQUENCES, _SHIFT_RELEASE, _PASTE_START, _PASTE_END)
_MATCH = re.compile("|".join(re.escape(token) for token in _TOKENS))
_PREFIXES = frozenset(token[:length] for token in _TOKENS for length in range(1, len(token)))
_PREFIX_LENGTH = max(map(len, _TOKENS)) - 1


def is_shift_enter(data: str | None) -> bool:
    return data in SHIFT_ENTER_SEQUENCES


class ModifiedEnter:
    """Streaming normalization; incomplete prefixes wait for input or Escape flush."""

    def __init__(self):
        self.pending = ""
        self.paste = False

    def feed(self, data: str) -> str:
        text, self.pending = self.pending + data, ""
        for length in range(min(_PREFIX_LENGTH, len(text)), 0, -1):
            if text[-length:] in _PREFIXES:
                self.pending, text = text[-length:], text[:-length]
                break

        def replace(match):
            token = match.group()
            if token == _PASTE_START:
                self.paste = True
            elif token == _PASTE_END:
                self.paste = False
            elif not self.paste:
                return "" if token == _SHIFT_RELEASE else SHIFT_ENTER_SEQUENCES[0]
            return token

        return _MATCH.sub(replace, text)

    def flush(self) -> str:
        if self.paste:
            return ""
        text, self.pending = self.pending, ""
        return text


class _Reader:
    def __init__(self, reader):
        self.reader = reader
        self.modified_enter = ModifiedEnter()

    @property
    def closed(self):
        return self.reader.closed

    def read(self, *args, **kwargs):
        return self.modified_enter.feed(self.reader.read(*args, **kwargs))


class ModifiedEnterInput(Vt100Input):
    """Keep native fd/TTY lifecycle; adapt this input's reader and flush only."""

    def __init__(self, stdin):
        super().__init__(stdin)
        self.stdin_reader = _Reader(self.stdin_reader)

    def flush_keys(self):
        self.vt100_parser.feed(self.stdin_reader.modified_enter.flush())
        return super().flush_keys()


def create_cli_input():
    return create_input() if os.name == "nt" else ModifiedEnterInput(sys.stdin)
