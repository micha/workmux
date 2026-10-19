"""Explicit installation of owned agent integrations, independent of tmux."""

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import tempfile

from .core import WorkmuxError


HARNESSES = ("pi", "opencode", "codex", "claude")
PI_FILENAME = "workmux-notify.ts"
_HEADER = "// workmux-managed: pi-notify sha256="
_ZERO_HASH = "0" * 64


@dataclass(frozen=True)
class _File:
    path: str
    resource: str
    kind: str


_FILES = {
    "pi": (_File(f"extensions/{PI_FILENAME}", "pi-notify.ts", "pi-notify"),),
    "opencode": (_File("plugins/workmux-notify.js", "opencode-notify.mjs", "opencode-notify"),),
    "codex": (_File("hooks.json", "codex-hooks.json", "codex-hooks"),),
    "claude": (
        _File("skills/workmux/.claude-plugin/plugin.json", "claude-plugin.json", "claude-plugin"),
        _File("skills/workmux/hooks/hooks.json", "claude-hooks.json", "claude-hooks"),
    ),
}
_RELOAD = {
    "pi": "Run /reload in Pi; test with /workmux-notify-test.",
    "opencode": "Restart the local OpenCode TUI.",
    "codex": "Restart Codex and approve the workmux hooks in /hooks; hooks must be enabled.",
    "claude": "Run /reload-plugins in Claude Code (skills-dir plugins require Claude Code 2.1.223+).",
}


def agent_dir(harness: str, directory: str | None = None) -> Path:
    """Use personal harness roots, never project configuration."""
    if directory is not None:
        return Path(directory).expanduser()
    variable = {"pi": "PI_CODING_AGENT_DIR", "opencode": "OPENCODE_CONFIG_DIR",
                "codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR"}[harness]
    selected = os.environ.get(variable)
    if selected:
        return Path(selected).expanduser()
    home = Path.home()
    if harness == "opencode":
        return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
    return {"pi": home / ".pi" / "agent", "codex": home / ".codex",
            "claude": home / ".claude"}[harness]


def pi_agent_dir(directory: str | None = None) -> Path:
    return agent_dir("pi", directory)


def _content(spec: _File) -> bytes:
    body = files("workmux").joinpath("resources", spec.resource).read_bytes()
    if spec.resource.endswith(".json"):
        # A schema-supported description carries a checksum of the exact file,
        # with the checksum itself zeroed. Formatting edits also invalidate it.
        data = {**json.loads(body),
                "description": f"workmux-managed: {spec.kind} sha256={_ZERO_HASH}"}
        content = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
        return content.replace(_ZERO_HASH.encode(), hashlib.sha256(content).hexdigest().encode(), 1)
    digest = hashlib.sha256(body).hexdigest()
    return f"// workmux-managed: {spec.kind} sha256={digest}\n".encode() + body


def pi_extension() -> bytes:
    return _content(_FILES["pi"][0])


def _owned(content: bytes, kind: str = "pi-notify", *, json_file: bool = False) -> bool:
    """Verify older intact versions too; a filename alone never proves ownership."""
    if json_file:
        try:
            description = json.loads(content).get("description", "")
            if not isinstance(description, str):
                return False
            match = re.fullmatch(rf"workmux-managed: {re.escape(kind)} sha256=([0-9a-f]{{64}})", description)
            if not match:
                return False
            marker = description.encode()
            if content.count(marker) != 1:
                return False
            zeroed = content.replace(marker, marker[:-64] + _ZERO_HASH.encode(), 1)
            return hashlib.sha256(zeroed).hexdigest() == match[1]
        except (ValueError, AttributeError, UnicodeError):
            return False
    header, separator, body = content.partition(b"\n")
    match = re.fullmatch(rb"// workmux-managed: " + re.escape(kind.encode()) + rb" sha256=([0-9a-f]{64})", header)
    return bool(separator and match and match[1].decode() == hashlib.sha256(body).hexdigest())


def _symlink(target: Path) -> Path | None:
    """Reject managed-file symlinks, not directory links chosen by the user."""
    return target if target.is_symlink() else None


def _existing(target: Path, kind: str = "pi-notify") -> bytes | None:
    symlink = _symlink(target)
    if symlink is not None:
        raise WorkmuxError(f"Refusing to modify symlink: {symlink}")
    try:
        content = target.read_bytes()
    except FileNotFoundError:
        return None
    if not _owned(content, kind, json_file=target.suffix == ".json"):
        raise WorkmuxError(
            f"Refusing to modify an unowned or edited agent integration: {target}. "
            "Move it aside manually if you want workmux to manage this path."
        )
    return content


def plugin_status(harness: str, directory: str | None = None) -> tuple[str, Path]:
    """Read-only status of all files owned by this harness integration."""
    root = agent_dir(harness, directory)
    specs = _FILES[harness]
    location = root / specs[0].path
    present = 0
    outdated = False
    for spec in specs:
        target = root / spec.path
        if _symlink(target) is not None:
            return "conflict (symlink)", target
        try:
            content = target.read_bytes()
        except FileNotFoundError:
            continue
        except IsADirectoryError:
            return "conflict (directory)", target
        if not _owned(content, spec.kind, json_file=target.suffix == ".json"):
            return "conflict (unowned or edited file)", target
        present += 1
        outdated |= content != _content(spec)
    if not present:
        return "not installed", location
    if present != len(specs):
        return "installed (incomplete)", location
    return "installed (update available)" if outdated else "installed", location


def pi_status(directory: str | None = None) -> tuple[str, Path]:
    return plugin_status("pi", directory)


def _write(target: Path, content: bytes, existing: bytes | None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if _symlink(target) is not None:
        raise WorkmuxError(f"Refusing to modify symlink: {target}")
    if existing is None:
        with target.open("xb") as output:
            output.write(content)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".workmux-", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        if _symlink(target) is not None or target.read_bytes() != existing:
            raise WorkmuxError(f"Agent integration changed during installation: {target}")
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def install_plugin(harness: str, directory: str | None = None) -> str:
    root = agent_dir(harness, directory)
    # Preflight every file before writing any: a conflict must not replace a
    # manifest and only then discover edited hooks elsewhere in the bundle.
    plan = [(root / spec.path, _content(spec), _existing(root / spec.path, spec.kind))
            for spec in _FILES[harness]]
    changed = False
    for target, content, existing in plan:
        if existing != content:
            _write(target, content, existing)
            changed = True
    prefix = "Installed" if changed else "Already installed"
    return f"{prefix}: {plan[0][0]}. {_RELOAD[harness]}"


def uninstall_plugin(harness: str, directory: str | None = None) -> str:
    root = agent_dir(harness, directory)
    plan = [(root / spec.path, _existing(root / spec.path, spec.kind)) for spec in _FILES[harness]]
    removed = False
    for target, existing in plan:
        if existing is not None:
            if _symlink(target) is not None or target.read_bytes() != existing:
                raise WorkmuxError(f"Agent integration changed during removal: {target}")
            target.unlink()
            removed = True
    prefix = "Removed" if removed else "Not installed"
    return f"{prefix}: {plan[0][0]}. {_RELOAD[harness]}"


def install_pi(directory: str | None = None) -> str:
    return install_plugin("pi", directory)


def uninstall_pi(directory: str | None = None) -> str:
    return uninstall_plugin("pi", directory)
