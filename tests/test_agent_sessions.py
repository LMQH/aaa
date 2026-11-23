"""Agent session creation and resume through the CLI."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from agno.session import AgentSession

from aaa.cli.app import apply_options, clear_credentials, last_completed_run_id
from aaa.cli.args import parse_args
from aaa.cli.commands import handle_command
from aaa.cli.run import stream_run
from aaa.cli.state import AppState
from aaa.config.settings import Settings
from aaa.sessions.query import list_sessions, remember_session
from fixtures import FakeModel, text


class AgentSessionCommandTest(TestCase):
    def test_failed_permission_switch_restores_mode(self):
        state = AppState(config={}, settings=Settings({}), catalog=SimpleNamespace(scope=lambda _: []),
                         provider='p', model_id='m', mode='read-only',
                         runnable=SimpleNamespace(), session_id='s', completer=None)
        with patch('aaa.cli.state.build_agent', side_effect=ValueError('rejected')):
            with self.assertRaises(ValueError):
                state.set_mode('workspace-write')
        self.assertEqual(state.mode, 'read-only')

    def test_clear_credentials_clears_model_key(self):
        model = FakeModel([])
        model.api_key = 'secret'
        clear_credentials(SimpleNamespace(model=model))
        self.assertIsNone(model.api_key)

    def test_native_agent_session_resume_and_new_history(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / 'sessions.db'
            main_model = FakeModel([text('first answer'), text('continued'), text('new answer')])
            catalog = SimpleNamespace(
                require=lambda *_: (None, SimpleNamespace(context_window=100_000)),
                scope=lambda _: [], build=lambda *_: main_model, fallbacks=lambda *_: [],
            )
            config = {'default': {'provider': 'p', 'model': 'm'}}
            with patch('aaa.cli.state.from_config', return_value=catalog), \
                    patch('aaa.cli.state.plugins.discover_mcp', return_value=[]), \
                    patch('aaa.runtime.agent.DATA_DIR', root), \
                    patch('aaa.runtime.agent.DB_FILE', db_path), \
                    patch('aaa.sessions.query.DB_FILE', db_path), \
                    redirect_stdout(StringIO()):
                state = AppState.start(config, Settings())
                db = state.runnable.db
                try:
                    original = state.session_id
                    stream_run(state.runnable, 'prior agent question', original)
                    self.assertIsInstance(db.get_session(original), AgentSession)
                    remember_session(str(Path.cwd()), original)

                    self.assertTrue(handle_command(state, '/new'))
                    self.assertNotEqual(state.session_id, original)

                    apply_options(state, parse_args('read-only', ['-c']))
                    self.assertEqual(state.session_id, original)
                    self.assertIsNotNone(last_completed_run_id(state.runnable, original))
                    stream_run(state.runnable, 'continue', state.session_id)
                    self.assertIn('prior agent question', str(main_model.invocations[-1]['messages']))

                    self.assertTrue(handle_command(state, '/new'))
                    fresh = state.session_id
                    stream_run(state.runnable, 'new conversation', fresh)
                    self.assertIsInstance(db.get_session(fresh), AgentSession)
                    self.assertNotIn('prior agent question', str(main_model.invocations[-1]['messages']))
                    self.assertEqual(
                        {s.session_id for s in list_sessions(state.runnable, 10)}, {original, fresh}
                    )
                    apply_options(state, parse_args('read-only', ['-r', original]))
                    self.assertEqual(state.session_id, original)
                finally:
                    db.db_engine.dispose()
