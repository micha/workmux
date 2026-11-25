"""Sidebar scrolling without a terminal, including full-loop refresh behavior."""

from contextlib import ExitStack
import curses
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from workmux.core import ActivitySnapshot, Pane, Tab, Workspace
from workmux.git_status import GitStatus
from workmux.sidebar import (SidebarRow, _heart_pulse, _italic_attribute, draw_row, draw_text,
                             mouse_wheel_delta, run_sidebar, scroll_offset)

ITALIC = 1 << 31


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


class TreeRenderingTests(unittest.TestCase):
    def test_heart_beats_twice_each_second(self):
        for now, expected in ((0, (True, 0.7)), (0.699, (True, 0.7)),
                              (0.7, (False, 0.8)), (0.799, (False, 0.8)),
                              (0.8, (True, 0.9)), (0.899, (True, 0.9)),
                              (0.9, (False, 1)), (0.999, (False, 1)),
                              (1, (True, 1.7)), (1.7, (False, 1.8))):
            with self.subTest(now=now):
                self.assertEqual(_heart_pulse(now), expected)

    def test_branches_are_dim_without_changing_label_attributes(self):
        for branch in ("├", "└"):
            for attribute in (0, curses.A_BOLD, curses.A_REVERSE, curses.A_DIM):
                for window, directory in (("@1", None), (None, "/jobs/repo")):
                    with self.subTest(branch=branch, attribute=attribute, directory=directory):
                        row = SidebarRow("$0", window, f"{branch} label", directory=directory)
                        screen = Mock()
                        screen.getmaxyx.return_value = (20, 80)
                        draw_row(screen, 2, row, attribute, {})
                        self.assertEqual([call.args for call in screen.addstr.call_args_list], [
                            (2, 0, f"  {row.text}", attribute),
                            (2, 2, branch, attribute | curses.A_DIM),
                        ])

    def test_previous_marker_is_undimmed_in_the_spacer_beside_git_status(self):
        screen = Mock()
        screen.getmaxyx.return_value = (20, 80)
        row = SidebarRow("$1", None, "2:repo", git_status=GitStatus(("dirty", "ahead")))
        draw_row(screen, 2, row, curses.A_DIM | curses.A_REVERSE, {"git-dirty": 256},
                 workspace_marker="-")
        self.assertEqual([call.args for call in screen.addstr.call_args_list], [
            (2, 0, "↑-2:repo", curses.A_DIM | curses.A_REVERSE),
            (2, 1, "-", curses.A_REVERSE),
            (2, 0, "↑", curses.A_REVERSE | 256),
        ])

    def test_long_labels_end_with_ellipsis_on_every_row_type(self):
        git = GitStatus(("clean",), "feature")
        for row, expected in (
            (SidebarRow("$1", None, "1:workspace-long", git_status=git), "  1:workspace-…"),
            (SidebarRow("$1", None, "├ ⎇ checkout-long", directory="/job/checkout-long",
                        git_status=git), "  ├ ⎇ checkout…"),
            (SidebarRow("$1", "@2", "├ ✦ 2:agent-long", "working"), "♥ ├ ✦ 2:agent-…"),
            (SidebarRow("$1", "@3", "└ 3:long-alert", "alert"), "▲ └ 3:long-ale…"),
        ):
            with self.subTest(label=row.text):
                screen = Mock()
                screen.getmaxyx.return_value = (20, 16)
                draw_row(screen, 2, row, curses.A_REVERSE, {}, branch_italic=ITALIC)
                self.assertEqual(screen.addstr.call_args_list[0].args, (2, 0, expected, curses.A_REVERSE))
                screen.getyx.assert_not_called()  # A truncated name takes priority over the Git suffix.

    def test_text_ellipsis_counts_columns_and_keeps_combining_marks(self):
        for text, width, expected in (
            ("界e\u0301x", 5, "界e\u0301x"),
            ("e\u0301界abc", 5, "e\u0301界…"),
            ("ab界cd", 5, "ab…"),
            ("e\u0301a\u0301b\u0301c\u0301", 5, "e\u0301a\u0301b\u0301c\u0301"),
            ("Enter: select  q: back", 16, "Enter: select …"),
            ("Error:\nlong message", 12, "Error: lon…"),
        ):
            with self.subTest(text=text):
                screen = Mock()
                screen.getmaxyx.return_value = (20, width)
                draw_text(screen, 2, text)
                screen.addstr.assert_called_once_with(2, 0, expected, 0)

    def test_italics_require_both_curses_and_terminfo_support(self):
        with patch("workmux.sidebar.curses", Mock(spec=["termattrs"])):
            self.assertEqual(_italic_attribute(), 0)
        with patch("workmux.sidebar.curses.A_ITALIC", ITALIC, create=True), \
                patch("workmux.sidebar.curses.termattrs") as attrs:
            attrs.return_value = curses.A_DIM
            self.assertEqual(_italic_attribute(), 0)
            attrs.return_value |= ITALIC
            self.assertEqual(_italic_attribute(), ITALIC)

    def test_branch_suffix_is_subdued_and_fits_after_name_in_terminal_columns(self):
        for row, end, width, plain, styled in (
            (SidebarRow("$1", None, "2:界", git_status=GitStatus(("clean",), "main")),
             (2, 6), 14, " (main)", " main"),
            (SidebarRow("$1", "@2", "└ ⎇ api", directory="/job/api",
                        git_status=GitStatus(("dirty",), "feat/login")),
             (2, 9), 18, " (feat…)", " feat/l…"),
            (SidebarRow("$1", None, "2:repo", git_status=GitStatus(("clean",), "界e\u0301-long")),
             (2, 8), 16, " (界e\u0301…)", " 界e\u0301-l…"),
            (SidebarRow("$1", None, "2:repo", git_status=GitStatus(("clean",), "feat\x1b")),
             (2, 8), 36, " (feat )", " feat "),
            (SidebarRow("$1", None, "2:repo", git_status=GitStatus(("clean",), "fix/npe")),
             (2, 8), 13, None, " fi…"),
        ):
            for italic, suffix in ((0, plain), (ITALIC, styled)):
                with self.subTest(label=row.text, width=width, italic=bool(italic)):
                    screen = Mock()
                    screen.getmaxyx.return_value = (20, width)
                    screen.getyx.return_value = end
                    attribute = curses.A_BOLD | curses.A_REVERSE
                    draw_row(screen, 2, row, attribute, {}, branch_italic=italic)
                    indicator = "~" if row.directory else " "
                    self.assertEqual(screen.addstr.call_args_list[0].args,
                                     (2, 0, f"{indicator} {row.text}", attribute))
                    suffix_calls = [call.args for call in screen.addstr.call_args_list[1:]
                                    if call.args[1] == end[1]]
                    self.assertEqual(suffix_calls, [] if suffix is None else [
                        (2, end[1], suffix, curses.A_DIM | curses.A_REVERSE | italic),
                    ])

    def test_workspace_label_is_not_treated_as_a_branch(self):
        with patch("workmux.sidebar.draw_text") as draw:
            draw_row(Mock(), 2, SidebarRow("$0", None, "└ workspace"), 0, {})
        draw.assert_called_once()


class _StopLoop(Exception):
    pass


class SidebarLoopTests(unittest.TestCase):
    def run_loop(self, actions, mouse_events=(), active=True, tabs=(), checkouts=None, windows=None,
                 step=1.0, previous_sessions=None, markers=None):
        """Advance a fake input clock; callable actions can resize or change tmux state."""
        tmux = Mock()
        tmux.repair_width.return_value = Pane("%0", "$0", "@0", True, active,
                                             36, 120, False, "/tmp")
        tmux.pane.side_effect = lambda _: tmux.repair_width.return_value
        tmux.workspaces.return_value = [Workspace(f"${index}", f"repo-{index}", "", "/jobs")
                                        for index in range(20)]
        tmux.sidebar_snapshot.side_effect = lambda: SimpleNamespace(
            workspaces=tmux.workspaces.return_value, activity=ActivitySnapshot(list(tabs), None, {}),
            checkout_windows=windows or {}, checkouts=checkouts or {}, git_statuses={},
            previous_sessions=dict(previous_sessions or {}),
        )
        screen = Mock()
        screen.getmaxyx.return_value = (10, 36)  # Six tree rows.
        frames = []
        drawn = []
        screen.erase.side_effect = drawn.clear
        screen.refresh.side_effect = lambda: frames.append(list(drawn))
        actions = iter(actions)
        now = 0.0

        def getch():
            nonlocal now
            try:
                action = next(actions)
            except StopIteration:
                raise _StopLoop
            now += step
            return action(screen, tmux) if callable(action) else action

        screen.getch.side_effect = getch

        def draw(screen, y, row, attr, colors, *, workspace_marker, working_resting, branch_italic):
            drawn.append((row.key, attr))
            if markers is not None and workspace_marker != ' ':
                markers.append((len(frames), row.key, workspace_marker))

        with ExitStack() as stack:
            stack.enter_context(patch("workmux.sidebar.curses.wrapper", side_effect=lambda loop: loop(screen)))
            stack.enter_context(patch("workmux.sidebar.curses.curs_set"))
            mousemask = stack.enter_context(patch("workmux.sidebar.curses.mousemask"))
            stack.enter_context(patch("workmux.sidebar.curses.getmouse", side_effect=mouse_events))
            stack.enter_context(patch("workmux.sidebar.curses.BUTTON4_PRESSED", 64, create=True))
            stack.enter_context(patch("workmux.sidebar.curses.BUTTON5_PRESSED", 128, create=True))
            stack.enter_context(patch("workmux.sidebar.status_attributes", return_value={}))
            stack.enter_context(patch("workmux.sidebar._italic_attribute", return_value=0))
            stack.enter_context(patch("workmux.sidebar.time.monotonic", side_effect=lambda: now))
            stack.enter_context(patch("workmux.sidebar.draw_row", side_effect=draw))
            state = stack.enter_context(patch("workmux.sidebar.SidebarState")).return_value
            state.snapshot.side_effect = tmux.sidebar_snapshot
            with self.assertRaises(_StopLoop):
                run_sidebar(tmux, "%0")
            state.close.assert_called_once()
            self.assertTrue(mousemask.call_args.args[0] & 64)
            self.assertTrue(mousemask.call_args.args[0] & 128)
        return frames, tmux

    def test_other_workspaces_and_children_are_dim_even_when_selected(self):
        tabs = [Tab('$0', '@0', 0, 'local', 'working'),
                Tab('$1', '@1', 1, 'remote', 'attention')]
        frames, _ = self.run_loop([curses.KEY_DOWN], tabs=tabs,
                                  checkouts={'$1': ('/jobs/repo',)})
        for key, attribute in frames[-1]:
            self.assertEqual(bool(attribute & curses.A_DIM), key[0] != '$0')
            if key[0] != '$0':
                self.assertFalse(attribute & curses.A_BOLD)
        attributes = dict(frames[-1])
        self.assertTrue(attributes[('$0', None)] & curses.A_BOLD)
        selected = attributes[('$1', None)]
        self.assertTrue(selected & curses.A_REVERSE)
        self.assertTrue(selected & curses.A_DIM)

    def test_workspace_markers_follow_history_without_changing_label_attributes(self):
        previous_sessions = {'$0': '$1'}
        tabs = [Tab('$1', '@1', 1, 'agent', 'attention', '%9')]
        markers = []

        def change_history(screen, tmux):
            previous_sessions['$0'] = '$2'
            return -1

        frames, _ = self.run_loop([change_history], active=False, tabs=tabs, markers=markers,
                                  checkouts={'$1': ('/jobs/repo',)}, previous_sessions=previous_sessions)
        self.assertEqual(markers, [(0, ('$0', None), '›'), (0, ('$1', None), '-'),
                                   (1, ('$0', None), '›'), (1, ('$2', None), '-')])
        before, after = frames
        self.assertEqual(dict(before)[('$0', None)], curses.A_BOLD)
        self.assertEqual(dict(before)[('$1', None)], curses.A_DIM)
        self.assertEqual(before, after)

    def test_wheel_survives_refreshes_without_focus_or_activation(self):
        for active in (True, False):
            with self.subTest(active=active):
                frames, tmux = self.run_loop([curses.KEY_MOUSE, -1, -1],
                                             [(0, 2, 2, 0, 128)], active=active)
                self.assertEqual([frame[0][0] for frame in frames], [("$0", None), ("$3", None)])
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

    def test_keyboard_checkout_selection_uses_path_and_parent_session(self):
        _, tmux = self.run_loop([curses.KEY_DOWN, curses.KEY_ENTER],
                                checkouts={'$0': ('/jobs/repos/api',)})
        tmux.select_checkout.assert_called_once_with('$0', '/jobs/repos/api', '%0')
        tmux.select_workspace.assert_not_called()
        tmux.select_tab.assert_not_called()

    def test_click_checkout_and_sibling_agent_target_distinct_stable_identities(self):
        path = '/jobs/repos/api'
        tabs = [Tab('$0', '@7', 1, 'agent', 'working', roots=(path,))]
        _, tmux = self.run_loop([curses.KEY_MOUSE, curses.KEY_MOUSE],
                                [(0, 2, 2, 0, curses.BUTTON1_PRESSED),
                                 (0, 2, 3, 0, curses.BUTTON1_PRESSED)],
                                tabs=tabs, checkouts={'$0': (path,)})
        tmux.select_checkout.assert_called_once_with('$0', path, '%0')
        tmux.select_tab.assert_called_once_with('$0', '@7', '%0')

    def test_idle_checkout_tab_focus_maps_back_to_checkout_row(self):
        def change_focus(screen, tmux):
            tmux.repair_width.return_value = Pane('%0', '$0', '@7', True, False,
                                                 36, 120, False, '/jobs')
            return -1
        frames, _ = self.run_loop([change_focus], active=False,
                                  checkouts={'$0': ('/jobs/repos/api',)},
                                  windows={('$0', '/jobs/repos/api'): '@7'})
        self.assertEqual(frames[-1][1][0], ('$0', 'checkout:/jobs/repos/api'))
        self.assertTrue(frames[-1][1][1] & curses.A_BOLD)

    def test_scrolling_both_directions_and_clamping(self):
        frames, _ = self.run_loop([curses.KEY_MOUSE] * 9,
                                  [(0, 2, 2, 0, button) for button in
                                   [128, 128, 128, 128, 128, 128, 64, 64, 64]])
        self.assertEqual([int(frame[0][0][0][1:]) for frame in frames],
                         [0, 3, 6, 9, 12, 14, 11, 8, 5])

    def test_key_repeat_does_not_fetch_focus_or_global_state_per_key(self):
        frames, tmux = self.run_loop([ord('j')] * 50, step=0.001)
        tmux.repair_width.assert_called_once_with('%0')
        tmux.sidebar_snapshot.assert_called_once_with()
        tmux.pane.assert_not_called()
        self.assertTrue(dict(frames[-1])[('$10', None)] & curses.A_REVERSE)

    def test_q_checks_current_focus_even_before_scheduled_refresh(self):
        def gained_focus(screen, tmux):
            tmux.pane.side_effect = None
            tmux.pane.return_value = Pane('%0', '$0', '@0', True, True, 36, 120, False, '/tmp')
            return ord('q')

        _, tmux = self.run_loop([gained_focus], active=False, step=0.001)
        tmux.toggle_sidebar.assert_called_once_with('%0')
        tmux.sidebar_snapshot.assert_called_once_with()

    def test_idle_redraws_only_when_a_working_row_is_visible(self):
        keys = [-1] * 32
        step = 0.0625
        frames, _ = self.run_loop(keys, step=step)
        self.assertEqual(len(frames), 1)
        visible = Tab('$0', '@0', 0, 'running', 'working')
        frames, _ = self.run_loop(keys, tabs=[visible], step=step)
        self.assertEqual(len(frames), 9)  # Initial draw plus four brightness changes per cycle.
        offscreen = Tab('$19', '@19', 0, 'running', 'working')
        frames, _ = self.run_loop(keys, tabs=[offscreen], step=step)
        self.assertEqual(len(frames), 1)

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
