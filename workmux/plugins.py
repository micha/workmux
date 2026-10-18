"""Explicit installation of optional agent plugins, independent of tmux."""

import hashlib
from importlib.resources import files
import os
from pathlib import Path
import re
import tempfile

from .core import WorkmuxError


PI_FILENAME = "workmux-notify.ts"
_HEADER = "// workmux-managed: pi-notify sha256="


def pi_agent_dir(directory: str | None = None) -> Path:
    """Mirror Pi's personal directory override; never use project configuration."""
    selected = directory if directory is not None else os.environ.get("PI_CODING_AGENT_DIR")
    return Path(selected).expanduser() if selected else Path.home() / ".pi" / "agent"


def pi_extension() -> bytes:
    body = files("workmux").joinpath("resources", "pi-notify.ts").read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    return f"{_HEADER}{digest}\n".encode() + body


def _owned(content: bytes) -> bool:
    """Verify the installed body, including older intact workmux versions."""
    header, separator, body = content.partition(b"\n")
    if not separator:
        return False
    match = re.fullmatch(re.escape(_HEADER.encode()) + rb"([0-9a-f]{64})", header)
    return bool(match and match[1].decode() == hashlib.sha256(body).hexdigest())


def _existing(target: Path) -> bytes | None:
    if target.is_symlink():
        raise WorkmuxError(f"Refusing to modify symlink: {target}")
    try:
        content = target.read_bytes()
    except FileNotFoundError:
        return None
    if not _owned(content):
        raise WorkmuxError(
            f"Refusing to modify an unowned or edited Pi extension: {target}. "
            "Move it aside manually if you want workmux to manage this path."
        )
    return content


def pi_status(directory: str | None = None) -> tuple[str, Path]:
    """Report the managed extension's disk status without changing anything."""
    target = pi_agent_dir(directory) / "extensions" / PI_FILENAME
    if target.is_symlink():
        return "conflict (symlink)", target
    try:
        content = target.read_bytes()
    except FileNotFoundError:
        return "not installed", target
    except IsADirectoryError:
        return "conflict (directory)", target
    if not _owned(content):
        return "conflict (unowned or edited file)", target
    if content != pi_extension():
        return "installed (update available)", target
    return "installed", target


def install_pi(directory: str | None = None) -> str:
    target = pi_agent_dir(directory) / "extensions" / PI_FILENAME
    existing = _existing(target)
    content = pi_extension()
    if existing == content:
        return f"Already installed: {target}. Run /reload in Pi."
    target.parent.mkdir(parents=True, exist_ok=True)
    if existing is None:
        # Exclusive creation avoids overwriting a file created after our check.
        with target.open("xb") as output:
            output.write(content)
    else:
        # Replace intact older versions atomically, leaving no partial extension.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".workmux-", delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
            if target.is_symlink() or target.read_bytes() != existing:
                raise WorkmuxError(f"Pi extension changed during installation: {target}")
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return f"Installed: {target}. Run /reload in Pi; test with /workmux-notify-test."


def uninstall_pi(directory: str | None = None) -> str:
    target = pi_agent_dir(directory) / "extensions" / PI_FILENAME
    if _existing(target) is None:
        return f"Not installed: {target}"
    target.unlink()
    return f"Removed: {target}. Run /reload in Pi."
