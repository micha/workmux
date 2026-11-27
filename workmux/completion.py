"""Opt-in Bash completion for the argparse CLI; live lookups never start tmux."""

import os
from pathlib import Path
import subprocess

from .core import Tmux, WorkmuxError, tmux_socket


def _argcomplete():
    try:
        import argcomplete
    except ModuleNotFoundError as error:
        if error.name != "argcomplete":
            raise
        raise WorkmuxError("Bash completion requires the optional workmux[completion] extra.") from error
    return argcomplete


def bash_script() -> str:
    return _argcomplete().shellcode(["workmux"], use_defaults=False, shell="bash")


def autocomplete(parser) -> None:
    argcomplete = _argcomplete()
    argcomplete.autocomplete(parser, always_complete_options=False,
                             default_completer=argcomplete.completers.SuppressCompleter())


def _candidates(values, prefix):
    return sorted({value for value in values if value.startswith(prefix) and value.isprintable()})


def _live(lookup):
    def complete(*, prefix, parsed_args, **_):
        server, socket = getattr(parsed_args, "server", None), getattr(parsed_args, "socket", None)
        tmux = Tmux(server=server or "workmux", socket=tmux_socket(server, socket),
                    start_server=False, timeout=0.4)
        try:
            return _candidates(lookup(tmux), prefix)
        except (WorkmuxError, OSError, subprocess.TimeoutExpired):
            return []
    return complete


@_live
def workspaces(tmux):
    names: dict[str, set[str]] = {}
    for workspace in tmux.workspaces():
        for name in (workspace.id, workspace.name, workspace.label):
            names.setdefault(name, set()).add(workspace.id)
    return (name for name, ids in names.items() if len(ids) == 1)


@_live
def workspace_indexes(tmux):
    return map(str, range(1, len(tmux.workspaces()) + 1))


@_live
def panes(tmux):
    return tmux.run("list-panes", "-a", "-f", "#{==:#{pane_dead},0}", "-F", "#{pane_id}").split("\n")


@_live
def clients(tmux):
    return tmux.run("list-clients", "-F", "#{client_name}").split("\n")


def servers(*, prefix, **_):
    directory = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
    try:
        return _candidates((entry.name for entry in directory.iterdir()
                            if entry.name.startswith(prefix) and entry.is_socket()), prefix)
    except OSError:
        return []


def _paths(prefix: str, *, files=None):
    parent, _, basename = prefix.rpartition("/")
    directory = Path(os.path.expanduser(parent or ("/" if prefix.startswith("/") else ".")))
    leading = prefix[:len(prefix) - len(basename)]
    result = []
    try:
        for entry in directory.iterdir():
            if not entry.name.startswith(basename) or (entry.name.startswith(".") and not basename.startswith(".")):
                continue
            if entry.is_dir():
                result.append(f"{leading}{entry.name}/")
            elif files is not None and files(entry):
                result.append(f"{leading}{entry.name}")
    except OSError:
        pass
    return _candidates(result, prefix)


def directories(*, prefix, **_):
    return _paths(prefix)


def sockets(*, prefix, **_):
    return _paths(prefix, files=Path.is_socket)


def executables(*, prefix, **_):
    return _paths(prefix, files=lambda entry: entry.is_file() and os.access(entry, os.X_OK))
