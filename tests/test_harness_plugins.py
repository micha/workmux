import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workmux.cli import main
from workmux.core import WorkmuxError
from workmux.plugins import (HARNESSES, _FILES, _ZERO_HASH, _content, agent_dir,
                             install_plugin, plugin_status, uninstall_plugin)


def older_content(spec):
    if spec.resource.endswith(".json"):
        data = {"description": f"workmux-managed: {spec.kind} sha256={_ZERO_HASH}", "old": True}
        content = (json.dumps(data, indent=2) + "\n").encode()
        return content.replace(_ZERO_HASH.encode(), hashlib.sha256(content).hexdigest().encode(), 1)
    body = b"// older integration\n"
    return f"// workmux-managed: {spec.kind} sha256={hashlib.sha256(body).hexdigest()}\n".encode() + body


class HarnessPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workmux-harness-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "agent ' $; space"

    def test_personal_directories_and_environment_overrides(self):
        with patch.dict(os.environ, {}, clear=True), patch("workmux.plugins.Path.home", return_value=self.root):
            for harness, expected in [("pi", ".pi/agent"), ("opencode", ".config/opencode"),
                                      ("codex", ".codex"), ("claude", ".claude")]:
                self.assertEqual(agent_dir(harness), self.root / expected)
        for harness, variable in [("pi", "PI_CODING_AGENT_DIR"), ("opencode", "OPENCODE_CONFIG_DIR"),
                                  ("codex", "CODEX_HOME"), ("claude", "CLAUDE_CONFIG_DIR")]:
            with patch.dict(os.environ, {variable: str(self.root)}):
                self.assertEqual(agent_dir(harness), self.root)
                self.assertEqual(agent_dir(harness, "/explicit"), Path("/explicit"))
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root), "OPENCODE_CONFIG_DIR": ""}):
            self.assertEqual(agent_dir("opencode"), self.root / "opencode")

    def test_all_harnesses_install_update_status_and_remove_only_owned_files(self):
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                root = self.root / harness
                self.assertEqual(plugin_status(harness, str(root))[0], "not installed")
                self.assertIn("Not installed:", uninstall_plugin(harness, str(root)))
                self.assertFalse(root.exists())
                root.mkdir(parents=True)
                settings = root / "config.toml"
                settings.write_text("# personal settings\n")
                extra = root / "personal.txt"
                extra.write_text("keep")
                self.assertIn("Installed:", install_plugin(harness, str(root)))
                timestamps = [(root / spec.path).stat().st_mtime_ns for spec in _FILES[harness]]
                self.assertEqual(plugin_status(harness, str(root))[0], "installed")
                self.assertIn("Already installed:", install_plugin(harness, str(root)))
                self.assertEqual(timestamps, [(root / spec.path).stat().st_mtime_ns for spec in _FILES[harness]])
                for spec in _FILES[harness]:
                    (root / spec.path).write_bytes(older_content(spec))
                self.assertEqual(plugin_status(harness, str(root))[0], "installed (update available)")
                install_plugin(harness, str(root))
                for spec in _FILES[harness]:
                    self.assertEqual((root / spec.path).read_bytes(), _content(spec))
                self.assertIn("Removed:", uninstall_plugin(harness, str(root)))
                self.assertTrue(all(not (root / spec.path).exists() for spec in _FILES[harness]))
                self.assertEqual(settings.read_text(), "# personal settings\n")
                self.assertEqual(extra.read_text(), "keep")

    def test_conflicts_and_formatting_edits_are_preserved(self):
        for harness in HARNESSES:
            root = self.root / harness
            install_plugin(harness, str(root))
            spec = _FILES[harness][-1]
            target = root / spec.path
            contents = (b"personal file", _content(spec) + b"\n", b"[]")
            if harness == "codex":
                contents = (b"personal file", b"[]")  # Shared JSON tolerates reformatting.
            for content in contents:
                with self.subTest(harness=harness, content=content[:30]):
                    target.write_bytes(content)
                    self.assertIn("conflict", plugin_status(harness, str(root))[0])
                    for operation in (install_plugin, uninstall_plugin):
                        with self.assertRaisesRegex(WorkmuxError, "unowned or edited"):
                            operation(harness, str(root))
                        self.assertEqual(target.read_bytes(), content)
            target.write_bytes(_content(spec))

    def test_bundle_preflight_and_incomplete_installation(self):
        install_plugin("claude", str(self.root))
        manifest, hooks = [self.root / spec.path for spec in _FILES["claude"]]
        manifest.write_bytes(older_content(_FILES["claude"][0]))
        hooks.write_bytes(b"edited hooks")
        original = manifest.read_bytes()
        for operation in (install_plugin, uninstall_plugin):
            with self.assertRaises(WorkmuxError):
                operation("claude", str(self.root))
            self.assertEqual(manifest.read_bytes(), original)
            self.assertEqual(hooks.read_bytes(), b"edited hooks")
        hooks.unlink()
        self.assertEqual(plugin_status("claude", str(self.root))[0], "installed (incomplete)")
        install_plugin("claude", str(self.root))
        self.assertEqual(plugin_status("claude", str(self.root))[0], "installed")

    def test_managed_file_symlinks_are_not_followed(self):
        for harness in HARNESSES:
            root = self.root / harness
            root.mkdir(parents=True)
            actual = self.root / f"{harness}-actual"
            actual.mkdir()
            linked = self.root / f"{harness}-linked"
            linked.symlink_to(root, target_is_directory=True)
            for spec in _FILES[harness]:
                target = root / spec.path
                target.parent.mkdir(parents=True, exist_ok=True)
                other = actual / Path(spec.path).name
                for dangling in (False, True):
                    with self.subTest(harness=harness, path=spec.path, dangling=dangling):
                        if not dangling:
                            other.write_bytes(_content(spec))
                        else:
                            other.unlink()
                        target.symlink_to(other)
                        for directory in (root, linked):
                            for operation in (install_plugin, uninstall_plugin):
                                with self.assertRaisesRegex(WorkmuxError, "symlink"):
                                    operation(harness, str(directory))
                            self.assertEqual(plugin_status(harness, str(directory))[0], "conflict (symlink)")
                        self.assertTrue(target.is_symlink())
                        if not dangling:
                            self.assertEqual(other.read_bytes(), _content(spec))
                        target.unlink()

    def test_symlinked_roots_and_ancestors_support_full_lifecycle(self):
        self.root.mkdir(parents=True)
        for harness in HARNESSES:
            for nested in (False, True):
                with self.subTest(harness=harness, nested=nested):
                    actual = self.root / f"{harness}-actual-{nested}"
                    actual.mkdir()
                    linked = self.root / f"{harness}-linked-{nested}"
                    linked.symlink_to(actual, target_is_directory=True)
                    root = linked / "config" / "agent" if nested else linked
                    real_root = actual / "config" / "agent" if nested else actual
                    inode = linked.lstat().st_ino
                    self.assertEqual(plugin_status(harness, str(root))[0], "not installed")
                    self.assertIn("Not installed:", uninstall_plugin(harness, str(root)))
                    self.assertEqual(list(actual.iterdir()), [])
                    install_plugin(harness, str(root))
                    self.assertEqual(plugin_status(harness, str(root))[0], "installed")
                    timestamps = [(real_root / spec.path).stat().st_mtime_ns for spec in _FILES[harness]]
                    self.assertIn("Already installed:", install_plugin(harness, str(root)))
                    self.assertEqual(timestamps, [(real_root / spec.path).stat().st_mtime_ns for spec in _FILES[harness]])
                    for spec in _FILES[harness]:
                        (real_root / spec.path).write_bytes(older_content(spec))
                    self.assertEqual(plugin_status(harness, str(root))[0], "installed (update available)")
                    install_plugin(harness, str(root))
                    extra = real_root / "personal-settings.json"
                    extra.write_text('{"keep": true}')
                    uninstall_plugin(harness, str(root))
                    self.assertEqual(plugin_status(harness, str(root))[0], "not installed")
                    self.assertEqual(extra.read_text(), '{"keep": true}')
                    self.assertTrue(linked.is_symlink())
                    self.assertEqual(linked.readlink(), actual)
                    self.assertEqual(linked.lstat().st_ino, inode)

    def test_nested_directory_symlinks_are_preserved(self):
        for harness in ("pi", "opencode", "claude"):
            for spec in _FILES[harness]:
                with self.subTest(harness=harness, path=spec.path):
                    root = self.root / harness / spec.kind
                    target = root / spec.path
                    actual = self.root / f"{spec.kind}-files"
                    actual.mkdir(parents=True)
                    target.parent.parent.mkdir(parents=True)
                    target.parent.symlink_to(actual, target_is_directory=True)
                    inode = target.parent.lstat().st_ino
                    extra = actual / "personal-file"
                    extra.write_text("keep")
                    install_plugin(harness, str(root))
                    self.assertEqual(plugin_status(harness, str(root))[0], "installed")
                    self.assertEqual((actual / target.name).read_bytes(), _content(spec))
                    uninstall_plugin(harness, str(root))
                    self.assertFalse((actual / target.name).exists())
                    self.assertEqual(extra.read_text(), "keep")
                    self.assertEqual(target.parent.readlink(), actual)
                    self.assertEqual(target.parent.lstat().st_ino, inode)

    def test_edited_files_through_linked_directories_are_preserved(self):
        for harness in HARNESSES:
            root = self.root / harness
            install_plugin(harness, str(root))
            linked = self.root / f"{harness}-linked"
            linked.symlink_to(root, target_is_directory=True)
            target = root / _FILES[harness][-1].path
            content = target.read_bytes() + b"\ncustom edit\n"
            target.write_bytes(content)
            for operation in (install_plugin, uninstall_plugin):
                with self.assertRaisesRegex(WorkmuxError, "unowned or edited"):
                    operation(harness, str(linked))
            self.assertEqual(plugin_status(harness, str(linked))[0], "conflict (unowned or edited file)")
            self.assertEqual(target.read_bytes(), content)
            self.assertTrue(linked.is_symlink())

    def test_cli_lists_every_harness_without_writes_or_tmux(self):
        with patch("workmux.cli.Tmux") as tmux, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["install", "--agent-dir", str(self.root)]), 0)
            for harness in HARNESSES:
                self.assertRegex(stdout.getvalue(), rf"{harness}\s+not installed\s+")
                self.assertEqual(main(["install", harness, "--agent-dir", str(self.root / harness)]), 0)
                self.assertEqual(main(["uninstall", harness, "--agent-dir", str(self.root / harness)]), 0)
            tmux.assert_not_called()
        self.assertTrue(all(not (self.root / spec.path).exists()
                            for specs in _FILES.values() for spec in specs))

    def test_listing_aligns_columns_with_mixed_statuses_and_spaceful_paths(self):
        statuses = ["not installed", "installed", "installed (update available)",
                    "conflict (unowned or edited file)"]
        paths = [Path(f"/agent roots/{harness}/managed file") for harness in HARNESSES]
        with patch("workmux.plugins.plugin_status", side_effect=zip(statuses, paths)) as status, \
                patch("workmux.cli.Tmux") as tmux, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["install", "--agent-dir", "/override path"]), 0)
            lines = stdout.getvalue().splitlines()
            self.assertEqual(lines[0], "Supported harnesses:")
            self.assertEqual(len(lines), len(HARNESSES) + 1)
            name_width = max(map(len, HARNESSES))
            status_width = max(map(len, statuses))
            for line, harness, state, path in zip(lines[1:], HARNESSES, statuses, paths):
                self.assertEqual(line.index(harness), 2)
                self.assertEqual(line.index(state), 2 + name_width + 2)
                self.assertEqual(line.index(str(path)), 2 + name_width + 2 + status_width + 2)
                self.assertNotIn("\t", line)
            self.assertEqual([call.args for call in status.call_args_list],
                             [(harness, "/override path") for harness in HARNESSES])
            tmux.assert_not_called()

    def test_json_hook_commands_are_portable_and_never_override_hook_decisions(self):
        for harness in ("codex", "claude"):
            spec = _FILES[harness][-1]
            data = json.loads(_content(spec))
            if harness == "claude":
                self.assertIn("description", data)
            else:
                self.assertTrue(all("description" in group for groups in data["hooks"].values()
                                    for group in groups))
            self.assertIn("Stop", data["hooks"])
            for event, groups in data["hooks"].items():
                self.assertNotIn(event, ("SubagentStart", "SubagentStop"))
                for group in groups:
                    for hook in group["hooks"]:
                        self.assertEqual(hook["timeout"], 3 if harness == "codex" and event == "SessionEnd" else 5)
                        self.assertEqual(hook["type"], "command")
                        self.assertEqual(hook["command"],
                                         f'workmux agent hook {harness} --pid "$PPID" 2>/dev/null || true')

    @unittest.skipUnless(shutil.which("node"), "Node is needed to test the bundled OpenCode plugin")
    def test_bundled_opencode_plugin(self):
        install_plugin("opencode", str(self.root))
        script = Path(__file__).with_name("opencode-notify.test.mjs")
        target = self.root / _FILES["opencode"][0].path
        result = subprocess.run([shutil.which("node"), str(script), str(target)],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS", result.stdout)
