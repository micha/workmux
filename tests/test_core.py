import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workmux.cli import main, resolve_workspace
from workmux.core import (Tmux, WorkmuxError, Workspace, sidebar_width, tmux_argument,
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

    def test_helper_preserves_shell_arguments(self):
        tmux = Tmux(socket="/tmp/socket ' with spaces")
        command = shlex.split(tmux.helper("tab", "new", "my tab"))
        self.assertEqual(command[-5:], ["--socket", tmux.socket, "tab", "new", "my tab"])

    def test_helper_uses_the_module_without_checkout_launcher(self):
        with patch("workmux.core.__file__", "/tmp/installed/site-packages/workmux/core.py"):
            command = shlex.split(Tmux().helper("--help"))
        self.assertEqual(command[:2], ["env", "PYTHONPATH=/tmp/installed/site-packages"])
        self.assertEqual(command[3:5], ["-m", "workmux"])
        self.assertNotIn("bin/workmux", " ".join(command))

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
        output = ("%1\t$0\t@0\t1\t0\t24\t120\t0\t/tmp\t28\n"
                  "%2\t$0\t@0\t\t1\t95\t120\t0\t/tmp\t28")
        tmux = Tmux()
        with patch.object(tmux, "run", return_value=output) as run:
            panes = tmux.panes("@0")
        self.assertEqual([pane.id for pane in panes], ["%1", "%2"])
        self.assertTrue(panes[0].sidebar)
        self.assertTrue(panes[1].active)
        self.assertEqual(panes[0].preferred_sidebar_width, 28)
        run.assert_called_once_with("list-panes", "-t", "@0", "-F", tmux._pane_format())

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
            with self.assertRaisesRegex(WorkmuxError, "not installed"):
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
