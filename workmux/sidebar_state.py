"""One sidebar samples server-wide state; the other panes read its snapshots."""

from dataclasses import asdict, dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from .checkouts import CheckoutCache
from .core import ActivitySnapshot, Tab, Tmux, WorkmuxError, Workspace
from .git_status import GitStatus, GitStatusCache


STALE_AFTER = 2.0
_ORPHAN_AFTER = 24 * 60 * 60


@dataclass(frozen=True)
class SidebarSnapshot:
    workspaces: list[Workspace] = field(default_factory=list)
    activity: ActivitySnapshot = field(default_factory=lambda: ActivitySnapshot([], None, {}))
    checkout_windows: dict[tuple[str, str], str] = field(default_factory=dict)
    checkouts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    git_states: dict[str, GitStatus | None] = field(default_factory=dict)


def _rows(mapping):
    return [(*key, value) for key, value in mapping.items()]


def _encode(snapshot: SidebarSnapshot) -> str:
    activity = snapshot.activity
    return json.dumps({"workspaces": [asdict(workspace) for workspace in snapshot.workspaces],
                       "tabs": [asdict(tab) for tab in activity.tabs], "publisher": activity.publisher,
                       "indicators": _rows(activity.indicators),
                       "checkout_windows": _rows(snapshot.checkout_windows),
                       "checkouts": snapshot.checkouts, "git_states": snapshot.git_states},
                      separators=(",", ":"))


def _tab(tab: dict) -> Tab:
    return Tab(**{**tab, "roots": tuple(tab["roots"]), "agents": tuple(map(_tab, tab["agents"]))})


def _decode(contents: str) -> SidebarSnapshot:
    data = json.loads(contents)
    return SidebarSnapshot(
        [Workspace(**workspace) for workspace in data["workspaces"]],
        ActivitySnapshot([_tab(tab) for tab in data["tabs"]], data["publisher"],
                         {(session, window): state for session, window, state in data["indicators"]}),
        {(session, path): window for session, path, window in data["checkout_windows"]},
        {session: tuple(paths) for session, paths in data["checkouts"].items()},
        {path: tuple(states) if states is not None else None for path, states in data["git_states"].items()},
    )


def _claim_lock(path: Path, *, create: bool = True) -> int | None:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        return None
    mine = None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"Not a regular lock file: {path}")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A graceful predecessor can unlink the lock while we still have its
        # inode open. Never lead unless this is the current lock file.
        current = path.lstat()
        if (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino):
            mine = fd
        return mine
    except (BlockingIOError, FileNotFoundError):
        return None
    finally:
        if mine is None:
            os.close(fd)


def _reap_orphans(directory: Path, current: Path) -> None:
    """Pane/server termination can bypass `finally`; reclaim old unused leases."""
    cutoff = time.time() - _ORPHAN_AFTER

    def old(path):
        try:
            return path.lstat().st_mtime < cutoff
        except FileNotFoundError:
            return True

    for lock in directory.glob("*.lock"):
        if lock == current or len(lock.stem) != 64 or any(c not in "0123456789abcdef" for c in lock.stem):
            continue
        snapshot = lock.with_suffix(".json")
        try:
            if not (old(lock) and old(snapshot)):
                continue
            fd = _claim_lock(lock, create=False)
            if fd is None:
                continue
            try:
                if old(lock) and old(snapshot):
                    snapshot.unlink(missing_ok=True)
                    for temporary in directory.glob(lock.stem + ".tmp.*"):
                        temporary.unlink(missing_ok=True)
                    lock.unlink(missing_ok=True)
            finally:
                os.close(fd)
        except OSError:
            continue  # Cleanup must not prevent current sidebars from sharing.


class _SharedFile:
    """A nonblocking leader lease with atomic, lock-free reads for followers."""

    def __init__(self, identity: str):
        # Include a schema version and tmux's server identity, not just the
        # selector: aliases converge and restarted servers never inherit state.
        key = hashlib.sha256(os.fsencode(identity)).hexdigest()
        uid = os.getuid()
        directory = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / f"workmux-sidebar-v1-{uid}"
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
            raise PermissionError(f"Not a private cache directory: {directory}")
        self.path = directory / (key + ".json")
        self.lock = directory / (key + ".lock")
        self.leader: int | None = None
        self.read_version = None
        self.cached: SidebarSnapshot | None = None
        self.last_publish = float("-inf")

    def claim(self) -> bool:
        if self.leader is not None:
            return True
        self.leader = _claim_lock(self.lock)
        if self.leader is None:
            return False
        _reap_orphans(self.lock.parent, self.lock)
        return True

    def read(self) -> SidebarSnapshot | None:
        try:
            info = self.path.stat()
            if abs(time.time() - info.st_mtime) >= STALE_AFTER:
                return None
            version = info.st_ino, info.st_mtime_ns, info.st_size
            if version != self.read_version:
                self.cached = _decode(self.path.read_text(encoding="utf-8"))
                self.read_version = version
            return self.cached
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            return None

    def publish(self, snapshot: SidebarSnapshot, changed: bool) -> None:
        if not changed and time.monotonic() < self.last_publish + STALE_AFTER / 2:
            return
        fd, path = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.stem + ".tmp.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(_encode(snapshot))
            os.replace(path, self.path)
        finally:
            Path(path).unlink(missing_ok=True)
        self.last_publish = time.monotonic()

    def close(self) -> None:
        if self.leader is not None:
            try:
                self.path.unlink(missing_ok=True)
                self.lock.unlink(missing_ok=True)
            finally:
                os.close(self.leader)
                self.leader = None


class _LocalState:
    def __init__(self, tmux: Tmux):
        self.tmux = tmux
        self.git = GitStatusCache()
        self.checkouts = CheckoutCache()

    def snapshot(self) -> SidebarSnapshot:
        state = self.tmux.sidebar_snapshot()
        checkouts = self.checkouts.snapshot(state.workspaces)
        directories = tuple({path for paths in checkouts.values() for path in paths})
        return SidebarSnapshot(state.workspaces, state.activity, state.checkout_windows, checkouts,
                               self.git.snapshot(state.workspaces, directories))

    def close(self) -> None:
        self.git.close()
        self.checkouts.close()


class SidebarState:
    """Snapshots for the sidebar loop, with local polling if sharing is unavailable."""

    def __init__(self, tmux: Tmux):
        self.tmux = tmux
        self.shared: _SharedFile | None = None
        self.local: _LocalState | None = None
        self.current = SidebarSnapshot()
        self.last_read = time.monotonic()
        try:
            identity = tmux.run("display-message", "-p", "#{pid}\t#{start_time}\t#{socket_path}", missing_ok=True)
            if identity:
                self.shared = _SharedFile(identity)
        except (OSError, WorkmuxError):
            pass

    def _stop_local(self) -> None:
        if self.local is not None:
            self.local.close()
            self.local = None

    def _stop_sharing(self) -> None:
        if self.shared is not None:
            try:
                self.shared.close()
            except OSError:
                pass
            self.shared = None

    def snapshot(self) -> SidebarSnapshot:
        shared = self.shared
        if shared is not None:
            try:
                if not shared.claim():
                    snapshot = shared.read()
                    now = time.monotonic()
                    if snapshot is not None:
                        self._stop_local()
                        self.last_read = now
                        if snapshot is not self.current and snapshot != self.current:
                            self.current = snapshot
                        return self.current
                    if now < self.last_read + STALE_AFTER:
                        # Another pane may be preparing its first snapshot. Do
                        # not start duplicate workers while it is in flight.
                        return self.current
                    shared = None  # A stopped leader must not freeze this view.
            except OSError:
                self._stop_sharing()
                shared = None
        if self.local is None:
            self.local = _LocalState(self.tmux)
        snapshot = self.local.snapshot()
        changed = snapshot != self.current
        if shared is not None:
            try:
                shared.publish(snapshot, changed)
            except OSError:
                self._stop_sharing()
        if changed:
            self.current = snapshot
        return self.current

    def close(self) -> None:
        self._stop_local()
        self._stop_sharing()
