"""Exercise the installed Bash registration against private, disposable tmux servers."""

from importlib.util import find_spec
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from workmux import completion
from workmux.core import Tmux


_BASH = shutil.which("bash")
_WORKMUX = shutil.which("workmux")
_REGISTRATION = '''
source <(workmux completion bash)
read -ra registration <<< "$(complete -p workmux)"
for (( i = 0; i < ${#registration[@]}; i++ )); do
    if [[ ${registration[i]} == -F ]]; then
        complete_function=${registration[i+1]}
        break
    fi
done
# compopt needs a real readline completion context; the generated function
# can otherwise be exercised directly with Bash's completion variables.
compopt() { :; }
COMP_LINE=$1
COMP_POINT=${#COMP_LINE}
COMPREPLY=()
"$complete_function" workmux ""
if ((${#COMPREPLY[@]})); then printf '%s\\0' "${COMPREPLY[@]}"; fi
'''


@unittest.skipUnless(find_spec("argcomplete") and _BASH and _WORKMUX, "Bash completion extra and launcher required")
class BashCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workmux-comp-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / ".config"),
                                 BASH_ENV=os.devnull, ENV=os.devnull, TMUX_TMPDIR=str(self.root))
        environment.start()
        self.addCleanup(environment.stop)
        for variable in ("TMUX", "TMUX_PANE", "_ARGCOMPLETE", "_ARC_DEBUG"):
            os.environ.pop(variable, None)

    def complete(self, line):
        result = subprocess.run([_BASH, "--noprofile", "--norc", "-ec", _REGISTRATION, "bash", line],
                                text=True, capture_output=True, cwd=self.root, timeout=5, check=True)
        self.assertEqual(result.stderr, "")
        return {value.removesuffix(" ") for value in result.stdout.split("\0") if value}

    def test_commands_options_choices_and_directories(self):
        self.assertEqual(self.complete("workmux team "), {"new", "reset", "status", "refresh", "clear"})
        self.assertEqual(self.complete("workmux team status --pa"), {"--pane"})
        self.assertEqual(self.complete("workmux team status --j"), {"--json"})
        self.assertEqual(self.complete("workmux team new review --l"), {"--launcher"})
        self.assertEqual(self.complete("workmux team new review --p"), {"--profile"})
        self.assertEqual(self.complete("workmux team reset --p"), {"--profile"})
        self.assertEqual(self.complete("workmux team reset --profile "), set())
        self.assertEqual(self.complete("workmux install p"), {"pi"})
        self.assertEqual(self.complete("workmux layout main-v"), {"main-vertical", "main-vertical-mirrored"})
        (self.root / "repo space").mkdir()
        (self.root / "repo-file").touch()
        for argument in ("workspace new --dir ", "workspace new --dir=", "install pi --agent-dir "):
            with self.subTest(argument=argument):
                self.assertEqual(self.complete(f"workmux {argument}repo"), {r"repo\ space/"})

    def test_team_launcher_completes_only_directories_and_executables(self):
        launcher = self.root / "launch worker"
        launcher.touch(mode=0o700)
        (self.root / "launch tools").mkdir()
        (self.root / "launch docs").touch(mode=0o600)
        for separator in (" ", "="):
            self.assertEqual(self.complete(f"workmux team new review --launcher{separator}./launch"),
                             {r"./launch\ worker", r"./launch\ tools/"})

    def test_missing_server_is_quiet_and_not_started(self):
        socket = self.root / "missing-socket"
        self.assertEqual(self.complete(f"workmux --socket {socket} attach "), set())
        self.assertEqual(self.complete(f"workmux --socket {socket} team status --pane "), set())
        self.assertFalse(socket.exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux required for live completion")
    def test_live_values_on_current_named_and_explicit_servers(self):
        primary = Tmux(server="demo")
        other = Tmux(socket=str(self.root / "other socket"))
        for tmux in (primary, other):
            tmux.run("new-session", "-d", "-s", "shell", "sleep 60")
            self.addCleanup(tmux.run, "kill-server", missing_ok=True)
        primary.run("new-session", "-d", "-s", "build", "sleep 60")
        primary.run("set-option", "-t", "build", "@workmux", "1", ";",
                    "set-option", "-t", "build", "@workspace-label", "build tools")
        other.run("new-session", "-d", "-s", "docs", "sleep 60")
        other.run("set-option", "-t", "docs", "@workmux", "1")
        build = primary.workspaces()[0]
        docs = other.workspaces()[0]
        pane_id = primary.run("list-panes", "-t", build.id, "-F", "#{pane_id}")
        # An attached control-mode client needs no user terminal or tmux configuration.
        with subprocess.Popen(["tmux", *primary.selector, "-C", "attach-session", "-t", build.id],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as client:
            try:
                deadline = time.monotonic() + 5
                while not (client_name := primary.run("list-clients", "-F", "#{client_name}")):
                    self.assertLess(time.monotonic(), deadline, "control client did not attach")
                    time.sleep(0.02)
                current = primary.run("display-message", "-p", "-t", build.id, "#{socket_path}")
                os.environ["TMUX"] = f"{current},1,0"
                self.assertEqual(self.complete("workmux workspace select "),
                                 {"build", r"build\ tools", "\\" + build.id})
                self.assertEqual(self.complete("workmux --server demo workspace select --index "), {"1"})
                self.assertIn(pane_id, self.complete("workmux team status --pane %"))
                self.assertEqual(self.complete("workmux pi reload --popup "), {client_name})
                self.assertEqual(self.complete("workmux enable --client "), {client_name})
                self.assertEqual(self.complete("workmux --server de"), {"demo"})
                self.assertEqual(self.complete("workmux --socket ./oth"), {r"./other\ socket"})
                self.assertEqual(self.complete(f"workmux --socket={shlex.quote(other.socket)} attach "),
                                 {"docs", "\\" + docs.id})
            finally:
                client.stdin.close()
                client.wait(timeout=5)


class CompletionLookupTests(unittest.TestCase):
    def test_slow_server_has_no_suggestions(self):
        with patch("workmux.completion.Tmux") as tmux:
            tmux.return_value.workspaces.side_effect = subprocess.TimeoutExpired("tmux", 0.4)
            self.assertEqual(completion.workspaces(prefix="", parsed_args=None), [])
            self.assertFalse(tmux.call_args.kwargs["start_server"])
            self.assertLess(tmux.call_args.kwargs["timeout"], 1)
