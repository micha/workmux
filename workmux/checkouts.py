"""Read-only, bounded discovery of checkouts beneath a workspace directory."""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time

from .core import Workspace


POLL_INTERVAL = 5.0
MAX_DEPTH = 6
MAX_DIRECTORIES = 2000
MAX_ENTRIES = 16000
MAX_GIT_QUERIES = 128
MAX_CHECKOUTS = 64
SCAN_TIMEOUT = 4.0


def _git(directory: str, *args: str, timeout: float = 2.0) -> bytes | None:
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", directory, *args],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout, check=False)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def checkout_root(directory: str) -> str | None:
    """Resolve a real checkout, not a .git marker alone or a bare repository."""
    output = _git(directory, "rev-parse", "--show-toplevel")
    if not output:
        return None
    path = os.fsdecode(output.rstrip(b"\n"))
    if any(char in path for char in "\t\r\n"):
        return None  # tmux's directory snapshots cannot represent these paths.
    return os.path.realpath(path)


def contains(directory: str, path: str, *, strict: bool = False) -> bool:
    """Component-wise containment; a matching string prefix is not sufficient."""
    parent, child = Path(directory), Path(path)
    return child.is_relative_to(parent) and (not strict or child != parent)


def worktree_paths(output: bytes) -> tuple[str, ...]:
    """Parse NUL porcelain attributes without splitting or unquoting filenames."""
    return tuple(os.fsdecode(field[len(b"worktree "):]) for field in output.split(b"\0")
                 if field.startswith(b"worktree "))


def discover_checkouts(directory: str, max_depth: int | None = None) -> tuple[str, ...]:
    if not directory:
        return ()
    max_depth = MAX_DEPTH if max_depth is None else max(0, max_depth)
    if max_depth == 0:
        return ()
    root = os.path.realpath(directory)
    deadline = time.monotonic() + SCAN_TIMEOUT
    found = set()
    queries = 0
    listed = set()

    def query(path, *args):
        nonlocal queries
        remaining = deadline - time.monotonic()
        if remaining <= 0 or queries >= MAX_GIT_QUERIES:
            return None
        queries += 1
        return _git(path, *args, timeout=min(2.0, remaining))

    def add(path):
        # Never follow a discovered directory symlink, including a symlink in
        # an ancestor of a registered worktree. User-selected workspace roots
        # themselves may be symlinks and have already been resolved.
        if path in found or any(char in path for char in "\t\r\n") or not contains(root, path, strict=True):
            return
        if len(Path(path).relative_to(root).parts) > max_depth:
            return
        if (os.path.realpath(path) != path or not os.path.isdir(path)
                or not os.path.lexists(os.path.join(path, ".git"))):
            return
        found.add(path)

    def inspect(path):
        output = query(path, "rev-parse", "--show-toplevel", "--git-common-dir")
        if not output:
            return
        top, _, common = os.fsdecode(output.removesuffix(b"\n")).partition("\n")
        if not common or not os.path.isabs(top) or "\t" in top or "\r" in top:
            return
        top = os.path.realpath(top)
        if not contains(top, path):
            return  # A newline in the top-level name cannot be represented.
        if top == path:
            add(top)
        if len(found) >= MAX_CHECKOUTS:
            return
        # Git may print a common directory relative to the query's cwd. All
        # linked worktrees share it even though each has its own top level.
        common = os.path.realpath(os.path.join(path, common))
        if common not in listed:
            listed.add(common)
            worktrees = query(path, "worktree", "list", "--porcelain", "-z")
            if worktrees is not None:
                for candidate in worktree_paths(worktrees):
                    if len(found) >= MAX_CHECKOUTS or time.monotonic() >= deadline:
                        break
                    add(candidate)

    # Also find linked worktrees beneath a saved subdirectory of a repository.
    inspect(root)
    pending = [(root, 0)]
    visited = examined = 0
    while (pending and visited < MAX_DIRECTORIES and examined < MAX_ENTRIES
           and len(found) < MAX_CHECKOUTS and queries < MAX_GIT_QUERIES
           and time.monotonic() < deadline):
        path, depth = pending.pop()
        visited += 1
        try:
            if path != root:
                if os.path.realpath(path) != path:
                    continue  # It may have become a symlink since being queued.
                if os.path.lexists(os.path.join(path, ".git")):
                    inspect(path)
                    if len(found) >= MAX_CHECKOUTS or queries >= MAX_GIT_QUERIES:
                        break
            if depth < max_depth:
                children = []
                with os.scandir(path) as entries:
                    for entry in entries:
                        if examined >= MAX_ENTRIES or time.monotonic() >= deadline:
                            break
                        examined += 1
                        if entry.name != ".git" and entry.is_dir(follow_symlinks=False):
                            children.append(entry.path)
                        if len(children) + len(pending) + visited >= MAX_DIRECTORIES:
                            break
                pending.extend((child, depth + 1) for child in sorted(children, reverse=True))
        except OSError:
            continue
    return tuple(sorted(found))


@dataclass
class _Entry:
    future: Future
    paths: tuple[str, ...] = ()
    next_poll: float = 0.0


class CheckoutCache:
    """Discover off the UI thread, deduplicating workspaces with the same path."""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workmux-checkouts")
        self.entries: dict[tuple[str, int], _Entry] = {}

    def snapshot(self, workspaces: list[Workspace]) -> dict[str, tuple[str, ...]]:
        now = time.monotonic()
        keys = {workspace.id: (workspace.directory,
                              MAX_DEPTH if workspace.checkout_max_depth is None
                              else workspace.checkout_max_depth)
                for workspace in workspaces if workspace.directory and workspace.checkout_max_depth != 0}
        paths = set(keys.values())
        for key in self.entries.keys() - paths:
            self.entries.pop(key).future.cancel()
        for key in paths:
            directory, depth = key
            entry = self.entries.get(key)
            if entry is None:
                self.entries[key] = _Entry(self.executor.submit(discover_checkouts, directory, depth),
                                           next_poll=now + POLL_INTERVAL)
            elif entry.future.done():
                entry.paths = entry.future.result()
                if now >= entry.next_poll:
                    entry.next_poll = now + POLL_INTERVAL
                    entry.future = self.executor.submit(discover_checkouts, directory, depth)
        return {workspace.id: self.entries[keys[workspace.id]].paths
                if workspace.id in keys else () for workspace in workspaces}

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
