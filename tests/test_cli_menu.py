"""Native menu input remains usable inside the already-running REPL loop."""

import unittest

import questionary
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from aaa.cli.commands import _ask_menu


class AsyncMenuTest(unittest.IsolatedAsyncioTestCase):
    async def test_selects_nondefault_choice_inside_running_loop(self):
        with create_pipe_input() as source, create_app_session(input=source, output=DummyOutput()):
            question = questionary.select("选择", choices=["first", "second"])
            source.send_text("\x1b[B\r")
            self.assertEqual(_ask_menu(question), "second")

    async def test_escape_and_interrupt_cancel_without_leaving_repl_loop(self):
        for key in ("\x1b", "\x03"):
            with self.subTest(key=key), create_pipe_input() as source, create_app_session(
                input=source, output=DummyOutput()
            ):
                question = questionary.select("选择", choices=["first", "second"])
                source.send_text(key)
                self.assertIsNone(_ask_menu(question))
