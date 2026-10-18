import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workmux.cli import main
from workmux.core import WorkmuxError
from workmux.plugins import PI_FILENAME, install_pi, pi_agent_dir, pi_extension, pi_status, uninstall_pi


def managed(body):
    return f"// workmux-managed: pi-notify sha256={hashlib.sha256(body).hexdigest()}\n".encode() + body


class PiPluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workmux-pi-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "agent ' $; with spaces"
        self.target = self.directory / "extensions" / PI_FILENAME

    def test_directory_precedence_and_home_default(self):
        with patch.dict(os.environ, {}, clear=True), patch("workmux.plugins.Path.home", return_value=self.directory):
            self.assertEqual(pi_agent_dir(), self.directory / ".pi" / "agent")
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.directory)}):
            self.assertEqual(pi_agent_dir(), self.directory)
            self.assertEqual(pi_agent_dir("/explicit"), Path("/explicit"))
        with patch.dict(os.environ, {"HOME": str(self.directory)}):
            self.assertEqual(pi_agent_dir("~/pi"), self.directory / "pi")

    def test_install_is_idempotent_and_uninstall_is_scoped(self):
        self.target.parent.mkdir(parents=True)
        other = self.target.parent / "desktop-notify.ts"
        other.write_text("personal extension")
        settings = self.directory / "settings.json"
        settings.write_text('{"someSetting": true}')
        self.assertIn("Installed:", install_pi(str(self.directory)))
        self.assertEqual(self.target.read_bytes(), pi_extension())
        stat = self.target.stat()
        self.assertIn("Already installed:", install_pi(str(self.directory)))
        self.assertEqual(self.target.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertIn("Removed:", uninstall_pi(str(self.directory)))
        self.assertFalse(self.target.exists())
        self.assertEqual(other.read_text(), "personal extension")
        self.assertEqual(settings.read_text(), '{"someSetting": true}')
        self.assertIn("Not installed:", uninstall_pi(str(self.directory)))

    def test_missing_uninstall_creates_no_directories(self):
        self.assertIn("Not installed:", uninstall_pi(str(self.directory)))
        self.assertFalse(self.directory.exists())

    def test_install_updates_and_uninstall_removes_intact_older_versions(self):
        self.target.parent.mkdir(parents=True)
        older = managed(b"// older workmux extension\n")
        self.target.write_bytes(older)
        install_pi(str(self.directory))
        self.assertEqual(self.target.read_bytes(), pi_extension())
        self.target.write_bytes(older)
        uninstall_pi(str(self.directory))
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_both_operations_preserve_unowned_and_edited_files(self):
        self.target.parent.mkdir(parents=True)
        for content in (b"personal file", pi_extension() + b"// custom edit\n",
                        b"// workmux-managed: pi-notify sha256=bad\n", b""):
            for operation in (install_pi, uninstall_pi):
                with self.subTest(content=content[:60], operation=operation.__name__):
                    self.target.write_bytes(content)
                    with self.assertRaisesRegex(WorkmuxError, "unowned or edited"):
                        operation(str(self.directory))
                    self.assertEqual(self.target.read_bytes(), content)

    def test_symlinks_including_dangling_ones_are_not_followed(self):
        self.target.parent.mkdir(parents=True)
        other = self.directory / "outside.ts"
        for dangling in (False, True):
            if not dangling:
                other.write_bytes(pi_extension())
            else:
                other.unlink()
            self.target.symlink_to(other)
            for operation in (install_pi, uninstall_pi):
                with self.assertRaisesRegex(WorkmuxError, "symlink"):
                    operation(str(self.directory))
                self.assertTrue(self.target.is_symlink())
            if not dangling:
                self.assertEqual(other.read_bytes(), pi_extension())
            self.target.unlink()

    def test_cli_uses_explicit_directory_and_never_constructs_tmux(self):
        with patch("workmux.cli.Tmux") as tmux, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["install", "pi", "--agent-dir", str(self.directory)]), 0)
            self.assertTrue(self.target.exists())
            self.assertIn("/reload", stdout.getvalue())
            self.assertEqual(main(["uninstall", "pi", "--agent-dir", str(self.directory)]), 0)
            tmux.assert_not_called()

    def test_cli_honors_pi_environment_override(self):
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.directory)}), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["install", "pi"]), 0)
            self.assertTrue(self.target.exists())
            self.assertEqual(main(["uninstall", "pi"]), 0)
            self.assertFalse(self.target.exists())

    def test_cli_reports_conflicts_and_filesystem_errors(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("personal file")
        for operation in ("install", "uninstall"):
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(main([operation, "pi", "--agent-dir", str(self.directory)]), 1)
                self.assertIn("Refusing", stderr.getvalue())
        with patch("workmux.plugins.pi_extension", side_effect=PermissionError("denied")), \
                patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.target.unlink()
            self.assertEqual(main(["install", "pi", "--agent-dir", str(self.directory)]), 1)
            self.assertIn("denied", stderr.getvalue())

    def test_cli_requires_a_supported_plugin(self):
        for arguments in (["uninstall"], ["install", "other"], ["uninstall", "other"]):
            with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                main(arguments)
            self.assertEqual(error.exception.code, 2)

    def test_status_reports_missing_current_older_and_conflicting_files(self):
        self.assertEqual(pi_status(str(self.directory)), ("not installed", self.target))
        self.assertFalse(self.directory.exists())
        install_pi(str(self.directory))
        timestamp = self.target.stat().st_mtime_ns
        self.assertEqual(pi_status(str(self.directory)), ("installed", self.target))
        self.assertEqual(self.target.stat().st_mtime_ns, timestamp)
        for content, expected in ((managed(b"// older version\n"), "installed (update available)"),
                                  (b"personal file", "conflict (unowned or edited file)"),
                                  (pi_extension() + b"// custom edit\n", "conflict (unowned or edited file)")):
            with self.subTest(expected=expected):
                self.target.write_bytes(content)
                self.assertEqual(pi_status(str(self.directory)), (expected, self.target))
                self.assertEqual(self.target.read_bytes(), content)
        self.target.unlink()
        self.target.symlink_to(self.directory / "missing")
        self.assertEqual(pi_status(str(self.directory)), ("conflict (symlink)", self.target))
        self.target.unlink()
        self.target.mkdir()
        self.assertEqual(pi_status(str(self.directory)), ("conflict (directory)", self.target))

    def test_cli_install_without_harness_lists_status_without_tmux_or_writes(self):
        with patch.dict(os.environ, {"PI_CODING_AGENT_DIR": str(self.directory)}), \
                patch("workmux.cli.Tmux") as tmux, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["install"]), 0)
            self.assertIn("Supported harnesses:", stdout.getvalue())
            self.assertIn("pi\tnot installed", stdout.getvalue())
            self.assertFalse(self.directory.exists())
            install_pi(str(self.directory))
            stdout.seek(0)
            stdout.truncate()
            self.assertEqual(main(["install", "--agent-dir", str(self.directory)]), 0)
            self.assertIn("pi\tinstalled\t", stdout.getvalue())
            self.assertIn(str(self.target), stdout.getvalue())
            tmux.assert_not_called()

    def test_bundled_typescript_extension(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node 24+ is needed to test the TypeScript extension without dependencies")
        version = subprocess.run([node, "--version"], capture_output=True, text=True, check=True)
        if int(version.stdout.strip().lstrip("v").split(".")[0]) < 24:
            self.skipTest("Node 24+ is needed for native TypeScript loading")
        install_pi(str(self.directory))
        script = Path(__file__).with_name("pi-notify.test.mjs")
        result = subprocess.run([node, str(script), str(self.target)], cwd=self.directory,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)
