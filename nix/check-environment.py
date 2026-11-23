"""Installed Nix launcher must not pin user shells to its private bin paths."""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time


launcher, tmux, shell, sleep, env_command = sys.argv[1:]
with tempfile.TemporaryDirectory(prefix="workmux-environment-") as temporary:
    root = Path(temporary)
    profile = root / "profile bin ; literal"
    profile.mkdir()
    (profile / "workmux").symlink_to(launcher)
    (profile / "env").symlink_to(env_command)
    # Deliberately omit Python and tmux: private dependencies must still work.
    path = str(profile)
    content_shell = root / "content-shell"
    content_shell.write_text(
        f"#!{shell}\n"
        f'if [ "$#" -gt 0 ]; then exec {shlex.quote(shell)} "$@"; fi\n'
        'printf "%s\\n" "$PATH" > "$HOME/path-$$"\n'
        'command -v workmux > "$HOME/workmux-$$"\n'
        'printf "%s\\n" "${PYTHONPATH-unset}" > "$HOME/pythonpath-$$"\n'
        f"exec {shlex.quote(sleep)} 600\n"
    )
    content_shell.chmod(0o755)
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700)
    env = {"HOME": temporary, "PATH": path, "SHELL": str(content_shell), "XDG_RUNTIME_DIR": str(runtime),
           "TERM": "xterm-256color", "LC_ALL": "C.UTF-8"}
    socket = str(root / "tmux.socket")

    def run(*args):
        return subprocess.check_output(args, env=env, text=True).strip()

    def workmux(*args):
        return run(launcher, "--socket", socket, *args)

    def native(*args):
        return run(tmux, "-S", socket, *args)

    try:
        session = workmux("workspace", "new", "test", "--dir", temporary)
        pane = native("list-panes", "-t", session, "-F",
                      "#{?@workspace-sidebar,,#{pane_id}}")
        tab = workmux("tab", "new", "second", "--pane", pane)
        workmux("split", "right", "--pane", tab)
        deadline = time.monotonic() + 5
        while len(list(root.glob("pythonpath-*"))) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(list(root.glob("path-*"))) == 3, list(root.iterdir())
        for captured in root.glob("path-*"):
            assert captured.read_text().strip() == path, captured.read_text()
        for captured in root.glob("workmux-*"):
            assert captured.read_text().strip() == str(profile / "workmux"), captured.read_text()
        for captured in root.glob("pythonpath-*"):
            assert captured.read_text().strip() == "unset", captured.read_text()
        # The actual sidebar processes must also start with the pinned Python
        # and module bootstrap, despite neither Python nor tmux being on PATH.
        deadline = time.monotonic() + 5
        while True:
            commands = native("list-panes", "-a", "-F",
                              "#{@workspace-sidebar}\t#{pane_dead}\t#{pane_current_command}"
                              "\t#{pane_start_command}")
            sidebars = [line for line in commands.splitlines() if line.startswith("1\t")]
            ready = len(sidebars) == 2 and all(
                line.startswith("1\t0\tpython") and "PYTHONPATH=" in line for line in sidebars)
            if ready or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        assert ready, commands
    except Exception:
        print("Captured shell files:", {p.name: p.read_text() for p in root.glob("*-*")
                                        if p.is_file()}, file=sys.stderr)
        subprocess.run([tmux, "-S", socket, "list-panes", "-a", "-F",
                        "#{pane_id}\t#{session_id}\t#{window_id}\t#{@workspace-sidebar}\t#{pane_active}"
                        "\t#{pane_width}\t#{window_width}\t#{window_zoomed_flag}\t#{pane_current_path}"
                        "\t#{@workmux-sidebar-width}"], env=env)
        raise
    finally:
        subprocess.run([tmux, "-S", socket, "kill-server"], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
