"""Optional schema/discovery checks using real harness CLIs, without model calls."""

import json
import os
from pathlib import Path
import pty
import select
import selectors
import shutil
import signal
import sys
import subprocess
import tempfile
import time
import unittest

from workmux.plugins import install_plugin
import workmux


class NativePluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workmux-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.root), CODEX_HOME=str(self.root / "codex"),
                        CLAUDE_CONFIG_DIR=str(self.root / "claude"),
                        OPENCODE_CONFIG_DIR=str(self.root / "config" / "opencode"),
                        XDG_CONFIG_HOME=str(self.root / "config"), XDG_DATA_HOME=str(self.root / "data"),
                        XDG_CACHE_HOME=str(self.root / "cache"), XDG_STATE_HOME=str(self.root / "state"),
                        OPENCODE_DISABLE_AUTOUPDATE="true", OPENCODE_DISABLE_MODELS_FETCH="true",
                        OPENCODE_DISABLE_DEFAULT_PLUGINS="true")
        for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "TMUX", "TMUX_PANE"):
            self.env.pop(key, None)

    def run_cli(self, command):
        result = subprocess.run(command, cwd=self.root, env=self.env, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    @unittest.skipUnless(shutil.which("claude"), "Claude Code is not installed")
    def test_claude_accepts_installed_manifest(self):
        install_plugin("claude", self.env["CLAUDE_CONFIG_DIR"])
        result = self.run_cli(["claude", "plugin", "validate",
                               str(self.root / "claude" / "skills" / "workmux")])
        self.assertIn("Validation passed", result.stdout)
        version = self.run_cli(["claude", "--version"]).stdout.split()[0]
        if tuple(int(part) for part in version.split(".")[:3]) >= (2, 1, 223):
            plugins = json.loads(self.run_cli(["claude", "plugin", "list", "--json"]).stdout)
            self.assertTrue(any(plugin["id"] == "workmux@skills-dir" and plugin["enabled"] for plugin in plugins))
            inventory = self.run_cli(["claude", "plugin", "details", "workmux@skills-dir"]).stdout
            self.assertIn("Hooks (8)", inventory)
            self.assertFalse((self.root / "claude" / "settings.json").exists())

    @unittest.skipUnless(shutil.which("claude") and Path("/proc/self/fd").exists(),
                         "Claude Code and Linux procfs/PTYs are required")
    def test_claude_detached_session_start_hook_verifies_harness_terminal(self):
        version = self.run_cli(["claude", "--version"]).stdout.split()[0]
        if tuple(int(part) for part in version.split(".")[:3]) < (2, 1, 223):
            self.skipTest("Native skills-dir plugin loading needs Claude Code 2.1.223+")
        config = self.root / "claude"
        install_plugin("claude", str(config))
        # Skip first-launch dialogs only in this disposable HOME. A dummy key
        # and a non-serving loopback URL prevent access to real model services;
        # no user prompt/turn is submitted. This exercises native SessionStart.
        (config / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True, "theme": "dark", "numStartups": 1,
            "customApiKeyResponses": {"approved": ["workmux-test-key"], "rejected": []},
            "projects": {str(self.root): {"hasTrustDialogAccepted": True,
                                          "hasCompletedProjectOnboarding": True}},
        }))
        log = self.root / "hook-results"
        trace = self.root / "trace-hook.py"
        trace.write_text(
            "import json, os, sys\n"
            "from workmux.agent_hooks import _interactive_terminal\n"
            "payload = json.load(sys.stdin)\n"
            "pid = int(sys.argv[sys.argv.index('--pid') + 1])\n"
            "fd = _interactive_terminal('claude', pid)\n"
            "result = {'event': payload.get('hook_event_name'), 'pid': pid,\n"
            "          'verified': fd is not None, 'detached': os.getsid(0) != os.getsid(pid)}\n"
            "with open(os.environ['WORKMUX_TEST_LOG'], 'a') as out:\n"
            "    out.write(json.dumps(result) + '\\n')\n"
            "if fd is not None: os.close(fd)\n"
        )
        bindir = self.root / "bin"
        bindir.mkdir()
        proxy = bindir / "workmux"
        proxy.write_text('#!/bin/sh\nexec "$WORKMUX_TEST_PYTHON" "$WORKMUX_TEST_TRACE" "$@"\n')
        proxy.chmod(0o755)
        env = dict(self.env, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                   PYTHONPATH=str(Path(workmux.__file__).parent.parent),
                   WORKMUX_TEST_LOG=str(log), WORKMUX_TEST_TRACE=str(trace),
                   WORKMUX_TEST_PYTHON=sys.executable, TMUX="/tmp/workmux-fake,1,0", TMUX_PANE="%42",
                   TERM="xterm-256color", ANTHROPIC_API_KEY="workmux-test-key",
                   ANTHROPIC_BASE_URL="http://127.0.0.1:1", DISABLE_TELEMETRY="1",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        for key in ("CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SHELL_PREFIX", "CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
            env.pop(key, None)
        executable = shutil.which("claude")
        pid, terminal = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execve(executable, [executable], env)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if log.exists() and log.read_text():
                    break
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        os.read(terminal, 65536)
                    except OSError:
                        break
            self.assertTrue(log.exists(), "Claude did not invoke the plugin's SessionStart hook")
            event = json.loads(log.read_text().splitlines()[0])
            self.assertEqual(event, {"event": "SessionStart", "pid": pid, "verified": True, "detached": True})
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while not os.waitpid(pid, os.WNOHANG)[0]:
                if time.monotonic() >= deadline:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    break
                time.sleep(0.05)
            os.close(terminal)

    @unittest.skipUnless(shutil.which("opencode"), "OpenCode is not installed")
    def test_opencode_discovers_plugin_without_settings_edits(self):
        install_plugin("opencode", self.env["OPENCODE_CONFIG_DIR"])
        result = self.run_cli(["opencode", "debug", "config"])
        config = json.loads(result.stdout)
        target = self.root / "config" / "opencode" / "plugins" / "workmux-notify.js"
        self.assertIn(target.as_uri(), config["plugin"])
        self.assertFalse((target.parent.parent / "opencode.json").exists())

    @unittest.skipUnless(shutil.which("opencode") and hasattr(os, "fork"), "OpenCode and Unix PTYs are required")
    def test_local_opencode_tui_loads_plugin_and_reports_without_model_calls(self):
        install_plugin("opencode", self.env["OPENCODE_CONFIG_DIR"])
        bindir = self.root / "bin"
        bindir.mkdir()
        target = bindir / "workmux"
        target.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$WORKMUX_TEST_REPORTS"\n')
        target.chmod(0o755)
        log = self.root / "reports"
        env = dict(self.env, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
                   WORKMUX_TEST_REPORTS=str(log), TMUX="/tmp/workmux-fake,1,0", TMUX_PANE="%42",
                   TERM="xterm-256color")
        executable = shutil.which("opencode")
        pid, terminal = pty.fork()
        if pid == 0:
            os.chdir(self.root)
            os.execve(executable, [executable], env)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if log.exists() and log.read_text():
                    break
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        os.read(terminal, 65536)
                    except OSError:
                        break
            self.assertTrue(log.exists(), "OpenCode did not load the plugin in its local TUI")
            self.assertIn(f"agent state idle --pane %42 --pid {pid}", log.read_text())
        finally:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 5
            while not os.waitpid(pid, os.WNOHANG)[0]:
                if time.monotonic() >= deadline:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    break
                time.sleep(0.05)
            os.close(terminal)

    @unittest.skipUnless(shutil.which("codex"), "Codex is not installed")
    def test_codex_discovers_valid_untrusted_hooks_without_model_calls(self):
        version = self.run_cli(["codex", "--version"]).stdout.strip().split()[-1]
        if tuple(int(part) for part in version.split(".")[:3]) < (0, 146, 0):
            self.skipTest("Native hooks/discovery validation needs Codex 0.146.0+")
        config = self.root / "codex"
        config.mkdir()
        (config / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [
            {"matcher": "", "hooks": [{"type": "command", "command": "echo personal"}]}
        ]}}))
        install_plugin("codex", self.env["CODEX_HOME"])
        process = subprocess.Popen(["codex", "app-server"], cwd=self.root, env=self.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffered = b""

        def send(request):
            process.stdin.write(json.dumps(request).encode() + b"\n")
            process.stdin.flush()

        def receive(identifier):
            nonlocal buffered
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                for _, _ in selector.select(0.1):
                    chunk = os.read(process.stdout.fileno(), 65536)
                    self.assertTrue(chunk, "Codex app-server closed before replying")
                    buffered += chunk
                    while b"\n" in buffered:
                        line, buffered = buffered.split(b"\n", 1)
                        response = json.loads(line)
                        if response.get("id") == identifier:
                            return response
            self.fail(f"No response to Codex request {identifier}")

        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "workmux-test", "version": "0.0.0"},
                "capabilities": {"experimentalApi": True}}})
            self.assertIn("result", receive(1))
            send({"method": "initialized", "params": {}})
            send({"id": 2, "method": "hooks/list", "params": {"cwds": [str(self.root)]}})
            response = receive(2)
            self.assertIn("result", response, response)
            entry = response["result"]["data"][0]
            self.assertEqual(entry["errors"], [])
            self.assertEqual(entry["warnings"], [])
            hooks = [hook for hook in entry["hooks"]
                     if hook["sourcePath"] == str(self.root / "codex" / "hooks.json")]
            self.assertEqual(len(hooks), 8)
            self.assertTrue(all(hook["trustStatus"] == "untrusted" for hook in hooks))
            self.assertEqual(sum(hook["command"].startswith("workmux agent hook codex")
                                 for hook in hooks), 7)
            self.assertEqual(sum(hook["command"] == "echo personal" for hook in hooks), 1)
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            selector.close()
            process.stdout.close()
