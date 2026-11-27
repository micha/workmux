import io
from importlib.util import find_spec
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

from workmux.cli import main, parser
from workmux.core import Tmux, WorkmuxError, Workspace, validate_directory
from workmux.workspace_prompt import directory_matches, prompt_workspace


class WorkspacePromptTests(unittest.TestCase):
    def test_completion_only_lists_directories_and_preserves_literal_paths(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, HOME=temporary):
            directory = Path(temporary)
            (directory / 'Git Projects' / 'a $literal; directory').mkdir(parents=True)
            (directory / 'Git file').touch()
            (directory / '.hidden').mkdir()
            self.assertEqual(directory_matches('~'), ['~/'])
            self.assertEqual(directory_matches('~/'), ['~/Git Projects/'])
            self.assertEqual(directory_matches('~/Git'), ['~/Git Projects/'])
            self.assertEqual(directory_matches('~/.h'), ['~/.hidden/'])
            self.assertEqual(directory_matches('~/Git Projects/a'), ['~/Git Projects/a $literal; directory/'])
            self.assertEqual(directory_matches('~/missing/'), [])

    @unittest.skipUnless(find_spec('readline'), 'Python readline is required')
    def test_prompt_uses_default_directory_and_resolved_basename(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, HOME=temporary), \
                patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
                patch('builtins.input', side_effect=['', '']) as read:
            path = Path(temporary) / 'Project With Spaces'
            self.assertEqual(prompt_workspace('~/other/../Project With Spaces'), (path.name, str(path)))
            self.assertEqual(read.call_args.args, ('Name [Project With Spaces]: ',))
            self.assertFalse(path.exists())

    @unittest.skipUnless(find_spec('readline'), 'Python readline is required')
    def test_prompt_cancellation_never_creates_directories_or_sessions(self):
        with tempfile.TemporaryDirectory() as temporary, patch('workmux.cli.Tmux') as tmux, \
                patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True):
            path = Path(temporary) / 'not-created'
            for steps in ([EOFError], [str(path), KeyboardInterrupt]):
                with self.subTest(steps=steps), patch('builtins.input', side_effect=steps):
                    self.assertEqual(main(['workspace', 'new', '--interactive']), 0)
                    self.assertFalse(path.exists())
                    tmux.return_value.new_workspace.assert_not_called()

    def test_interactive_mode_requires_a_terminal(self):
        with patch('sys.stdin', new_callable=io.StringIO), patch('workmux.cli.Tmux') as tmux, \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            self.assertEqual(main(['workspace', 'new', '--interactive']), 1)
            self.assertIn('requires a terminal', stderr.getvalue())
            tmux.return_value.new_workspace.assert_not_called()

    def test_interactive_mode_opts_in_to_directory_creation(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch('workmux.workspace_prompt.prompt_workspace', return_value=('label', '/dir')), \
                patch('workmux.cli.Tmux') as tmux, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['workspace', 'new', '--interactive']), 0)
            tmux.return_value.new_workspace.assert_called_once_with(
                'label', '/dir', create_directory=True, reuse_existing=True,
            )
            tmux.return_value.new_workspace.reset_mock()
            self.assertEqual(main(['workspace', 'new', 'other', '--dir', '/other']), 0)
            tmux.return_value.new_workspace.assert_called_once_with(
                'other', '/other', create_directory=False, reuse_existing=False,
            )
            tmux.return_value.run.assert_not_called()

    def test_interactive_finds_moved_panes_session_by_tty(self):
        with patch.dict(os.environ, {'TMUX': '/tmp/socket,123,1'}, clear=True), \
                patch('os.ttyname', return_value='/dev/pts/2'), \
                patch('workmux.workspace_prompt.prompt_workspace', return_value=('label', '/dir')), \
                patch('workmux.cli.Tmux') as tmux, patch('sys.stdout', new_callable=io.StringIO):
            server = tmux.return_value
            server.run.side_effect = ['/dev/pts/1\t$1\n/dev/pts/2\t$3', '/dev/pts/4', '']
            server.new_workspace.return_value = '$5'
            self.assertEqual(main(['workspace', 'new', '--interactive']), 0)
            self.assertEqual(server.method_calls, [
                call.run('list-panes', '-a', '-F', '#{pane_tty}\t#{session_id}'),
                call.run('list-clients', '-t', '$3', '-F', '#{client_name}'),
                call.new_workspace('label', '/dir', create_directory=True, reuse_existing=True),
                call.run('switch-client', '-c', '/dev/pts/4', '-t', '$5'),
            ])

    def test_interactive_does_not_guess_when_source_client_is_ambiguous(self):
        with patch.dict(os.environ, {'TMUX': '/tmp/socket,123,1'}, clear=True), \
                patch('os.ttyname', return_value='/dev/pts/2'), \
                patch('workmux.workspace_prompt.prompt_workspace', return_value=('label', '/dir')), \
                patch('workmux.cli.Tmux') as tmux, patch('sys.stderr', new_callable=io.StringIO) as stderr:
            tmux.return_value.run.side_effect = ['/dev/pts/3\t$1', '/dev/pts/1\n/dev/pts/4']
            self.assertEqual(main(['workspace', 'new', '--interactive']), 1)
            self.assertIn('requires exactly one attached client', stderr.getvalue())
            tmux.return_value.run.assert_called_with('list-clients', '-t', '$1', '-F', '#{client_name}')
            tmux.return_value.new_workspace.assert_not_called()

    def test_creation_does_not_switch_for_another_server_or_noninteractive_mode(self):
        with patch.dict(os.environ, {'TMUX': '/tmp/socket,123,1'}, clear=True), \
                patch('workmux.workspace_prompt.prompt_workspace', return_value=('label', '/dir')), \
                patch('workmux.cli.Tmux') as tmux, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['--socket', '/tmp/other', 'workspace', 'new', '--interactive']), 0)
            self.assertEqual(main(['workspace', 'new', 'named']), 0)
            tmux.return_value.run.assert_not_called()

    def test_name_collision_in_another_directory_does_not_reuse_or_create(self):
        tmux = Tmux()
        with tempfile.TemporaryDirectory() as temporary, patch.object(tmux, 'run') as run, \
                patch.object(tmux, 'workspaces', return_value=[Workspace('$1', 'label', 'label', temporary)]):
            missing = Path(temporary) / 'not-created'
            with self.assertRaisesRegex(WorkmuxError, 'different directory'):
                tmux.new_workspace('label', str(missing), create_directory=True, reuse_existing=True)
            self.assertFalse(missing.exists())
            with self.assertRaisesRegex(WorkmuxError, 'Workspace already exists'):
                tmux.new_workspace('label', temporary)
            run.assert_not_called()

    def test_directory_creation_remains_opt_in_and_checks_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / 'a b' / '$literal; child'
            with self.assertRaises(WorkmuxError):
                validate_directory(str(missing))
            self.assertFalse(missing.parent.exists())
            self.assertEqual(validate_directory(str(missing), create=True), str(missing))
            self.assertTrue(missing.is_dir())
            bad = missing / 'bad\tname'
            with self.assertRaises(WorkmuxError):
                validate_directory(str(bad), create=True)
            self.assertFalse(bad.exists())

    def test_parser_requires_a_name_or_interactive_mode_but_not_both(self):
        for arguments in ([], ['label', '--interactive']):
            with self.subTest(arguments=arguments), patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    parser().parse_args(['workspace', 'new', *arguments])
                self.assertEqual(error.exception.code, 2)
