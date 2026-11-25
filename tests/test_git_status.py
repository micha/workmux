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
from workmux.git_status import GitStatus, GitStatusCache, POLL_INTERVAL, parse_status, probe_status
from workmux.sidebar import draw_row, sidebar_rows


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
                self.assertEqual(parse_status(output).states, expected)

    def test_branch_metadata_ignores_filenames_and_decodes_utf8(self):
        output = (b"# branch.head feat/caf\xc3\xa9\0"
                  b"2 R. fields new\0# branch.head fake\0? file\n# branch.head fake\0")
        self.assertEqual(parse_status(output), GitStatus(("dirty",), "feat/café"))
        self.assertIsNone(parse_status(b"# branch.ab +0 -0\0").head)
        self.assertEqual(parse_status(b"# branch.head feat/\xff\0").head, "feat/\ufffd")

    def test_detached_head_uses_short_oid_but_named_head_keeps_branch(self):
        for length in (40, 64):
            oid = b"123abcd" + b"0" * (length - 7)
            oid_header = b"# branch.oid " + oid + b"\0"
            self.assertEqual(parse_status(oid_header + b"# branch.head (detached)\0? new\0"),
                             GitStatus(("dirty",), "123abcd"))
            self.assertEqual(parse_status(oid_header + b"# branch.head main\0").head, "main")
        self.assertIsNone(parse_status(b"# branch.head (detached)\0").head)
        self.assertIsNone(parse_status(b"# branch.oid (initial)\0# branch.head (detached)\0").head)
        self.assertIsNone(parse_status(b"# branch.head (detached)\0" + b"2 R. fields new\0" + oid_header).head)

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
            self.assertEqual(probe_status(directory), GitStatus(("clean",)))
        self.assertEqual(run.call_args.args[0], [
            "git", "--no-optional-locks", "-C", directory, "status", "--porcelain=v2",
            "--branch", "-z", "--untracked-files=normal", "--ignore-submodules=none"])
        self.assertEqual(run.call_args.kwargs["timeout"], 2.0)

    def test_cache_is_nonblocking_deduplicates_and_polls(self):
        first, second, third = Future(), Future(), Future()
        main = GitStatus(("dirty", "ahead"), "main")
        workspaces = [Workspace("$1", "one", "", "/repo"),
                      Workspace("$2", "two", "", "/repo"),
                      Workspace("$3", "empty", "", "")]
        with patch("workmux.git_status.ThreadPoolExecutor") as executor, \
                patch("workmux.git_status.time.monotonic", return_value=0) as clock:
            executor.return_value.submit.side_effect = [first, second, third]
            cache = GitStatusCache()
            self.assertEqual(cache.snapshot(workspaces), {"$1": None, "$2": None, "$3": None})
            cache.snapshot(workspaces)  # An unfinished probe never blocks or gets resubmitted.
            self.assertEqual(executor.return_value.submit.call_count, 1)
            first.set_result(main)
            self.assertEqual(cache.snapshot(workspaces)["$2"], main)
            clock.return_value = POLL_INTERVAL
            self.assertEqual(cache.snapshot(workspaces)["$1"], main)
            self.assertEqual(executor.return_value.submit.call_count, 2)
            self.assertEqual(cache.snapshot(workspaces)["$1"], main)
            updated = GitStatus(main.states, "fix/npe")
            second.set_result(updated)
            self.assertEqual(cache.snapshot(workspaces)["$1"], updated)
            clock.return_value += POLL_INTERVAL
            self.assertEqual(cache.snapshot(workspaces)["$1"], updated)
            third.set_result(None)
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
        clean = GitStatus(("clean",), "main")
        dirty = GitStatus(("dirty",), "main")
        self.assertEqual(probe_status(str(self.repo)), clean)
        (self.repo / "new\nu fake").write_text("new")
        self.assertEqual(probe_status(str(self.repo)), dirty)
        self.commit("initial")
        self.assertEqual(probe_status(str(self.repo)), clean)
        (self.repo / ".git" / "info" / "exclude").write_text("ignored\n")
        (self.repo / "ignored").write_text("ignored")
        self.assertEqual(probe_status(str(self.repo)), clean)
        self.git("mv", "file", "u fake")
        self.assertEqual(probe_status(str(self.repo)), dirty)
        self.git("commit", "-m", "rename")
        head = self.git("rev-parse", "HEAD").stdout.decode().strip()[:7]
        self.git("checkout", "--detach")
        self.assertEqual(probe_status(str(self.repo)), GitStatus(("clean",), head))
        self.assertIsNone(probe_status(self.temp.name))
        self.assertIsNone(probe_status(str(self.repo / "missing")))

    def test_linked_worktree_reports_its_own_branch_not_the_main_checkout(self):
        self.commit("initial")
        worktree = str(Path(self.temp.name) / "api")
        self.git("worktree", "add", "-b", "feat/café", worktree)
        self.assertEqual(probe_status(str(self.repo)), GitStatus(("clean",), "main"))
        self.assertEqual(probe_status(worktree), GitStatus(("clean",), "feat/café"))

    def test_upstream_states_combine_with_changes_and_conflict(self):
        self.commit("initial\n")
        self.git("branch", "upstream")
        self.git("branch", "--set-upstream-to=upstream")
        self.assertEqual(probe_status(str(self.repo)), GitStatus(("clean",), "main"))
        self.commit("main\n")
        self.assertEqual(probe_status(str(self.repo)), GitStatus(("ahead",), "main"))
        self.git("checkout", "upstream")
        self.commit("other\n")
        self.git("checkout", "main")
        self.assertEqual(probe_status(str(self.repo)), GitStatus(("diverged",), "main"))
        (self.repo / "untracked").write_text("new")
        self.assertEqual(probe_status(str(self.repo)), GitStatus(("dirty", "diverged"), "main"))
        result = subprocess.run(["git", "-C", str(self.repo), "merge", "upstream"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(probe_status(str(self.repo)), GitStatus(("conflict", "dirty", "diverged"), "main"))
        (self.repo / "untracked").unlink()
        self.assertEqual(probe_status(str(self.repo)), GitStatus(("conflict", "diverged"), "main"))
        self.git("merge", "--abort")
        self.git("reset", "--hard", "upstream~1")
        self.assertEqual(probe_status(str(self.repo)), GitStatus(("behind",), "main"))


class GitSidebarTests(unittest.TestCase):
    def test_status_preserves_labels_and_stable_identity(self):
        workspace = Workspace("$1", "repo", "", "/repo")
        tabs = [Tab("$1", "@2", 2, "build", "working")]
        original = sidebar_rows([workspace], tabs)
        status = GitStatus(("dirty", "ahead"), "fix/npe")
        rows = sidebar_rows([workspace], tabs, {"$1": status})
        self.assertEqual(rows[0].text, "1:repo")
        self.assertEqual(rows[0].git_status, status)
        self.assertEqual(rows[0].key, original[0].key)
        self.assertEqual(rows[1], original[1])
        self.assertEqual(sidebar_rows([workspace], tabs, {"$1": None}), original)

    def test_one_undimmed_gutter_symbol_combines_health_and_divergence(self):
        workspace = Workspace("$1", "~↑↓↕界e\u0301", "", "/repo")
        colors = {"git-conflict": 256, "git-dirty": 512, "git-clean": 1024}
        attribute = curses.A_REVERSE | curses.A_DIM
        for health, color in [((), 1024), (("dirty",), 512), (("conflict",), 256),
                              (("dirty", "conflict"), 256)]:
            for divergence, symbol in [((), "~"), (("ahead",), "↑"),
                                       (("behind",), "↓"), (("diverged",), "↕")]:
                with self.subTest(health=health, divergence=divergence):
                    if not health and not divergence:
                        continue  # Clean and in sync is tested separately.
                    screen = Mock()
                    screen.getmaxyx.return_value = (20, 80)
                    row = sidebar_rows([workspace], [], {"$1": GitStatus(health + divergence)})[0]
                    draw_row(screen, 2, row, attribute, colors)
                    self.assertEqual([call.args for call in screen.addnstr.call_args_list], [
                        (2, 0, f"{symbol} 1:~↑↓↕界e\u0301", 79, attribute),
                        (2, 0, symbol, 1, curses.A_REVERSE | color),
                    ])

    def test_no_symbol_for_clean_in_sync_or_unavailable(self):
        workspace = Workspace("$1", "repo", "", "/repo")
        for status in (GitStatus(("clean",)), None):
            with self.subTest(status=status):
                screen = Mock()
                screen.getmaxyx.return_value = (20, 80)
                row = sidebar_rows([workspace], [], {"$1": status})[0]
                draw_row(screen, 2, row, curses.A_BOLD, {"git-clean": 256})
                screen.addnstr.assert_called_once_with(2, 0, "  1:repo", 79, curses.A_BOLD)
                screen.addstr.assert_not_called()

    def test_checkout_symbol_stays_bright_without_color_in_a_narrow_sidebar(self):
        screen = Mock()
        screen.getmaxyx.return_value = (20, 4)
        workspace = Workspace("$1", "job", "", "/job")
        screen.getyx.return_value = (2, 3)
        row = sidebar_rows([workspace], [], {"/job/repo": GitStatus(("dirty", "ahead"), "fix/npe")},
                           {"$1": ("/job/repo",)})[1]
        draw_row(screen, 2, row, curses.A_DIM, {})
        self.assertEqual([call.args for call in screen.addnstr.call_args_list], [
            (2, 0, "↑ └ ⎇ repo", 3, curses.A_DIM),
            (2, 2, "└", 1, curses.A_DIM),
            (2, 0, "↑", 1, 0),
        ])
        screen.addstr.assert_not_called()
