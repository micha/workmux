"""Sidebar scrolling without a terminal, including full-loop refresh behavior."""

from contextlib import ExitStack
import curses
import itertools
import unittest
from unittest.mock import Mock, patch

from workmux.core import Pane, Tab, Workspace
from workmux.sidebar import mouse_wheel_delta, run_sidebar, scroll_offset


class ScrollGeometryTests(unittest.TestCase):
    def test_scroll_clamps_in_both_directions(self):
        self.assertEqual(scroll_offset(0, 3, 20, 6), 3)
        self.assertEqual(scroll_offset(3, -3, 20, 6), 0)
        self.assertEqual(scroll_offset(0, -3, 20, 6), 0)
        self.assertEqual(scroll_offset(13, 3, 20, 6), 14)
        self.assertEqual(scroll_offset(14, 3, 20, 6), 14)

    def test_short_empty_resized_and_shrunken_lists(self):
        for total in (0, 1, 6):
            self.assertEqual(scroll_offset(4, 3, total, 6), 0)
        self.assertEqual(scroll_offset(14, 0, 20, 10), 10)
        self.assertEqual(scroll_offset(14, 0, 8, 6), 2)

    def test_wheel_direction_modifiers_and_other_mouse_events(self):
        with patch("workmux.sidebar.curses.BUTTON4_PRESSED", 64, create=True), \
                patch("workmux.sidebar.curses.BUTTON5_PRESSED", 128, create=True):
            self.assertEqual(mouse_wheel_delta(64), -3)
            self.assertEqual(mouse_wheel_delta(128), 3)
            self.assertEqual(mouse_wheel_delta(128 | curses.BUTTON_SHIFT), 3)
            self.assertEqual(mouse_wheel_delta(curses.BUTTON1_PRESSED), 0)

    def test_missing_button5_constant_is_supported(self):
        with patch("workmux.sidebar.curses.BUTTON5_PRESSED", 0, create=True):
            self.assertEqual(mouse_wheel_delta(0), 0)
            self.assertEqual(mouse_wheel_delta(curses.BUTTON4_PRESSED), -3)


class _StopLoop(Exception):
    pass


class SidebarLoopTests(unittest.TestCase):
    def run_loop(self, actions, mouse_events=(), active=True, tabs=()):
        """Each action runs after a rendered frame; callable actions can resize."""
        tmux = Mock()
        tmux.repair_width.return_value = Pane("%0", "$0", "@0", True, active,
                                             36, 120, False, "/tmp")
        tmux.run.return_value = "2"
        tmux.workspaces.return_value = [Workspace(f"${index}", f"repo-{index}", "", "")
                                        for index in range(20)]
        tmux.attention_tabs.return_value = list(tabs)
        screen = Mock()
        screen.getmaxyx.return_value = (10, 36)  # Six tree rows.
        frames = []
        drawn = []
        screen.erase.side_effect = drawn.clear
        screen.refresh.side_effect = lambda: frames.append(list(drawn))
        actions = iter(actions)

        def getch():
            try:
                action = next(actions)
            except StopIteration:
                raise _StopLoop
            return action(screen, tmux) if callable(action) else action

        screen.getch.side_effect = getch
        with ExitStack() as stack:
            stack.enter_context(patch("workmux.sidebar.curses.wrapper", side_effect=lambda loop: loop(screen)))
            stack.enter_context(patch("workmux.sidebar.curses.curs_set"))
            mousemask = stack.enter_context(patch("workmux.sidebar.curses.mousemask"))
            stack.enter_context(patch("workmux.sidebar.curses.getmouse", side_effect=mouse_events))
            stack.enter_context(patch("workmux.sidebar.curses.BUTTON4_PRESSED", 64, create=True))
            stack.enter_context(patch("workmux.sidebar.curses.BUTTON5_PRESSED", 128, create=True))
            stack.enter_context(patch("workmux.sidebar.status_attributes", return_value={}))
            stack.enter_context(patch("workmux.sidebar.time.monotonic", side_effect=itertools.count()))
            stack.enter_context(patch("workmux.sidebar.draw_row",
                                      side_effect=lambda screen, y, marker, row, attr, *args:
                                      drawn.append((row.key, attr))))
            cache = stack.enter_context(patch("workmux.sidebar.GitStatusCache"))
            cache.return_value.snapshot.return_value = {}
            with self.assertRaises(_StopLoop):
                run_sidebar(tmux, "%0")
            cache.return_value.close.assert_called_once()
            self.assertTrue(mousemask.call_args.args[0] & 64)
            self.assertTrue(mousemask.call_args.args[0] & 128)
        return frames, tmux

    def test_wheel_survives_refreshes_without_focus_or_activation(self):
        for active in (True, False):
            with self.subTest(active=active):
                frames, tmux = self.run_loop([curses.KEY_MOUSE, -1, -1],
                                             [(0, 2, 2, 0, 128)], active=active)
                self.assertEqual([frame[0][0] for frame in frames],
                                 [("$0", None), ("$3", None), ("$3", None), ("$3", None)])
                tmux.select_workspace.assert_not_called()
                tmux.select_tab.assert_not_called()
                tmux.toggle_sidebar.assert_not_called()

    def test_keyboard_resumes_following_unchanged_selection(self):
        frames, _ = self.run_loop([curses.KEY_MOUSE, curses.KEY_DOWN, curses.KEY_UP],
                                  [(0, 2, 2, 0, 128)])
        self.assertEqual([frame[0][0] for frame in frames],
                         [("$0", None), ("$3", None), ("$1", None), ("$0", None)])
        self.assertTrue(frames[2][0][1] & curses.A_REVERSE)

    def test_click_after_scroll_targets_stable_workspace_id(self):
        _, tmux = self.run_loop([curses.KEY_MOUSE, curses.KEY_MOUSE],
                                [(0, 2, 2, 0, 128), (0, 2, 2, 0, curses.BUTTON1_PRESSED)])
        tmux.select_workspace.assert_called_once_with("$4", "%0")

    def test_click_after_scroll_targets_stable_child_id(self):
        tabs = [Tab("$0", f"@{index}", index, "build", "working") for index in range(6)]
        _, tmux = self.run_loop([curses.KEY_MOUSE, curses.KEY_MOUSE],
                                [(0, 2, 2, 0, 128), (0, 2, 2, 0, curses.BUTTON1_PRESSED)],
                                tabs=tabs)
        tmux.select_tab.assert_called_once_with("$0", "@3", "%0")

    def test_scrolling_both_directions_and_clamping(self):
        frames, _ = self.run_loop([curses.KEY_MOUSE] * 9,
                                  [(0, 2, 2, 0, button) for button in
                                   [128, 128, 128, 128, 128, 128, 64, 64, 64]])
        self.assertEqual([int(frame[0][0][0][1:]) for frame in frames],
                         [0, 3, 6, 9, 12, 14, 14, 11, 8, 5])

    def test_resize_and_row_removal_clamp_without_selection_snapback(self):
        def resize(screen, tmux):
            screen.getmaxyx.return_value = (22, 36)  # Eighteen visible rows.
            return curses.KEY_RESIZE

        def remove(screen, tmux):
            tmux.workspaces.return_value = tmux.workspaces.return_value[:4]
            return -1

        frames, _ = self.run_loop([curses.KEY_MOUSE, curses.KEY_MOUSE, resize, remove],
                                  [(0, 2, 2, 0, 128), (0, 2, 2, 0, 128)])
        self.assertEqual([frame[0][0] for frame in frames],
                         [("$0", None), ("$3", None), ("$6", None), ("$2", None), ("$0", None)])

    def test_mouse_errors_and_coordinates_outside_pane_are_ignored(self):
        frames, tmux = self.run_loop([curses.KEY_MOUSE] * 3,
                                     [curses.error(), (0, -1, 2, 0, 128), (0, 2, 10, 0, 128)])
        self.assertTrue(all(frame[0][0] == ("$0", None) for frame in frames))
        tmux.select_workspace.assert_not_called()
