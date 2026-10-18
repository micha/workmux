"""Command-line entrypoint for workspace creation and tmux keybindings."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from .core import AGENT_STATES, Tmux, WorkmuxError
from .layouts import LAYOUTS


def add_pane(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pane", default=os.environ.get("TMUX_PANE"),
                        help="Source pane ID (defaults to TMUX_PANE inside a pane)")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Workspace sidebar and sidebar-aware tiling for tmux.")
    selector = result.add_mutually_exclusive_group()
    selector.add_argument("--server", help="tmux server name (default: current server inside tmux, else workmux)")
    selector.add_argument("--socket", help="Explicit tmux socket path")
    commands = result.add_subparsers(dest="command", required=True)

    for action, description in (("install", "List supported harnesses or install an agent plugin"),
                                ("uninstall", "Remove an intact workmux agent plugin")):
        plugin = commands.add_parser(action, help=description)
        plugin.add_argument("plugin", choices=("pi",), nargs="?" if action == "install" else None,
                            help="Agent integration to manage (omit for install to list status)")
        plugin.add_argument("--agent-dir", help="Pi personal directory (default: PI_CODING_AGENT_DIR or ~/.pi/agent)")

    workspace = commands.add_parser("workspace", help="Create and list workspaces")
    actions = workspace.add_subparsers(dest="action", required=True)
    new = actions.add_parser("new", help="Create a workspace with one tab and sidebar")
    new.add_argument("name")
    new.add_argument("--dir", default=str(Path.cwd()))
    new.add_argument("--attach", action="store_true")
    listing = actions.add_parser("list", help="List managed workspaces in sidebar order")
    listing.add_argument("--json", action="store_true")
    select = actions.add_parser("select", help="Select a workspace for an attached client")
    select.add_argument("target", help="Workspace ID, numbered session name, or display label")
    select.add_argument("--client")
    add_pane(select)

    attach = commands.add_parser("attach", help="Attach to an existing workspace from outside tmux")
    attach.add_argument("target", nargs="?")

    tab = commands.add_parser("tab", help="Create a tab with a sidebar")
    tabs = tab.add_subparsers(dest="action", required=True)
    new_tab = tabs.add_parser("new")
    new_tab.add_argument("name", nargs="?", default="terminal")
    add_pane(new_tab)

    split = commands.add_parser("split", help="Split content without splitting the sidebar")
    split.add_argument("direction", choices=("right", "down"))
    add_pane(split)

    layout = commands.add_parser("layout", help="Retile content while keeping the sidebar on the left")
    layout.add_argument("preset", choices=(*LAYOUTS, "next", "previous", "balance"))
    add_pane(layout)

    sidebar = commands.add_parser("sidebar", help="Run or focus the sidebar")
    sidebars = sidebar.add_subparsers(dest="action", required=True)
    for action in ("run", "toggle-focus"):
        add_pane(sidebars.add_parser(action))

    agent = commands.add_parser("agent", help="Report an agent's state for sidebar tab indicators")
    agents = agent.add_subparsers(dest="action", required=True)
    state = agents.add_parser("state", help="Set or clear the agent state of a content pane")
    state.add_argument("state", choices=AGENT_STATES)
    state.add_argument("--pid", type=int, help="Agent process ID; ignore stale reports after it exits")
    add_pane(state)

    enable = commands.add_parser("enable", help="Enable or reconcile workmux in an existing tmux session")
    enable.add_argument("--client", help="Invoking client; validates the source session when supplied")
    add_pane(enable)
    dispatch = commands.add_parser("dispatch", help="Internal dispatcher for adapted keybindings")
    dispatch.add_argument("--binding", required=True)
    add_pane(dispatch)
    return result


def require_pane(args) -> str:
    if not args.pane:
        raise WorkmuxError("Pass --pane PANE_ID, or run this command inside a workmux pane.")
    return args.pane


def resolve_workspace(tmux: Tmux, target: str | None) -> str:
    workspaces = tmux.workspaces()
    if not workspaces:
        raise WorkmuxError("No workspaces. Create one with: workmux workspace new NAME --dir DIRECTORY")
    if target is None:
        return workspaces[0].id
    matches = [workspace.id for workspace in workspaces if target in (
        workspace.id, workspace.name, workspace.label,
    )]
    if len(matches) != 1:
        raise WorkmuxError(f"Unknown or ambiguous workspace: {target}")
    return matches[0]


def attach(tmux: Tmux, session: str) -> int:
    if os.environ.get("TMUX"):
        raise WorkmuxError("Already inside tmux. Use workspace select, or attach from a terminal outside tmux.")
    return subprocess.call(["tmux", *tmux.selector, "attach-session", "-t", session])


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in ("install", "uninstall"):
            from .plugins import install_pi, pi_status, uninstall_pi
            if args.command == "install" and args.plugin is None:
                status, path = pi_status(args.agent_dir)
                print(f"Supported harnesses:\n  pi\t{status}\t{path}")
                return 0
            operation = install_pi if args.command == "install" else uninstall_pi
            print(operation(args.agent_dir))
            return 0
        socket = args.socket
        if not socket and not args.server and os.environ.get("TMUX"):
            socket = os.environ["TMUX"].rsplit(",", 2)[0]
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
            tmux.report_agent_state(require_pane(args), args.state, args.pid)
        elif args.command == "dispatch":
            tmux.dispatch(args.binding, require_pane(args))
        elif args.command == "workspace":
            if args.action == "new":
                if args.attach and os.environ.get("TMUX"):
                    raise WorkmuxError("Use --attach from outside tmux, or omit it to create a detached workspace.")
                session = tmux.new_workspace(args.name, args.dir)
                print(session, flush=True)
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
                tmux.select_workspace(resolve_workspace(tmux, args.target), require_pane(args), args.client)
        elif args.command == "attach":
            return attach(tmux, resolve_workspace(tmux, args.target))
        elif args.command == "tab":
            print(tmux.new_tab(require_pane(args), args.name))
        elif args.command == "split":
            print(tmux.split(require_pane(args), args.direction))
        elif args.command == "layout":
            tmux.retile(require_pane(args), args.preset)
        elif args.command == "sidebar":
            pane = require_pane(args)
            if args.action == "toggle-focus":
                tmux.toggle_sidebar(pane)
            else:
                from .sidebar import run_sidebar
                run_sidebar(tmux, pane)
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
