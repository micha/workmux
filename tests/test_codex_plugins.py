"""Ownership and safe patching of the shared Codex hook configuration."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workmux.core import WorkmuxError
from workmux.plugins import (_FILES, _ZERO_HASH, _codex_marker, _content,
                             install_plugin, plugin_status, uninstall_plugin)


class CodexPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workmux-codex-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "hooks.json"
        self.personal = {
            "description": "My personal hooks",
            "custom": {"keep": "日本語"},
            "hooks": {
                "Stop": [{"matcher": "", "hooks": [
                    {"type": "command", "command": "echo personal", "timeout": 9},
                    {"type": "command", "command": "echo another"}]}],
                "FutureEvent": [{"matcher": "example", "hooks": []}],
                "PreCompact": [],
            },
        }

    def write(self, data):
        self.target.write_text(json.dumps(data, ensure_ascii=False))

    def install(self):
        return install_plugin("codex", str(self.root))

    def uninstall(self):
        return uninstall_plugin("codex", str(self.root))

    def status(self):
        return plugin_status("codex", str(self.root))[0]

    def test_merge_idempotence_and_remove_preserve_personal_configuration(self):
        self.write(self.personal)
        original = self.target.read_bytes()
        self.assertEqual(self.status(), "not installed")
        self.assertEqual(self.target.read_bytes(), original)
        self.assertIn("Not installed:", self.uninstall())
        self.assertEqual(self.target.read_bytes(), original)
        self.target.chmod(0o640)
        self.assertIn("Installed:", self.install())
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)
        data = json.loads(self.target.read_bytes())
        self.assertEqual(data["hooks"]["Stop"][0], self.personal["hooks"]["Stop"][0])
        self.assertEqual(data["custom"], self.personal["custom"])
        self.assertEqual(data["description"], self.personal["description"])
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertEqual(self.status(), "installed")
        content, timestamp = self.target.read_bytes(), self.target.stat().st_mtime_ns
        self.assertIn("Already installed:", self.install())
        self.assertEqual(self.target.read_bytes(), content)
        self.assertEqual(self.target.stat().st_mtime_ns, timestamp)
        # Native JSON editors and additions to unrelated groups remain safe.
        data["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 11
        self.write(data)
        self.assertEqual(self.status(), "installed")
        self.assertIn("Already installed:", self.install())
        self.assertIn("Removed:", self.uninstall())
        expected = self.personal
        expected["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 11
        self.assertEqual(json.loads(self.target.read_bytes()), expected)
        self.assertEqual(self.status(), "not installed")

    def test_empty_objects_and_missing_hooks_can_be_installed(self):
        for data in ({}, {"hooks": {}}, {"description": "keep", "other": True}):
            with self.subTest(data=data):
                self.write(data)
                self.install()
                self.assertEqual(self.status(), "installed")
                self.uninstall()
                if "other" in data:
                    self.assertEqual(json.loads(self.target.read_bytes()), {**data, "hooks": {}})
                else:
                    self.assertFalse(self.target.exists())

    def test_partial_and_older_groups_update_without_touching_personal_hooks(self):
        self.write(self.personal)
        self.install()
        data = json.loads(self.target.read_bytes())
        del data["hooks"]["SessionEnd"]
        self.write(data)
        self.assertEqual(self.status(), "installed (incomplete)")
        self.install()
        data = json.loads(self.target.read_bytes())
        group = data["hooks"]["Stop"][-1]
        group["hooks"][0]["timeout"] = 1
        group["description"] = _codex_marker("Stop", group)
        self.write(data)
        self.assertEqual(self.status(), "installed (update available)")
        self.install()
        self.assertEqual(self.status(), "installed")
        self.uninstall()
        self.assertEqual(json.loads(self.target.read_bytes()), self.personal)

    def test_edited_owned_groups_and_moved_groups_are_conflicts(self):
        for change in ("command", "matcher", "marker", "event", "extra_hook"):
            with self.subTest(change=change):
                self.target.unlink(missing_ok=True)
                self.install()
                data = json.loads(self.target.read_bytes())
                group = data["hooks"]["Stop"][0]
                if change == "command":
                    group["hooks"][0]["command"] += " edited"
                elif change == "matcher":
                    group["matcher"] = "changed"
                elif change == "marker":
                    group["description"] += "edited"
                elif change == "event":
                    data["hooks"]["FutureEvent"] = data["hooks"].pop("Stop")
                else:
                    group["hooks"].append({"type": "command", "command": "personal"})
                self.write(data)
                self.assert_conflict_preserved()

    def assert_conflict_preserved(self):
        original = self.target.read_bytes()
        self.assertIn("conflict", self.status())
        for operation in (self.install, self.uninstall):
            with self.assertRaises(WorkmuxError):
                operation()
            self.assertEqual(self.target.read_bytes(), original)

    def test_malformed_and_ambiguous_json_is_preserved(self):
        for content in (b"{", b"[]", b"null", b'{"hooks": null}',
                        b'{"hooks": {"Stop": {}}}', b'{"hooks": {"Stop": [null]}}',
                        b'{"hooks": {"Stop": [{"hooks": [42]}]}}',
                        b'{"hooks": {}, "hooks": {}}',
                        b'{"hooks": {}, "value": NaN}', b'\xff'):
            with self.subTest(content=content):
                self.target.write_bytes(content)
                self.assert_conflict_preserved()

    def test_unmarked_workmux_commands_are_not_claimed(self):
        data = json.loads(_content(_FILES["codex"][0]))
        for groups in data["hooks"].values():
            for group in groups:
                del group["description"]
        self.write(data)
        self.assertEqual(self.status(), "not installed")
        self.install()
        installed = json.loads(self.target.read_bytes())
        self.assertTrue(all(len(groups) == 2 for groups in installed["hooks"].values()))
        self.uninstall()
        self.assertEqual(json.loads(self.target.read_bytes()), data)

    def test_migrate_intact_legacy_whole_file_but_reject_edited_legacy(self):
        data = json.loads(_content(_FILES["codex"][0]))
        for groups in data["hooks"].values():
            for group in groups:
                del group["description"]
        data["description"] = f"workmux-managed: codex-hooks sha256={_ZERO_HASH}"
        zeroed = (json.dumps(data, indent=2) + "\n").encode()
        legacy = zeroed.replace(_ZERO_HASH.encode(), hashlib.sha256(zeroed).hexdigest().encode())
        self.target.write_bytes(legacy + b"\n")
        self.assert_conflict_preserved()
        self.target.write_bytes(legacy)
        self.assertEqual(self.status(), "installed (update available)")
        self.install()
        self.assertEqual(self.target.read_bytes(), _content(_FILES["codex"][0]))
        self.assertEqual(self.status(), "installed")
        self.uninstall()
        self.assertFalse(self.target.exists())
        self.target.write_bytes(legacy)
        self.uninstall()
        self.assertFalse(self.target.exists())

    def test_replacement_rechecks_concurrent_edits(self):
        self.write(self.personal)
        from workmux.plugins import _write

        def concurrent_write(target, content, existing):
            self.target.write_text('{"description": "concurrent edit"}')
            _write(target, content, existing)

        with patch("workmux.plugins._write", side_effect=concurrent_write):
            with self.assertRaisesRegex(WorkmuxError, "changed during installation"):
                self.install()
        self.assertEqual(json.loads(self.target.read_bytes()), {"description": "concurrent edit"})
        self.assertFalse(list(self.root.glob(".workmux-*")))
