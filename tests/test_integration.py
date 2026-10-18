"""Real tmux tests. Every test uses a private socket and cleans up its server."""

import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch

from workmux.cli import main
from workmux.core import Tmux, WorkmuxError
from workmux.layouts import LAYOUTS


def eventually(predicate, message="condition was not reached", timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(message)


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for integration tests")
class TmuxIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workmux-test-")
        # Deliberately exercise both tmux config and shell quoting.
        self.socket = str(Path(self.temporary.name) / "socket ' with spaces")
        self.tmux = Tmux(socket=self.socket)
        self.clients = []
        self.master_fds = []
        self.reader_threads = []
        self.stop_readers = threading.Event()

    def tearDown(self):
        self.tmux.run("kill-server", missing_ok=True)
        for client in self.clients:
            try:
                client.wait(timeout=3)
            except subprocess.TimeoutExpired:
                client.kill()
                client.wait()
        self.stop_readers.set()
        for reader in self.reader_threads:
            reader.join(timeout=2)
        for fd in self.master_fds:
            os.close(fd)
        self.temporary.cleanup()

    def workspace(self, name="project-a"):
        session = self.tmux.new_workspace(name, self.temporary.name)
        content = self.tmux.pane(session + ":")
        sidebar = next(pane for pane in self.tmux.panes(content.window) if pane.sidebar)
        return session, content, sidebar

    def attach(self, session):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        environment = dict(os.environ, TERM="xterm-256color")
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        client = subprocess.Popen(
            ["tmux", "-S", self.socket, "-f", os.devnull, "attach-session", "-t", session],
            stdin=slave, stdout=slave, stderr=slave, env=environment,
        )
        os.close(slave)
        self.clients.append(client)
        self.master_fds.append(master)

        def drain():
            while not self.stop_readers.is_set():
                try:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable and not os.read(master, 65536):
                        break
                except OSError:
                    break

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        self.reader_threads.append(reader)
        client_name = eventually(lambda: self.tmux.run("list-clients", "-F", "#{client_name}"))
        return master, client, client_name

    def native_session(self, name="native"):
        pane = self.tmux.run("new-session", "-d", "-s", name, "-x", "120", "-y", "40",
                             "-P", "-F", "#{pane_id}")
        return self.tmux.pane(pane).session, pane

    def test_last_shell_exit_shows_empty_state_and_prefix_c_replaces_placeholder(self):
        session, pane = self.native_session()
        self.tmux.enable(pane)
        original_window = self.tmux.pane(pane).window
        master, _, _ = self.attach(session)
        self.tmux.run("send-keys", "-t", pane, "exit", "Enter")
        eventually(lambda: len(self.tmux.panes(original_window)) == 1)
        placeholder = self.tmux.pane(session + ":")
        self.assertTrue(placeholder.sidebar)
        eventually(lambda: "No terminals in this tab" in self.tmux.run("capture-pane", "-p", "-t", placeholder.id))
        os.write(master, b"\x02c")
        eventually(lambda: not self.tmux.pane(session + ":").sidebar)
        eventually(lambda: self.tmux.run("list-windows", "-t", session, "-F", "#{window_id}")
                   == self.tmux.pane(session + ":").window)
        active = self.tmux.pane(session + ":")
        self.assertNotEqual(active.window, original_window)
        self.assertEqual(len(self.tmux.panes(active.window)), 2)
        self.assertEqual(sum(item.sidebar for item in self.tmux.panes(active.window)), 1)

    def test_new_tab_helper_recovers_empty_workspace_in_its_directory(self):
        session, content, sidebar = self.workspace()
        self.tmux.run("send-keys", "-t", content.id, "exit", "Enter")
        eventually(lambda: len(self.tmux.panes(content.window)) == 1)
        created = self.tmux.new_tab(sidebar.id, "recovered")
        self.assertEqual(self.tmux.pane(created).directory, self.temporary.name)
        self.assertEqual(self.tmux.pane(session + ":").id, created)
        self.assertEqual(self.tmux.run("list-windows", "-t", session, "-F", "#{window_id}"),
                         self.tmux.pane(created).window)

    def test_detached_new_tab_from_empty_workspace_preserves_current_placeholder(self):
        session, pane = self.native_session()
        self.tmux.run("bind-key", "v", "new-window", "-d", "-n", "background")
        self.tmux.enable(pane)
        original_window = self.tmux.pane(pane).window
        master, _, _ = self.attach(session)
        self.tmux.run("send-keys", "-t", pane, "exit", "Enter")
        eventually(lambda: len(self.tmux.panes(original_window)) == 1)
        placeholder = self.tmux.pane(session + ":").id
        os.write(master, b"\x02v")
        eventually(lambda: len(self.tmux.run("list-windows", "-t", session, "-F", "#{window_id}").splitlines()) == 2)
        self.assertEqual(self.tmux.pane(session + ":").id, placeholder)
        background = self.tmux.run("list-windows", "-t", session, "-f", "#{==:#{window_name},background}",
                                   "-F", "#{window_id}")
        eventually(lambda: len(self.tmux.panes(background)) == 2)
        self.assertTrue(self.tmux.pane(placeholder).sidebar)

    def test_switching_to_empty_workspace_and_focus_toggle_do_not_error(self):
        empty, pane = self.native_session("empty")
        self.tmux.enable(pane)
        original_window = self.tmux.pane(pane).window
        self.tmux.run("send-keys", "-t", pane, "exit", "Enter")
        eventually(lambda: len(self.tmux.panes(original_window)) == 1)
        other, other_pane = self.native_session("other")
        self.tmux.enable(other_pane)
        _, _, client = self.attach(other)
        self.tmux.select_workspace(empty, other_pane, client)
        placeholder = self.tmux.pane(empty + ":")
        self.assertTrue(placeholder.sidebar)
        self.tmux.toggle_sidebar(placeholder.id)
        self.assertTrue(self.tmux.pane(placeholder.id).active)

    def test_new_tab_from_focused_sidebar_never_retires_live_content(self):
        session, content, sidebar = self.workspace()
        self.tmux.toggle_sidebar(content.id)
        self.tmux.new_tab(sidebar.id, "next")
        self.assertEqual(len(self.tmux.panes(content.window)), 2)
        self.assertFalse(self.tmux.pane(content.id).sidebar)
        self.assertEqual(len(self.tmux.run("list-windows", "-t", session, "-F", "#{window_id}").splitlines()), 2)

    def test_reenable_empty_workspace_recovers_missing_directory_metadata(self):
        session, pane = self.native_session()
        self.tmux.enable(pane)
        original_window = self.tmux.pane(pane).window
        self.tmux.run("send-keys", "-t", pane, "exit", "Enter")
        eventually(lambda: len(self.tmux.panes(original_window)) == 1)
        placeholder = self.tmux.pane(session + ":")
        self.tmux.run("set-option", "-u", "-t", session, "@workspace-dir")
        result = self.tmux.enable(placeholder.id)
        self.assertEqual(result.sidebars_added, 0)
        self.assertTrue(self.tmux.run("show-options", "-qv", "-t", session, "@workspace-dir"))
        self.assertEqual(len(self.tmux.panes(original_window)), 1)

    def test_stock_bindings_enable_without_warnings_and_keep_menus_unchanged(self):
        _, pane = self.native_session()
        menus = {key: self.tmux.run("list-keys", "-T", "prefix", key) for key in ("<", ">")}
        result = self.tmux.enable(pane)
        self.assertEqual(result.warnings, ())
        self.assertIn("dispatch", self.tmux.run("list-keys", "-T", "prefix", "E"))
        for key, original in menus.items():
            self.assertEqual(self.tmux.run("list-keys", "-T", "prefix", key), original)
        self.assertEqual(self.tmux.enable(pane).warnings, ())

    def test_native_balance_key_equalizes_content_without_resizing_sidebar(self):
        session, pane = self.native_session()
        self.tmux.enable(pane)
        self.tmux.split(pane, "right")
        self.tmux.run("select-pane", "-t", pane)
        master, _, _ = self.attach(session)
        self.tmux.run("resize-pane", "-t", pane, "-x", "20")
        os.write(master, b"\x02E")
        eventually(lambda: self.tmux.pane(pane).width == 47)
        window = self.tmux.pane(pane).window
        sidebar = next(item.id for item in self.tmux.panes(window) if item.sidebar)
        self.assert_sidebar_layout(window, sidebar, [item.id for item in self.tmux.panes(window)])
        self.assertTrue(self.tmux.pane(pane).active)

    def test_balancing_nested_content_preserves_topology_and_other_groups(self):
        _, content, sidebar = self.workspace()
        right = self.tmux.split(content.id, "right")
        bottom = self.tmux.split(right, "down")
        self.tmux.run("resize-pane", "-t", right, "-y", "10")
        self.tmux.run("set-option", "-w", "-t", content.window, "@workspace-layout", "main-vertical")
        before_left = self.tmux.run("display-message", "-p", "-t", content.id,
                                    "#{pane_left} #{pane_top} #{pane_width} #{pane_height}")
        self.tmux.balance(right)
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", right, "#{pane_height}"), "20")
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", bottom, "#{pane_height}"), "19")
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", content.id,
                                      "#{pane_left} #{pane_top} #{pane_width} #{pane_height}"), before_left)
        self.assertTrue(self.tmux.pane(bottom).active)
        self.assertEqual(self.tmux.run("show-options", "-wqv", "-t", content.window, "@workspace-layout"),
                         "main-vertical")
        first = self.tmux.run("display-message", "-p", "-t", content.window, "#{window_layout}")
        self.tmux.balance(right)
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", content.window, "#{window_layout}"), first)
        self.assert_sidebar_layout(content.window, sidebar.id, [content.id, sidebar.id, right, bottom])

    def test_balance_from_sidebar_uses_content_and_preserves_sidebar_focus(self):
        _, content, sidebar = self.workspace()
        self.tmux.split(content.id, "right")
        self.tmux.run("resize-pane", "-t", content.id, "-x", "20")
        self.tmux.toggle_sidebar(content.id)
        self.tmux.run("resize-pane", "-Z", "-t", sidebar.id)
        self.tmux.balance(sidebar.id)
        self.assertTrue(self.tmux.pane(sidebar.id).active)
        self.assertFalse(self.tmux.pane(sidebar.id).zoomed)
        self.assertEqual(self.tmux.pane(sidebar.id).width, 24)
        self.assertEqual(self.tmux.pane(content.id).width, 47)

    def test_balancing_single_content_pane_does_not_spread_sidebar_and_content(self):
        _, content, sidebar = self.workspace()
        self.tmux.balance(content.id)
        self.tmux.balance(content.id)
        self.assertEqual(self.tmux.pane(sidebar.id).width, 24)
        self.assertEqual(self.tmux.pane(content.id).width, 95)

    def test_balancing_honors_top_and_bottom_pane_border_status(self):
        for status in ("top", "bottom"):
            session, pane = self.native_session(status)
            self.tmux.run("set-option", "-w", "-t", self.tmux.pane(pane).window, "pane-border-status", status)
            self.tmux.enable(pane)
            other = self.tmux.split(pane, "down")
            self.tmux.run("resize-pane", "-t", pane, "-y", "10")
            self.tmux.balance(pane)
            first_height = int(self.tmux.run("display-message", "-p", "-t", pane, "#{pane_height}"))
            second_height = int(self.tmux.run("display-message", "-p", "-t", other, "#{pane_height}"))
            self.assertLessEqual(abs(first_height - second_height), 1)
            sidebar = next(item for item in self.tmux.panes(self.tmux.pane(pane).window) if item.sidebar)
            self.assertEqual(sidebar.width, 24)

    def test_unsupported_scrollbar_balancing_does_not_modify_layout(self):
        _, content, _ = self.workspace()
        self.tmux.split(content.id, "right")
        try:
            self.tmux.run("set-option", "-w", "-t", content.window, "pane-scrollbars", "on")
        except WorkmuxError:
            self.skipTest("This tmux version does not support scrollbars")
        before = self.tmux.run("display-message", "-p", "-t", content.window, "#{window_layout}")
        with self.assertRaisesRegex(WorkmuxError, "scrollbars"):
            self.tmux.balance(content.id)
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", content.window, "#{window_layout}"), before)

    def test_native_enable_is_idempotent_and_preserves_user_preferences(self):
        session, pane = self.native_session()
        self.tmux.run("split-window", "-h", "-t", pane)
        self.tmux.run("new-window", "-t", session + ":", "-n", "second")
        before = self.tmux.run("list-panes", "-s", "-t", session, "-F", "#{pane_id}").splitlines()
        active = self.tmux.pane(session + ":").id
        self.tmux.run("set-option", "-g", "prefix", "C-a")
        self.tmux.run("set-option", "-g", "status-position", "bottom")
        self.tmux.run("set-option", "-g", "status-style", "bg=blue")
        self.tmux.run("bind-key", "X", "display-message", "user-action")
        unrelated = self.tmux.run("list-keys", "-T", "prefix", "X")
        first = self.tmux.enable(pane)
        bindings = self.tmux.run("list-keys")
        second = self.tmux.enable(pane)
        self.assertEqual(first.sidebars_added, 2)
        self.assertEqual(second.sidebars_added, 0)
        self.assertEqual(self.tmux.run("list-keys"), bindings)
        self.assertEqual(self.tmux.run("list-keys", "-T", "prefix", "X"), unrelated)
        self.assertEqual(self.tmux.run("show-options", "-gv", "prefix"), "C-a")
        self.assertEqual(self.tmux.run("show-options", "-gv", "status-position"), "bottom")
        self.assertEqual(self.tmux.run("show-options", "-gv", "status-style"), "bg=blue")
        self.assertEqual(self.tmux.pane(session + ":").id, active)
        after = self.tmux.run("list-panes", "-s", "-t", session, "-F", "#{pane_id}").splitlines()
        self.assertTrue(set(before).issubset(after))
        self.assertEqual(len(after), len(before) + 2)
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", session, "#{session_name}"), "native")

    def test_activation_keybinding_and_native_creation_layout_shortcuts(self):
        session, pane = self.native_session()
        self.tmux.run("set-option", "-g", "prefix", "C-a")
        self.tmux.run("set-option", "-g", "repeat-time", "0")
        command = self.tmux.helper("enable", "--pane", "#{pane_id}", "--client", "#{client_name}", portable=True)
        self.tmux.run("bind-key", "W", "run-shell", "-b", command)
        master, _, _ = self.attach(session)
        os.write(master, b"\x01W")
        eventually(lambda: self.tmux.run("show-options", "-t", session, "-qv", "@workmux-enabled") == "1")
        eventually(lambda: "dispatch" in self.tmux.run("list-keys", "-T", "prefix", "c"))
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", pane, "#{pane_in_mode}"), "0")
        os.write(master, b"\x01c")
        eventually(lambda: len(self.tmux.run("list-windows", "-t", session, "-F", "#{window_id}").splitlines()) == 2)
        eventually(lambda: len(self.tmux.panes(self.tmux.pane(session + ":").window)) == 2)
        window = self.tmux.pane(session + ":").window
        os.write(master, b"\x01%")
        eventually(lambda: len(self.tmux.panes(window)) == 3)
        os.write(master, b'\x01"')
        eventually(lambda: len(self.tmux.panes(window)) == 4)
        os.write(master, b"\x01\x1b5")
        eventually(lambda: self.tmux.run("show-options", "-wqv", "-t", window, "@workspace-layout") == "tiled")
        sidebar = next(item.id for item in self.tmux.panes(window) if item.sidebar)
        self.assert_sidebar_layout(window, sidebar, [item.id for item in self.tmux.panes(window)])

    def test_adapted_bindings_preserve_native_behavior_in_disabled_session(self):
        enabled, pane = self.native_session("enabled")
        disabled, other = self.native_session("disabled")
        self.tmux.enable(pane)
        master, _, _ = self.attach(disabled)
        os.write(master, b"\x02c")
        eventually(lambda: len(self.tmux.run("list-windows", "-t", disabled, "-F", "#{window_id}").splitlines()) == 2)
        self.assertEqual(len(self.tmux.panes(self.tmux.pane(disabled + ":").window)), 1)
        os.write(master, b"\x02%")
        eventually(lambda: len(self.tmux.panes(self.tmux.pane(disabled + ":").window)) == 2)
        self.assertFalse(any(item.sidebar for item in self.tmux.panes(self.tmux.pane(disabled + ":").window)))
        self.assertEqual(self.tmux.run("show-options", "-qv", "-t", disabled, "@workmux"), "")

    def test_reload_then_reenable_adapts_new_binding_without_nesting(self):
        session, pane = self.native_session()
        self.tmux.run("source-file", "-", input='bind-key -r -T prefix v split-window -h -l 30\n')
        self.tmux.enable(pane)
        before_panes = self.tmux.run("list-panes", "-s", "-t", session, "-F", "#{pane_id}")
        self.tmux.run("source-file", "-", input=(
            'bind-key -r -N "new note" -T prefix v split-window -v -l 10\n'
            'set-option -g status-style bg=green\n'
        ))
        self.tmux.enable(pane)
        self.tmux.enable(pane)
        adapted = self.tmux.run("list-keys", "-T", "prefix", "v")
        self.assertEqual(adapted.count("@workmux-enabled"), 1)
        state = json.loads(self.tmux.run("show-options", "-sqv", "@workmux-binding-state"))
        self.assertTrue(all("@workmux-enabled" not in entry["original"] for entry in state["bindings"].values()))
        self.assertIn("new note", self.tmux.run("list-keys", "-N", "-T", "prefix", "v"))
        self.assertIn("split-window -v -l 10", adapted)
        self.assertEqual(self.tmux.run("show-options", "-gv", "status-style"), "bg=green")
        self.assertEqual(self.tmux.run("list-panes", "-s", "-t", session, "-F", "#{pane_id}"), before_panes)
        master, _, _ = self.attach(session)
        os.write(master, b"\x02v")
        eventually(lambda: len(self.tmux.panes(self.tmux.pane(pane).window)) == 3)
        created = self.tmux.pane(session + ":")
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", created.id, "#{pane_height}"), "10")

    def test_reenable_adds_missing_sidebars_without_touching_existing_ones(self):
        session, pane = self.native_session()
        self.tmux.enable(pane)
        original = next(item.id for item in self.tmux.panes(self.tmux.pane(pane).window) if item.sidebar)
        created = self.tmux.run("new-window", "-t", session + ":", "-P", "-F", "#{pane_id}")
        result = self.tmux.enable(created)
        self.assertEqual(result.sidebars_added, 1)
        self.assertTrue(self.tmux.pane(original).sidebar)
        self.assertTrue(self.tmux.pane(created).active)

    def test_reenable_does_not_recreate_intentionally_unbound_keys(self):
        _, pane = self.native_session()
        self.tmux.run("bind-key", "v", "split-window", "-h")
        self.tmux.enable(pane)
        self.tmux.run("unbind-key", "v")
        self.tmux.enable(pane)
        with self.assertRaises(WorkmuxError):
            self.tmux.run("list-keys", "-T", "prefix", "v")

    def test_root_and_custom_tables_keep_keys_repeat_flags_and_notes(self):
        _, pane = self.native_session()
        note = "café price $5"
        self.tmux.run("bind-key", "-r", "-N", note, "-T", "root", "M-v", "split-window", "-h")
        self.tmux.run("bind-key", "-r", "-N", note, "-T", "custom-table", "v", "new-window")
        self.tmux.enable(pane)
        self.tmux.enable(pane)
        for table, key in (("root", "M-v"), ("custom-table", "v")):
            binding = self.tmux.run("list-keys", "-T", table, key)
            self.assertIn("dispatch", binding)
            self.assertIn("-r", binding)
            self.assertIn(note, self.tmux.run("list-keys", "-N", "-T", table, key))

    def test_multiline_binding_note_does_not_change_another_keys_note(self):
        _, pane = self.native_session()
        note = "first line\nc second line"
        self.tmux.run("bind-key", "-N", note, "v", "split-window", "-h")
        original_c = self.tmux.run("list-keys", "-N", "-T", "prefix", "c")
        self.tmux.enable(pane)
        self.tmux.enable(pane)
        self.assertIn(note, self.tmux.run("list-keys", "-N", "-T", "prefix", "v"))
        self.assertEqual(self.tmux.run("list-keys", "-N", "-T", "prefix", "c"), original_c)

    def test_unquoted_whitespace_table_serialization_is_left_unchanged_with_warning(self):
        _, pane = self.native_session()
        self.tmux.run("bind-key", "-T", "custom table", "v", "new-window")
        before = self.tmux.run("list-keys", "-T", "custom table", "v")
        result = self.tmux.enable(pane)
        self.assertEqual(self.tmux.run("list-keys", "-T", "custom table", "v"), before)
        self.assertTrue(any("Unrecognized" in warning for warning in result.warnings))

    def test_unsupported_bindings_remain_unchanged_after_reload_and_reenable(self):
        _, pane = self.native_session()
        self.tmux.run("bind-key", "v", "split-window", "-h")
        self.tmux.enable(pane)
        self.tmux.run("bind-key", "v", "split-window", "-f", "-h")
        native = self.tmux.run("list-keys", "-T", "prefix", "v")
        for _ in range(2):
            result = self.tmux.enable(pane)
            self.assertEqual(self.tmux.run("list-keys", "-T", "prefix", "v"), native)
            self.assertTrue(any("prefix/v" in warning for warning in result.warnings))

    def test_enable_preserves_zoom_and_window_focus(self):
        session, pane = self.native_session()
        self.tmux.run("split-window", "-h", "-t", pane)
        self.tmux.run("select-pane", "-t", pane)
        self.tmux.run("resize-pane", "-Z", "-t", pane)
        self.tmux.enable(pane)
        self.assertTrue(self.tmux.pane(pane).zoomed)
        self.assertTrue(self.tmux.pane(pane).active)
        self.assertEqual(self.tmux.pane(session + ":").id, pane)
        self.tmux.enable(pane)
        self.assertTrue(self.tmux.pane(pane).zoomed)

    def test_partial_sidebar_failure_rolls_back_without_killing_content(self):
        session, pane = self.native_session()
        self.tmux.run("new-window", "-t", session + ":")
        original_panes = self.tmux.run("list-panes", "-s", "-t", session, "-F", "#{pane_id}")
        original_bindings = self.tmux.run("list-keys")
        real_add = self.tmux.add_sidebar
        calls = 0

        def fail_second(target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise WorkmuxError("injected sidebar failure")
            return real_add(target)

        with patch.object(self.tmux, "add_sidebar", side_effect=fail_second):
            with self.assertRaisesRegex(WorkmuxError, "injected sidebar"):
                self.tmux.enable(pane)
        self.assertEqual(self.tmux.run("list-panes", "-s", "-t", session, "-F", "#{pane_id}"), original_panes)
        self.assertEqual(self.tmux.run("list-keys"), original_bindings)
        self.assertEqual(self.tmux.run("show-options", "-qv", "-t", session, "@workmux-enabled"), "")
        self.assertEqual(self.tmux.enable(pane).sidebars_added, 2)

    def test_binding_state_write_failure_restores_original_bindings(self):
        session, pane = self.native_session()
        original_bindings = self.tmux.run("list-keys")
        real_run = self.tmux.run

        def fail_state_write(*args, **kwargs):
            if args[:3] == ("set-option", "-s", "@workmux-binding-state"):
                raise WorkmuxError("injected state write failure")
            return real_run(*args, **kwargs)

        with patch.object(self.tmux, "run", side_effect=fail_state_write):
            with self.assertRaisesRegex(WorkmuxError, "injected state write"):
                self.tmux.enable(pane)
        self.assertEqual(self.tmux.run("list-keys"), original_bindings)
        self.assertEqual(self.tmux.run("show-options", "-sqv", "@workmux-binding-state"), "")
        self.assertEqual(len(self.tmux.panes(self.tmux.pane(pane).window)), 1)
        self.assertEqual(self.tmux.run("show-options", "-qv", "-t", session, "@workmux-enabled"), "")

    def test_workspace_creation_in_existing_server_does_not_apply_isolated_defaults(self):
        original, _ = self.native_session()
        self.tmux.run("set-option", "-g", "prefix", "C-a")
        self.tmux.run("set-option", "-g", "status-style", "bg=blue")
        created = self.tmux.new_workspace("new", self.temporary.name)
        self.assertEqual(self.tmux.run("show-options", "-gv", "prefix"), "C-a")
        self.assertEqual(self.tmux.run("show-options", "-gv", "status-style"), "bg=blue")
        self.assertEqual(self.tmux.run("show-options", "-qv", "-t", created, "@workmux-enabled"), "1")
        self.assertEqual(self.tmux.run("show-options", "-qv", "-t", original, "@workmux-enabled"), "")
        self.assertEqual(self.tmux.run("show-options", "-sqv", "@workmux-configured"), "")

    def test_new_workspaces_on_integrated_server_do_not_reset_user_defaults(self):
        _, pane = self.native_session()
        self.tmux.run("set-option", "-g", "prefix", "C-a")
        self.tmux.enable(pane)
        binding = self.tmux.run("list-keys", "-T", "prefix", "c")
        created = self.tmux.new_workspace("new", self.temporary.name)
        self.assertEqual(self.tmux.run("show-options", "-gv", "prefix"), "C-a")
        self.assertEqual(self.tmux.run("list-keys", "-T", "prefix", "c"), binding)
        self.assertEqual(self.tmux.run("show-options", "-qv", "-t", created, "@workmux-enabled"), "1")

    def test_shared_windows_are_rejected_before_modification(self):
        session, pane = self.native_session("first")
        other, _ = self.native_session("second")
        self.tmux.run("link-window", "-s", self.tmux.pane(pane).window, "-t", other + ":")
        before = self.tmux.run("list-keys")
        with self.assertRaisesRegex(WorkmuxError, "shares windows"):
            self.tmux.enable(pane)
        self.assertEqual(self.tmux.run("list-keys"), before)
        self.assertEqual(self.tmux.run("show-options", "-qv", "-t", session, "@workmux"), "")

    def test_corrupt_binding_state_is_reported_without_modification(self):
        _, pane = self.native_session()
        self.tmux.run("set-option", "-s", "@workmux-binding-state", "not-json")
        before = self.tmux.run("list-keys")
        with self.assertRaisesRegex(WorkmuxError, "Invalid workmux binding state"):
            self.tmux.enable(pane)
        self.assertEqual(self.tmux.run("list-keys"), before)

    def test_native_new_window_preserves_name_directory_command_and_detached_flag(self):
        session, pane = self.native_session()
        self.tmux.run("bind-key", "v", "new-window", "-d", "-n", "user tab", "-c", self.temporary.name,
                      "sleep 999")
        self.tmux.enable(pane)
        master, _, _ = self.attach(session)
        os.write(master, b"\x02v")
        eventually(lambda: len(self.tmux.run("list-windows", "-t", session, "-F", "#{window_id}").splitlines()) == 2)
        window = self.tmux.run("list-windows", "-t", session, "-f", "#{==:#{window_name},user tab}",
                               "-F", "#{window_id}")
        eventually(lambda: len(self.tmux.panes(window)) == 2)
        content = next(item for item in self.tmux.panes(window) if not item.sidebar)
        self.assertEqual(content.directory, self.temporary.name)
        self.assertEqual(self.tmux.run("display-message", "-p", "-t", content.id, "#{pane_current_command}"), "sleep")
        self.assertTrue(self.tmux.pane(pane).active)
        self.assertEqual(self.tmux.pane(session + ":").window, self.tmux.pane(pane).window)

    def assert_sidebar_layout(self, window, sidebar, pane_ids):
        rows = self.tmux.run(
            "list-panes", "-t", window, "-F",
            "#{pane_id} #{pane_left} #{pane_top} #{pane_width} #{pane_height} #{pane_in_mode}",
        ).splitlines()
        geometry = {fields[0]: list(map(int, fields[1:])) for fields in map(str.split, rows)}
        self.assertCountEqual(geometry, pane_ids)
        height = int(self.tmux.run("display-message", "-p", "-t", window, "#{window_height}"))
        self.assertEqual(geometry[sidebar], [0, 0, 24, height, 0])
        for pane_id, (x, y, width, pane_height, mode) in geometry.items():
            if pane_id != sidebar:
                self.assertGreaterEqual(x, 25)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(y + pane_height, height)
                self.assertGreaterEqual(width, 2)
                self.assertEqual(mode, 0)
        return geometry

    def test_all_layouts_preserve_sidebar_panes_focus_and_main_identity(self):
        _, content, sidebar = self.workspace()
        right = self.tmux.split(content.id, "right")
        bottom = self.tmux.split(right, "down")
        ids = [content.id, sidebar.id, right, bottom]
        for preset in LAYOUTS:
            with self.subTest(preset=preset):
                self.tmux.retile(content.id, preset)
                geometry = self.assert_sidebar_layout(content.window, sidebar.id, ids)
                self.assertTrue(self.tmux.pane(bottom).active)
                if preset == "even-horizontal":
                    self.assertEqual({geometry[item][1] for item in (content.id, right, bottom)}, {0})
                elif preset == "even-vertical":
                    self.assertEqual({geometry[item][0] for item in (content.id, right, bottom)}, {25})
                elif preset.startswith("main-horizontal"):
                    self.assertEqual(geometry[content.id][2:4], [95, 24])
                    self.assertEqual(geometry[content.id][1], 16 if preset.endswith("mirrored") else 0)
                elif preset.startswith("main-vertical"):
                    self.assertEqual(geometry[content.id][2:4], [80, 40])
                    self.assertEqual(geometry[content.id][0], 40 if preset.endswith("mirrored") else 25)

    def test_layouts_repair_damage_from_native_presets_and_keep_sidebar_focus(self):
        _, content, sidebar = self.workspace()
        right = self.tmux.split(content.id, "right")
        self.tmux.run("select-layout", "-t", content.window, "even-vertical")
        self.tmux.run("swap-pane", "-d", "-s", sidebar.id, "-t", right)
        self.tmux.run("select-pane", "-t", sidebar.id)
        self.tmux.retile(sidebar.id, "tiled")
        self.assert_sidebar_layout(content.window, sidebar.id, [content.id, sidebar.id, right])
        self.assertTrue(self.tmux.pane(sidebar.id).active)

    def test_layouts_unzoom_and_preserve_content_focus(self):
        _, content, sidebar = self.workspace()
        right = self.tmux.split(content.id, "right")
        self.tmux.run("resize-pane", "-Z", "-t", right)
        self.tmux.retile(right, "even-vertical")
        self.assertFalse(self.tmux.pane(right).zoomed)
        self.assertTrue(self.tmux.pane(right).active)
        self.assert_sidebar_layout(content.window, sidebar.id, [content.id, sidebar.id, right])

    def test_layout_order_tracks_created_and_closed_content_panes(self):
        _, content, sidebar = self.workspace()
        right = self.tmux.split(content.id, "right")
        self.tmux.retile(content.id, "main-vertical-mirrored")
        bottom = self.tmux.split(content.id, "down")
        self.tmux.run("kill-pane", "-t", right)
        self.tmux.retile(content.id, "even-horizontal")
        self.assert_sidebar_layout(content.window, sidebar.id, [content.id, sidebar.id, bottom])
        order = self.tmux.run("show-options", "-wqv", "-t", content.window, "@workspace-pane-order")
        self.assertEqual(order.split(), [content.id, bottom])

    def test_impossible_layout_keeps_previous_geometry(self):
        _, content, _ = self.workspace()
        self.tmux.split(content.id, "right")
        self.tmux.split(content.id, "right")
        self.tmux.run("resize-window", "-t", content.window, "-y", "6")
        before = self.tmux.run("display-message", "-p", "-t", content.window, "#{window_layout}")
        with self.assertRaisesRegex(WorkmuxError, "Too little space"):
            self.tmux.retile(content.id, "even-vertical")
        after = self.tmux.run("display-message", "-p", "-t", content.window, "#{window_layout}")
        self.assertEqual(before, after)

    def test_one_content_pane_supports_every_layout(self):
        _, content, sidebar = self.workspace()
        for preset in LAYOUTS:
            self.tmux.retile(content.id, preset)
            self.assert_sidebar_layout(content.window, sidebar.id, [content.id, sidebar.id])

    def test_bindings_follow_entrypoint_updates_without_reconfiguration(self):
        session, content, _ = self.workspace()
        master, _, _ = self.attach(session)
        self.tmux.run("set-option", "-g", "repeat-time", "0")
        directory = Path(self.temporary.name) / "new installation"
        directory.mkdir()
        launcher = directory / "workmux"
        log = Path(self.temporary.name) / "entrypoint-calls"
        # Forward to this test's package without depending on a global installation.
        bootstrap = shlex.join(shlex.split(self.tmux.helper())[:-2])

        def install(version):
            launcher.write_text(
                "#!/bin/sh\n" + f"printf '%s\\n' {shlex.quote(version)} >> {shlex.quote(str(log))}\n"
                + f'exec {bootstrap} "$@"\n'
            )
            launcher.chmod(0o755)

        self.tmux.run("set-environment", "-g", "PATH", str(directory) + os.pathsep + os.environ["PATH"])
        original_binding = self.tmux.run("list-keys", "-T", "prefix", "%")
        self.assertIn('workmux --socket', original_binding)
        install("first")
        os.write(master, b"\x02%")
        eventually(lambda: len(self.tmux.panes(content.window)) == 3)
        install("second")
        os.write(master, b'\x02"')
        eventually(lambda: len(self.tmux.panes(content.window)) == 4)
        self.assertEqual(log.read_text().splitlines(), ["first", "second"])
        self.assertEqual(self.tmux.run("list-keys", "-T", "prefix", "%"), original_binding)

    def test_enabling_preserves_unrelated_bindings_and_options(self):
        self.workspace()
        self.tmux.run("bind-key", "c", "display-message", "custom-tab")
        self.tmux.run("set-option", "-g", "status-position", "bottom")
        self.tmux.run("bind-key", "M-1", "select-layout", "even-horizontal")
        pane = self.tmux.pane(self.tmux.workspaces()[0].id + ":")
        self.assertEqual(main(["--socket", self.socket, "enable", "--pane", pane.id]), 0)
        self.assertIn("custom-tab", self.tmux.run("list-keys", "-T", "prefix", "c"))
        self.assertEqual(self.tmux.run("show-options", "-gv", "status-position"), "bottom")
        self.assertIn("workmux", self.tmux.run("list-keys", "-T", "prefix", "M-1"))
        self.assertIn("dispatch", self.tmux.run("list-keys", "-T", "prefix", "M-1"))

    def test_real_layout_shortcuts_retile_only_content(self):
        session, content, sidebar = self.workspace()
        right = self.tmux.split(content.id, "right")
        bottom = self.tmux.split(right, "down")
        master, _, _ = self.attach(session)
        self.tmux.run("set-option", "-g", "repeat-time", "0")
        presets = ("even-horizontal", "even-vertical", "main-horizontal", "main-vertical", "tiled",
                   "main-horizontal-mirrored", "main-vertical-mirrored")
        for number, preset in enumerate(presets, 1):
            os.write(master, b"\x02\x1b" + str(number).encode())
            eventually(lambda: self.tmux.run(
                "show-options", "-wqv", "-t", content.window, "@workspace-layout",
            ) == preset)
            self.assert_sidebar_layout(content.window, sidebar.id, [content.id, sidebar.id, right, bottom])
        os.write(master, b"\x02 ")
        eventually(lambda: self.tmux.run(
            "show-options", "-wqv", "-t", content.window, "@workspace-layout",
        ) == "tiled")
        os.write(master, b"\x02\x1b ")
        eventually(lambda: self.tmux.run(
            "show-options", "-wqv", "-t", content.window, "@workspace-layout",
        ) == "main-vertical-mirrored")
        self.assert_sidebar_layout(content.window, sidebar.id, [content.id, sidebar.id, right, bottom])

    def test_creation_uses_stable_ids_and_one_live_sidebar_per_tab(self):
        session, content, sidebar = self.workspace("My Project")
        workspace = self.tmux.workspaces()[0]
        self.assertEqual(workspace.id, session)
        self.assertEqual(workspace.name, "000001-My Project")
        self.assertEqual(workspace.label, "My Project")
        self.assertEqual(sidebar.width, 24)
        self.assertEqual(self.tmux.run("show-options", "-gv", "status-position"), "top")
        eventually(lambda: "WORKSPACES" in self.tmux.run("capture-pane", "-p", "-t", sidebar.id))
        tab_content = self.tmux.new_tab(content.id, "editor")
        panes = self.tmux.panes(self.tmux.pane(tab_content).window)
        self.assertEqual(sum(pane.sidebar for pane in panes), 1)
        self.assertEqual(self.tmux.pane(session + ":").id, tab_content)
        for pane in panes:
            if not pane.sidebar:
                self.assertEqual(pane.directory, self.temporary.name)

    def test_splitting_from_sidebar_preserves_full_height_and_restores_content(self):
        _, content, sidebar = self.workspace()
        self.tmux.toggle_sidebar(content.id)
        self.assertTrue(self.tmux.pane(sidebar.id).active)
        right = self.tmux.split(sidebar.id, "right")
        self.tmux.split(right, "down")
        panes = self.tmux.panes(content.window)
        self.assertEqual(len(panes), 4)
        self.assertEqual(sum(pane.sidebar for pane in panes), 1)
        self.assertEqual(self.tmux.pane(sidebar.id).width, 24)
        sidebar_height = self.tmux.run("display-message", "-p", "-t", sidebar.id, "#{pane_height}")
        window_height = self.tmux.run("display-message", "-p", "-t", sidebar.id, "#{window_height}")
        self.assertEqual(sidebar_height, window_height)
        active = self.tmux.pane(content.window)
        self.tmux.toggle_sidebar(active.id)
        self.tmux.toggle_sidebar(sidebar.id)
        self.assertTrue(self.tmux.pane(active.id).active)

    def test_validation_does_not_create_extra_sessions(self):
        self.workspace()
        for label, directory in (("project-a", self.temporary.name), ("bad:name", self.temporary.name),
                                 ("new", self.temporary.name + "/missing")):
            with self.assertRaises(WorkmuxError):
                self.tmux.new_workspace(label, directory)
        self.assertEqual(len(self.tmux.workspaces()), 1)

    def test_manually_renamed_session_does_not_break_workspace_creation(self):
        first, _, _ = self.workspace()
        self.tmux.run("rename-session", "-t", first, "manual-name")
        second, _, _ = self.workspace("next")
        self.assertEqual(len(self.tmux.workspaces()), 2)
        self.assertNotEqual(first, second)

    def test_directory_shell_metacharacters_are_literal(self):
        directory = Path(self.temporary.name) / "source ' ; $(echo nope)"
        directory.mkdir()
        session = self.tmux.new_workspace("safe", str(directory))
        self.assertEqual(self.tmux.pane(session + ":").directory, str(directory))
        self.assertEqual(self.tmux.workspaces()[0].directory, str(directory))

    def test_native_next_previous_follow_numbered_name_order_after_rename(self):
        first, _, _ = self.workspace("first")
        second, _, _ = self.workspace("second")
        third, _, _ = self.workspace("third")
        _, _, client = self.attach(first)
        # Demonstrate the proposed future reordering mechanism, without adding UI.
        self.tmux.run("rename-session", "-t", third, "000000-third")
        self.tmux.run("switch-client", "-c", client, "-n")
        self.assertEqual(self.tmux.run("display-message", "-p", "-c", client, "#{session_id}"), second)
        self.tmux.run("switch-client", "-c", client, "-n")
        self.assertEqual(self.tmux.run("display-message", "-p", "-c", client, "#{session_id}"), third)
        self.tmux.run("switch-client", "-c", client, "-p")
        self.assertEqual(self.tmux.run("display-message", "-p", "-c", client, "#{session_id}"), second)

    def test_real_prefix_bindings_and_picker_switch_workspaces(self):
        first, content, sidebar = self.workspace("first")
        second, _, _ = self.workspace("second")
        master, _, client = self.attach(first)
        self.tmux.run("set-option", "-g", "repeat-time", "0")
        # Actual terminal input exercises generated run-shell commands and quoting.
        os.write(master, b"\x02c")
        eventually(lambda: len(self.tmux.run("list-windows", "-t", first, "-F", "#{window_id}").splitlines()) == 2)
        eventually(lambda: len(self.tmux.panes(self.tmux.pane(first + ":").window)) == 2)
        tab = self.tmux.pane(first + ":")
        self.assertNotEqual(tab.window, content.window)
        os.write(master, b"\x02%")
        eventually(lambda: len(self.tmux.panes(tab.window)) == 3)
        os.write(master, b'\x02"')
        eventually(lambda: len(self.tmux.panes(tab.window)) == 4)
        modes = self.tmux.run("list-panes", "-s", "-t", first, "-F", "#{pane_in_mode}")
        self.assertTrue(all(mode == "0" for mode in modes.splitlines()), modes)
        os.write(master, b"\x02s")
        eventually(lambda: self.tmux.pane(first + ":").sidebar)
        selected_sidebar = self.tmux.pane(first + ":")
        # Send navigation through curses, then verify the attached client's session.
        eventually(lambda: "WORKSPACES" in self.tmux.run("capture-pane", "-p", "-t", selected_sidebar.id))
        self.tmux.run("send-keys", "-t", selected_sidebar.id, "Down", "Enter")
        eventually(lambda: self.tmux.run("display-message", "-p", "-c", client, "#{session_id}") == second)
        self.assertFalse(self.tmux.pane(second + ":").sidebar)
        self.tmux.run("switch-client", "-c", client, "-t", first)
        self.assertEqual(self.tmux.pane(first + ":").window, tab.window)

    def test_resizing_restores_sidebar_width_and_zoom_does_not_fight_it(self):
        session, content, sidebar = self.workspace()
        master, client, _ = self.attach(session)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 80, 0, 0))
        os.kill(client.pid, signal.SIGWINCH)
        eventually(lambda: self.tmux.pane(sidebar.id).window_width == 80)
        eventually(lambda: self.tmux.pane(sidebar.id).width == 24)
        self.tmux.run("resize-pane", "-Z", "-t", content.id)
        self.tmux.repair_width(sidebar.id)
        self.assertTrue(self.tmux.pane(sidebar.id).zoomed)
        self.tmux.run("resize-pane", "-Z", "-t", content.id)
        eventually(lambda: self.tmux.pane(sidebar.id).width == 24)

    def test_mouse_click_selects_workspace_from_content_focus(self):
        first, _, sidebar = self.workspace("first")
        second, _, _ = self.workspace("second")
        master, _, client = self.attach(first)
        eventually(lambda: "WORKSPACES" in self.tmux.run("capture-pane", "-p", "-t", sidebar.id))
        # SGR left-click at sidebar item two: status row + heading + blank + two items.
        os.write(master, b"\x1b[<0;3;5M\x1b[<0;3;5m")
        eventually(lambda: self.tmux.run("display-message", "-p", "-c", client, "#{session_id}") == second)
        self.assertFalse(self.tmux.pane(second + ":").sidebar)

    def test_workspace_and_tab_navigation_bindings_work_from_terminal(self):
        first, original, _ = self.workspace("first")
        second, _, _ = self.workspace("second")
        tab = self.tmux.new_tab(original.id, "editor")
        master, _, client = self.attach(first)
        self.tmux.run("set-option", "-g", "repeat-time", "0")
        os.write(master, b"\x02\x1b[D")
        eventually(lambda: self.tmux.pane(first + ":").window == original.window)
        os.write(master, b"\x02\x1b[C")
        eventually(lambda: self.tmux.pane(first + ":").id == tab)
        os.write(master, b"\x02\x1b[B")
        eventually(lambda: self.tmux.run("display-message", "-p", "-c", client, "#{session_id}") == second)
        os.write(master, b"\x02\x1b[A")
        eventually(lambda: self.tmux.run("display-message", "-p", "-c", client, "#{session_id}") == first)
        self.assertEqual(self.tmux.pane(first + ":").id, tab)

    def test_detach_reattach_keeps_workspace_tabs_and_panes(self):
        session, content, _ = self.workspace()
        tab = self.tmux.new_tab(content.id, "editor")
        self.tmux.split(tab, "right")
        _, _, client = self.attach(session)
        windows = self.tmux.run("list-windows", "-t", session, "-F", "#{window_id}")
        panes = self.tmux.run("list-panes", "-s", "-t", session, "-F", "#{pane_id}")
        self.tmux.run("detach-client", "-t", client)
        eventually(lambda: not self.tmux.run("list-clients"))
        self.attach(session)
        self.assertEqual(self.tmux.run("list-windows", "-t", session, "-F", "#{window_id}"), windows)
        self.assertEqual(self.tmux.run("list-panes", "-s", "-t", session, "-F", "#{pane_id}"), panes)
        self.assertEqual(self.tmux.pane(session + ":").window, self.tmux.pane(tab).window)

    def test_selection_without_attached_client_has_clear_error(self):
        session, _, sidebar = self.workspace()
        with self.assertRaisesRegex(WorkmuxError, "exactly one attached client"):
            self.tmux.select_workspace(session, sidebar.id)

    def test_empty_server_is_normal(self):
        self.assertEqual(self.tmux.workspaces(), [])
