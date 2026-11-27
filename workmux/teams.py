"""Launch and label a three-pane Pi team in the caller's existing window."""

import base64
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
import fcntl
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import stat
import sys
import time
from uuid import uuid4

from .core import TMUX_EXECUTABLE, Tmux, WorkmuxError, quote_tmux, tmux_argument, validate_label
from .layouts import parse_layout, protect_sidebar, split_sizes
from .team_worker import EXPECTED, LEADER, OWNER, REFUSED, REGISTRATION, SPAWN, START, process_stat


_OPTION = "@workmux-team"
_NAME_OPTION = "@workmux-team-name"
_ROLE_OPTION = "@workmux-team-role"
_ROLES = ("master", "slave 1", "slave 2")
_MANAGED = "@workmux_team_id"
_STATE = "@workmux_team_generation"
_LAUNCH_ID = "WORKMUX_TEAM_LAUNCH_ID"
_ROLE = "WORKMUX_TEAM_ROLE"
_ID = r"[0-9a-f]{32}"
_NUMBER = r"[0-9]+"
_PROCESS = rf"{_NUMBER}\.{_NUMBER}"
_START_TIMEOUT = 10.0
_STOP_TIMEOUT = 5.0
_POLL = 0.05


def _window(tmux: Tmux, source: str) -> str:
    pane = tmux.pane(source)
    tmux.require_workspace(pane.session)
    return pane.window


def _metadata(tmux: Tmux, window: str) -> str:
    return tmux.run("show-options", "-wqv", "-t", window, _OPTION)


@dataclass(frozen=True)
class _Managed:
    id: str
    launcher: str
    directory: str


def _team(tmux: Tmux, window: str) -> tuple[str, list[str], _Managed | None]:
    return _decode_team(_metadata(tmux, window))


def _decode_team(raw: str) -> tuple[str, list[str], _Managed | None]:
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
        managed = None
        if "managed" in data:
            managed = _Managed(**data["managed"])
            if (not re.fullmatch(_ID, managed.id) or not os.path.isabs(managed.launcher)
                    or not os.path.isabs(managed.directory)):
                raise ValueError("invalid managed team configuration")
    except (KeyError, TypeError, ValueError, WorkmuxError) as error:
        raise WorkmuxError("Invalid team metadata. Use workmux team clear to remove it.") from error
    return name, ids, managed


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


def _clear(tmux: Tmux, window: str, formats: tuple[str, str] | None) -> None:
    commands = ["set-option", "-wqu", "-t", window, _OPTION]
    if formats:
        for option, value in zip((_NAME_OPTION, _ROLE_OPTION), formats):
            if tmux.run("show-options", "-wqv", "-t", window, option) == value:
                commands.extend((";", "set-option", "-wqu", "-t", window, option))
    tmux.run(*commands)


def _require_foreground(tty: str) -> None:
    try:
        fd = sys.stdin.fileno()
        if (tty == os.ttyname(fd) == os.ttyname(sys.stdout.fileno())
                and os.tcgetpgrp(fd) == os.getpgrp()):
            return
    except (OSError, ValueError):
        pass
    raise WorkmuxError("Run team new directly in the foreground shell of the master pane.")


def new_team(tmux: Tmux, source: str | None, name: str, *, launcher: str | None = None,
             profile: str | None = None) -> None:
    if not source:
        raise WorkmuxError("Run team new from a shell inside a workmux pane.")
    if (launcher is None) != (profile is None):
        raise WorkmuxError("Use --launcher and --profile together.")
    name = validate_label(name)
    window = _window(tmux, source)
    if launcher is None:
        _new_team(tmux, window, source, name, None, None)
    else:
        _require_linux()
        resolved = shutil.which(launcher)
        if not resolved:
            raise WorkmuxError(f"Launcher is not executable: {launcher!r}")
        managed = _Managed(uuid4().hex, str(Path(resolved).resolve()), os.getcwd())
        _preflight_paths(managed, profile)
        with _lock(tmux, window):
            _new_team(tmux, window, source, name, managed, profile)


def _new_team(tmux: Tmux, window: str, source: str, name: str, managed: _Managed | None,
              profile: str | None) -> None:
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

    if managed:
        _new_managed(tmux, window, source, name, managed, profile, executable, left, right,
                     content[0].zoomed)
        return

    created: list[str] = []
    formats = None

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
        commands = _set_formats(window, name, ids)
        formats = (name, commands[-1])  # Clean up even if the batch only partially applies.
        tmux.run("set-option", "-w", "-t", window, _OPTION, data, ";", *commands)
        command = "exec " + shlex.quote(executable)
        tmux.run("respawn-pane", "-t", created[0], "-e", path, command, ";",
                 "respawn-pane", "-t", created[1], "-e", path, command, ";",
                 "select-pane", "-t", source)
        # Replace only this foreground job, not the pane's original shell.
        os.execv(executable, [executable])
    except (WorkmuxError, OSError, KeyboardInterrupt):
        if formats:
            with suppress(WorkmuxError, OSError):
                _clear(tmux, window, formats)
        for pane in reversed(created):
            with suppress(WorkmuxError, OSError):
                tmux.run("kill-pane", "-t", pane)
        if content[0].zoomed:
            with suppress(WorkmuxError, OSError):
                tmux.run("if-shell", "-F", "-t", source, "#{!=:#{window_zoomed_flag},1}",
                         f"resize-pane -Z -t {source}")
        raise


@dataclass(frozen=True)
class _Process:
    pid: int
    start: int

    def __str__(self) -> str:
        return f"{self.pid}.{self.start}"

    def alive(self) -> bool:
        identity = process_stat(self.pid)
        return identity is not None and identity[0] == self.start and identity[1] not in ("Z", "X")


@dataclass(frozen=True)
class _Generation:
    ready: bool
    launches: tuple[str, str]
    processes: tuple[_Process, ...] = ()  # Pi pair if ready; teardown obligations if pending.

    def __str__(self) -> str:
        return ":".join(("ready" if self.ready else "pending", *self.launches, *map(str, self.processes)))

    @classmethod
    def parse(cls, raw: str) -> "_Generation | None":
        if not raw:
            return None
        if not re.fullmatch(rf"(?:ready|pending):{_ID}:{_ID}(?::{_PROCESS})*", raw):
            raise WorkmuxError("Invalid managed team generation. Use workmux team clear.")
        kind, first, second, *processes = raw.split(":")
        if kind == "ready" and len(processes) != 2:
            raise WorkmuxError("Incomplete committed worker pair. Use workmux team clear.")
        return cls(kind == "ready", (first, second),
                   tuple(_Process(*map(int, value.split("."))) for value in processes))


@dataclass(frozen=True)
class _Pane:
    id: str
    sidebar: str
    dead: str
    pid: str
    exited: str
    exit_status: str
    exit_signal: str
    owner: str
    leader: str
    start: str
    expected: str
    registration: str
    remain: str
    command: str

    @property
    def launch(self) -> str:
        return self.owner.partition(".")[2]

    @property
    def process(self) -> _Process:
        return _Process(int(self.leader), int(self.start))


_FIELDS = ("pane_id", "@workspace-sidebar", "pane_dead", "pane_pid", "pane_dead_time", "pane_dead_status",
           "pane_dead_signal", OWNER, LEADER, START, EXPECTED, REGISTRATION, "remain-on-exit",
           "s/[\t\r\n]/ /:pane_start_command")


def _var(key: str) -> str:
    return "#{" + key + "}"


def _eq(key: str, value: str) -> str:
    return "#{==:" + _var(key) + "," + value + "}"


def _all(*conditions: str) -> str:
    result = conditions[-1]
    for condition in reversed(conditions[:-1]):
        result = "#{&&:" + condition + "," + result + "}"
    return result


def _command(*args: str) -> str:
    return " ".join(arg if arg == ";" else quote_tmux(arg) for arg in args)


def _set(target: str, option: str, value: str, *, window: bool = False) -> tuple[str, ...]:
    return "set-option", "-w" if window else "-p", "-t", target, option, value


def _checked(tmux: Tmux, target: str, guard: str, action: str) -> None:
    if tmux.run("if-shell", "-F", "-t", target, guard, action + " ; display-message -p accepted") != "accepted":
        raise WorkmuxError(f"Team target {target} changed during the operation; stopped without overwriting it.")


def _snapshot(tmux: Tmux, window: str, team: _Managed, *, ids: list[str] | None = None,
              read_generation: bool = True) -> tuple[_Generation | None, dict[str, _Pane]]:
    generation = _var(_STATE) if read_generation else ""
    assignments = _var(_OPTION) if ids is not None else ""
    fmt = "\t".join((_var(_MANAGED), generation, assignments, *map(_var, _FIELDS)))
    panes = {}
    state = None
    for line in tmux.run("list-panes", "-t", window, "-F", fmt).split("\n"):
        row = line.split("\t")
        if len(row) != len(_FIELDS) + 3 or not re.fullmatch(r"%[0-9]+", row[3]):
            raise WorkmuxError("Invalid managed pane metadata. Use workmux team clear.")
        owner, raw, assignments, *fields = row
        if owner != team.id:
            return None, {}
        if not panes:
            if ids is not None and (not assignments or _decode_team(assignments)[1:] != (ids, team)):
                return None, {}
            state = _Generation.parse(raw)
        pane = _Pane(*fields)
        panes[pane.id] = pane
    return state, panes


def _require_linux() -> None:
    message = "Managed teams require Linux with /proc and pidfd support."
    if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
        raise WorkmuxError(message)
    try:
        os.close(os.pidfd_open(os.getpid()))
        if process_stat(os.getpid()) is None:
            raise WorkmuxError(message)
    except OSError as error:
        raise WorkmuxError(message) from error


def _preflight_paths(team: _Managed, profile: str | None) -> None:
    if not profile or "\x00" in profile:
        raise WorkmuxError("A nonempty launcher profile is required.")
    if not os.path.isfile(team.launcher) or not os.access(team.launcher, os.X_OK):
        raise WorkmuxError(f"Saved launcher is not executable: {team.launcher!r}")
    if not os.path.isdir(team.directory) or not os.access(team.directory, os.X_OK):
        raise WorkmuxError(f"Saved team directory is not accessible: {team.directory!r}")


@contextmanager
def _lock(tmux: Tmux, window: str):
    # Fixed per-UID location so callers with different TMPDIR/XDG settings still
    # serialize. Window IDs are not reused in the same tmux server incarnation.
    pid = int(tmux.run("display-message", "-p", "-t", window, "#{pid}"))
    identity = process_stat(pid)
    if identity is None:
        raise WorkmuxError("The tmux server exited.")
    directory = Path("/tmp") / f"workmux-teams-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise WorkmuxError(f"Not a private team lock directory: {directory}")
    path = directory / f"{pid}-{identity[0]}-{window[1:]}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkmuxError(f"Not a regular team lock: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkmuxError("Another managed team operation is in progress.") from error
        yield
    finally:
        os.close(fd)


def _owner_error(pane: _Pane | None, team: _Managed, *, require_remain: bool = True) -> str | None:
    if pane is None or pane.sidebar not in ("", "0"):
        return "pane is missing, moved, or marked as a sidebar"
    if not re.fullmatch(rf"{team.id}\.{_ID}", pane.owner) or pane.owner not in pane.command:
        return "pane was not started by this team"
    if not re.fullmatch(_NUMBER, pane.leader) or not re.fullmatch(_NUMBER, pane.start):
        return "pane leader identity was not recorded"
    if pane.expected not in ("", pane.launch) or not re.fullmatch(rf"(?:{_ID}:{_NUMBER})?", pane.registration):
        return "pane registration state was changed"
    if (require_remain and pane.remain != "on") or not re.fullmatch(r"[a-z-]+", pane.remain):
        return "pane remain-on-exit changed"
    if pane.dead == "1":
        if (not re.fullmatch(_NUMBER, pane.exited) or not re.fullmatch(r"[0-9]*", pane.exit_status)
                or not re.fullmatch(r"[A-Z0-9]*", pane.exit_signal)):
            return "pane exit cannot be verified"
        if pane.exit_status == str(REFUSED):
            return "pane was respawned without Workmux ownership or launcher exited 125"
    elif pane.dead != "0" or pane.pid != pane.leader or not pane.process.alive():
        return "pane leader was replaced or is exiting"
    return None


def _marker(owner: str) -> str:
    return "#{m:*" + owner + "*,#{pane_start_command}}"


def _guard(window: str, team: _Managed, pane: _Pane) -> str:
    fields = [("@workspace-sidebar", pane.sidebar), (OWNER, pane.owner), (LEADER, pane.leader),
              (START, pane.start), (EXPECTED, pane.expected),
              (REGISTRATION, pane.registration), ("remain-on-exit", pane.remain), ("pane_dead", pane.dead)]
    if pane.dead == "1":
        fields.extend((("pane_dead_time", pane.exited), ("pane_dead_status", pane.exit_status),
                       ("pane_dead_signal", pane.exit_signal)))
    else:
        fields.append(("pane_pid", pane.leader))
    return _all(_eq("window_id", window), _eq(_MANAGED, team.id), _marker(pane.owner),
                *(_eq(key, value) for key, value in fields))


def _registered(pane: _Pane) -> _Process | None:
    launch, _, pid = pane.registration.partition(":")
    if launch != pane.launch:
        return None
    pid = int(pid)
    identity = process_stat(pid)
    if identity is None or identity[1] in ("Z", "X"):
        return None
    process = _Process(pid, identity[0])
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except (FileNotFoundError, ProcessLookupError):
        return None
    # Include the launch ID and pane when adopting initial registration. This
    # also distinguishes an unrelated process that reused a stale registered PID.
    if (os.fsencode(f"{_LAUNCH_ID}={launch}") in env and os.fsencode(f"TMUX_PANE={pane.id}") in env
            and process.alive()):
        return process
    return None


def _registrations(ids: list[str], team: _Managed, state: _Generation, panes: dict[str, _Pane]
                   ) -> tuple[tuple[_Process, ...] | None, list[str]]:
    reports = []
    diagnostics = []
    for role, pane_id, launch in zip(_ROLES[1:], ids[1:], state.launches):
        pane = panes.get(pane_id)
        error = _owner_error(pane, team)
        if pane and pane.dead == "1" and error is None:
            error = f"launcher exited{f' with status {pane.exit_status}' if pane.exit_status else ''}"
        if error is None and (pane.launch != launch or pane.expected != launch):
            error = "launch ID changed"
        process = None
        if error is None:
            try:
                process = _registered(pane)
            except (OSError, OverflowError):
                error = "registered Pi process identity is inaccessible"
        if process:
            reports.append(process)
        description = f"Pi registered (PID {process.pid})" if process else error or "no live Pi registration"
        diagnostics.append(f"{role} ({pane_id}): {description}")
    if ids[0] not in panes or panes[ids[0]].dead != "0" or panes[ids[0]].sidebar == "1":
        diagnostics.append(f"master ({ids[0]}): pane is missing or moved")
        return None, diagnostics
    return (tuple(reports) if len(reports) == 2 else None), diagnostics


def _write_state(tmux: Tmux, window: str, team: _Managed, previous: _Generation | None,
                 state: _Generation, panes: list[_Pane] | None = None) -> None:
    action = _command(*_set(window, _STATE, str(state), window=True), ";", "display-message", "-p", "accepted")
    for pane in reversed(panes or []):
        action = _command("if-shell", "-F", "-t", pane.id, _guard(window, team, pane), action)
    guard = _all(_eq("window_id", window), _eq(_MANAGED, team.id), _eq(_STATE, str(previous) if previous else ""))
    if tmux.run("if-shell", "-F", "-t", window, guard, action) != "accepted":
        raise WorkmuxError("Managed team changed before its generation could be updated.")


def _worker_args(window: str, owner: str, operation: str, *args: str) -> tuple[str, ...]:
    return (sys.executable, "-I", str(Path(__file__).resolve().with_name("team_worker.py")), owner,
            TMUX_EXECUTABLE, window, operation, *args)


def _stamp(pane: str, owner: str, expected: str) -> tuple[str, ...]:
    return (*_set(pane, "remain-on-exit", "on"), ";", *_set(pane, OWNER, owner), ";",
            "set-option", "-pF", "-t", pane, LEADER, "#{pane_pid}", ";", *_set(pane, START, ""), ";",
            *_set(pane, EXPECTED, expected), ";", *_set(pane, REGISTRATION, ""))


def _respawn(tmux: Tmux, window: str, team: _Managed, pane: _Pane, operation: str,
             token: str, role: str = "", profile: str = "") -> None:
    error = _owner_error(pane, team)
    if error:
        raise WorkmuxError(f"Worker {pane.id}: {error}")
    owner = f"{team.id}.{token}"
    args = ["respawn-pane", "-t", pane.id, "-e", f"{SPAWN}={owner}"]
    if pane.dead != "1":
        args.append("-k")
    expected = ""
    worker = ()
    if operation == "launch":
        expected = token
        args.extend(("-e", f"{_LAUNCH_ID}={token}", "-e", f"{_ROLE}={role}"))
        # The guarded tmux command list parses argv a second time. Keep paths,
        # profiles, and PATH out of that syntax altogether (even a literal ';').
        payload = json.dumps([team.launcher, profile, team.directory, os.environ.get("PATH", os.defpath)])
        worker = (base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii"),)
    args.extend((*_worker_args(window, owner, operation, *worker), ";", *_stamp(pane.id, owner, expected)))
    _checked(tmux, pane.id, _guard(window, team, pane), _command(*args))


def _wait_stopped(tmux: Tmux, window: str, team: _Managed, ids: list[str],
                  owners: dict[str, str], handles: list[int]) -> list[_Pane]:
    poll = select.poll()
    for fd in handles:
        poll.register(fd, select.POLLIN)
    deadline = time.monotonic() + _STOP_TIMEOUT
    while True:
        for fd, _ in poll.poll(0):
            poll.unregister(fd)
            handles.remove(fd)
            os.close(fd)
        _, panes = _snapshot(tmux, window, team, ids=ids)
        workers = [panes.get(pane) for pane in owners]
        for pane, (pane_id, owner) in zip(workers, owners.items()):
            if not pane or pane.owner != owner or owner not in pane.command:
                raise WorkmuxError(f"Worker {pane_id} changed during teardown; no new profiles were started.")
        if not handles and all(pane.dead == "1" for pane in workers):
            for role, pane in zip(_ROLES[1:], workers):
                error = _owner_error(pane, team)
                if error:
                    raise WorkmuxError(f"{role}: {error}")
            return workers
        if time.monotonic() >= deadline:
            raise WorkmuxError("Worker teardown did not complete; no new profiles were started.")
        time.sleep(_POLL)


def _close_registration(tmux: Tmux, window: str, team: _Managed, pane: _Pane) -> None:
    if pane.expected:
        _checked(tmux, pane.id, _guard(window, team, pane), _command(*_set(pane.id, EXPECTED, "")))


def _run_generation(tmux: Tmux, window: str, ids: list[str], team: _Managed, previous: _Generation | None,
                    panes: dict[str, _Pane], profile: str) -> str:
    workers = []
    old = set(previous.processes if previous else ())
    for role, pane_id in zip(_ROLES[1:], ids[1:]):
        pane = panes.get(pane_id)
        error = _owner_error(pane, team)
        if error:
            raise WorkmuxError(f"{role} ({pane_id}): {error}")
        workers.append(pane)
        old.add(pane.process)
        process = _registered(pane)
        if process:
            old.add(process)
    state = _Generation(False, (uuid4().hex, uuid4().hex), tuple(sorted(old, key=str)))
    handles = []
    try:
        for process in old:
            if not process.alive():
                continue
            try:
                fd = os.pidfd_open(process.pid)
            except ProcessLookupError:
                continue
            if process.alive():  # Reject a PID reused between stat and pidfd_open.
                handles.append(fd)
            else:
                os.close(fd)
        _write_state(tmux, window, team, previous, state, workers)
        # Close both slots before parking either pane. Keep the old process
        # identities in tmux so interruption can't forget a live old worker.
        for pane in workers:
            _close_registration(tmux, window, team, pane)
        owners = {}
        for pane in workers:
            owners[pane.id] = pane.owner
            if pane.dead == "0":
                token = uuid4().hex
                _respawn(tmux, window, team, replace(pane, expected=""), "park", token)
                owners[pane.id] = f"{team.id}.{token}"
        workers = _wait_stopped(tmux, window, team, ids, owners, handles)
        stopped = replace(state, processes=())
        _write_state(tmux, window, team, state, stopped, workers)
        state = stopped
        for role, pane, token in zip(_ROLES[1:], workers, state.launches):
            _respawn(tmux, window, team, pane, "launch", token, role, profile)
        deadline = time.monotonic() + _START_TIMEOUT
        while True:
            current, panes = _snapshot(tmux, window, team, ids=ids)
            if current != state:
                raise WorkmuxError("Managed team changed during startup.")
            pair, errors = _registrations(ids, team, state, panes)
            if pair:
                _write_state(tmux, window, team, state, replace(state, ready=True, processes=pair),
                             [panes[pane_id] for pane_id in ids[1:]])
                return json.dumps(dict(zip(_ROLES[1:], (process.pid for process in pair))))
            if (time.monotonic() >= deadline or any(pane not in panes or panes[pane].dead == "1" for pane in ids[1:])):
                raise WorkmuxError("Worker startup failed: " + "; ".join(errors))
            time.sleep(_POLL)
    except (WorkmuxError, OSError, KeyboardInterrupt):
        # A failed round leaves diagnostic terminal output/owned processes in
        # place. Close pending IDs, not registrations (needed to verify retry).
        for pane_id, token in zip(ids[1:], state.launches):
            with suppress(WorkmuxError, OSError):
                # Close this round's unique slot even if the runtime exited or
                # changed while the startup snapshot was in flight.
                guard = _all(_eq(OWNER, f"{team.id}.{token}"), _eq(EXPECTED, token))
                tmux.run("if-shell", "-F", "-t", pane_id, guard, _command(*_set(pane_id, EXPECTED, "")))
        raise
    finally:
        for fd in handles:
            os.close(fd)


def reset_team(tmux: Tmux, source: str, profile: str) -> str:
    _require_linux()
    window = _window(tmux, source)
    with _lock(tmux, window):
        _, ids, team = _team(tmux, window)
        if not team or source != ids[0]:
            raise WorkmuxError("Run team reset from the managed team's original master pane.")
        _preflight_paths(team, profile)
        state, panes = _snapshot(tmux, window, team, ids=ids)
        if state is None or source not in panes or panes[source].dead != "0":
            raise WorkmuxError("Managed team state or original master pane is missing.")
        return _run_generation(tmux, window, ids, team, state, panes, profile)


def _new_managed(tmux: Tmux, window: str, source: str, name: str, team: _Managed, profile: str,
                 executable: str, left: int, right: int, zoomed: bool) -> None:
    created = {}
    formats = None
    data = None
    try:
        target = source
        for width in (left + right + 1, right):
            owner = f"{team.id}.{uuid4().hex}"
            pane = tmux.run("split-window", "-hdP", "-l", str(width), "-c",
                            tmux_argument(team.directory.replace("#", "##")), "-t", target, "-F", "#{pane_id}",
                            "-e", f"{SPAWN}={owner}", *_worker_args(window, owner, "reserve"))
            created[pane] = owner
            _checked(tmux, pane, _all(_eq("window_id", window), _marker(owner), _eq("pane_dead", "0")),
                     _command(*_stamp(pane, owner, "")))
            target = pane
        ids = [source, *created]
        commands = _set_formats(window, name, ids)
        formats = (name, commands[-1])
        data = json.dumps({"name": name, "panes": ids, "managed": vars(team)})
        tmux.run(*_set(window, _OPTION, data, window=True), ";", *_set(window, _MANAGED, team.id, window=True),
                 ";", *commands)
        deadline = time.monotonic() + _STOP_TIMEOUT
        while True:
            _, panes = _snapshot(tmux, window, team, ids=ids)
            if all(pane in panes and panes[pane].start for pane in created):
                break
            if time.monotonic() >= deadline:
                raise WorkmuxError("Could not establish ownership of both reserved worker panes.")
            time.sleep(_POLL)
        tmux.run("select-pane", "-t", source)
        _run_generation(tmux, window, ids, team, None, panes, profile)
        # The lock FD is close-on-exec. Only this foreground job is replaced.
        os.execv(executable, [executable])
    except (WorkmuxError, OSError, KeyboardInterrupt):
        for pane, owner in reversed(created.items()):
            with suppress(WorkmuxError, OSError):
                if formats:
                    _, panes = _snapshot(tmux, window, team)
                    if pane in panes and not _owner_error(panes[pane], team):
                        _checked(tmux, pane, _guard(window, team, panes[pane]), f"kill-pane -t {pane}")
                        continue
                # A split can fail before even its reservation stamp is written.
                # A manual bare respawn repeats argv but not the spawn environment.
                current_window, pid = tmux.run("display-message", "-p", "-t", pane,
                                              "#{window_id}\t#{pane_pid}").split("\t")
                if current_window == window and re.fullmatch(_NUMBER, pid):
                    env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                    if os.fsencode(f"{SPAWN}={owner}") in env:
                        guard = _all(_eq("window_id", window), _marker(owner), _eq("pane_pid", pid))
                        _checked(tmux, pane, guard, f"kill-pane -t {pane}")
        if formats:
            with suppress(WorkmuxError, OSError):
                if _metadata(tmux, window) == data:
                    _clear_managed(tmux, window, [source, *created], team, formats)
        if zoomed:
            with suppress(WorkmuxError, OSError):
                tmux.run("if-shell", "-F", "-t", source, "#{!=:#{window_zoomed_flag},1}", f"resize-pane -Z -t {source}")
        raise


def _clear_managed(tmux: Tmux, window: str, ids: list[str], team: _Managed, formats: tuple[str, str]) -> None:
    panes = {}
    with suppress(WorkmuxError):
        _, panes = _snapshot(tmux, window, team, read_generation=False)
    for pane_id in ids[1:]:
        pane = panes.get(pane_id)
        if pane and not _owner_error(pane, team, require_remain=False):
            commands = []
            options = [EXPECTED, REGISTRATION, OWNER, LEADER, START]
            if pane.remain == "on":
                options.append("remain-on-exit")
            for option in options:
                commands.extend(("set-option", "-pqu", "-t", pane.id, option, ";"))
            with suppress(WorkmuxError):
                _checked(tmux, pane.id, _guard(window, team, pane), _command(*commands[:-1]))
    _clear(tmux, window, formats)
    tmux.run("if-shell", "-F", "-t", window, _eq(_MANAGED, team.id),
             _command("set-option", "-wqu", "-t", window, _STATE, ";", "set-option", "-wqu", "-t", window, _MANAGED))


def team_status(tmux: Tmux, source: str, *, as_json: bool = False) -> str:
    window = _window(tmux, source)
    name, ids, managed = _team(tmux, window)
    pair = None
    if managed:
        state, panes = _snapshot(tmux, window, managed, ids=ids)
        if state and state.ready:
            registrations, _ = _registrations(ids, managed, state, panes)
            if registrations == state.processes:
                pair = dict(zip(_ROLES[1:], (process.pid for process in registrations)))
    if as_json:
        return json.dumps(pair)
    present = {pane.id for pane in tmux.panes(window) if not pane.sidebar}
    rows = [f"Team: {name}"]
    for role, pane in zip(_ROLES, ids):
        status = "" if pane in present else " (missing)"
        if pair and role in pair:
            status = f" (PID {pair[role]})"
        rows.append(f"{role}\t{pane}" + status)
    if managed and not pair:
        rows.append("Managed workers: no current pair")
    return "\n".join(rows)


def refresh_team(tmux: Tmux, source: str) -> None:
    window = _window(tmux, source)
    name, ids, _ = _team(tmux, window)
    tmux.run(*_set_formats(window, name, ids))


def clear_team(tmux: Tmux, source: str) -> None:
    window = _window(tmux, source)
    if sys.platform == "linux":
        # Resolve only after acquiring the lock: an empty window may be in the
        # middle of managed creation, before its team metadata is published.
        with _lock(tmux, window):
            _clear_current(tmux, window)
    else:
        _clear_current(tmux, window)


def _clear_current(tmux: Tmux, window: str) -> None:
    try:
        name, ids, managed = _team(tmux, window)
    except WorkmuxError:
        _clear(tmux, window, None)
        return
    formats = (name, _set_formats(window, name, ids)[-1])
    if managed:
        _clear_managed(tmux, window, ids, managed, formats)
    else:
        _clear(tmux, window, formats)
