"""Command-line entrypoint for workspace creation and tmux keybindings."""

import argparse
import json
import os
import re
import subprocess
import sys

from . import completion
from .core import AGENT_STATES, TMUX_EXECUTABLE, Tmux, WorkmuxError, tmux_socket
from .layouts import LAYOUTS
from .plugins import HARNESSES


def add_pane(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pane", default=os.environ.get("TMUX_PANE"),
                        help="Source pane ID (defaults to TMUX_PANE inside a pane)").completer = completion.panes


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Workspace sidebar and sidebar-aware tiling for tmux.")
    selector = result.add_mutually_exclusive_group()
    selector.add_argument(
        "--server", help="tmux server name (default: current server inside tmux, else workmux)",
    ).completer = completion.servers
    selector.add_argument("--socket", help="Explicit tmux socket path").completer = completion.sockets
    commands = result.add_subparsers(dest="command", required=True)

    shell_completion = commands.add_parser("completion", help="Print a shell completion script")
    shell_completion.add_argument("shell", choices=("bash",))

    for action, description in (("install", "List supported harnesses or install an agent plugin"),
                                ("uninstall", "Remove an intact workmux agent plugin")):
        plugin = commands.add_parser(action, help=description)
        plugin.add_argument("plugin", choices=HARNESSES, nargs="?" if action == "install" else None,
                            help="Agent integration to manage (omit for install to list status)")
        plugin.add_argument(
            "--agent-dir", help="Override the selected harness's personal configuration directory",
        ).completer = completion.directories

    workspace = commands.add_parser("workspace", help="Create and list workspaces")
    actions = workspace.add_subparsers(dest="action", required=True)
    new = actions.add_parser("new", help="Create a workspace with one tab and sidebar")
    creation = new.add_mutually_exclusive_group(required=True)
    creation.add_argument("name", nargs="?", help="Workspace name")
    creation.add_argument("--interactive", action="store_true",
                          help="Prompt for a directory (and a name if needed), then open or create the workspace")
    new.add_argument(
        "--dir", default=".", help="Workspace directory (prompt default in interactive mode)",
    ).completer = completion.directories
    new.add_argument("--attach", action="store_true", help="Attach after creating the workspace (outside tmux only)")
    listing = actions.add_parser("list", help="List managed workspaces in sidebar order")
    listing.add_argument("--json", action="store_true")
    select = actions.add_parser("select", help="Select a workspace for an attached client")
    selection = select.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "target", nargs="?", help="Workspace ID, session name, or display label",
    ).completer = completion.workspaces
    selection.add_argument(
        "--index", type=int, help="1-based workspace index shown in the sidebar",
    ).completer = completion.workspace_indexes
    select.add_argument("--client").completer = completion.clients
    add_pane(select)

    attach = commands.add_parser("attach", help="Attach to an existing workspace from outside tmux")
    attach.add_argument("target", nargs="?").completer = completion.workspaces

    tab = commands.add_parser("tab", help="Create a tab with a sidebar")
    tabs = tab.add_subparsers(dest="action", required=True)
    new_tab = tabs.add_parser("new")
    new_tab.add_argument("name", nargs="?", default="terminal")
    add_pane(new_tab)

    team = commands.add_parser("team", help="Launch and manage a three-pane Pi team")
    teams = team.add_subparsers(dest="action", required=True)
    new_team = teams.add_parser("new", help="Start a team in a new tab after the calling tab")
    new_team.add_argument(
        "--launcher", help="Worker launcher executable for managed teams on Linux (requires --profile)",
    ).completer = completion.executables
    new_team.add_argument("--profile", help="Initial worker launcher profile (requires --launcher)")
    reset_team = teams.add_parser("reset", help="Replace both managed workers from their master pane")
    reset_team.add_argument("--profile", required=True, help="Worker launcher profile for the replacement pair")
    team_status = teams.add_parser("status", help="Show role assignments for a team")
    team_status.add_argument("--json", action="store_true", help="Print the current managed worker PID pair or null")
    add_pane(team_status)
    add_pane(teams.add_parser("refresh", help="Publish tmux format variables for an existing team"))
    add_pane(teams.add_parser("clear", help="Forget a team without closing any pane"))

    split = commands.add_parser("split", help="Split content without splitting the sidebar")
    split.add_argument("direction", choices=("right", "down"))
    add_pane(split)

    layout = commands.add_parser("layout", help="Retile content while keeping the sidebar on the left")
    layout.add_argument("preset", choices=(*LAYOUTS, "next", "previous", "balance"))
    add_pane(layout)

    sidebar = commands.add_parser("sidebar", help="Run, focus, or reload sidebars")
    sidebars = sidebar.add_subparsers(dest="action", required=True)
    for action in ("run", "toggle-focus"):
        add_pane(sidebars.add_parser(action))
    sidebars.add_parser("reload", help="Restart all owned sidebar panes on the selected tmux server")

    pi = commands.add_parser("pi", help="Manage registered interactive Pi instances")
    pi_actions = pi.add_subparsers(dest="action", required=True)
    reload_pi = pi_actions.add_parser("reload", help="Request /reload in ready Pi panes on the selected tmux server")
    reload_pi.add_argument(
        "--popup", metavar="CLIENT", help="Show the completed results in a tmux popup using less",
    ).completer = completion.clients

    agent = commands.add_parser("agent", help="Report an agent's state for sidebar tab indicators")
    agents = agent.add_subparsers(dest="action", required=True)
    state = agents.add_parser("state", help="Set or clear the agent state of a content pane")
    state.add_argument("state", choices=AGENT_STATES)
    state.add_argument("--pid", type=int, help="Agent process ID; ignore stale reports after it exits")
    state.add_argument(
        "--root", action="append", help="Absolute harness root directory for checkout grouping; repeat for multiple roots",
    ).completer = completion.directories
    add_pane(state)
    hook = agents.add_parser("hook", help="Internal best-effort Codex/Claude command hook")
    hook.add_argument("harness", choices=("codex", "claude"))
    hook.add_argument("--pid", required=True, type=int, help="Harness process ID supplied by its hook shell")

    enable = commands.add_parser("enable", help="Enable or reconcile workmux in an existing tmux session")
    enable.add_argument(
        "--client", help="Invoking client; validates the source session when supplied",
    ).completer = completion.clients
    add_pane(enable)
    dispatch = commands.add_parser("dispatch", help="Internal dispatcher for adapted keybindings")
    dispatch.add_argument("--binding", required=True)
    add_pane(dispatch)
    return result


def require_pane(args) -> str:
    if not args.pane:
        raise WorkmuxError("Pass --pane PANE_ID, or run this command inside a workmux pane.")
    return args.pane


def resolve_workspace(tmux: Tmux, target: str | None, *, index: int | None = None) -> str:
    workspaces = tmux.workspaces()
    if not workspaces:
        raise WorkmuxError("No workspaces. Create one with: workmux workspace new NAME --dir DIRECTORY")
    if index is not None:
        if not 1 <= index <= len(workspaces):
            raise WorkmuxError(f"Workspace index out of range: {index} (expected 1-{len(workspaces)})")
        return workspaces[index - 1].id
    if target is None:
        return workspaces[0].id
    matches = [workspace.id for workspace in workspaces if target in (
        workspace.id, workspace.name, workspace.label,
    )]
    if len(matches) != 1:
        raise WorkmuxError(f"Unknown or ambiguous workspace: {target}")
    return matches[0]


def workspace_creation_client(tmux: Tmux, session_number: str) -> str:
    # A pane may have moved since TMUX was set. Popups have a separate tty and
    # can inherit a stale TMUX_PANE; for them use the session ID in TMUX instead.
    tty = os.ttyname(sys.stdin.fileno())
    panes = tmux.run("list-panes", "-a", "-F", "#{pane_tty}\t#{session_id}").splitlines()
    sessions = {line.partition("\t")[2] for line in panes if line.startswith(tty + "\t")}
    source = "$" + session_number if session_number.isascii() and session_number.isdecimal() else ""
    if len(sessions) == 1:
        source, = sessions
    if not source or (sessions and source not in sessions):
        raise WorkmuxError("Cannot identify the current tmux session.")
    clients = tmux.run("list-clients", "-t", source, "-F", "#{client_name}").splitlines()
    if len(clients) != 1:
        raise WorkmuxError("Interactive workspace creation requires exactly one attached client in this session.")
    return clients[0]


def attach(tmux: Tmux, session: str) -> int:
    if os.environ.get("TMUX"):
        raise WorkmuxError("Already inside tmux. Use workspace select, or attach from a terminal outside tmux.")
    return subprocess.call([TMUX_EXECUTABLE, *tmux.selector, "attach-session", "-t", session])


def main(argv: list[str] | None = None) -> int:
    arguments = parser()
    try:
        if argv is None and os.environ.get("_ARGCOMPLETE"):
            completion.autocomplete(arguments)
        args = arguments.parse_args(argv)
        if args.command == "completion":
            print(completion.bash_script())
            return 0
        if args.command in ("install", "uninstall"):
            from .plugins import install_plugin, plugin_status, uninstall_plugin
            if args.command == "install" and args.plugin is None:
                entries = [(harness, *plugin_status(harness, args.agent_dir)) for harness in HARNESSES]
                name_width = max(len(harness) for harness, _, _ in entries)
                status_width = max(len(status) for _, status, _ in entries)
                print("Supported harnesses:")
                for harness, status, path in entries:
                    print(f"  {harness:<{name_width}}  {status:<{status_width}}  {path}")
                return 0
            operation = install_plugin if args.command == "install" else uninstall_plugin
            print(operation(args.plugin, args.agent_dir))
            return 0
        if args.command == "agent" and args.action == "hook":
            from .agent_hooks import run_hook
            run_hook(args.harness, args.pid)
            return 0
        current_tmux = os.environ.get("TMUX", "").rsplit(",", 2)
        current_socket = current_tmux[0]
        socket = tmux_socket(args.server, args.socket)
        if args.command == "team" and args.action in ("new", "reset"):
            if args.action == "new" and (args.launcher is None) != (args.profile is None):
                raise WorkmuxError("Pass --launcher and --profile together to start a managed team.")
            origin = "the master pane" if args.action == "reset" else "a workmux pane"
            source = os.environ.get("TMUX_PANE", "")
            if (len(current_tmux) != 3 or not os.path.isabs(current_socket) or
                    not all(re.fullmatch(r"[0-9]+", field) for field in current_tmux[1:]) or
                    not re.fullmatch(r"%[0-9]+", source)):
                raise WorkmuxError(f"Run team {args.action} from {origin} with TMUX and TMUX_PANE set.")
            if args.server or (args.socket is not None and
                               os.path.realpath(args.socket) != os.path.realpath(current_socket)):
                raise WorkmuxError(f"Team {args.action} uses {origin}'s tmux server. Omit --server and --socket.")
            tmux = Tmux(socket=current_socket, start_server=False)
            if tmux.run("display-message", "-p", "-t", source, "#{pid}") != current_tmux[1]:
                raise WorkmuxError(f"The tmux server for {origin} has changed. Run team {args.action} from {origin}.")
        else:
            tmux = Tmux(server=args.server or "workmux", socket=socket)
        if args.command == "enable":
            result = tmux.enable(require_pane(args), args.client)
            if args.client:
                message = (f"workmux: {result.sidebars_added} sidebars added, "
                           f"{result.bindings_adapted} bindings adapted"
                           f", {len(result.warnings)} adaptation warnings")
                tmux.run("display-message", "-c", args.client, message)
            else:
                for warning in result.warnings:
                    print(f"workmux: {warning}", file=sys.stderr)
        elif args.command == "agent":
            roots = args.root[0] if args.root and len(args.root) == 1 else args.root
            tmux.report_agent_state(require_pane(args), args.state, args.pid, roots)
        elif args.command == "pi":
            from .pi_reload import reload_popup, reload_table
            if args.popup is None:
                print(reload_table(tmux))
            else:
                reload_popup(tmux, args.popup)
        elif args.command == "dispatch":
            tmux.dispatch(args.binding, require_pane(args))
        elif args.command == "workspace":
            if args.action == "new":
                if args.attach and current_socket:
                    raise WorkmuxError("Use --attach only from outside tmux.")
                name, directory = args.name, args.dir
                client = None
                if args.interactive:
                    from .workspace_prompt import prompt_workspace
                    selection = prompt_workspace(directory, tmux.workspace_name_taken)
                    if selection is None:
                        return 0
                    name, directory = selection
                    if socket and socket == current_socket:
                        session_number = current_tmux[2] if len(current_tmux) == 3 else ""
                        client = workspace_creation_client(tmux, session_number)
                session = tmux.new_workspace(name, directory, create_directory=args.interactive,
                                             reuse_existing=args.interactive)
                print(session, flush=True)
                if client is not None:
                    tmux.run("switch-client", "-c", client, "-t", session)
                if args.attach:
                    return attach(tmux, session)
            elif args.action == "list":
                workspaces = tmux.workspaces()
                if args.json:
                    print(json.dumps([vars(workspace) for workspace in workspaces]))
                else:
                    for workspace in workspaces:
                        print(f"{workspace.id}\t{workspace.label}\t{workspace.directory}")
            else:
                tmux.select_workspace(resolve_workspace(tmux, args.target, index=args.index),
                                      require_pane(args), args.client)
        elif args.command == "attach":
            return attach(tmux, resolve_workspace(tmux, args.target))
        elif args.command == "tab":
            print(tmux.new_tab(require_pane(args), args.name))
        elif args.command == "team":
            from .teams import clear_team, new_team, refresh_team, reset_team, team_status
            if args.action == "new":
                new_team(tmux, source, launcher=args.launcher, profile=args.profile)
            elif args.action == "reset":
                print(reset_team(tmux, source, args.profile))
            elif args.action == "status":
                print(team_status(tmux, require_pane(args), as_json=args.json))
            elif args.action == "refresh":
                refresh_team(tmux, require_pane(args))
            else:
                clear_team(tmux, require_pane(args))
        elif args.command == "split":
            print(tmux.split(require_pane(args), args.direction))
        elif args.command == "layout":
            tmux.retile(require_pane(args), args.preset)
        elif args.command == "sidebar":
            if args.action == "reload":
                print(f"Reloaded {tmux.reload_sidebars()} sidebar panes.")
            elif args.action == "toggle-focus":
                tmux.toggle_sidebar(require_pane(args))
            else:
                from .sidebar import run_sidebar
                run_sidebar(tmux, require_pane(args))
    except ModuleNotFoundError as error:
        if error.name not in ("tree_sitter", "tree_sitter_tmux"):
            raise
        print("workmux: Install the updated package and its dependencies, or use nix-shell for checkout usage.",
              file=sys.stderr)
        return 1
    except (WorkmuxError, OSError) as error:
        print(f"workmux: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
