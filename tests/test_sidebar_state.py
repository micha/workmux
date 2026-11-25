"""Shared sidebar sampling uses private tmux servers."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from workmux.core import Tmux
from workmux.git_status import GitStatus
from workmux.sidebar_state import SidebarSnapshot, SidebarState, _decode, _encode


class SnapshotEncodingTests(unittest.TestCase):
    def test_branch_metadata_is_compatible_with_older_sidebars(self):
        snapshot = SidebarSnapshot(git_statuses={
            "$1": GitStatus(("dirty",), "main"), "/checkout": GitStatus(("clean",)), "$2": None,
        })
        data = json.loads(_encode(snapshot))
        self.assertEqual(data["git_states"], {"$1": ["dirty"], "/checkout": ["clean"], "$2": None})
        self.assertEqual(data.pop("git_branches"), {"$1": "main"})
        self.assertEqual(_decode(json.dumps(data)).git_statuses,
                         {"$1": GitStatus(("dirty",)), "/checkout": GitStatus(("clean",)), "$2": None})


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for integration tests")
class SidebarSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="workmux-state-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        environment = patch.dict(os.environ, XDG_RUNTIME_DIR=directory.name, HOME=directory.name,
                                 ENV=os.devnull, BASH_ENV=os.devnull)
        environment.start()
        self.addCleanup(environment.stop)
        self.tmux = Tmux(socket=str(self.path / "socket ' with spaces"))
        self.addCleanup(self.tmux.run, "kill-server", missing_ok=True)

    def test_batched_snapshot_matches_independent_reads_including_linked_windows(self):
        first = self.tmux.run("new-session", "-ds", "beta", "-P", "-F", "#{session_id}", "sleep 60")
        second = self.tmux.run("new-session", "-ds", "alpha", "-P", "-F", "#{session_id}", "sleep 60")
        for session in (first, second):
            self.tmux.run("set-option", "-t", session, "@workmux", "1")
            self.tmux.run("set-option", "-t", session, "@workspace-dir", str(self.path))
        self.tmux.run("set-option", "-t", second, "@workmux-checkout-max-depth", "0")
        window = self.tmux.run("display-message", "-p", "-t", first + ":", "#{window_id}")
        self.tmux.run("link-window", "-ds", first + ":", "-t", second + ":")
        directory = str(self.path / "checkout ' with spaces")
        self.tmux.run("set-option", "-w", "-t", window, "@workmux-checkout", json.dumps(directory))
        self.tmux.run("set-option", "-p", "-t", first + ":", "@workmux-agent", '{"state":"working"}')
        pane = self.tmux.run("split-window", "-hd", "-t", window, "-P", "-F", "#{pane_id}", "sleep 60")
        self.tmux.run("set-option", "-p", "-t", pane, "@workspace-sidebar", "1")
        with patch.object(self.tmux, "run", wraps=self.tmux.run) as run:
            snapshot = self.tmux.sidebar_snapshot()
        run.assert_called_once()
        self.assertEqual(snapshot.workspaces, self.tmux.workspaces())
        self.assertEqual([workspace.name for workspace in snapshot.workspaces], ["alpha", "beta"])
        self.assertEqual(snapshot.workspaces[0].checkout_max_depth, 0)
        self.assertEqual(snapshot.activity, self.tmux.activity_snapshot())
        self.assertEqual(snapshot.activity.publisher, pane)
        self.assertEqual({(tab.session, tab.id, tab.state) for tab in snapshot.activity.tabs},
                         {(first, window, "working"), (second, window, "working")})
        self.assertEqual(snapshot.checkout_windows, self.tmux.checkout_tabs())
        self.assertEqual(snapshot.checkout_windows, {(first, directory): window, (second, directory): window})

    def managed(self, tmux, name, directory):
        session = tmux.run("new-session", "-ds", name, "-P", "-F", "#{session_id}", "sleep 60")
        tmux.run("set-option", "-t", session, "@workmux", "1")
        tmux.run("set-option", "-t", session, "@workspace-dir", str(directory))
        return session

    def state(self, tmux):
        state = SidebarState(tmux)
        self.addCleanup(state.close)
        return state

    def test_shared_tmux_and_git_polls_handoff_and_socket_isolation(self):
        directory = self.path / "workspace"
        checkout = directory / "checkout"
        checkout.mkdir(parents=True)
        first = self.managed(self.tmux, "first", directory)
        second = self.managed(self.tmux, "second", directory)
        window = self.tmux.run("display-message", "-p", "-t", first + ":", "#{window_id}")
        self.tmux.run("set-option", "-w", "-t", window, "@workmux-checkout", json.dumps(str(checkout)))
        self.tmux.run("set-option", "-p", "-t", first + ":", "@workmux-agent",
                      json.dumps({"state": "working", "roots": [str(directory)]}))
        leader = self.state(self.tmux)
        alias = self.path / "socket alias"
        alias.symlink_to(self.tmux.socket)
        follower = self.state(Tmux(socket=str(alias)))
        other = Tmux(socket=str(self.path / "other"))
        # Register the kill before its state so that cleanup closes its lease first.
        self.addCleanup(other.run, "kill-server", missing_ok=True)
        self.managed(other, "unrelated", directory)
        isolated = self.state(other)
        statuses = {str(directory): GitStatus(("dirty",), "main"),
                    str(checkout): GitStatus(("dirty",), "feat/login")}
        with (patch.object(Tmux, "sidebar_snapshot", autospec=True,
                           side_effect=Tmux.sidebar_snapshot) as tmux_poll,
              patch("workmux.checkouts.discover_checkouts", return_value=(str(checkout),)) as scan,
              patch("workmux.git_status.probe_status", side_effect=statuses.__getitem__) as git_probe):
            expected = {first: statuses[str(directory)], second: statuses[str(directory)],
                        str(checkout): statuses[str(checkout)]}
            for _ in range(100):
                snapshot = leader.snapshot()
                count = tmux_poll.call_count
                received = follower.snapshot()
                self.assertEqual(received, snapshot)
                self.assertIs(follower.snapshot(), received)
                self.assertEqual(tmux_poll.call_count, count)
                if snapshot.git_statuses == expected:
                    break
                time.sleep(0.01)
            self.assertEqual(snapshot.git_statuses, expected)
            self.assertEqual(snapshot.activity.tabs[0].agents[0].roots, (str(directory),))
            self.assertEqual(snapshot.checkouts, {first: (str(checkout),), second: (str(checkout),)})
            self.assertEqual(snapshot.checkout_windows, {(first, str(checkout)): window})
            scan.assert_called_once()
            self.assertCountEqual([call.args for call in git_probe.call_args_list],
                                  [(str(directory),), (str(checkout),)])

            # One file/lease per server, not per sidebar or original selector.
            self.assertEqual([item.name for item in isolated.snapshot().workspaces], ["unrelated"])
            leader.close()
            self.tmux.run("rename-session", "-t", first, "chosen")
            new_snapshot = follower.snapshot()
            self.assertEqual([item.name for item in new_snapshot.workspaces], ["chosen", "second"])
            self.assertIs(tmux_poll.call_args.args[0], follower.tmux)
            # The new producer presents fresh tmux state while its async Git
            # and checkout caches warm up. A cold cache never removes a window.
            self.assertEqual(new_snapshot.checkouts, {first: (), second: ()})
            self.assertEqual(new_snapshot.git_statuses, {first: None, second: None})
            self.assertEqual(new_snapshot.checkout_windows, snapshot.checkout_windows)
            last_reader = self.state(self.tmux)
            count = tmux_poll.call_count
            self.assertEqual(last_reader.snapshot(), new_snapshot)
            self.assertEqual(tmux_poll.call_count, count)
            for _ in range(100):
                warmed = follower.snapshot()
                if warmed.git_statuses == expected:
                    break
                time.sleep(0.01)
            self.assertEqual(warmed.git_statuses, expected)
            self.assertEqual(warmed.checkouts, snapshot.checkouts)
            self.assertEqual(last_reader.snapshot(), warmed)

    def test_crashed_producer_releases_lease_and_orphans_are_reaped(self):
        session = self.managed(self.tmux, "before", self.path)
        self.tmux.run("set-option", "-u", "-t", session, "@workspace-dir")
        subprocess.run([sys.executable, "-c", """
import os, sys
from workmux.core import Tmux
from workmux.sidebar_state import SidebarState

state = SidebarState(Tmux(socket=sys.argv[1]))
assert state.snapshot().workspaces[0].name == 'before'
os._exit(0)  # No state.close() on unexpected process termination.
""", self.tmux.socket], check=True, timeout=5)
        cache = self.path / f"workmux-sidebar-v1-{os.getuid()}"
        [orphan_lock] = list(cache.glob("*.lock"))
        orphan_data = orphan_lock.with_suffix(".json")
        self.assertTrue(orphan_data.is_file())

        follower = self.state(self.tmux)
        self.tmux.run("rename-session", "-t", session, "after")
        self.assertEqual([item.name for item in follower.snapshot().workspaces], ["after"])
        # Old mtimes alone are not enough to remove a still-held lease.
        for path in (orphan_lock, orphan_data):
            os.utime(path, (1, 1))
        second = Tmux(socket=str(self.path / "second"))
        self.addCleanup(second.run, "kill-server", missing_ok=True)
        self.managed(second, "second", "")
        state = self.state(second)
        state.snapshot()
        self.assertTrue(orphan_lock.exists())
        self.assertTrue(orphan_data.exists())
        state.close()
        follower.close()
        self.assertFalse(orphan_data.exists())

        # Model a stale lock + atomic staging file left by an already-dead server.
        temporary = orphan_lock.with_suffix(".tmp.leftover")
        for path in (orphan_lock, orphan_data, temporary):
            path.touch()
            os.utime(path, (1, 1))
        self.state(second).snapshot()
        self.assertFalse(any(path.exists() for path in (orphan_lock, orphan_data, temporary)))

    def test_non_private_cache_falls_back_without_changing_it(self):
        cache = self.path / f"workmux-sidebar-v1-{os.getuid()}"
        target = self.path / "keep"
        target.mkdir()
        cache.symlink_to(target)
        session = self.managed(self.tmux, "local", self.path)
        self.tmux.run("set-option", "-t", session, "@workmux-checkout-max-depth", "0")
        state = self.state(self.tmux)
        self.assertIsNone(state.shared)
        self.assertEqual([item.name for item in state.snapshot().workspaces], ["local"])
        self.assertEqual(list(target.iterdir()), [])

    def test_snapshot_is_empty_with_no_server(self):
        snapshot = self.tmux.sidebar_snapshot()
        self.assertEqual(snapshot.workspaces, [])
        self.assertEqual(snapshot.activity.tabs, [])
        self.assertIsNone(snapshot.activity.publisher)
        self.assertEqual(snapshot.checkout_windows, {})


if __name__ == "__main__":
    unittest.main()
