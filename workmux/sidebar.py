"""Small, replicated workspace picker. tmux owns the actual terminals."""

import curses
from dataclasses import dataclass
import re
import time
import unicodedata

from .core import Tab, Tmux, WorkmuxError, Workspace
from .git_status import GitStatusCache


BRAILLE_WHEEL = ("⣷", "⣯", "⣟", "⡿", "⢿", "⣻", "⣽", "⣾")
ANIMATION_INTERVAL = 0.125
SNAPSHOT_INTERVAL = 0.4
WHEEL_SCROLL_ROWS = 3
INDICATORS = {"working": BRAILLE_WHEEL[0], "attention": "⚠", "finished": "✓", "alert": "●"}
GIT_INDICATORS = {"clean": "✓", "dirty": "●", "ahead": "↑", "behind": "↓",
                  "diverged": "↕", "conflict": "!"}
STATUS_FOREGROUNDS = {
    "working": curses.COLOR_BLUE,
    "attention": curses.COLOR_RED,
    "finished": curses.COLOR_GREEN,
    "other": curses.COLOR_YELLOW,
    "git-clean": curses.COLOR_GREEN,
    "git-dirty": curses.COLOR_YELLOW,
    "git-ahead": curses.COLOR_CYAN,
    "git-behind": curses.COLOR_BLUE,
    "git-diverged": curses.COLOR_MAGENTA,
    "git-conflict": curses.COLOR_RED,
}


@dataclass(frozen=True)
class SidebarRow:
    session: str
    window: str | None
    text: str
    state: str | None = None
    git_state: str | None = None

    @property
    def key(self) -> tuple[str, str | None]:
        return self.session, self.window


def _label(text: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)


def sidebar_rows(workspaces: list[Workspace], tabs: list[Tab],
                 git_states: dict[str, str | None] | None = None) -> list[SidebarRow]:
    """Build a stable-ID tree; ordinary idle tabs never appear as child rows."""
    children = {}
    for tab in tabs:
        if tab.state in INDICATORS:
            children.setdefault(tab.session, []).append(tab)
    rows = []
    for position, workspace in enumerate(workspaces, 1):
        git_state = (git_states or {}).get(workspace.id)
        symbol = GIT_INDICATORS.get(git_state)
        suffix = f" {symbol}" if symbol else ""
        rows.append(SidebarRow(workspace.id, None, f"{position}:{_label(workspace.name)}{suffix}",
                               git_state=git_state))
        windows = sorted(children.get(workspace.id, []), key=lambda tab: tab.index)
        for index, tab in enumerate(windows):
            branch = "└─" if index == len(windows) - 1 else "├─"
            text = f"  {branch} {INDICATORS[tab.state]} {tab.index}:{_label(tab.name)}"
            rows.append(SidebarRow(workspace.id, tab.id, text, tab.state))
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


def working_indicator(now: float) -> str:
    """Select a time-based animation frame, independent of input and polling."""
    return BRAILLE_WHEEL[int(now / ANIMATION_INTERVAL) % len(BRAILLE_WHEEL)]


def draw_row(screen, position: int, marker: str, row: SidebarRow, attribute: int,
             colors: dict[str, int], working_frame: str | None = None) -> None:
    """Draw a row, applying color only to its possibly animated status symbol."""
    text = f"{marker} {row.text}"
    indicator = INDICATORS.get(row.state or "")
    symbol_x = text.find(indicator) if indicator else -1
    if row.state == "working" and symbol_x >= 0:
        indicator = working_frame or BRAILLE_WHEEL[0]
        text = text[:symbol_x] + indicator + text[symbol_x + 1:]
    draw_text(screen, position, text, attribute)
    if indicator and symbol_x >= 0:
        color = colors.get(row.state or "", colors.get("other", 0))
        if color:
            draw_text(screen, position, indicator, attribute | color, x=symbol_x, max_width=2)
    if row.git_state in GIT_INDICATORS:
        # The suffix is not found by searching: a session name may contain it too.
        indicator = GIT_INDICATORS[row.git_state]
        prefix = text[:-len(indicator)]
        symbol_x = sum(0 if unicodedata.combining(char) else
                       2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                       for char in prefix)
        color = colors.get(f"git-{row.git_state}", 0)
        if color:
            draw_text(screen, position, indicator, attribute | color, x=symbol_x, max_width=2)


def run_sidebar(tmux: Tmux, pane_id: str) -> None:
    git_status = GitStatusCache()

    def loop(screen):
        screen.keypad(True)
        screen.timeout(round(ANIMATION_INTERVAL * 1000))
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # Cursor visibility is optional on some terminals.
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED |
                         getattr(curses, "BUTTON4_PRESSED", 0) |
                         getattr(curses, "BUTTON5_PRESSED", 0))
        colors = status_attributes()
        selected_key = None
        previous_pane = None
        offset = 0
        follow_selection = True
        message = ""
        pane = None
        rows = []
        next_snapshot = 0.0
        while True:
            now = time.monotonic()
            if now >= next_snapshot:
                try:
                    pane = tmux.repair_width(pane_id, previous_pane)
                    previous_pane = pane
                    if pane.sidebar and tmux.run("display-message", "-p", "-t", pane_id, "#{window_panes}") == "1":
                        tmux.close_empty_window(pane)
                    workspaces = tmux.workspaces()
                    rows = sidebar_rows(workspaces, tmux.attention_tabs(), git_status.snapshot(workspaces))
                except WorkmuxError as error:
                    screen.erase()
                    draw_text(screen, 1, str(error))
                    screen.refresh()
                    screen.getch()
                    continue
                now = time.monotonic()
                next_snapshot = now + SNAPSHOT_INTERVAL
            if pane is None:
                continue

            keys = [row.key for row in rows]
            if selected_key not in keys or not pane.active:
                current = (pane.session, pane.window)
                parent = (pane.session, None)
                new_selection = current if current in keys else parent if parent in keys else (keys[0] if keys else None)
                if new_selection != selected_key:
                    follow_selection = True
                selected_key = new_selection
            index = keys.index(selected_key) if selected_key in keys else 0
            height, width = screen.getmaxyx()
            visible_rows = max(1, height - 4)
            offset = scroll_offset(offset, 0, len(rows), visible_rows)
            if follow_selection:
                offset = visible_offset(index, offset, visible_rows)
            screen.erase()
            for position, row in enumerate(rows[offset:offset + visible_rows]):
                current = row.session == pane.session and (row.window is None or row.window == pane.window)
                marker = "›" if current else " "
                attribute = curses.A_REVERSE if pane.active and row.key == selected_key else 0
                if current or row.state in ("attention", "alert"):
                    attribute |= curses.A_BOLD
                draw_row(screen, position + 1, marker, row, attribute, colors, working_indicator(now))
            draw_text(screen, height - 2, message or "Enter: select  q: back", curses.A_DIM)
            screen.refresh()
            key = screen.getch()
            if key != -1:
                # Refresh focus before the next queued key without polling on
                # animation-only timeouts.
                next_snapshot = 0.0
            destination = None
            if key in (curses.KEY_UP, ord("k")) and keys:
                selected_key = keys[(index - 1) % len(keys)]
                follow_selection = True
            elif key in (curses.KEY_DOWN, ord("j")) and keys:
                selected_key = keys[(index + 1) % len(keys)]
                follow_selection = True
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
                    if destination.window is None:
                        tmux.select_workspace(destination.session, pane_id)
                    else:
                        tmux.select_tab(destination.session, destination.window, pane_id)
                    message = ""
                    next_snapshot = 0.0
                elif key in (27, ord("q")) and pane.active:
                    tmux.toggle_sidebar(pane_id)
                    next_snapshot = 0.0
            except WorkmuxError as error:
                message = str(error)
                next_snapshot = 0.0

    try:
        curses.wrapper(loop)
    finally:
        git_status.close()
