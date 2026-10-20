"""Server-wide sidebar reload ownership, races, and CLI behavior."""

import io
import os
import unittest
from unittest.mock import patch

from workmux.cli import main
from workmux.core import Tmux, WorkmuxError, quote_tmux


class SidebarReloadTests(unittest.TestCase):
    def test_only_owned_panes_are_selected_and_shared_panes_are_deduplicated(self):
        snapshot = ('%1\t$0\t@0\t1\t1\n'
                    '%2\t$0\t@0\t1\t\n'
                    '%3\t$1\t@1\t\t1\n'
                    '%1\t$2\t@0\t1\t1\n'
                    '%4\t$2\t@2\t1\t1\n'
                    'malformed row')
        tmux = Tmux(socket="/tmp/socket ' $TOKEN ; space")
        with patch.object(tmux, 'run', side_effect=[snapshot, '1', '']) as run:
            self.assertEqual(tmux.reload_sidebars(), 1)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args_list[0].args[:3], ('list-panes', '-a', '-F'))
        for call, target, pane in zip(run.call_args_list[1:], ('$0:@0.%1', '$2:@2.%4'), ('%1', '%4')):
            self.assertEqual(call.args[:4], ('if-shell', '-F', '-t', target))
            self.assertIn('#{@workmux}', call.args[4])
            self.assertIn('#{@workspace-sidebar}', call.args[4])
            self.assertIn('respawn-pane -k -t ' + pane, call.args[5])
            self.assertIn(quote_tmux(tmux.helper('sidebar', 'run')), call.args[5])
            self.assertNotIn('kill-pane', call.args[5])

    def test_closed_panes_are_skipped_but_other_errors_propagate(self):
        snapshot = '%1\t$0\t@0\t1\t1'
        for message in ("can't find pane: %1", "can't find window: @0", "can't find session: $0"):
            with patch.object(Tmux, 'run', side_effect=[snapshot, WorkmuxError(message)]):
                self.assertEqual(Tmux().reload_sidebars(), 0)
        with patch.object(Tmux, 'run', side_effect=[snapshot, WorkmuxError('permission denied')]):
            with self.assertRaisesRegex(WorkmuxError, 'permission denied'):
                Tmux().reload_sidebars()

    def test_no_server_or_no_owned_sidebars_is_a_noop(self):
        for snapshot in ('', '%1\t$0\t@0\t\t', '%1\t$0\t@0\t1\t'):
            with patch.object(Tmux, 'run', return_value=snapshot) as run:
                self.assertEqual(Tmux().reload_sidebars(), 0)
            self.assertEqual(run.call_count, 1)
            self.assertTrue(run.call_args.kwargs['missing_ok'])

    def test_cli_needs_no_source_pane_and_uses_the_selected_server(self):
        with patch.dict(os.environ, {}, clear=True), patch('workmux.cli.Tmux') as tmux, \
                patch('sys.stdout', new_callable=io.StringIO) as stdout:
            tmux.return_value.reload_sidebars.return_value = 3
            self.assertEqual(main(['--socket', '/tmp/selected', 'sidebar', 'reload']), 0)
            tmux.assert_called_once_with(server='workmux', socket='/tmp/selected')
            tmux.return_value.reload_sidebars.assert_called_once_with()
            self.assertEqual(stdout.getvalue(), 'Reloaded 3 sidebar panes.\n')
        with patch.dict(os.environ, {'TMUX': '/tmp/current,123,0'}, clear=True), \
                patch('workmux.cli.Tmux') as tmux:
            tmux.return_value.reload_sidebars.return_value = 0
            with patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(main(['sidebar', 'reload']), 0)
            tmux.assert_called_once_with(server='workmux', socket='/tmp/current')
