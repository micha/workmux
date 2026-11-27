"""Terminal prompts for creating a workspace interactively."""

from collections.abc import Callable
import os
from pathlib import Path
import sys

from .core import WorkmuxError, validate_label


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


def prompt_workspace(default_directory: str, name_taken: Callable[[str], bool]) -> tuple[str, str] | None:
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

    def read_with_default(prompt: str, default: str) -> str:
        # Don't put control characters from --dir into Readline's edit buffer.
        if not default.isprintable():
            return input(prompt) or default
        readline.set_startup_hook(lambda: readline.insert_text(default))
        try:
            return input(prompt) or default
        finally:
            readline.set_startup_hook(None)

    completer, delimiters = readline.get_completer(), readline.get_completer_delims()
    editline = getattr(readline, "backend", None) == "editline" or "libedit" in (readline.__doc__ or "")
    readline.parse_and_bind("bind ^I rl_complete" if editline else "tab: complete")
    readline.set_completer_delims("")  # Spaces in paths are literal, not word breaks.
    readline.set_completer(complete)
    try:
        entered = read_with_default("Directory: ", default_directory)
        try:
            directory = Path(entered).expanduser().resolve()
        except (RuntimeError, ValueError) as error:
            raise WorkmuxError(f"Cannot resolve workspace directory: {error}") from error
        default_name = directory.name
        try:
            validate_label(default_name)
        except WorkmuxError:
            label_prompt = "Name (directory basename is not a valid workspace name): "
            default_name = ""
        else:
            if not name_taken(default_name):
                return default_name, str(directory)
            label_prompt = "Name: "
        readline.set_completer(lambda *_: None)
        return read_with_default(label_prompt, default_name), str(directory)
    except (EOFError, KeyboardInterrupt):
        return None
    finally:
        readline.set_completer(completer)
        readline.set_completer_delims(delimiters)
