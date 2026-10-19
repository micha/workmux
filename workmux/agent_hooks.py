"""Best-effort command hooks for Codex and Claude Code; hook stdout stays empty."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


_MAX_INPUT = 1024 * 1024
_HEADLESS = {
    "codex": {"exec", "e", "review", "mcp-server", "app-server", "exec-server", "--remote"},
    "claude": {"-p", "--print", "--output-format", "--input-format", "--background", "--bg"},
}


def hook_state(harness: str, payload: dict) -> str | None:
    """Use explicit root-agent lifecycle events, not tool output or transcripts."""
    if payload.get("agent_id") or payload.get("parent_agent_id"):
        return None
    event = payload.get("hook_event_name")
    if event in ("SessionStart", "SessionEnd"):
        return "idle"
    if event == "PermissionRequest":
        return "attention"
    if event == "Notification":
        return "attention" if harness == "claude" and payload.get("notification_type") in (
            "permission_prompt", "elicitation_dialog") else None
    if event == "PreToolUse" and payload.get("tool_name") in ("AskUserQuestion", "request_user_input"):
        return "attention"
    if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        return "working"
    if event == "Stop" and not payload.get("stop_hook_active"):
        if payload.get("background_tasks") or payload.get("session_crons"):
            return "working"
        return "finished"
    return None


def _interactive_terminal(harness: str, pid: int) -> int | None:
    """Return the verified foreground harness's terminal, not the hook's tty.

    Claude detaches hook processes into a new session, so /dev/tty and
    tcgetpgrp in the hook cannot describe Claude's controlling terminal. Linux
    procfs supplies the harness's argv, process group, controlling tty device,
    and foreground group. Its stdout must refer to that same tty. If any check
    fails, do nothing rather than notify from a headless or redirected run.
    """
    parent_output = None
    try:
        if pid <= 1 or pid == os.getpid():
            return None
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        args = [part.decode(errors="replace") for part in command[1:] if part]
        if any(arg.split("=", 1)[0] in _HEADLESS[harness] for arg in args):
            return None
        if harness == "claude" and any(arg.startswith("-p") and not arg.startswith("--") for arg in args):
            return None
        # comm may contain spaces or parentheses; numeric fields follow the
        # last closing parenthesis. Offsets are relative to the state field.
        fields = Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")[2].split()
        group, tty, foreground = int(fields[2]), int(fields[4]), int(fields[5])
        if group <= 0 or foreground != group or tty == 0:
            return None
        parent_output = os.open(f"/proc/{pid}/fd/1", os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
        if not os.isatty(parent_output):
            return None
        # procfs tty_nr is a signed, kernel-encoded device number, not the
        # userspace dev_t encoding returned by fstat (notably for large minors).
        tty &= 0xffffffff
        device = os.fstat(parent_output).st_rdev
        if (os.major(device), os.minor(device)) != ((tty >> 8) & 0xfff,
                                                   (tty & 0xff) | ((tty >> 12) & 0xfff00)):
            return None
        terminal, parent_output = parent_output, None
        return terminal
    except (OSError, ValueError, IndexError):
        return None
    finally:
        if parent_output is not None:
            os.close(parent_output)


def _notification(harness: str, state: str, cwd: str) -> bytes:
    directory = Path(cwd).name or cwd
    message = "Input needed" if state == "attention" else "Finished — ready for input"
    text = f"{'Claude Code' if harness == 'claude' else 'Codex'} ({directory}): {message}"
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)[:240]
    osc = f"\x1b]9;{text}\x07"
    return ("\x07\x1bPtmux;" + osc.replace("\x1b", "\x1b\x1b") + "\x1b\\").encode()


def run_hook(harness: str, pid: int) -> None:
    """Never block a prompt, alter a hook decision, or write protocol output."""
    terminal = None
    try:
        pane = os.environ.get("TMUX_PANE")
        if not os.environ.get("TMUX") or not pane:
            return
        raw = sys.stdin.read(_MAX_INPUT + 1)
        if len(raw) > _MAX_INPUT:
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return
        state = hook_state(harness, payload)
        if state is None:
            return
        terminal = _interactive_terminal(harness, pid)
        if terminal is None:
            return
        cwd = payload.get("cwd", "")
        if state in ("attention", "finished"):
            os.write(terminal, _notification(harness, state, cwd if isinstance(cwd, str) else ""))
        args = ["workmux", "agent", "state", state, "--pane", pane, "--pid", str(pid)]
        if isinstance(cwd, str) and os.path.isabs(cwd):
            args.extend(["--root", cwd])
        subprocess.run(
            args,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2, check=False,
        )
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        pass
    finally:
        if terminal is not None:
            os.close(terminal)
