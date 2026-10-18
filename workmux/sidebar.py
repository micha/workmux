"""Small, replicated workspace picker. tmux owns the actual terminals."""

import curses

from .core import Tmux, WorkmuxError, sidebar_width


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
    screen.addnstr(row, x, text, width - 1, attribute)


def run_sidebar(tmux: Tmux, pane_id: str) -> None:
    def loop(screen):
        screen.keypad(True)
        screen.timeout(400)
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # Cursor visibility is optional on some terminals.
        curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)
        selected_id = None
        offset = 0
        message = ""
        while True:
            try:
                pane = tmux.pane(pane_id)
                empty = tmux.run("display-message", "-p", "-t", pane_id, "#{window_panes}") == "1"
                tmux.repair_width(pane_id)
                workspaces = tmux.workspaces()
            except WorkmuxError as error:
                screen.erase()
                draw_text(screen, 0, "WORKSPACES", curses.A_BOLD)
                draw_text(screen, 2, str(error))
                screen.refresh()
                screen.getch()
                continue

            ids = [workspace.id for workspace in workspaces]
            if selected_id not in ids or not pane.active:
                selected_id = pane.session if pane.session in ids else (ids[0] if ids else None)
            index = ids.index(selected_id) if selected_id in ids else 0
            height, width = screen.getmaxyx()
            picker_width = sidebar_width(width) if empty else width
            rows = max(1, height - 5)
            offset = visible_offset(index, offset, rows)
            screen.erase()
            draw_text(screen, 0, "WORKSPACES", curses.A_BOLD, max_width=picker_width)
            if empty:
                draw_text(screen, 2, "No terminals in this tab", curses.A_BOLD, x=picker_width + 2)
                draw_text(screen, 4, "Use your new-tab binding", x=picker_width + 2)
                draw_text(screen, 5, "or workmux tab new.", x=picker_width + 2)
            for position, workspace in enumerate(workspaces[offset:offset + rows]):
                marker = ">" if workspace.id == pane.session else " "
                attribute = curses.A_REVERSE if pane.active and workspace.id == selected_id else 0
                if workspace.id == pane.session:
                    attribute |= curses.A_BOLD
                draw_text(screen, position + 2, f"{marker} {workspace.label}", attribute, max_width=picker_width)
            footer = "Enter: select" if empty else "Enter: select  q: back"
            draw_text(screen, height - 2, message or footer, curses.A_DIM, max_width=picker_width)
            screen.refresh()
            key = screen.getch()
            destination = None
            if key in (curses.KEY_UP, ord("k")) and ids:
                selected_id = ids[(index - 1) % len(ids)]
            elif key in (curses.KEY_DOWN, ord("j")) and ids:
                selected_id = ids[(index + 1) % len(ids)]
            elif key in (10, 13, curses.KEY_ENTER):
                destination = selected_id
            elif key == curses.KEY_MOUSE:
                try:
                    _, x, y, _, buttons = curses.getmouse()
                except curses.error:
                    continue  # A mouse event may disappear during a resize.
                clicked = y - 2 + offset
                if 0 <= x < picker_width and 2 <= y < 2 + rows and 0 <= clicked < len(ids) and buttons & (
                    curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED
                ):
                    destination = ids[clicked]
                    selected_id = destination
            try:
                if destination:
                    tmux.select_workspace(destination, pane_id)
                    message = ""
                elif key in (27, ord("q")) and pane.active:
                    tmux.toggle_sidebar(pane_id)
            except WorkmuxError as error:
                message = str(error)

    curses.wrapper(loop)
