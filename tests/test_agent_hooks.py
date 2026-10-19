import io
import json
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from workmux.agent_hooks import _interactive_terminal, _notification, hook_state, run_hook
from workmux.cli import main


class HookStateTests(unittest.TestCase):
    def test_lifecycle_mapping(self):
        for harness in ("codex", "claude"):
            for event, expected in [("SessionStart", "idle"), ("SessionEnd", "idle"),
                                    ("UserPromptSubmit", "working"), ("PermissionRequest", "attention"),
                                    ("PreToolUse", "working"), ("PostToolUse", "working"),
                                    ("Stop", "finished"), ("unknown", None)]:
                with self.subTest(harness=harness, event=event):
                    self.assertEqual(hook_state(harness, {"hook_event_name": event}), expected)
        self.assertEqual(hook_state("claude", {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"}), "attention")
        self.assertEqual(hook_state("codex", {"hook_event_name": "PreToolUse", "tool_name": "request_user_input"}), "attention")

    def test_subagents_reentrant_stop_and_background_work(self):
        for harness in ("codex", "claude"):
            for payload in ({"hook_event_name": "Stop", "stop_hook_active": True},
                            {"hook_event_name": "UserPromptSubmit", "agent_id": "child"},
                            {"hook_event_name": "PermissionRequest", "parent_agent_id": "parent"}):
                self.assertIsNone(hook_state(harness, payload))
        for field in ("background_tasks", "session_crons"):
            self.assertEqual(hook_state("claude", {"hook_event_name": "Stop", field: [{}]}), "working")

    def test_only_actionable_claude_notifications_need_attention(self):
        for kind in ("permission_prompt", "elicitation_dialog"):
            self.assertEqual(hook_state("claude", {"hook_event_name": "Notification", "notification_type": kind}), "attention")
            self.assertIsNone(hook_state("codex", {"hook_event_name": "Notification", "notification_type": kind}))
        for kind in ("idle_prompt", "auth_success", "unknown"):
            self.assertIsNone(hook_state("claude", {"hook_event_name": "Notification", "notification_type": kind}))

    def test_notification_sanitizes_payload_and_uses_tmux_passthrough(self):
        payload = _notification("claude", "finished", "/repo/bad\x1b\x07\nname")
        self.assertTrue(payload.startswith(b"\x07\x1bPtmux;\x1b\x1b]9;Claude Code"))
        self.assertEqual(payload.count(b"\x1b"), 4)
        self.assertEqual(payload.count(b"\x07"), 2)
        self.assertNotIn(b"\n", payload)
        self.assertIn("Finished".encode(), payload)


class InteractiveParentTests(unittest.TestCase):
    @staticmethod
    def process_stat(group=123, foreground=123, major=136, minor=5):
        tty = (major << 8) | (minor & 0xff) | ((minor & ~0xff) << 12)
        if tty >= 2**31:
            tty -= 2**32
        return f"123 (harness (with spaces)) S 1 {group} 123 {tty} {foreground} 0".encode()

    def test_foreground_tty_parent_only_without_hook_controlling_terminal(self):
        for major, minor in ((136, 5), (136, 4097), (136, 1048575)):
            with self.subTest(major=major, minor=minor), \
                    patch("workmux.agent_hooks.Path.read_bytes",
                          side_effect=[b"claude\0", self.process_stat(major=major, minor=minor)]), \
                    patch("workmux.agent_hooks.os.open", return_value=10) as opened, \
                    patch("workmux.agent_hooks.os.close") as close, \
                    patch("workmux.agent_hooks.os.isatty", return_value=True), \
                    patch("workmux.agent_hooks.os.fstat", return_value=SimpleNamespace(st_rdev=os.makedev(major, minor))), \
                    patch("workmux.agent_hooks.os.tcgetpgrp", side_effect=OSError("no hook controlling tty")):
                self.assertEqual(_interactive_terminal("claude", 123), 10)
                opened.assert_called_once_with("/proc/123/fd/1", os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
                close.assert_not_called()  # Ownership of the verified descriptor passes to run_hook.

    def test_headless_arguments_never_open_a_terminal(self):
        for harness, command in [("codex", b"codex\0exec\0"), ("codex", b"codex\0--remote=ws://example\0"),
                                 ("claude", b"claude\0-p\0query\0"), ("claude", b"claude\0-pquery\0"),
                                 ("claude", b"claude\0--output-format=json\0"), ("claude", b"claude\0--bg\0")]:
            with patch("workmux.agent_hooks.Path.read_bytes", return_value=command), \
                    patch("workmux.agent_hooks.os.open") as opened:
                self.assertIsNone(_interactive_terminal(harness, 123))
                opened.assert_not_called()

    def test_background_missing_proc_and_malformed_stat_are_ignored(self):
        with patch("workmux.agent_hooks.Path.read_bytes", side_effect=FileNotFoundError):
            self.assertIsNone(_interactive_terminal("codex", 123))
        for stat in (self.process_stat(foreground=456), b"malformed", b"123 (name) S 1 x 123 1 123", b"123 (name) S 1 123 123 0 123"):
            with patch("workmux.agent_hooks.Path.read_bytes", side_effect=[b"claude\0", stat]), \
                    patch("workmux.agent_hooks.os.open") as opened:
                self.assertIsNone(_interactive_terminal("claude", 123))
                opened.assert_not_called()

    def test_redirected_stdout_and_wrong_tty_are_ignored_and_closed(self):
        for is_tty, device in ((False, 0), (True, os.makedev(136, 6))):
            with self.subTest(is_tty=is_tty), \
                    patch("workmux.agent_hooks.Path.read_bytes", side_effect=[b"claude\0", self.process_stat()]), \
                    patch("workmux.agent_hooks.os.open", return_value=10), \
                    patch("workmux.agent_hooks.os.close") as close, \
                    patch("workmux.agent_hooks.os.isatty", return_value=is_tty), \
                    patch("workmux.agent_hooks.os.fstat", return_value=SimpleNamespace(st_rdev=device)):
                self.assertIsNone(_interactive_terminal("claude", 123))
                close.assert_called_once_with(10)


class HookRunnerTests(unittest.TestCase):
    def run_payload(self, payload, terminal=10, failure=None):
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        with patch.dict(os.environ, {"TMUX": "/tmp/fake,1,0", "TMUX_PANE": "%42"}), \
                patch("sys.stdin", io.StringIO(raw)), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                patch("workmux.agent_hooks._interactive_terminal", return_value=terminal), \
                patch("workmux.agent_hooks.os.write") as write, \
                patch("workmux.agent_hooks.os.close") as close, \
                patch("workmux.agent_hooks.subprocess.run", side_effect=failure) as report:
            run_hook("codex", 123)
            self.assertEqual(stdout.getvalue(), "")
        return write, close, report

    def test_reports_use_harness_pid_not_ephemeral_hook_pid(self):
        write, close, report = self.run_payload({"hook_event_name": "Stop", "cwd": "/repo"})
        self.assertIn(b"Finished", write.call_args.args[1])
        close.assert_called_once_with(10)
        self.assertEqual(report.call_args.args[0],
                         ["workmux", "agent", "state", "finished", "--pane", "%42", "--pid", "123"])
        self.assertEqual(report.call_args.kwargs["timeout"], 2)
        self.assertEqual(report.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_working_and_idle_do_not_ring_terminal(self):
        for event in ("UserPromptSubmit", "PostToolUse", "SessionStart", "SessionEnd"):
            write, close, report = self.run_payload({"hook_event_name": event})
            write.assert_not_called()
            report.assert_called_once()
            close.assert_called_once_with(10)

    def test_invalid_subagent_and_unverified_terminal_do_nothing(self):
        for payload in ("broken", "[]", "null", "x" * (1024 * 1024 + 1),
                        {"hook_event_name": "unknown"}, {"hook_event_name": "Stop", "agent_id": "child"}):
            write, close, report = self.run_payload(payload)
            write.assert_not_called()
            report.assert_not_called()
            close.assert_not_called()
        write, close, report = self.run_payload({"hook_event_name": "Stop"}, terminal=None)
        write.assert_not_called()
        report.assert_not_called()
        close.assert_not_called()

    def test_missing_workmux_or_timeout_is_best_effort(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("workmux", 2)):
            _, close, report = self.run_payload({"hook_event_name": "UserPromptSubmit"}, failure=error)
            close.assert_called_once_with(10)
            report.assert_called_once()

    def test_cli_internal_hook_never_constructs_tmux_or_prints(self):
        with patch("workmux.agent_hooks.run_hook") as hook, patch("workmux.cli.Tmux") as tmux, \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main(["agent", "hook", "claude", "--pid", "123"]), 0)
            hook.assert_called_once_with("claude", 123)
            tmux.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
