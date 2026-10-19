"""Small, replicated workspace picker. tmux owns the actual terminals."""

import curses
from dataclasses import dataclass
import re

from .core import Tab, Tmux, WorkmuxError, Workspace


INDICATORS = {"working": "⚙", "attention": "⚠", "finished": "✓", "alert": "●"}


@dataclass(frozen=True)
class SidebarRow:
    session: str
    window: str | None
    text: str
    state: str | None = None

    @property
    def key(self) -> tuple[str, str | None]:
        return self.session, self.window


def _label(text: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)


def sidebar_rows(workspaces: list[Workspace], tabs: list[Tab]) -> list[SidebarRow]:
    """Build a stable-ID tree; ordinary idle tabs never appear as child rows."""
    children = {}
    for tab in tabs:
        if tab.state in INDICATORS:
            children.setdefault(tab.session, []).append(tab)
    rows = []
    for position, workspace in enumerate(workspaces, 1):
        rows.append(SidebarRow(workspace.id, None, f"{position}:{_label(workspace.name)}"))
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


def run_sidebar(tmux: Tmux, pane_id: str) -> None:
    def loop(screen):
        screen.keypad(True)
        screen.timeout(400)
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # Cursor visibility is optional on some terminals.
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)
        selected_key = None
        previous_pane = None
        offset = 0
        message = ""
        while True:
            try:
                pane = tmux.repair_width(pane_id, previous_pane)
                previous_pane = pane
                if pane.sidebar and tmux.run("display-message", "-p", "-t", pane_id, "#{window_panes}") == "1":
                    tmux.close_empty_window(pane)
                rows = sidebar_rows(tmux.workspaces(), tmux.attention_tabs())
            except WorkmuxError as error:
                screen.erase()
                draw_text(screen, 1, str(error))
                screen.refresh()
                screen.getch()
                continue

            keys = [row.key for row in rows]
            if selected_key not in keys or not pane.active:
                current = (pane.session, pane.window)
                parent = (pane.session, None)
                selected_key = current if current in keys else parent if parent in keys else (keys[0] if keys else None)
            index = keys.index(selected_key) if selected_key in keys else 0
            height, width = screen.getmaxyx()
            visible_rows = max(1, height - 4)
            offset = visible_offset(index, offset, visible_rows)
            screen.erase()
            for position, row in enumerate(rows[offset:offset + visible_rows]):
                current = row.session == pane.session and (row.window is None or row.window == pane.window)
                marker = "›" if current else " "
                attribute = curses.A_REVERSE if pane.active and row.key == selected_key else 0
                if current or row.state in ("attention", "alert"):
                    attribute |= curses.A_BOLD
                draw_text(screen, position + 1, f"{marker} {row.text}", attribute)
            draw_text(screen, height - 2, message or "Enter: select  q: back", curses.A_DIM)
            screen.refresh()
            key = screen.getch()
            destination = None
            if key in (curses.KEY_UP, ord("k")) and keys:
                selected_key = keys[(index - 1) % len(keys)]
            elif key in (curses.KEY_DOWN, ord("j")) and keys:
                selected_key = keys[(index + 1) % len(keys)]
            elif key in (10, 13, curses.KEY_ENTER) and rows:
                destination = rows[index]
            elif key == curses.KEY_MOUSE:
                try:
                    _, x, y, _, buttons = curses.getmouse()
                except curses.error:
                    continue  # A mouse event may disappear during a resize.
                clicked = y - 1 + offset
                if 0 <= x < width and 1 <= y < 1 + visible_rows and 0 <= clicked < len(rows) and buttons & (
                    curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED
                ):
                    destination = rows[clicked]
                    selected_key = destination.key
            try:
                if destination:
                    if destination.window is None:
                        tmux.select_workspace(destination.session, pane_id)
                    else:
                        tmux.select_tab(destination.session, destination.window, pane_id)
                    message = ""
                elif key in (27, ord("q")) and pane.active:
                    tmux.toggle_sidebar(pane_id)
            except WorkmuxError as error:
                message = str(error)

    curses.wrapper(loop)
