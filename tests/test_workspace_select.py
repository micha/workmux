"""Workspace selection by exact target or sidebar position."""

import io
import os
import unittest
from unittest.mock import patch

from workmux.cli import main, parser, resolve_workspace
from workmux.core import Tmux, WorkmuxError, Workspace


class WorkspaceSelectTests(unittest.TestCase):
    snapshot = ('$2\tzebra\tZebra\t/tmp/z\t1\n'
                '$9\talpha\tAlpha\t/tmp/a\t1\n'
                '$1\t3\tNumeric\t/tmp/n\t1\n'
                '$0\tunmanaged\tIgnored\t/tmp/u\t0')

    def test_indexes_use_sidebar_workspace_order_not_session_ids(self):
        tmux = Tmux()
        with patch.object(tmux, 'run', return_value=self.snapshot):
            for index, expected in enumerate(('$1', '$9', '$2'), 1):
                with self.subTest(index=index):
                    self.assertEqual(resolve_workspace(tmux, None, index=index), expected)
            for target in ('3', '$1', 'Numeric'):
                self.assertEqual(resolve_workspace(tmux, target), '$1')

    def test_invalid_indexes_and_empty_list(self):
        tmux = Tmux()
        with patch.object(tmux, 'run', return_value=self.snapshot):
            for index in (0, -1, 4):
                with self.subTest(index=index), self.assertRaisesRegex(WorkmuxError, 'index out of range'):
                    resolve_workspace(tmux, None, index=index)
        with patch.object(tmux, 'run', return_value=''):
            with self.assertRaisesRegex(WorkmuxError, 'No workspaces'):
                resolve_workspace(tmux, None, index=1)

    def test_parser_requires_exactly_one_selection_mode(self):
        for arguments in ([], ['alpha', '--index', '1'], ['--index', 'abc']):
            with self.subTest(arguments=arguments), patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:
                    parser().parse_args(['workspace', 'select', *arguments])
                self.assertEqual(error.exception.code, 2)

    def test_cli_resolves_index_and_preserves_client_and_pane_options(self):
        with patch.dict(os.environ, {'TMUX_PANE': '%5'}, clear=True), \
                patch('workmux.cli.Tmux') as tmux:
            tmux.return_value.workspaces.return_value = [
                Workspace('$9', 'alpha', 'Alpha', '/tmp/a'),
                Workspace('$2', 'zebra', 'Zebra', '/tmp/z'),
            ]
            self.assertEqual(main(['workspace', 'select', '--index', '2',
                                   '--client', '/dev/pts/7', '--pane', '%8']), 0)
            tmux.return_value.select_workspace.assert_called_once_with('$2', '%8', '/dev/pts/7')

    def test_cli_invalid_index_does_not_switch(self):
        with patch.dict(os.environ, {'TMUX_PANE': '%5'}, clear=True), \
                patch('workmux.cli.Tmux') as tmux, \
                patch('sys.stderr', new_callable=io.StringIO) as stderr:
            tmux.return_value.workspaces.return_value = [object()]
            self.assertEqual(main(['workspace', 'select', '--index', '0']), 1)
            self.assertIn('index out of range', stderr.getvalue())
            tmux.return_value.select_workspace.assert_not_called()
