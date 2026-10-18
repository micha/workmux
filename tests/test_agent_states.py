import io
import json
import os
import unittest
from unittest.mock import patch

from workmux.cli import main
from workmux.core import Pane, Tab, Tmux, WorkmuxError, Workspace, _agent_state
from workmux.sidebar import sidebar_rows


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
            '$1\t@1\t2\t!\t%1\t\t0\t{"state":"finished"}\tbuild\n'
            '$1\t@1\t2\t!\t%2\t\t0\t{"state":"working"}\tbuild\n'
            '$1\t@1\t2\t!\t%3\t\t0\t{"state":"attention"}\tbuild\n'
            '$1\t@1\t2\t!\t%4\t1\t0\t{"state":"attention"}\tbuild\n'
            '$1\t@2\t1\t#\t%5\t\t0\t\tlogs\n'
            '$1\t@3\t3\t~\t%6\t\t1\t{"state":"working"}\tsilence\n'
            '$1\t@4\t4\t*\t%7\t\t0\t\tordinary\n'
            '$2\t@5\t0\t!\t%8\t\t0\t{"state":"finished"}\treview\n'
            'malformed row\n'
        )
        with patch.object(Tmux, "run", return_value=output) as run:
            tabs = Tmux().attention_tabs()
        self.assertEqual(tabs, [Tab("$1", "@2", 1, "logs", "alert"),
                                Tab("$1", "@1", 2, "build", "attention", "%3"),
                                Tab("$1", "@3", 3, "silence", "alert"),
                                Tab("$2", "@5", 0, "review", "finished", "%8")])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[:3], ("list-panes", "-a", "-F"))

    def test_passive_states_disappear_when_native_alert_is_cleared(self):
        for state in ("attention", "finished", "working"):
            with self.subTest(state=state):
                line = '$0\t@0\t1\t{flags}\t%0\t\t0\t' + json.dumps({"state": state}) + '\twork'
                with patch.object(Tmux, "run", side_effect=[line.replace("{flags}", "!"),
                                                           line.replace("{flags}", "*")]) as run:
                    tmux = Tmux()
                    self.assertEqual(tmux.attention_tabs(), [Tab("$0", "@0", 1, "work", state, "%0")])
                    expected = [Tab("$0", "@0", 1, "work", "working", "%0")] if state == "working" else []
                    self.assertEqual(tmux.attention_tabs(), expected)
                    self.assertEqual(run.call_count, 2)  # Snapshotting does not mutate reports/options.

    def test_cleared_attention_falls_back_to_another_working_agent(self):
        output = ('$0\t@0\t1\t*\t%0\t\t0\t{"state":"attention"}\twork\n'
                  '$0\t@0\t1\t*\t%1\t\t0\t{"state":"working"}\twork')
        with patch.object(Tmux, "run", return_value=output):
            self.assertEqual(Tmux().attention_tabs(), [Tab("$0", "@0", 1, "work", "working", "%1")])

    def test_stale_pid_does_not_mask_native_alert_and_window_name_keeps_tabs(self):
        output = '$0\t@0\t1\t!\t%0\t\t0\t{"state":"working","pid":123}\tname\twith tab'
        with patch.object(Tmux, "run", return_value=output), \
                patch("workmux.core.os.kill", side_effect=ProcessLookupError):
            self.assertEqual(Tmux().attention_tabs(), [Tab("$0", "@0", 1, "name\twith tab", "alert")])

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

    def test_cli_reports_state_and_requires_a_content_target(self):
        with patch("workmux.cli.Tmux") as tmux:
            self.assertEqual(main(["agent", "state", "working", "--pane", "%4", "--pid", "123"]), 0)
            tmux.return_value.report_agent_state.assert_called_once_with("%4", "working", 123)
        with patch.dict(os.environ, {}, clear=True), patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(main(["agent", "state", "idle"]), 1)
            self.assertIn("--pane", stderr.getvalue())


class SidebarTreeTests(unittest.TestCase):
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
        self.assertEqual([row.text for row in rows], ["first", "  ├─ ⚙ 1:build", "  ├─ ⚠ 2:question",
                                                     "  └─ ✓ 3:done", "second", "  └─ ● 1:logs"])

    def test_row_identity_survives_renames_and_index_changes(self):
        workspace = Workspace("$1", "one", "name", "/tmp")
        original = sidebar_rows([workspace], [Tab("$1", "@3", 3, "old", "working")])
        changed = sidebar_rows([workspace], [Tab("$1", "@3", 1, "new", "attention")])
        self.assertEqual(original[1].key, changed[1].key)
        self.assertNotEqual(original[1].text, changed[1].text)

    def test_labels_cannot_inject_terminal_controls(self):
        rows = sidebar_rows([Workspace("$1", "one", "bad\x1b\tname", "/tmp")],
                            [Tab("$1", "@1", 1, "window\x07\x1b\tname", "working")])
        self.assertEqual(rows[0].text, "bad  name")
        self.assertNotIn("\x1b", rows[1].text)
        self.assertNotIn("\x07", rows[1].text)
        self.assertNotIn("\t", rows[1].text)
