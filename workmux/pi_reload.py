"""One-shot Pi reload requests over the current tmux server's pane options."""

from dataclasses import dataclass, field
import re
import time
from uuid import uuid4

from .core import Tmux, WorkmuxError


_TOKEN = "@workmux-pi-reload-token"
_REQUEST = "@workmux-pi-reload-request"
_RESULT = "@workmux-pi-reload-result"
_ID = r"[0-9a-f]{32}"
_REQUEST_FORMAT = re.compile(rf"{_ID}:{_ID}:([0-9]{{1,16}})")
_PANE_FORMAT = ("#{pane_id}\t#{s/[\t\r\n]/ /:session_name}\t#{pane_dead}\t"
                + "\t".join("#{" + option + "}" for option in (_TOKEN, _REQUEST)))
_RESULTS = {"applied": "Applied", "draft": "Skipped: draft", "busy": "Skipped: busy",
            "prompt": "Skipped: prompt", "failed": "Reload failed"}
_TIMEOUT = 5.0
_POLL_INTERVAL = 0.05
_MISSING_PANE = ("can't find pane:", "can't find window:", "can't find session:")


@dataclass
class _Pane:
    id: str
    token: str
    request: str
    sessions: set[str] = field(default_factory=set)
    result: str = "No reload handler"


def _panes(tmux: Tmux) -> dict[str, _Pane]:
    panes: dict[str, _Pane] = {}
    for line in tmux.run("list-panes", "-a", "-F", _PANE_FORMAT, missing_ok=True).split("\n"):
        fields = line.split("\t")
        if len(fields) != 5 or not re.fullmatch(r"%[0-9]+", fields[0]):
            continue
        pane_id, session, dead, token, request = fields
        pane = panes.setdefault(pane_id, _Pane(pane_id, token if dead == "0" else "", request))
        pane.sessions.add(re.sub(r"[\x00-\x1f\x7f-\x9f|]", "?", session))
    return panes


def _advertise(tmux: Tmux, pane: _Pane, request: str) -> str:
    # Exact comparisons in the tmux queue, not just in the Python snapshot. Never
    # target a new runtime or replace a request written by another invocation.
    condition = (f"#{{&&:#{{!:#{{pane_dead}}}},#{{&&:#{{==:#{{{_TOKEN}}},{pane.token}}},"
                 f"#{{==:#{{{_REQUEST}}},{pane.request}}}}}}}")
    try:
        accepted = tmux.run(
            "if-shell", "-F", "-t", pane.id, condition,
            f"set-option -pqu -t {pane.id} {_RESULT} ; "
            f"set-option -p -t {pane.id} {_REQUEST} {request} ; "
            f"wait-for -S workmux-pi-reload-{pane.token} ; display-message -p accepted",
        )
    except WorkmuxError as error:
        if str(error).startswith(_MISSING_PANE):
            return "Pane closed"
        raise
    return "" if accepted == "accepted" else "Handler or request changed"


def _clear(tmux: Tmux, pane: _Pane, request: str) -> None:
    # This is best-effort: an expired request cannot be executed and a later
    # invocation can replace it. Do not clear newer requests or their replies.
    try:
        tmux.run("if-shell", "-F", "-t", pane.id, f"#{{==:#{{{_REQUEST}}},{request}}}",
                 f"set-option -pqu -t {pane.id} {_REQUEST} ; set-option -pqu -t {pane.id} {_RESULT}",
                 missing_ok=True)
    except WorkmuxError:
        pass


def reload_panes(tmux: Tmux, *, timeout: float = _TIMEOUT) -> list[tuple[str, str, str]]:
    """Request one reload per registered pane; `Applied` requires a matching reply."""
    panes = _panes(tmux)
    deadline = time.monotonic() + timeout
    expires = (time.time_ns() // 1_000_000) + int(timeout * 1000)
    pending: dict[str, tuple[_Pane, str, str]] = {}
    try:
        for pane in panes.values():
            if not re.fullmatch(_ID, pane.token):
                continue
            previous = _REQUEST_FORMAT.fullmatch(pane.request)
            if pane.request and not previous:
                pane.result = "Invalid reload request"
                continue
            if previous and int(previous[1]) > time.time_ns() // 1_000_000:
                pane.result = "Already requested"
                continue
            if time.monotonic() >= deadline:
                pane.result = "Not requested: time limit"
                continue
            request_id = uuid4().hex
            request = f"{pane.token}:{request_id}:{expires}"
            pane.result = _advertise(tmux, pane, request)
            if not pane.result:
                pane.result = "No response"
                pending[pane.id] = pane, request_id, request
        remaining = set(pending)
        while remaining:
            output = tmux.run("list-panes", "-a", "-F", f"#{{pane_id}}\t#{{{_RESULT}}}", missing_ok=True)
            found = set()
            for line in output.split("\n"):
                pane_id, _, reply = line.partition("\t")
                if pane_id not in remaining:
                    continue
                found.add(pane_id)
                pane, request_id, _ = pending[pane_id]
                reply_id, _, status = reply.partition(":")
                if reply_id == request_id and status in _RESULTS:
                    pane.result = _RESULTS[status]
                    remaining.remove(pane_id)
            for pane_id in remaining - found:
                panes[pane_id].result = "Pane closed"
                remaining.remove(pane_id)
            wait = deadline - time.monotonic()
            if wait <= 0 or not remaining:
                break
            time.sleep(min(_POLL_INTERVAL, wait))
    finally:
        for pane, _, request in pending.values():
            _clear(tmux, pane, request)
    return sorted((", ".join(sorted(pane.sessions)), pane.id, pane.result) for pane in panes.values())


def reload_table(tmux: Tmux) -> str:
    rows = [("Session", "Pane", "Result"), *reload_panes(tmux)]
    session_width = max(len(session) for session, _, _ in rows)
    pane_width = max(len(pane) for _, pane, _ in rows)
    return "\n".join(f"{session:<{session_width}} | {pane:<{pane_width}} | {result}"
                     for session, pane, result in rows)
