"""Terminal prompts for creating a workspace interactively."""

import os
from pathlib import Path
import sys

from .core import WorkmuxError


def directory_matches(text: str) -> list[str]:
    parent, slash, prefix = text.rpartition("/")
    lead = parent + slash
    try:
        if not slash and text.startswith("~"):
            return [text + "/"] if Path(text).expanduser().is_dir() else []
        with os.scandir(Path(lead or ".").expanduser()) as entries:
            return sorted(
                lead + entry.name + "/" for entry in entries
                if entry.name.startswith(prefix)
                and (prefix.startswith(".") or not entry.name.startswith("."))
                and entry.name.isprintable()
                and entry.is_dir()
            )
    except (OSError, RuntimeError, ValueError):
        return []


def prompt_value(value: str) -> str:
    return value if value.isprintable() else repr(value)


def prompt_workspace(default_directory: str) -> tuple[str, str] | None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise WorkmuxError("Interactive workspace creation requires a terminal.")
    try:
        import readline
    except ImportError as error:
        raise WorkmuxError("Interactive workspace creation requires Python's readline module.") from error

    matches = []

    def complete(text, state):
        nonlocal matches
        if state == 0:
            matches = directory_matches(text)
        return matches[state] if state < len(matches) else None

    completer, delimiters = readline.get_completer(), readline.get_completer_delims()
    editline = getattr(readline, "backend", None) == "editline" or "libedit" in (readline.__doc__ or "")
    readline.parse_and_bind("bind ^I rl_complete" if editline else "tab: complete")
    readline.set_completer_delims("")  # Spaces in paths are literal, not word breaks.
    readline.set_completer(complete)
    try:
        entered = input(f"Directory [{prompt_value(default_directory)}]: ")
        try:
            directory = Path(entered or default_directory).expanduser().resolve()
        except (RuntimeError, ValueError) as error:
            raise WorkmuxError(f"Cannot resolve workspace directory: {error}") from error
        readline.set_completer(lambda *_: None)
        default_name = directory.name or directory.anchor
        name = input(f"Name [{prompt_value(default_name)}]: ") or default_name
        return name, str(directory)
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        readline.set_completer(completer)
        readline.set_completer_delims(delimiters)
