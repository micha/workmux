import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from workmux.cli import attach, main, resolve_workspace
from workmux.core import (Pane, Tmux, WorkmuxError, Workspace, sidebar_width, tmux_argument,
                          validate_directory, validate_label)
from workmux.sidebar import visible_offset


class InputTests(unittest.TestCase):
    def test_labels_allow_spaces_but_reject_tmux_and_shell_syntax(self):
        for label in ("project-a", "My Project", "123", "a_b"):
            self.assertEqual(validate_label(label), label)
        for label in ("", "-a", "a.b", "a:b", "a;touch x", "a\nb", "a\tb", "a" * 65):
            with self.subTest(label=label), self.assertRaises(WorkmuxError):
                validate_label(label)

    def test_directory_validation_resolves_paths_and_rejects_non_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(validate_directory(directory), str(Path(directory).resolve()))
            for path in (str(Path(directory) / "missing"), str(Path(directory) / "file")):
                if path.endswith("file"):
                    Path(path).touch()
                with self.assertRaises(WorkmuxError):
                    validate_directory(path)
            tabbed = Path(directory) / "bad\tname"
            tabbed.mkdir()
            with self.assertRaises(WorkmuxError):
                validate_directory(str(tabbed))

    def test_native_tmux_arguments_escape_only_trailing_semicolons(self):
        for value, expected in [('path space; middle', 'path space; middle'),
                                ('name;', 'name\\;'), (';', '\\;'), ('normal', 'normal')]:
            self.assertEqual(tmux_argument(value), expected)

    def test_sidebar_width_keeps_room_for_content(self):
        for width in range(25, 501):
            self.assertLessEqual(sidebar_width(width), 36)
            self.assertGreaterEqual(width - sidebar_width(width), 20)
        self.assertEqual(sidebar_width(120), 36)
        self.assertEqual(sidebar_width(56), 36)
        self.assertEqual(sidebar_width(40), 20)
        self.assertEqual(sidebar_width(30), 10)
        self.assertEqual(sidebar_width(120, 48), 48)
        self.assertEqual(sidebar_width(50, 48), 30)
        self.assertEqual(sidebar_width(120, 2), 4)

    def test_scroll_offset_always_keeps_selected_row_visible(self):
        for rows in range(1, 20):
            for index in range(100):
                for offset in (0, 5, 30, 99):
                    result = visible_offset(index, offset, rows)
                    self.assertLessEqual(result, index)
                    self.assertLess(index, result + rows)

    def test_private_tmux_executable_does_not_require_path_changes(self):
        executable = "/private package/bin/tmux"
        environment = {"PATH": "/user/profile/bin"}
        with patch.dict(os.environ, environment, clear=True), \
                patch("workmux.core.TMUX_EXECUTABLE", executable), \
                patch("workmux.core.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")
            self.assertEqual(Tmux(socket="/tmp/private").run("list-sessions"), "ok")
            self.assertEqual(run.call_args.args[0][:3], [executable, "-S", "/tmp/private"])
            self.assertEqual(dict(os.environ), environment)
        with patch.dict(os.environ, environment, clear=True), \
                patch("workmux.cli.TMUX_EXECUTABLE", executable), \
                patch("workmux.cli.subprocess.call", return_value=0) as call:
            self.assertEqual(attach(Tmux(socket="/tmp/private"), "$9"), 0)
            call.assert_called_once_with([executable, "-S", "/tmp/private", "attach-session", "-t", "$9"])
            self.assertEqual(dict(os.environ), environment)

    def test_helper_preserves_shell_arguments(self):
        tmux = Tmux(socket="/tmp/socket ' with spaces")
        command = shlex.split(tmux.helper("tab", "new", "my tab"))
        self.assertEqual(command[-5:], ["--socket", tmux.socket, "tab", "new", "my tab"])

    def test_helper_uses_the_module_without_checkout_launcher(self):
        with patch("workmux.core.__file__", "/tmp/installed/site-packages/workmux/core.py"), \
                patch("workmux.core.importlib.util.find_spec", return_value=None):
            command = shlex.split(Tmux().helper("--help"))
        self.assertEqual(command[:2], ["env", "PYTHONPATH=/tmp/installed/site-packages"])
        self.assertEqual(command[3:5], ["-m", "workmux"])
        self.assertNotIn("bin/workmux", " ".join(command))

    def test_helper_preserves_split_dependency_paths_and_deduplicates_them(self):
        for roots in (("/tmp/parser ' $;", "/tmp/grammar space"), ("/tmp/shared", "/tmp/shared")):
            with self.subTest(roots=roots), \
                    patch("workmux.core.__file__", "/tmp/app/workmux/core.py"), \
                    patch("workmux.core.importlib.util.find_spec", side_effect=[
                        Mock(origin=f"{roots[0]}/tree_sitter/__init__.py"),
                        Mock(origin=f"{roots[1]}/tree_sitter_tmux/__init__.py"),
                    ]) as find_spec:
                command = shlex.split(Tmux().helper("--help"))
                self.assertEqual(command[1], "PYTHONPATH=" + os.pathsep.join(dict.fromkeys(["/tmp/app", *roots])))
                self.assertEqual([call.args[0] for call in find_spec.call_args_list],
                                 ["tree_sitter", "tree_sitter_tmux"])

    def test_module_helper_runs_outside_checkout_with_no_pythonpath(self):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(shlex.split(Tmux().helper("--help")), cwd=directory,
                                    env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Workspace sidebar", result.stdout)

    def test_portable_helpers_use_the_entrypoint_and_quote_arguments(self):
        command = shlex.split(Tmux(socket="/tmp/socket ' with spaces").helper("tab", "new", portable=True))
        self.assertEqual(command, ["workmux", "--socket", "/tmp/socket ' with spaces", "tab", "new"])

    def test_config_contains_no_python_or_installation_paths(self):
        with patch("workmux.core.__file__", "/nix/store/example/site-packages/workmux/core.py"):
            config = Tmux()._default_config()
        for output in (config,):
            self.assertNotIn("/nix/store", output)
            self.assertNotIn("PYTHONPATH", output)
            self.assertNotIn("python", output)
            self.assertIn('run-shell -b "workmux --server workmux ', output)

    def test_panes_are_read_in_one_snapshot_without_individual_id_lookups(self):
        output = ("%1\t$0\t@0\t1\t0\t24\t120\t0\t/tmp\t28\t2\n"
                  "%2\t$0\t@0\t\t1\t95\t120\t0\t/tmp\t28\t2")
        tmux = Tmux()
        with patch.object(tmux, "run", return_value=output) as run:
            panes = tmux.panes("@0")
        self.assertEqual([pane.id for pane in panes], ["%1", "%2"])
        self.assertTrue(panes[0].sidebar)
        self.assertTrue(panes[1].active)
        self.assertEqual(panes[0].preferred_sidebar_width, 28)
        self.assertEqual(panes[0].window_panes, 2)
        run.assert_called_once_with("list-panes", "-t", "@0", "-F", tmux._pane_format())

    def test_width_repair_already_knows_when_sidebar_is_the_only_pane(self):
        tmux = Tmux()
        output = "%1\t$0\t@0\t1\t1\t120\t120\t0\t/tmp\t\t1"
        with patch.object(tmux, "run", return_value=output) as run:
            pane = tmux.repair_width("%1")
        self.assertEqual((pane.width, pane.window_panes), (120, 1))
        run.assert_called_once_with("display-message", "-p", "-t", "%1", tmux._pane_format())

    def test_selecting_an_explicit_agent_pane_skips_the_global_activity_scan(self):
        tmux = Tmux()
        sidebar = Pane('%0', '$0', '@0', True, True, 36, 120, False, '/tmp')
        content = Pane('%1', '$0', '@0', False, False, 83, 120, False, '/tmp')
        with patch.object(tmux, 'require_workspace'), patch.object(tmux, 'pane', return_value=sidebar), \
                patch.object(tmux, 'panes', return_value=[sidebar, content]), \
                patch.object(tmux, 'attention_tabs') as activity, patch.object(tmux, 'run') as run:
            tmux.select_tab('$0', '@0', '%0', 'client', destination_pane='%1')
        activity.assert_not_called()
        run.assert_any_call('select-pane', '-t', '%1')

    def test_workspace_resolution_by_id_name_or_label(self):
        with patch.object(Tmux, "workspaces", return_value=[
            Workspace("$4", "demo-session", "demo", "/tmp"),
        ]):
            for target in (None, "$4", "demo-session", "demo"):
                self.assertEqual(resolve_workspace(Tmux(), target), "$4")
            with self.assertRaises(WorkmuxError):
                resolve_workspace(Tmux(), "missing")

    def test_cli_error_is_actionable(self):
        with patch("sys.stderr", new_callable=io.StringIO) as stderr, patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(["split", "right"]), 1)
            self.assertIn("--pane", stderr.getvalue())

    def test_missing_tmux_is_reported(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(WorkmuxError, "Install tmux 3.8 or newer"):
                Tmux().run("list-sessions")

    def test_permission_errors_are_not_treated_as_an_empty_server(self):
        result = subprocess.CompletedProcess([], 1, "", "error connecting to socket (Permission denied)")
        with patch("subprocess.run", return_value=result):
            with self.assertRaisesRegex(WorkmuxError, "Permission denied"):
                Tmux().workspaces()

    def test_config_and_configure_are_no_longer_public_commands(self):
        for command in ("config", "configure"):
            with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                main([command])
            self.assertEqual(error.exception.code, 2)

    def test_missing_parser_dependency_has_installation_guidance(self):
        error = ModuleNotFoundError("No module", name="tree_sitter")
        with patch("workmux.cli.Tmux") as tmux, patch("sys.stderr", new_callable=io.StringIO) as stderr:
            tmux.return_value.enable.side_effect = error
            self.assertEqual(main(["enable", "--pane", "%1"]), 1)
            self.assertIn("dependencies", stderr.getvalue())

    def test_cli_infers_socket_from_tmux_environment(self):
        with patch.dict(os.environ, {"TMUX": "/tmp/socket,with,commas,1234,0", "TMUX_PANE": "%3"}):
            with patch("workmux.cli.Tmux") as tmux:
                tmux.return_value.enable.return_value.warnings = ()
                self.assertEqual(main(["enable"]), 0)
                tmux.assert_called_once_with(server="workmux", socket="/tmp/socket,with,commas")
                tmux.return_value.enable.assert_called_once_with("%3", None)

    def test_explicit_server_wins_over_tmux_environment(self):
        with patch.dict(os.environ, {"TMUX": "/tmp/wrong,1234,0", "TMUX_PANE": "%3"}):
            with patch("workmux.cli.Tmux") as tmux:
                tmux.return_value.enable.return_value.warnings = ()
                self.assertEqual(main(["--server", "chosen", "enable"]), 0)
                tmux.assert_called_once_with(server="chosen", socket=None)

    def test_team_reset_uses_calling_pane_and_its_socket(self):
        socket = "/tmp/socket,with,commas"
        pair = '{"slave 1": 172, "slave 2": 173}'
        with patch.dict(os.environ, {"TMUX": f"{socket},1234,0", "TMUX_PANE": "%3"}, clear=True), \
                patch("workmux.cli.Tmux") as tmux, patch("workmux.teams.reset_team", return_value=pair) as reset, \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            tmux.return_value.run.return_value = "1234"
            self.assertEqual(main(["--socket", socket, "team", "reset", "--profile", "phase ; two"]), 0)
            tmux.assert_called_once_with(socket=socket, start_server=False)
            tmux.return_value.run.assert_called_once_with("display-message", "-p", "-t", "%3", "#{pid}")
            reset.assert_called_once_with(tmux.return_value, "%3", "phase ; two")
            self.assertEqual(stdout.getvalue(), pair + "\n")

    def test_team_reset_rejects_other_servers_and_missing_pane_context(self):
        environment = {"TMUX": "/tmp/current,1234,0", "TMUX_PANE": "%3"}
        for env, flags in (({}, []), ({"TMUX": environment["TMUX"]}, []),
                           ({**environment, "TMUX_PANE": "3"}, []),
                           ({**environment, "TMUX": "/tmp/current"}, []),
                           (environment, ["--server", "other"]),
                           (environment, ["--socket", "/tmp/other"])):
            with self.subTest(env=env, flags=flags), patch.dict(os.environ, env, clear=True), \
                    patch("workmux.cli.Tmux") as tmux, patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(main([*flags, "team", "reset", "--profile", "implementation"]), 1)
                self.assertIn("master pane", stderr.getvalue())
                tmux.assert_not_called()

    def test_team_reset_rejects_a_replaced_server_at_the_same_socket(self):
        with patch.dict(os.environ, {"TMUX": "/tmp/current,1234,0", "TMUX_PANE": "%3"}, clear=True), \
                patch("workmux.cli.Tmux") as tmux, patch("workmux.teams.reset_team") as reset, \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            tmux.return_value.run.return_value = "5678"
            self.assertEqual(main(["team", "reset", "--profile", "implementation"]), 1)
            self.assertIn("server for the master pane has changed", stderr.getvalue())
            reset.assert_not_called()

    def test_team_new_rejects_a_team_name(self):
        with patch("workmux.cli.Tmux") as tmux, patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as error:
                main(["team", "new", "review"])
            self.assertEqual(error.exception.code, 2)
            self.assertIn("unrecognized arguments: review", stderr.getvalue())
            tmux.assert_not_called()

    def test_team_new_requires_both_managed_options(self):
        for flag in ("--launcher", "--profile"):
            with self.subTest(flag=flag), patch("workmux.teams.new_team") as new, \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(main(["team", "new", flag, "value"]), 1)
                self.assertIn("--launcher and --profile together", stderr.getvalue())
                new.assert_not_called()
