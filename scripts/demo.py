#!/usr/bin/env python3
"""Capture a staged, model-free demo using VHS and a private tmux server."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from workmux.core import Tmux  # noqa: E402


def repository(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=Demo",
                    "-c", "user.email=demo@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "-q", "--allow-empty", "-m", "Initial commit"], check=True)


def display(tmux: Tmux, pane: str, title: str, lines: list[str], directory: Path, *, bell: bool = False) -> None:
    """Use a live, idle process instead of invoking any agent or external service."""
    text = "\033[2J\033[H\033[?25l\n" + "\n".join(lines) + "\n" + ("\a" if bell else "")
    program = "import signal,sys; sys.stdout.write(" + repr(text) + "); sys.stdout.flush(); signal.pause()"
    tmux.run("respawn-pane", "-k", "-t", pane, "-c", str(directory),
             shlex.join([sys.executable, "-c", program]))
    tmux.run("select-pane", "-t", pane, "-T", title)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "demo.png")
    args = parser.parse_args()
    output = args.output.resolve()
    for tool in ("tmux", "git", "vhs", "bash"):
        if not shutil.which(tool):
            parser.error(f"Missing {tool}; use nix develop .#demo --command python scripts/demo.py")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="workmux-demo-") as temporary:
        base = Path(temporary)
        home = base / "home"
        home.mkdir()
        # Neither the demo shells nor Git read personal configuration.
        os.environ.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
                          XDG_RUNTIME_DIR=str(base),
                          XDG_CACHE_HOME=str(home / ".cache"), XDG_DATA_HOME=str(home / ".local/share"),
                          GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                          SHELL=shutil.which("bash"), TERM="xterm-256color")
        for variable in ("TMUX", "TMUX_PANE", "BASH_ENV", "ENV"):
            os.environ.pop(variable, None)
        tmux = Tmux(socket=str(base / "tmux.sock"))
        try:
            project = base / "workmux"
            for directory in (project / "main", project / "sidebar", base / "website", base / "dotfiles"):
                repository(directory)
            (project / "sidebar" / "changes.txt").write_text("Demo changes\n")
            session = tmux.new_workspace("workmux", str(project))
            primary = next(pane.id for pane in tmux.panes(session) if not pane.sidebar)
            tmux.run("rename-window", "-t", primary, "sidebar")
            lower = tmux.split(primary, "down")
            display(tmux, primary, "Pi · sidebar polish", [
                "  \033[1;36mPi\033[0m  /  workmux",
                "  \033[2mStaged demo · no model calls\033[0m",
                "",
                "  \033[1mYou\033[0m",
                "  Let's move Git status into the sidebar gutter.",
                "",
                "  \033[1;36mPi\033[0m",
                "  Updating the sidebar renderer and regression tests.",
                "  Color shows repo health; arrows show upstream divergence.",
                "",
                "  \033[34m⠿ Working\033[0m  ·  sidebar.py + tests",
            ], project / "sidebar")
            display(tmux, lower, "tests", [
                "  \033[1;32m$\033[0m python -m unittest discover -s tests -p test_sidebar.py",
                "",
                "  ..................",
                "  \033[2mRan 18 tests\033[0m",
                "",
                "  \033[1;32mOK\033[0m",
                "",
                "  \033[2m$ git diff --stat\033[0m",
                "   workmux/sidebar.py    |  2 ++",
                "   tests/test_sidebar.py | 22 +++++++++++++++++++++-",
            ], project / "sidebar")
            tmux.report_agent_state(primary, "working")
            for name in ("website", "dotfiles"):
                other = tmux.new_workspace(name, str(base / name))
                content = next(pane.id for pane in tmux.panes(other) if not pane.sidebar)
                display(tmux, content, "Docs · navigation", ["  Demo workspace"], base / name)
                if name == "website":
                    tmux.report_agent_state(content, "working")
            background = [(tmux.new_tab(primary, name), title, state) for name, title, state in (
                ("review", "Pi · ready for review", "finished"),
                ("plan", "Pi · awaiting input", "attention"),
                ("logs", "Build logs", None),
            )]
            tmux.run("select-window", "-t", primary)
            tmux.run("select-pane", "-t", primary)
            for pane, title, state in background:
                if state:
                    tmux.report_agent_state(pane, state)
                display(tmux, pane, title, [f"  {title}"], project / "main", bell=True)
            tmux.run("set-option", "-g", "status-style", "bg=default,fg=colour245")
            tmux.run("set-option", "-g", "window-status-current-style", "bold,fg=colour117,bg=colour236")
            tmux.run("set-option", "-g", "pane-border-style", "fg=colour238")
            tmux.run("set-option", "-g", "pane-active-border-style", "fg=colour238")
            command = shlex.join(["tmux", "-S", tmux.socket, "attach-session", "-t", session])
            tape = base / "demo.tape"
            tape.write_text("\n".join([
                "Set Shell bash", "Set FontFamily \"DejaVu Sans Mono\"",
                "Set FontSize 18", "Set Width 1440", "Set Height 840",
                "Set Padding 16", "Set Theme \"Catppuccin Mocha\"",
                "Hide", "Type " + json.dumps(command), "Enter", "Sleep 5s", "Show",
                "Screenshot " + json.dumps(str(output)), "Sleep 100ms", "",
            ]))
            subprocess.run(["vhs", str(tape)], check=True)
            if not output.is_file():
                raise RuntimeError("VHS did not produce the screenshot")
            print(f"Screenshot: {output}")
        finally:
            tmux.run("kill-server", missing_ok=True)


if __name__ == "__main__":
    main()
