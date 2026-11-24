import curses
import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from workmux.cli import main
from workmux.core import Pane, Tab, Tmux, WorkmuxError, Workspace, _agent_state
from workmux.sidebar import SidebarRow, draw_row, sidebar_rows, status_attributes
from workmux.status import ANIMATION_INTERVAL, WORKING_FRAMES, working_indicator


class AgentStateTests(unittest.TestCase):
    def test_valid_reports_and_process_lifetime(self):
        self.assertEqual(_agent_state('{"state":"working"}', {}), "working")
        with patch("workmux.core.os.kill") as kill:
            cache = {}
            report = json.dumps({"state": "attention", "pid": 123})
            self.assertEqual(_agent_state(report, cache), "attention")
            self.assertEqual(_agent_state(report, cache), "attention")
            kill.assert_called_once_with(123, 0)
        with patch("workmux.core.os.kill", side_effect=ProcessLookupError):
            self.assertIsNone(_agent_state('{"state":"working","pid":123}', {}))
        with patch("workmux.core.os.kill", side_effect=PermissionError):
            self.assertEqual(_agent_state('{"state":"finished","pid":123}', {}), "finished")

    def test_invalid_reports_are_ignored(self):
        for value in ("", "broken", "[]", "null", "1", '{"state":[]}',
                      '{"state":"unknown"}', '{"state":"idle"}',
                      '{"state":"working","pid":true}', '{"state":"working","pid":0}',
                      '{"state":"working","pid":-123}', '{"state":"working","pid":"123"}'):
            with self.subTest(value=value):
                self.assertIsNone(_agent_state(value, {}))
        with patch("workmux.core.os.kill", side_effect=OverflowError):
            self.assertIsNone(_agent_state('{"state":"working","pid":123}', {}))

    def test_snapshot_aggregates_priority_and_native_alerts_in_one_call(self):
        output = (
            '$1\t@1\t2\t!\t%1\t\t0\t{"state":"finished"}\t\t\tbuild\n'
            '$1\t@1\t2\t!\t%2\t\t0\t{"state":"working"}\t\t\tbuild\n'
            '$1\t@1\t2\t!\t%3\t\t0\t{"state":"attention"}\t\t\tbuild\n'
            '$1\t@1\t2\t!\t%4\t1\t0\t{"state":"attention"}\t\t\tbuild\n'
            '$1\t@2\t1\t#\t%5\t\t0\t\t\t\tlogs\n'
            '$1\t@3\t3\t~\t%6\t\t1\t{"state":"working"}\t\t\tsilence\n'
            '$1\t@4\t4\t*\t%7\t\t0\t\t\t\tordinary\n'
            '$2\t@5\t0\t!\t%8\t\t0\t{"state":"finished"}\t\t\treview\n'
            'malformed row\n'
        )
        with patch.object(Tmux, "run", return_value=output) as run:
            tabs = Tmux().attention_tabs()
        self.assertEqual(tabs, [Tab("$1", "@2", 1, "logs", "alert"),
                                Tab("$1", "@1", 2, "build", "attention", "%3", agents=(
                                    Tab("$1", "@1", 2, "build", "attention", "%3"),
                                    Tab("$1", "@1", 2, "build", "working", "%2"),
                                    Tab("$1", "@1", 2, "build", "finished", "%1"))),
                                Tab("$1", "@3", 3, "silence", "alert"),
                                Tab("$2", "@5", 0, "review", "finished", "%8", agents=(
                                    Tab("$2", "@5", 0, "review", "finished", "%8"),))])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[:3], ("list-panes", "-a", "-F"))

    def test_passive_states_disappear_when_native_alert_is_cleared(self):
        for state in ("attention", "finished", "working"):
            with self.subTest(state=state):
                line = '$0\t@0\t1\t{flags}\t%0\t\t0\t' + json.dumps({"state": state}) + '\t\t\twork'
                with patch.object(Tmux, "run", side_effect=[line.replace("{flags}", "!"),
                                                           line.replace("{flags}", "*")]) as run:
                    tmux = Tmux()
                    agent = Tab("$0", "@0", 1, "work", state, "%0")
                    self.assertEqual(tmux.attention_tabs(), [Tab("$0", "@0", 1, "work", state, "%0",
                                                              agents=(agent,))])
                    expected = [Tab("$0", "@0", 1, "work", "working", "%0", agents=(agent,))] if state == "working" else []
                    self.assertEqual(tmux.attention_tabs(), expected)
                    self.assertEqual(run.call_count, 2)  # Snapshotting does not mutate reports/options.

    def test_cleared_attention_falls_back_to_another_working_agent(self):
        output = ('$0\t@0\t1\t*\t%0\t\t0\t{"state":"attention"}\t\t\twork\n'
                  '$0\t@0\t1\t*\t%1\t\t0\t{"state":"working"}\t\t\twork')
        with patch.object(Tmux, "run", return_value=output):
            self.assertEqual(Tmux().attention_tabs(), [Tab("$0", "@0", 1, "work", "working", "%1",
                agents=(Tab("$0", "@0", 1, "work", "working", "%1"),))])

    def test_stale_pid_does_not_mask_native_alert_and_window_name_keeps_tabs(self):
        output = '$0\t@0\t1\t!\t%0\t\t0\t{"state":"working","pid":123}\t\tstale title\tname\twith tab'
        with patch.object(Tmux, "run", return_value=output), \
                patch("workmux.core.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(Tmux().attention_tabs(), [Tab("$0", "@0", 1, "name\twith tab", "alert")])

    def test_titles_follow_winning_agent_and_empty_titles_fall_back(self):
        output = ('\u00240\t@0\t1\t!\t%0\t\t0\t{"state":"working"}\t\tworker\twork\n'
                  '\u00240\t@0\t1\t!\t%1\t\t0\t{"state":"attention"}\t\tπ - workmux\twork\n'
                  '\u00240\t@0\t1\t!\t%2\t1\t0\t{"state":"attention"}\t\tsidebar\twork\n'
                  '\u00240\t@0\t1\t!\t%3\t\t1\t{"state":"attention"}\t\tdead\twork')
        with patch.object(Tmux, 'run', return_value=output) as run:
            tab = Tmux().attention_tabs()[0]
        self.assertEqual(tab.name, 'work')
        self.assertEqual(tab.title, 'π - workmux')
        self.assertEqual(tab.pane, '%1')
        self.assertEqual(run.call_count, 1)
        workspace = Workspace('$0', 'workspace', '', '/jobs')
        self.assertTrue(sidebar_rows([workspace], [tab])[1].text.endswith('1:π - workmux'))
        for title in ('', '   '):
            with patch.object(Tmux, 'run', return_value=output.splitlines()[1].replace('π - workmux', title)):
                tab = Tmux().attention_tabs()[0]
            self.assertIsNone(tab.title)
            self.assertTrue(sidebar_rows([workspace], [tab])[1].text.endswith('1:work'))

    def test_titles_are_used_in_checkout_groups_and_sanitized(self):
        tab = Tab('$0', '@0', 1, 'work', 'working', '%0', ('/jobs/repo',), 'π\x1b\x07 title')
        workspace = Workspace('$0', 'workspace', '', '/jobs')
        rows = sidebar_rows([workspace], [tab], checkouts={'$0': ('/jobs/repo',)})
        self.assertTrue(rows[-1].text.endswith('1:π   title'))
        self.assertEqual(rows[-1].key, ('$0', '%0'))

    def test_pane_indexes_are_display_only_and_native_alerts_keep_window_index(self):
        output = ('$0\t@0\t2\t!\t%7\t\t0\t{"state":"working","root":"/jobs/repo"}\t0\tleft\twork\n'
                  '$0\t@0\t2\t!\t%9\t\t0\t{"state":"attention","root":"/jobs/repo"}\t3\tright\twork\n'
                  '$0\t@1\t4\t!\t%8\t\t0\t\t1\tshell\tlogs')
        workspace = Workspace('$0', 'workspace', '', '/jobs')
        with patch.object(Tmux, 'run', return_value=output):
            tabs = Tmux().attention_tabs()
        self.assertEqual(tabs[0].pane_index, 3)
        self.assertEqual([agent.pane_index for agent in tabs[0].agents], [3, 0])
        for checkouts in ({}, {'$0': ('/jobs/repo',)}):
            rows = sidebar_rows([workspace], tabs, checkouts=checkouts)
            agents = [row for row in rows if row.pane]
            self.assertEqual([row.text.rsplit(' ', 1)[-1] for row in agents],
                             ['2.3:right', '2.0:left'])
            self.assertTrue(rows[-1].text.endswith('4:logs'))
        with patch.object(Tmux, 'run', return_value=output.replace('\t3\tright', '\t5\tright')):
            changed = sidebar_rows([workspace], Tmux().attention_tabs())
        self.assertEqual([row.key for row in rows if row.pane],
                         [row.key for row in changed if row.pane])
        self.assertTrue(changed[1].text.endswith('2.5:right'))

    def test_native_alerts_do_not_use_unreported_pane_titles(self):
        line = '$0\t@0\t1\t!\t%0\t\t0\t\t\tterminal title\twork'
        with patch.object(Tmux, 'run', return_value=line):
            self.assertEqual(Tmux().attention_tabs(), [Tab('$0', '@0', 1, 'work', 'alert')])

    def test_report_sets_only_pane_metadata_and_idle_clears_it(self):
        pane = Pane("%4", "$0", "@0", False, True, 95, 120, False, "/tmp")
        with patch.object(Tmux, "pane", return_value=pane), patch.object(Tmux, "run") as run:
            tmux = Tmux()
            tmux.report_agent_state("%4", "working", 123)
            args = run.call_args.args
            self.assertEqual(args[:5], ("set-option", "-p", "-t", "%4", "@workmux-agent"))
            self.assertEqual(json.loads(args[5]), {"state": "working", "pid": 123})
            tmux.report_agent_state("%4", "idle")
            self.assertEqual(run.call_args.args, ("set-option", "-pu", "-t", "%4", "@workmux-agent"))

    def test_invalid_state_pid_and_sidebar_are_rejected(self):
        sidebar = Pane("%4", "$0", "@0", True, False, 24, 120, False, "/tmp")
        tmux = Tmux()
        for state, pid in (("unknown", None), ("working", -1), ("working", 0)):
            with self.assertRaises(WorkmuxError):
                tmux.report_agent_state("%4", state, pid)
        with patch.object(Tmux, "pane", return_value=sidebar), self.assertRaisesRegex(WorkmuxError, "sidebar"):
            tmux.report_agent_state("%4", "working")

    def test_roots_aggregate_all_live_agents_independently_of_display_priority(self):
        def line(pane, state, root=None, pid=None, dead='0'):
            report = {'state': state}
            if root is not None:
                report['root'] = root
            if pid is not None:
                report['pid'] = pid
            return f'$1\t@1\t1\t!\t{pane}\t\t{dead}\t{json.dumps(report)}\t\t\twork'

        output = '\n'.join([line('%1', 'attention', '/job/api'),
                            line('%2', 'working', '/job/ui'),
                            line('%3', 'finished'),
                            line('%4', 'working', '/stale', 123),
                            line('%5', 'working', '/dead', dead='1')])
        with patch.object(Tmux, 'run', return_value=output) as run, \
                patch('workmux.core.os.kill', side_effect=ProcessLookupError):
            tab = Tmux().attention_tabs()[0]
        self.assertEqual(tab.state, 'attention')
        self.assertEqual(tab.roots, ('/job/api', '/job/ui', None))
        self.assertEqual([(agent.pane, agent.roots) for agent in tab.agents],
                         [('%1', ('/job/api',)), ('%2', ('/job/ui',)), ('%3', ())])
        self.assertEqual(run.call_count, 1)

    def test_each_pane_is_a_workspace_child_regardless_of_reported_roots(self):
        workspace = Workspace('$1', 'job', '', '/job')
        cases = [
            [['/job'], ['/job/api']],
            [['/job/api'], ['/job/ui']],
            [[], ['/job/api']],
            [['/job/api', '/job/ui'], ['/job/api/src']],
            [['/job/api', None], ['/job/api']],
        ]
        for roots in cases:
            for flags in ('!', '*'):
                with self.subTest(roots=roots, flags=flags):
                    lines = []
                    for index, paths in enumerate(roots):
                        report = json.dumps({'state': 'working', 'roots': paths})
                        lines.append(f'$1\t@1\t1\t{flags}\t%{index}\t\t0\t{report}\t{index}\tagent{index}\twork')
                    # Passive reports still follow native alert visibility.
                    report = json.dumps({'state': 'finished', 'root': '/elsewhere'})
                    lines.append(f'$1\t@1\t1\t{flags}\t%9\t\t0\t{report}\t9\tpassive\twork')
                    with patch.object(Tmux, 'run', return_value='\n'.join(lines)):
                        tabs = Tmux().attention_tabs()
                    rows = sidebar_rows([workspace], tabs,
                                        checkouts={'$1': ('/job/api', '/job/ui')})
                    agents = {row.pane: row for row in rows if row.pane}
                    for row in agents.values():
                        self.assertTrue(row.text.startswith(('├ ✦', '└ ✦')))
                    self.assertTrue(all(f'%{index}' in agents for index in range(len(roots))))
                    self.assertEqual(tabs[0].state, 'working')
                    self.assertEqual('%9' in agents, flags == '!')

    def test_multiple_root_sessions_in_one_pane_are_reported_and_snapshotted(self):
        pane = Pane('%4', '$0', '@0', False, True, 95, 120, False, '/shell')
        with patch.object(Tmux, 'pane', return_value=pane), patch.object(Tmux, 'run') as run:
            Tmux().report_agent_state('%4', 'working', root=['/job/api', '/job/api/src', '/job/api'])
            report = json.loads(run.call_args.args[5])
        self.assertEqual(report, {'state': 'working', 'roots': ['/job/api', '/job/api/src']})
        line = '$0\t@0\t1\t*\t%4\t\t0\t' + json.dumps(report) + '\t\t\twork'
        with patch.object(Tmux, 'run', return_value=line):
            self.assertEqual(Tmux().attention_tabs()[0].roots, ('/job/api', '/job/api/src'))
        with patch('workmux.cli.Tmux') as tmux:
            self.assertEqual(main(['agent', 'state', 'working', '--pane', '%4',
                                   '--root', '/job/api', '--root', '/job/ui']), 0)
            tmux.return_value.report_agent_state.assert_called_once_with(
                '%4', 'working', None, ['/job/api', '/job/ui'])

    def test_root_is_explicit_absolute_metadata_and_cli_forwards_it(self):
        pane = Pane('%4', '$0', '@0', False, True, 95, 120, False, '/unrelated-shell')
        with patch.object(Tmux, 'pane', return_value=pane), patch.object(Tmux, 'run') as run:
            Tmux().report_agent_state('%4', 'working', root='/job/repo/src/..')
            self.assertEqual(json.loads(run.call_args.args[5]), {'state': 'working', 'root': '/job/repo'})
            with self.assertRaisesRegex(WorkmuxError, 'absolute'):
                Tmux().report_agent_state('%4', 'working', root='relative')
        with patch('workmux.cli.Tmux') as tmux:
            self.assertEqual(main(['agent', 'state', 'working', '--pane', '%4', '--root', '/job/repo']), 0)
            tmux.return_value.report_agent_state.assert_called_once_with('%4', 'working', None, '/job/repo')

    def test_cli_reports_state_and_requires_a_content_target(self):
        with patch("workmux.cli.Tmux") as tmux:
            self.assertEqual(main(["agent", "state", "working", "--pane", "%4", "--pid", "123"]), 0)
            tmux.return_value.report_agent_state.assert_called_once_with("%4", "working", 123, None)
        with patch.dict(os.environ, {}, clear=True), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main(["agent", "state", "idle"]), 1)
            self.assertIn("--pane", stderr.getvalue())


class SidebarTreeTests(unittest.TestCase):
    def test_status_colors_match_agent_meanings_and_other_alerts_are_yellow(self):
        with patch("workmux.sidebar.curses.has_colors", return_value=True), \
                patch("workmux.sidebar.curses.start_color"), \
                patch("workmux.sidebar.curses.use_default_colors"), \
                patch("workmux.sidebar.curses.init_pair") as init_pair, \
                patch("workmux.sidebar.curses.color_pair", side_effect=lambda pair: pair * 256):
            colors = status_attributes()

        self.assertEqual(colors, {"working": 256, "attention": 512,
                                  "finished": 768, "other": 1024,
                                  "git-clean": 1280, "git-dirty": 1536,
                                  "git-ahead": 1792, "git-behind": 2048,
                                  "git-diverged": 2304, "git-conflict": 2560})
        self.assertEqual([call.args[1] for call in init_pair.call_args_list],
                         [curses.COLOR_BLUE, curses.COLOR_RED,
                          curses.COLOR_GREEN, curses.COLOR_YELLOW,
                          curses.COLOR_GREEN, curses.COLOR_YELLOW,
                          curses.COLOR_CYAN, curses.COLOR_BLUE,
                          curses.COLOR_MAGENTA, curses.COLOR_RED])

    def test_working_indicator_cycles_through_circle_phases(self):
        self.assertEqual([working_indicator(frame * ANIMATION_INTERVAL)
                          for frame in range(len(WORKING_FRAMES))], list(WORKING_FRAMES))
        self.assertEqual(working_indicator(len(WORKING_FRAMES) * ANIMATION_INTERVAL), WORKING_FRAMES[0])
        self.assertEqual(working_indicator(ANIMATION_INTERVAL - 0.001), WORKING_FRAMES[0])

    def test_animated_working_symbol_receives_its_color(self):
        class Screen:
            def __init__(self):
                self.calls = []

            def getmaxyx(self):
                return 20, 80

            def addnstr(self, *args):
                self.calls.append(args)

        screen = Screen()
        row = SidebarRow("$1", "@1", "└ ✦ 1:build", "working")
        draw_row(screen, 2, "›", row, 0, {"working": 256}, "◑")
        self.assertEqual(screen.calls[0], (2, 0, "◑ └ ✦ 1:build", 79, 0))
        self.assertEqual(screen.calls[1], (2, 2, "└", 1, curses.A_DIM))
        self.assertEqual(screen.calls[2], (2, 0, "◑", 1, 256))

    def test_agent_status_uses_gutter_without_changing_title_or_spark(self):
        for state, indicator in (('working', '◑'), ('attention', '⚠'), ('finished', '✓')):
            for prefix in ('└', '│  └', '   └'):
                with self.subTest(state=state, prefix=prefix):
                    screen = Mock()
                    screen.getmaxyx.return_value = (20, 80)
                    # A title containing status symbols must be rendered verbatim.
                    text = f'{prefix} ✦ 2.1:⣷ ⚠ ✓ title'
                    row = SidebarRow('$1', '@1', text, state, pane='%7')
                    draw_row(screen, 2, '›', row, curses.A_REVERSE | curses.A_DIM,
                             {state: 256}, '◑')
                    self.assertEqual(screen.addnstr.call_args_list[0].args,
                                     (2, 0, f'{indicator} {text}', 79, curses.A_REVERSE | curses.A_DIM))
                    self.assertEqual(screen.addnstr.call_args_list[-1].args,
                                     (2, 0, indicator, 1, curses.A_REVERSE | 256))
                    self.assertEqual(screen.addnstr.call_count, 3 if prefix == '└' else 2)

    def test_native_alert_is_undimmed_in_the_gutter_with_or_without_color(self):
        for attr in (curses.A_BOLD, curses.A_REVERSE | curses.A_DIM):
            for colors in ({}, {"other": 1024}):
                with self.subTest(attr=attr, colors=colors):
                    screen = Mock()
                    screen.getmaxyx.return_value = (20, 80)
                    row = SidebarRow("$1", "@1", "└ 1:logs ◆ in title", "alert")
                    draw_row(screen, 2, " ", row, attr, colors)
                    self.assertEqual([call.args for call in screen.addnstr.call_args_list], [
                        (2, 0, "◆ └ 1:logs ◆ in title", 79, attr),
                        (2, 2, "└", 1, attr | curses.A_DIM),
                        (2, 0, "◆", 1, (attr & ~curses.A_DIM) | colors.get("other", 0)),
                    ])

    def test_idle_tabs_are_hidden_and_children_are_in_window_order(self):
        workspaces = [Workspace("$1", "one", "first", "/tmp"), Workspace("$2", "two", "second", "/tmp")]
        tabs = [Tab("$1", "@3", 3, "done", "finished"),
                Tab("$1", "@2", 2, "question", "attention"),
                Tab("$1", "@1", 1, "build", "working"),
                Tab("$1", "@0", 0, "idle", "idle"),
                Tab("$2", "@4", 1, "logs", "alert"),
                Tab("$9", "@9", 0, "unmanaged", "working")]
        rows = sidebar_rows(workspaces, tabs)
        self.assertEqual([row.key for row in rows], [("$1", None), ("$1", "@1"), ("$1", "@2"),
                                                    ("$1", "@3"), ("$2", None), ("$2", "@4")])
        self.assertEqual([row.text for row in rows], ["1:one", "├ ✦ 1:build", "├ ✦ 2:question",
                                                     "└ ✦ 3:done", "2:two", "└ 1:logs"])

    def test_workspace_positions_change_without_changing_row_identity(self):
        alpha = Workspace("$8", "alpha", "old label", "/tmp")
        beta = Workspace("$2", "beta", "beta", "/tmp")
        original = sidebar_rows([beta], [])
        reordered = sidebar_rows([alpha, beta], [])
        self.assertEqual(original[0].text, "1:beta")
        self.assertEqual(reordered[0].text, "1:alpha")
        self.assertEqual(reordered[1].text, "2:beta")
        self.assertEqual(original[0].key, reordered[1].key)

    def test_row_identity_survives_renames_and_index_changes(self):
        workspace = Workspace("$1", "one", "name", "/tmp")
        original = sidebar_rows([workspace], [Tab("$1", "@3", 3, "old", "working")])
        changed = sidebar_rows([workspace], [Tab("$1", "@3", 1, "new", "attention")])
        self.assertEqual(original[1].key, changed[1].key)
        self.assertNotEqual(original[1].text, changed[1].text)

    def test_labels_cannot_inject_terminal_controls(self):
        rows = sidebar_rows([Workspace("$1", "bad\x1b\tname", "unused", "/tmp")],
                            [Tab("$1", "@1", 1, "window\x07\x1b\tname", "working")])
        self.assertEqual(rows[0].text, "1:bad  name")
        self.assertNotIn("\x1b", rows[1].text)
        self.assertNotIn("\x07", rows[1].text)
        self.assertNotIn("\t", rows[1].text)
