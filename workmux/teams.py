"""Launch and label a three-pane Pi team in the caller's existing window."""

from contextlib import suppress
import json
import os
import re
import shlex
import shutil
import sys

from .core import Tmux, WorkmuxError, tmux_argument, validate_label
from .layouts import parse_layout, protect_sidebar, split_sizes


_OPTION = "@workmux-team"
_NAME_OPTION = "@workmux-team-name"
_ROLE_OPTION = "@workmux-team-role"
_ROLES = ("master", "slave 1", "slave 2")


def _window(tmux: Tmux, source: str) -> str:
    pane = tmux.pane(source)
    tmux.require_workspace(pane.session)
    return f"{pane.session}:{pane.window}"


def _metadata(tmux: Tmux, window: str) -> str:
    return tmux.run("show-options", "-wqv", "-t", window, _OPTION)


def _team(tmux: Tmux, window: str) -> tuple[str, list[str]]:
    raw = _metadata(tmux, window)
    if not raw:
        raise WorkmuxError("This window has no team.")
    try:
        data = json.loads(raw)
        name, ids = data["name"], data["panes"]
        validate_label(name)
        if (not isinstance(ids, list) or len(ids) != len(_ROLES)
                or not all(re.fullmatch(r"%[0-9]+", pane) for pane in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("invalid pane IDs")
    except (KeyError, TypeError, ValueError, WorkmuxError) as error:
        raise WorkmuxError("Invalid team metadata. Use workmux team clear to remove it.") from error
    return name, ids


def _set_formats(window: str, name: str, ids: list[str]) -> tuple[str, ...]:
    # Evaluate the role against the current pane ID *in this window*. A pane
    # moved out immediately stops matching, with no hooks or stale pane options.
    role_format = ""
    for role, pane in zip(_ROLES, ids):
        # A literal %3 (and higher IDs) can be expanded as a time format.
        same_pane = "#{==:#{s/^%//:pane_id}," + pane.removeprefix("%") + "}"
        role_format += "#{?" + same_pane + "," + role + ",}"
    return ("set-option", "-w", "-t", window, _NAME_OPTION, name, ";",
            "set-option", "-w", "-t", window, _ROLE_OPTION, role_format)


def _clear(tmux: Tmux, window: str) -> None:
    tmux.run("set-option", "-wu", "-t", window, _OPTION, ";",
             "set-option", "-wu", "-t", window, _NAME_OPTION, ";",
             "set-option", "-wu", "-t", window, _ROLE_OPTION)


def _require_foreground(tty: str) -> None:
    try:
        fd = sys.stdin.fileno()
        if (tty == os.ttyname(fd) == os.ttyname(sys.stdout.fileno())
                and os.tcgetpgrp(fd) == os.getpgrp()):
            return
    except (OSError, ValueError):
        pass
    raise WorkmuxError("Run team new directly in the foreground shell of the master pane.")


def new_team(tmux: Tmux, source: str | None, name: str) -> None:
    if not source:
        raise WorkmuxError("Run team new from a shell inside a workmux pane.")
    name = validate_label(name)
    window = _window(tmux, source)
    if _metadata(tmux, window):
        raise WorkmuxError("This window already has a team. Use workmux team status or team clear.")
    panes = tmux.panes(window)
    sidebars = [pane for pane in panes if pane.sidebar]
    content = [pane for pane in panes if not pane.sidebar]
    if len(content) != 1 or content[0].id != source:
        raise WorkmuxError("Team creation requires exactly one live content pane: your shell.")
    if len(sidebars) != 1:
        raise WorkmuxError("Team creation requires exactly one workmux sidebar.")
    tty, dead, raw_layout = tmux.run("display-message", "-p", "-t", source,
                                    "#{pane_tty}\t#{pane_dead}\t#{window_layout}").split("\t")
    if dead != "0":
        raise WorkmuxError("Team creation requires exactly one live content pane: your shell.")
    _require_foreground(tty)
    executable = shutil.which("pi")
    if executable is None:
        raise WorkmuxError("pi was not found on your PATH.")
    executable = os.path.abspath(executable)
    directory = tmux_argument(os.getcwd().replace("#", "##"))  # -c expands tmux formats.
    path = tmux_argument("PATH=" + os.environ.get("PATH", os.defpath))
    try:
        layout = protect_sidebar(parse_layout(raw_layout), sidebars[0].id)
        _, left, right = split_sizes(layout.children[1].width, 3)
    except ValueError as error:
        raise WorkmuxError(str(error)) from error

    created: list[str] = []
    saved = False

    def reserve(target: str, width: int) -> None:
        created.append(tmux.run("split-window", "-hd", "-l", str(width), "-c", directory,
                                "-t", target, "-P", "-F", "#{pane_id}", ""))

    try:
        # Reserve both panes before starting Pi. The first split leaves room for
        # both slaves and the border between them.
        reserve(source, left + right + 1)
        reserve(created[0], right)
        ids = [source, *created]
        data = json.dumps({"name": name, "panes": ids})
        saved = True  # Clear the options even if the batch only partially applies.
        tmux.run("set-option", "-w", "-t", window, _OPTION, data, ";", *_set_formats(window, name, ids))
        command = "exec " + shlex.quote(executable)
        tmux.run("respawn-pane", "-t", created[0], "-e", path, command, ";",
                 "respawn-pane", "-t", created[1], "-e", path, command, ";",
                 "select-pane", "-t", source)
        # Replace only this foreground job, not the pane's original shell.
        os.execv(executable, [executable])
    except (WorkmuxError, OSError, KeyboardInterrupt):
        if saved:
            with suppress(WorkmuxError, OSError):
                _clear(tmux, window)
        for pane in reversed(created):
            with suppress(WorkmuxError, OSError):
                tmux.run("kill-pane", "-t", pane)
        if content[0].zoomed:
            with suppress(WorkmuxError, OSError):
                tmux.run("if-shell", "-F", "-t", source, "#{!=:#{window_zoomed_flag},1}",
                         f"resize-pane -Z -t {source}")
        raise


def team_status(tmux: Tmux, source: str) -> str:
    window = _window(tmux, source)
    name, ids = _team(tmux, window)
    present = {pane.id for pane in tmux.panes(window) if not pane.sidebar}
    rows = [f"Team: {name}"]
    for role, pane in zip(_ROLES, ids):
        rows.append(f"{role}\t{pane}" + ("" if pane in present else " (missing)"))
    return "\n".join(rows)


def refresh_team(tmux: Tmux, source: str) -> None:
    window = _window(tmux, source)
    name, ids = _team(tmux, window)
    tmux.run(*_set_formats(window, name, ids))


def clear_team(tmux: Tmux, source: str) -> None:
    _clear(tmux, _window(tmux, source))
