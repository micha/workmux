"""Small, replicated workspace picker. tmux owns the actual terminals."""

import curses
from dataclasses import dataclass
from math import ceil, floor
from pathlib import Path
import re
import time

from .core import Tab, Tmux, WorkmuxError, Workspace
from .git_status import GitStatus
from .sidebar_state import SidebarState
from .status import WindowIndicators


AGENT_ROW_STATES = ("working", "attention", "finished")
SNAPSHOT_INTERVAL = 0.4
HEART_PERIOD_SECONDS = 1.0
HEART_TRANSITIONS = ((0.7, True), (0.8, False), (0.9, True))  # End offset, resting.
WHEEL_SCROLL_ROWS = 3
SIDEBAR_INDICATORS = {"working": "♥", "attention": "▲", "finished": "▲", "alert": "▲"}
GIT_DIVERGENCE = {"ahead": "↑", "behind": "↓", "diverged": "↕"}
AMBER_256 = 214
STATUS_FOREGROUNDS = {
    "working": curses.COLOR_RED,
    "attention": curses.COLOR_RED,
    "finished": curses.COLOR_GREEN,
    "alert": curses.COLOR_YELLOW,
    "git-clean": curses.COLOR_BLUE,
    "git-dirty": curses.COLOR_YELLOW,  # Fallback for terminals without 256 colors.
    "git-conflict": curses.COLOR_RED,
}


@dataclass(frozen=True)
class SidebarRow:
    session: str
    window: str | None
    text: str
    state: str | None = None
    git_states: GitStatus = ()
    directory: str | None = None
    pane: str | None = None

    @property
    def key(self) -> tuple[str, str | None]:
        return self.session, ("checkout:" + self.directory if self.directory is not None
                              else self.pane or self.window)


def _label(text: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)


def _tab_label(tab: Tab) -> str:
    number = str(tab.index)
    if tab.pane is not None and tab.pane_index is not None:
        number += f".{tab.pane_index}"
    icon = "✦ " if tab.state in AGENT_ROW_STATES else ""
    return f"{icon}{number}:{_label(tab.title or tab.name)}"


def _git_indicator(states: GitStatus) -> tuple[str, str]:
    health = "conflict" if "conflict" in states else "dirty" if "dirty" in states else "clean"
    symbol = next((GIT_DIVERGENCE[state] for state in states if state in GIT_DIVERGENCE), "")
    return symbol or ("~" if health != "clean" else ""), f"git-{health}"


def sidebar_rows(workspaces: list[Workspace], tabs: list[Tab],
                 git_states: dict[str, GitStatus | None] | None = None,
                 checkouts: dict[str, tuple[str, ...]] | None = None,
                 checkout_windows: dict[tuple[str, str], str] | None = None) -> list[SidebarRow]:
    """Stable-ID tree of workspaces, discovered checkouts, and active/alert tabs."""
    git_states, checkouts, checkout_windows = git_states or {}, checkouts or {}, checkout_windows or {}
    children = {}
    for tab in tabs:
        if tab.state in SIDEBAR_INDICATORS:
            children.setdefault(tab.session, []).append(tab)
    rows = []
    for position, workspace in enumerate(workspaces, 1):
        states = git_states.get(workspace.id) or ()
        rows.append(SidebarRow(workspace.id, None,
                               f"{position}:{_label(workspace.name)}", git_states=states))
        directories = tuple(sorted(set(checkouts.get(workspace.id, ()))))
        entries = [agent for tab in sorted(children.get(workspace.id, []), key=lambda tab: tab.index)
                   for agent in tab.agents or (tab,)]
        total = len(directories) + len(entries)
        for index, directory in enumerate(directories):
            last = index == total - 1
            branch = "└" if last else "├"
            name = Path(directory).name
            states = git_states.get(directory) or ()
            window = checkout_windows.get((workspace.id, directory))
            rows.append(SidebarRow(workspace.id, window,
                                   f"{branch} ⎇ {_label(name)}",
                                   git_states=states, directory=directory))
        for index, tab in enumerate(entries):
            branch = "└" if index == len(entries) - 1 else "├"
            text = f"{branch} {_tab_label(tab)}"
            rows.append(SidebarRow(workspace.id, tab.id, text, tab.state, pane=tab.pane))
    return rows


def visible_offset(index: int, offset: int, rows: int) -> int:
    """Keep the selected item visible in a scrolling list."""
    if index < offset:
        return index
    if index >= offset + rows:
        return max(0, index - rows + 1)
    return offset


def scroll_offset(offset: int, delta: int, total: int, rows: int) -> int:
    """Clamp the viewport, including after resize or removal of tree rows."""
    return min(max(0, offset + delta), max(0, total - rows))


def mouse_wheel_delta(buttons: int) -> int:
    """Older curses builds may not expose the wheel-down button."""
    if buttons & getattr(curses, "BUTTON4_PRESSED", 0):
        return -WHEEL_SCROLL_ROWS
    if buttons & getattr(curses, "BUTTON5_PRESSED", 0):
        return WHEEL_SCROLL_ROWS
    return 0


def status_attributes() -> dict[str, int]:
    """Initialize symbol colors, falling back to an uncolored sidebar."""
    try:
        if not curses.has_colors():
            return {}
        curses.start_color()
        curses.use_default_colors()
        attributes = {}
        for pair, (state, foreground) in enumerate(STATUS_FOREGROUNDS.items(), 1):
            if foreground == curses.COLOR_YELLOW and curses.COLORS >= 256:
                foreground = AMBER_256
            curses.init_pair(pair, foreground, -1)
            attributes[state] = curses.color_pair(pair)
        return attributes
    except curses.error:
        return {}


def draw_text(screen, row: int, text: str, attribute: int = 0, *, x: int = 0,
              max_width: int | None = None) -> None:
    height, width = screen.getmaxyx()
    width -= x
    if max_width is not None:
        width = min(width, max_width)
    if not 0 <= row < height or x < 0 or width < 2:
        return
    # Avoid writing the bottom-right cell, where curses may raise after drawing.
    try:
        screen.addnstr(row, x, text, width - 1, attribute)
    except curses.error:
        pass  # Wide window-name glyphs can hit the edge before the character limit.


def _heart_pulse(now: float) -> tuple[bool, float]:
    """Return whether the heart is between beats and when it next changes."""
    start = floor(now / HEART_PERIOD_SECONDS) * HEART_PERIOD_SECONDS
    for end, resting in HEART_TRANSITIONS:
        deadline = start + end
        if now < deadline:
            return resting, deadline
    return False, start + HEART_PERIOD_SECONDS


def draw_row(screen, position: int, row: SidebarRow, attribute: int,
             colors: dict[str, int], *, workspace_marker: str = " ", working_resting: bool = False) -> None:
    """Draw status and navigation marks independently of workspace dimming."""
    state = row.state or ""
    indicator = SIDEBAR_INDICATORS.get(state, "")
    color = state
    if not indicator:
        indicator, color = _git_indicator(row.git_states)
    draw_text(screen, position, f"{indicator or ' '}{workspace_marker}{row.text}", attribute)
    if workspace_marker != " " and attribute & curses.A_DIM:
        draw_text(screen, position, workspace_marker, attribute & ~curses.A_DIM, x=1, max_width=2)
    if (row.window is not None or row.directory is not None) and row.text.startswith(("├", "└")):
        draw_text(screen, position, row.text[0], attribute | curses.A_DIM, x=2, max_width=2)
    if indicator:
        gutter = (attribute & ~curses.A_DIM) | colors.get(color, 0)
        if state == "working":
            gutter &= ~curses.A_BOLD
            if working_resting:
                gutter |= curses.A_DIM
        draw_text(screen, position, indicator, gutter, max_width=2)


def run_sidebar(tmux: Tmux, pane_id: str) -> None:
    state = SidebarState(tmux)
    indicators = WindowIndicators(pane_id)

    def loop(screen):
        screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # Cursor visibility is optional on some terminals.
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED |
                         getattr(curses, "BUTTON4_PRESSED", 0) |
                         getattr(curses, "BUTTON5_PRESSED", 0))
        colors = status_attributes()
        selected_key = None
        offset = 0
        follow_selection = True
        message = ""
        pane = None
        snapshot = row_inputs = None
        rows = []
        keys = []
        indexes = {}
        displayed = None
        pane_error = snapshot_error = ""
        next_pane = next_snapshot = 0.0
        while True:
            now = time.monotonic()
            previous_pane = pane
            rows_changed = False
            if now >= next_pane:
                try:
                    pane = tmux.repair_width(pane_id)
                    if pane.sidebar and pane.window_panes == 1:
                        tmux.close_empty_window(pane)
                    pane_error = ""
                except WorkmuxError as error:
                    pane_error = str(error)
                next_pane = time.monotonic() + SNAPSHOT_INTERVAL
            if now >= next_snapshot:
                try:
                    updated = state.snapshot()
                    if updated is not snapshot:
                        snapshot = updated
                        indicators.update(snapshot.activity)
                        inputs = (snapshot.workspaces, snapshot.activity.tabs, snapshot.git_states,
                                  snapshot.checkouts, snapshot.checkout_windows)
                        if inputs != row_inputs:
                            row_inputs = inputs
                            new_rows = sidebar_rows(*inputs)
                            if new_rows != rows:
                                rows = new_rows
                                keys = [row.key for row in rows]
                                indexes = {key: index for index, key in enumerate(keys)}
                                rows_changed = True
                    snapshot_error = ""
                except WorkmuxError as error:
                    snapshot_error = str(error)
                next_snapshot = time.monotonic() + SNAPSHOT_INTERVAL
            now = time.monotonic()
            deadline = min(next_pane, next_snapshot)
            height, width = screen.getmaxyx()
            error = pane_error or snapshot_error
            if error or pane is None:
                view = (height, width, error)
                if view != displayed:
                    screen.erase()
                    draw_text(screen, 1, error)
                    screen.refresh()
                    displayed = view
                screen.timeout(max(1, ceil((deadline - now) * 1000)))
                if screen.getch() == curses.KEY_RESIZE:
                    displayed = None
                    next_pane = 0.0
                continue
            indicators.publish(tmux)

            if selected_key not in indexes or (not pane.active and (pane != previous_pane or rows_changed)):
                current = next((row.key for row in rows
                                if row.session == pane.session and row.window == pane.window
                                and row.directory is None), (pane.session, pane.window))
                checkout = next((row.key for row in rows
                                 if row.directory and row.session == pane.session and row.window == pane.window), None)
                parent = (pane.session, None)
                new_selection = (current if current in indexes else checkout if checkout in indexes else
                                 parent if parent in indexes else (keys[0] if keys else None))
                if new_selection != selected_key:
                    follow_selection = True
                selected_key = new_selection
            index = indexes.get(selected_key, 0)
            visible_rows = max(1, height - 4)
            offset = scroll_offset(offset, 0, len(rows), visible_rows)
            if follow_selection:
                offset = visible_offset(index, offset, visible_rows)
            visible = rows[offset:offset + visible_rows]
            working_resting = False
            if any(row.state == "working" for row in visible):
                working_resting, next_beat = _heart_pulse(now)
                deadline = min(deadline, next_beat)
            highlight = selected_key if pane.active else None
            previous_session = snapshot.previous_sessions.get(pane.session)
            view = (height, width, pane.session, pane.window, previous_session, highlight, message, visible,
                    working_resting)
            if view != displayed:
                screen.erase()
                for position, row in enumerate(visible):
                    workspace = row.window is None and row.directory is None
                    current = row.session == pane.session and (workspace or row.window == pane.window)
                    attribute = curses.A_REVERSE if row.key == highlight else 0
                    if row.session != pane.session:
                        attribute |= curses.A_DIM
                    elif current or row.state in ("attention", "alert"):
                        attribute |= curses.A_BOLD
                    marker = " "
                    if workspace:
                        marker = "›" if current else "-" if row.session == previous_session else " "
                    draw_row(screen, position + 1, row, attribute, colors,
                             workspace_marker=marker, working_resting=working_resting)
                draw_text(screen, height - 2, message or "Enter: select  q: back", curses.A_DIM)
                screen.refresh()
                displayed = view
            screen.timeout(max(1, ceil((deadline - time.monotonic()) * 1000)))
            key = screen.getch()
            destination = None
            if key in (curses.KEY_UP, ord("k")) and keys:
                selected_key = keys[(index - 1) % len(keys)]
                follow_selection = True
            elif key in (curses.KEY_DOWN, ord("j")) and keys:
                selected_key = keys[(index + 1) % len(keys)]
                follow_selection = True
            elif key == curses.KEY_RESIZE:
                displayed = None
                next_pane = 0.0
            elif key in (10, 13, curses.KEY_ENTER) and rows:
                destination = rows[index]
                follow_selection = True
            elif key == curses.KEY_MOUSE:
                try:
                    _, x, y, _, buttons = curses.getmouse()
                except curses.error:
                    continue  # A mouse event may disappear during a resize.
                delta = mouse_wheel_delta(buttons)
                clicked = y - 1 + offset
                if 0 <= x < width and 0 <= y < height and delta:
                    offset = scroll_offset(offset, delta, len(rows), visible_rows)
                    follow_selection = False
                elif 0 <= x < width and 1 <= y < 1 + visible_rows and 0 <= clicked < len(rows) and buttons & (
                    curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED
                ):
                    destination = rows[clicked]
                    selected_key = destination.key
                    follow_selection = True
            try:
                if destination:
                    if destination.directory is not None:
                        tmux.select_checkout(destination.session, destination.directory, pane_id)
                    elif destination.window is None:
                        tmux.select_workspace(destination.session, pane_id)
                    else:
                        if destination.pane:
                            tmux.select_tab(destination.session, destination.window, pane_id,
                                            destination_pane=destination.pane)
                        else:
                            tmux.select_tab(destination.session, destination.window, pane_id)
                    message = ""
                    next_pane = 0.0
                elif key in (27, ord("q")):
                    # Input may arrive immediately after focus enters the sidebar,
                    # before the scheduled local pane snapshot has observed it.
                    focused = tmux.pane(pane_id)
                    if focused.sidebar and focused.active:
                        tmux.toggle_sidebar(pane_id)
                        next_pane = 0.0
            except WorkmuxError as error:
                message = str(error)
                next_pane = 0.0

    try:
        curses.wrapper(loop)
    finally:
        state.close()
