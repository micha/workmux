from concurrent.futures import Future
import curses
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from workmux.core import Tab, Workspace
from workmux.git_status import GitStatusCache, POLL_INTERVAL, parse_status, probe_status
from workmux.sidebar import GIT_INDICATORS, draw_row, sidebar_rows


class GitStatusTests(unittest.TestCase):
    def test_all_applicable_states_and_opaque_filenames(self):
        for output, expected in [
            (b"# branch.head main\0", ("clean",)),
            (b"# branch.head (detached)\0", ("clean",)),
            (b"# branch.ab +0 -0\0", ("clean",)),
            (b"# branch.ab +2 -0\0", ("ahead",)),
            (b"# branch.ab +0 -2\0", ("behind",)),
            (b"# branch.ab +2 -3\0", ("diverged",)),
            (b"# branch.ab +2 -3\0? new\0", ("dirty", "diverged")),
            (b"# branch.ab +2 -0\0? new\0", ("dirty", "ahead")),
            (b"# branch.ab +0 -3\0? new\0", ("dirty", "behind")),
            (b"1 M. fields path\0", ("dirty",)),
            (b"1 .M fields path\0", ("dirty",)),
            (b"u UU fields path\0", ("conflict",)),
            (b"? new\0u UU fields path\0", ("conflict", "dirty")),
            (b"# branch.ab +2 -3\0? new\0u UU fields path\0",
             ("conflict", "dirty", "diverged")),
            (b"# branch.ab +2 -0\0u UU fields path\0", ("conflict", "ahead")),
            (b"# branch.ab +0 -3\0u UU fields path\0", ("conflict", "behind")),
            (b"2 R. fields new\0u not-a-conflict\0", ("dirty",)),
            (b"2 R. fields new\0# branch.ab +bad -bad\0", ("dirty",)),
            (b"? file\nu fake\n# branch.ab +5 -2\0", ("dirty",)),
        ]:
            with self.subTest(output=output):
                self.assertEqual(parse_status(output), expected)

    def test_optional_git_failures_and_timeout(self):
        for error in (FileNotFoundError(), OSError(), subprocess.TimeoutExpired("git", 2)):
            with patch("workmux.git_status.subprocess.run", side_effect=error):
                self.assertIsNone(probe_status("/some path; not a shell"))
        with patch("workmux.git_status.subprocess.run", return_value=Mock(returncode=128)):
            self.assertIsNone(probe_status("/missing"))
        with patch("workmux.git_status.subprocess.run") as run:
            self.assertIsNone(probe_status(""))
            run.assert_not_called()

    def test_safe_probe_arguments(self):
        directory = "/some path; $(false)"
        with patch("workmux.git_status.subprocess.run",
                   return_value=Mock(returncode=0, stdout=b"")) as run:
            self.assertEqual(probe_status(directory), ("clean",))
        self.assertEqual(run.call_args.args[0], [
            "git", "--no-optional-locks", "-C", directory, "status", "--porcelain=v2",
            "--branch", "-z", "--untracked-files=normal", "--ignore-submodules=none"])
        self.assertEqual(run.call_args.kwargs["timeout"], 2.0)

    def test_cache_is_nonblocking_deduplicates_and_polls(self):
        first, second = Future(), Future()
        workspaces = [Workspace("$1", "one", "", "/repo"),
                      Workspace("$2", "two", "", "/repo"),
                      Workspace("$3", "empty", "", "")]
        with patch("workmux.git_status.ThreadPoolExecutor") as executor, \
                patch("workmux.git_status.time.monotonic", return_value=0) as clock:
            executor.return_value.submit.side_effect = [first, second]
            cache = GitStatusCache()
            self.assertEqual(cache.snapshot(workspaces), {"$1": None, "$2": None, "$3": None})
            cache.snapshot(workspaces)  # An unfinished probe never blocks or gets resubmitted.
            self.assertEqual(executor.return_value.submit.call_count, 1)
            first.set_result(("dirty", "ahead"))
            self.assertEqual(cache.snapshot(workspaces)["$2"], ("dirty", "ahead"))
            clock.return_value = POLL_INTERVAL
            self.assertEqual(cache.snapshot(workspaces)["$1"], ("dirty", "ahead"))
            self.assertEqual(executor.return_value.submit.call_count, 2)
            self.assertEqual(cache.snapshot(workspaces)["$1"], ("dirty", "ahead"))
            second.set_result(None)
            self.assertIsNone(cache.snapshot(workspaces)["$1"])
            cache.snapshot([])
            self.assertEqual(cache.entries, {})
            cache.close()
            executor.return_value.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_directory_change_drops_old_status_and_cancels_pending_check(self):
        old, new = Future(), Future()
        with patch("workmux.git_status.ThreadPoolExecutor") as executor:
            executor.return_value.submit.side_effect = [old, new]
            cache = GitStatusCache()
            cache.snapshot([Workspace("$1", "one", "", "/old")])
            self.assertEqual(cache.snapshot([Workspace("$1", "renamed", "", "/new")]), {"$1": None})
            self.assertTrue(old.cancelled())
            cache.close()


@unittest.skipUnless(shutil.which("git"), "Git is optional")
class GitRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workmux-git-")
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull,
                                         "GIT_CONFIG_NOSYSTEM": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = Path(self.temp.name) / "repo space; $literal"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Workmux Test")
        self.git("config", "user.email", "test@example.invalid")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def commit(self, text):
        (self.repo / "file").write_text(text)
        self.git("add", ".")
        self.git("commit", "-m", "test")

    def test_unborn_dirty_clean_ignored_rename_detached_and_non_repo(self):
        self.assertEqual(probe_status(str(self.repo)), ("clean",))
        (self.repo / "new\nu fake").write_text("new")
        self.assertEqual(probe_status(str(self.repo)), ("dirty",))
        self.commit("initial")
        self.assertEqual(probe_status(str(self.repo)), ("clean",))
        (self.repo / ".git" / "info" / "exclude").write_text("ignored\n")
        (self.repo / "ignored").write_text("ignored")
        self.assertEqual(probe_status(str(self.repo)), ("clean",))
        self.git("mv", "file", "u fake")
        self.assertEqual(probe_status(str(self.repo)), ("dirty",))
        self.git("commit", "-m", "rename")
        self.git("checkout", "--detach")
        self.assertEqual(probe_status(str(self.repo)), ("clean",))
        self.assertIsNone(probe_status(self.temp.name))
        self.assertIsNone(probe_status(str(self.repo / "missing")))

    def test_upstream_states_combine_with_changes_and_conflict(self):
        self.commit("initial\n")
        self.git("branch", "upstream")
        self.git("branch", "--set-upstream-to=upstream")
        self.assertEqual(probe_status(str(self.repo)), ("clean",))
        self.commit("main\n")
        self.assertEqual(probe_status(str(self.repo)), ("ahead",))
        self.git("checkout", "upstream")
        self.commit("other\n")
        self.git("checkout", "main")
        self.assertEqual(probe_status(str(self.repo)), ("diverged",))
        (self.repo / "untracked").write_text("new")
        self.assertEqual(probe_status(str(self.repo)), ("dirty", "diverged"))
        result = subprocess.run(["git", "-C", str(self.repo), "merge", "upstream"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(probe_status(str(self.repo)), ("conflict", "dirty", "diverged"))
        (self.repo / "untracked").unlink()
        self.assertEqual(probe_status(str(self.repo)), ("conflict", "diverged"))
        self.git("merge", "--abort")
        self.git("reset", "--hard", "upstream~1")
        self.assertEqual(probe_status(str(self.repo)), ("behind",))


class GitSidebarTests(unittest.TestCase):
    def test_session_suffix_only_and_stable_identity(self):
        workspace = Workspace("$1", "repo", "", "/repo")
        tabs = [Tab("$1", "@2", 2, "build", "working")]
        original = sidebar_rows([workspace], tabs)
        for state, symbol in GIT_INDICATORS.items():
            with self.subTest(state=state):
                rows = sidebar_rows([workspace], tabs, {"$1": (state,)})
                self.assertEqual(rows[0].text, f"1:repo {symbol}")
                self.assertEqual(rows[0].key, original[0].key)
                self.assertEqual(rows[1], original[1])
        self.assertEqual(sidebar_rows([workspace], tabs, {"$1": None}), original)

    def test_only_suffix_colored_even_with_same_symbol_in_wide_name(self):
        screen = Mock()
        screen.getmaxyx.return_value = (20, 80)
        row = sidebar_rows([Workspace("$1", "✓界e\u0301", "", "/repo")], [], {"$1": ("clean",)})[0]
        draw_row(screen, 2, "›", row, curses.A_REVERSE, {"git-clean": 256})
        self.assertEqual(screen.addnstr.call_args_list[0].args,
                         (2, 0, "› 1:✓界e\u0301 ✓", 79, curses.A_REVERSE))
        self.assertEqual(screen.addnstr.call_args_list[1].args,
                         (2, 9, "✓", 1, curses.A_REVERSE | 256))

    def test_combined_suffix_and_independent_colors_with_wide_name(self):
        screen = Mock()
        screen.getmaxyx.return_value = (20, 80)
        workspace = Workspace("$1", "!●↕界e\u0301", "", "/repo")
        tabs = [Tab("$1", "@2", 2, "build", "working")]
        states = ("conflict", "dirty", "diverged")
        rows = sidebar_rows([workspace], tabs, {"$1": states})
        self.assertEqual(rows[0].text, "1:!●↕界e\u0301 ! ● ↕")
        self.assertEqual(rows[0].git_states, states)
        self.assertEqual(rows[0].key, ("$1", None))
        self.assertEqual(rows[1], sidebar_rows([workspace], tabs)[1])
        colors = {"git-conflict": 256, "git-dirty": 512, "git-diverged": 1024}
        draw_row(screen, 2, "›", rows[0], curses.A_REVERSE, colors)
        self.assertEqual([call.args for call in screen.addnstr.call_args_list], [
            (2, 0, "› 1:!●↕界e\u0301 ! ● ↕", 79, curses.A_REVERSE),
            (2, 11, "!", 1, curses.A_REVERSE | 256),
            (2, 13, "●", 1, curses.A_REVERSE | 512),
            (2, 15, "↕", 1, curses.A_REVERSE | 1024),
        ])
        screen.reset_mock()
        draw_row(screen, 2, "›", rows[0], 0, {})
        self.assertEqual(screen.addnstr.call_count, 1)

    def test_combined_suffix_clips_each_symbol_independently(self):
        screen = Mock()
        screen.getmaxyx.return_value = (20, 9)
        row = sidebar_rows([Workspace("$1", "x", "", "/repo")], [],
                           {"$1": ("dirty", "ahead")})[0]
        draw_row(screen, 2, " ", row, 0, {"git-dirty": 256, "git-ahead": 512})
        self.assertEqual([call.args for call in screen.addnstr.call_args_list], [
            (2, 0, "  1:x ● ↑", 8, 0),
            (2, 6, "●", 1, 256),
        ])

    def test_no_color_and_clipped_suffix(self):
        screen = Mock()
        screen.getmaxyx.return_value = (20, 8)
        row = sidebar_rows([Workspace("$1", "long name", "", "/repo")], [], {"$1": ("dirty",)})[0]
        for colors in ({}, {"git-dirty": 256}):
            screen.reset_mock()
            draw_row(screen, 2, " ", row, 0, colors)
            self.assertEqual(screen.addnstr.call_count, 1)
