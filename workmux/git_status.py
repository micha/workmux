"""Optional, local-only Git status checks for workspace directories."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import subprocess
import time

from .core import Workspace


POLL_INTERVAL = 2.0
PROBE_TIMEOUT = 2.0
GitStatus = tuple[str, ...]


def parse_status(output: bytes) -> GitStatus:
    """Collect porcelain v2 states; filenames (including rename sources) stay opaque."""
    dirty = conflict = False
    ahead = behind = 0
    records = iter(output.split(b"\0"))
    for record in records:
        if record.startswith(b"# branch.ab "):
            counts = record.split()
            ahead, behind = int(counts[2]), -int(counts[3])
        elif record.startswith(b"u "):
            conflict = True
        elif record.startswith((b"1 ", b"2 ", b"? ")):
            dirty = True
            if record.startswith(b"2 "):
                next(records, None)  # Rename/copy source is a separate NUL field.
    states = []
    if conflict:
        states.append("conflict")
    if dirty:
        states.append("dirty")
    if ahead and behind:
        states.append("diverged")
    elif behind:
        states.append("behind")
    elif ahead:
        states.append("ahead")
    return tuple(states) or ("clean",)


def probe_status(directory: str) -> GitStatus | None:
    if not directory:
        return None
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", directory, "status", "--porcelain=v2",
             "--branch", "-z", "--untracked-files=normal", "--ignore-submodules=none"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PROBE_TIMEOUT,
            check=False,
        )
        return parse_status(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


@dataclass
class _Entry:
    future: Future
    state: GitStatus | None = None
    next_poll: float = 0.0


class GitStatusCache:
    """Poll off the UI thread, sharing checks for identical workspace paths."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workmux-git")
        self.entries: dict[str, _Entry] = {}

    def snapshot(self, workspaces: list[Workspace]) -> dict[str, GitStatus | None]:
        now = time.monotonic()
        directories = {workspace.directory for workspace in workspaces if workspace.directory}
        for directory in self.entries.keys() - directories:
            self.entries.pop(directory).future.cancel()
        for directory in directories:
            entry = self.entries.get(directory)
            if entry is None:
                self.entries[directory] = _Entry(self.executor.submit(probe_status, directory),
                                                 next_poll=now + POLL_INTERVAL)
            elif entry.future.done():
                entry.state = entry.future.result()
                if now >= entry.next_poll:
                    entry.next_poll = now + POLL_INTERVAL
                    entry.future = self.executor.submit(probe_status, directory)
        return {workspace.id: self.entries[workspace.directory].state
                if workspace.directory in self.entries else None for workspace in workspaces}

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
