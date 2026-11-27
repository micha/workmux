"""Internal tmux pane entry point, executed by absolute path with Python -I.

The one-use spawn environment is not retained in tmux's saved argv: a bare
manual respawn must not start a managed launcher again.
"""

import base64
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

OWNER = "@workmux_team_owner"
LEADER = "@workmux_team_leader"
START = "@workmux_team_start"
EXPECTED = "@workmux_team_expected_launch"
REGISTRATION = "@workmux_team_registration"
SPAWN = "WORKMUX_TEAM_SPAWN"
REFUSED = 125


def process_stat(pid: int) -> tuple[int, str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")[2].split()
    except (ProcessLookupError, FileNotFoundError):
        return None
    return int(fields[19]), fields[0].decode("ascii")


def main() -> int:
    token, executable, window, operation, *args = sys.argv[1:]
    if os.environ.pop(SPAWN, None) != token:
        return REFUSED
    pane = os.environ.get("TMUX_PANE", "")
    if not re.fullmatch(r"%[0-9]+", pane):
        return REFUSED
    pid = os.getpid()
    start, _ = process_stat(pid)
    guard = (f"#{{&&:#{{==:#{{window_id}},{window}}},"
             f"#{{&&:#{{==:#{{{OWNER}}},{token}}},"
             f"#{{&&:#{{==:#{{{LEADER}}},{pid}}},#{{==:#{{pane_pid}},{pid}}}}}}}}}")
    command = f"set-option -p -t {pane} {START} {start} ; display-message -p owned"
    socket = os.environ["TMUX"].rsplit(",", 2)[0]
    # An initial split has to tell the parent its new pane ID before it can
    # stamp the leader. A respawn and its stamp are already in one tmux queue.
    deadline = time.monotonic() + 5
    while True:
        result = subprocess.run([executable, "-S", socket, "-N", "if-shell", "-F", "-t", pane,
                                 guard, command], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=3)
        if result.stdout.strip() == "owned":
            break
        if operation != "reserve" or result.returncode or time.monotonic() >= deadline:
            return REFUSED
        time.sleep(0.03)
    if operation == "reserve":
        # Hold the pane open without consuming any terminal input.
        while True:
            signal.pause()
    if operation == "park":
        return 0
    launcher, profile, directory, path = json.loads(base64.urlsafe_b64decode(args[0]))
    try:
        os.chdir(directory)
        os.environ["PATH"] = path
        os.execv(launcher, [launcher, profile])
    except OSError as error:
        print(f"workmux: {error}", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
