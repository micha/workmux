"""tmux operations; no terminal rendering or global mutable state."""

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .layouts import Cell, LAYOUTS, content_needs_balance, parse_layout, protect_sidebar, workspace_layout

if TYPE_CHECKING:
    from .integration import EnableResult


class WorkmuxError(Exception):
    """An actionable error suitable for display at the CLI boundary."""


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    label: str
    directory: str


AGENT_STATES = ("idle", "working", "attention", "finished")
_AGENT_PRIORITY = {"finished": 1, "working": 2, "attention": 3}


@dataclass(frozen=True)
class Tab:
    session: str
    id: str
    index: int
    name: str
    state: str
    pane: str | None = None


def _agent_state(value: str, alive: dict[int, bool]) -> str | None:
    try:
        report = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(report, dict):
        return None
    state = report.get("state")
    if not isinstance(state, str) or state not in _AGENT_PRIORITY:
        return None
    pid = report.get("pid")
    if pid is not None:
        if type(pid) is not int or pid <= 0:
            return None
        if pid not in alive:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive[pid] = False
            except PermissionError:
                alive[pid] = True
            except (OSError, OverflowError):
                alive[pid] = False
            else:
                alive[pid] = True
        if not alive[pid]:
            return None
    return state


@dataclass(frozen=True)
class Pane:
    id: str
    session: str
    window: str
    sidebar: bool
    active: bool
    width: int
    window_width: int
    zoomed: bool
    directory: str
    preferred_sidebar_width: int = 36


def validate_label(label: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}", label):
        raise WorkmuxError("Use 1–64 letters, digits, spaces, underscores or hyphens; start with a letter or digit.")
    return label


def validate_directory(directory: str) -> str:
    path = Path(directory).expanduser().resolve()
    if not path.is_dir():
        raise WorkmuxError(f"Directory does not exist: {path}")
    if any(character in str(path) for character in "\t\r\n"):
        raise WorkmuxError("Workspace directories cannot contain tabs or newlines.")
    return str(path)


def sidebar_width(window_width: int, preferred: int = 36) -> int:
    return min(max(4, preferred), max(4, window_width - 20))


class Tmux:
    def __init__(self, *, server: str = "workmux", socket: str | None = None):
        self.server = server
        self.socket = socket

    @property
    def selector(self) -> list[str]:
        return ["-S", self.socket] if self.socket else ["-L", self.server]

    def run(self, *args: str, input: str | None = None, missing_ok: bool = False) -> str:
        try:
            result = subprocess.run(
                ["tmux", *self.selector, "-f", os.devnull, *args], input=input, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
        except FileNotFoundError as error:
            raise WorkmuxError("tmux is not installed. Install tmux 3.3 or newer.") from error
        if result.returncode:
            message = result.stderr.strip()
            # An empty, not-yet-started dedicated server is normal for discovery.
            if missing_ok and ("no server running" in message or (
                "error connecting" in message and (
                    "No such file or directory" in message or "Connection refused" in message
                )
            )):
                return ""
            raise WorkmuxError(message or f"tmux command failed: {args[0]}")
        return result.stdout.rstrip("\n")

    def enable(self, target: str, client: str | None = None) -> "EnableResult":
        try:
            from .integration import Integration
        except ModuleNotFoundError as error:
            if error.name not in ("tree_sitter", "tree_sitter_tmux"):
                raise
            raise WorkmuxError("Install workmux's dependencies, or use nix-shell for checkout usage.") from error
        return Integration(self).enable(target, client)

    def dispatch(self, token: str, target: str) -> None:
        from .integration import Integration
        Integration(self).dispatch(token, target)

    def helper(self, *args: str, portable: bool = False) -> str:
        selector = ["--socket", self.socket] if self.socket else ["--server", self.server]
        if portable:
            return shlex.join(["workmux", *selector, *args])
        package_root = str(Path(__file__).resolve().parent.parent)
        # Pin module discovery explicitly: tmux may have been started without the
        # pipx environment or Nix wrapper's PYTHONPATH, and may run from any directory.
        return shlex.join(["env", f"PYTHONPATH={package_root}", sys.executable, "-m", "workmux",
                           *selector, *args])

    def workspaces(self) -> list[Workspace]:
        output = self.run(
            "list-sessions", "-F",
            "#{session_id}\t#{session_name}\t#{@workspace-label}\t#{@workspace-dir}\t#{@workmux}",
            missing_ok=True,
        )
        result = []
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) == 5 and fields[4] == "1":
                result.append(Workspace(*fields[:4]))
        return sorted(result, key=lambda workspace: workspace.name)

    def report_agent_state(self, target: str, state: str, pid: int | None = None) -> None:
        if state not in AGENT_STATES:
            raise WorkmuxError(f"Unknown agent state: {state}")
        if pid is not None and pid <= 0:
            raise WorkmuxError("Agent PID must be positive.")
        pane = self.pane(target)
        if pane.sidebar:
            raise WorkmuxError("Agent state must target a content pane, not a sidebar.")
        # Reports can precede enable; only managed workspaces display them.
        if state == "idle":
            self.run("set-option", "-pu", "-t", pane.id, "@workmux-agent")
        else:
            report = {"state": state}
            if pid is not None:
                report["pid"] = pid
            self.run("set-option", "-p", "-t", pane.id, "@workmux-agent", json.dumps(report))

    def attention_tabs(self) -> list[Tab]:
        """Aggregate agent reports and native alerts in one server-wide snapshot."""
        output = self.run(
            "list-panes", "-a", "-F",
            "#{session_id}\t#{window_id}\t#{window_index}\t#{window_flags}\t#{pane_id}"
            "\t#{@workspace-sidebar}\t#{pane_dead}\t#{@workmux-agent}\t#{window_name}",
            missing_ok=True,
        )
        tabs = {}
        alive = {}
        for line in output.splitlines():
            fields = line.split("\t", 8)
            if len(fields) != 9:
                continue
            session, window, index, flags, pane, sidebar, dead, report, name = fields
            try:
                index = int(index)
            except ValueError:
                continue
            key = (session, window)
            native = "alert" if any(flag in flags for flag in "!#~") else "idle"
            current = tabs.get(key, Tab(session, window, index, name, native))
            state = _agent_state(report, alive) if sidebar != "1" and dead != "1" else None
            # Passive agent states classify pending alerts, not permanent tree rows.
            # Keep working visible independently, but hide attention/finished after
            # tmux acknowledges the window's bell/activity/silence alert.
            if state in ("attention", "finished") and native != "alert":
                state = None
            if state and _AGENT_PRIORITY[state] > _AGENT_PRIORITY.get(current.state, 0):
                current = Tab(session, window, index, name, state, pane)
            tabs[key] = current
        return sorted((tab for tab in tabs.values() if tab.state != "idle"),
                      key=lambda tab: (tab.session, tab.index))

    @staticmethod
    def _pane_format() -> str:
        return ("#{pane_id}\t#{session_id}\t#{window_id}\t#{@workspace-sidebar}\t#{pane_active}"
                "\t#{pane_width}\t#{window_width}\t#{window_zoomed_flag}\t#{pane_current_path}"
                "\t#{@workmux-sidebar-width}")

    @staticmethod
    def _parse_pane(line: str, target: str) -> Pane:
        fields = line.split("\t")
        if len(fields) != 10 or not fields[0]:
            raise WorkmuxError(f"Cannot resolve pane: {target}")
        try:
            preferred_width = int(fields[9]) if fields[9] else 36
        except ValueError:
            preferred_width = 36
        return Pane(fields[0], fields[1], fields[2], fields[3] == "1", fields[4] == "1",
                    int(fields[5]), int(fields[6]), fields[7] == "1", fields[8], preferred_width)

    def pane(self, target: str) -> Pane:
        line = self.run("display-message", "-p", "-t", target, self._pane_format())
        return self._parse_pane(line, target)

    def panes(self, window: str) -> list[Pane]:
        # A single snapshot avoids resolving IDs individually while shells exit.
        lines = self.run("list-panes", "-t", window, "-F", self._pane_format()).splitlines()
        return [self._parse_pane(line, window) for line in lines]

    def require_workspace(self, session: str) -> Workspace:
        for workspace in self.workspaces():
            if workspace.id == session:
                return workspace
        raise WorkmuxError("This session is not a workmux workspace.")

    def content_or_none(self, pane: Pane) -> Pane | None:
        if not pane.sidebar:
            return pane
        candidates = [item for item in self.panes(pane.window) if not item.sidebar]
        saved = self.run("show-options", "-wqv", "-t", pane.window, "@workspace-last-content")
        for candidate in candidates:
            if candidate.id == saved:
                return candidate
        return candidates[0] if candidates else None

    def content(self, pane: Pane) -> Pane:
        content = self.content_or_none(pane)
        if content is None:
            raise WorkmuxError("This window has no content pane. Create a new tab.")
        return content

    def close_empty_window(self, pane: Pane) -> None:
        """Close an owned sidebar-only tab, including the workspace's final tab."""
        # Recheck in tmux's command queue: a content pane may have been added since
        # the sidebar's snapshot. Unlink only this session's window, not other links.
        condition = ("#{&&:#{==:#{@workmux},1},"
                     "#{&&:#{==:#{@workspace-sidebar},1},#{==:#{window_panes},1}}}")
        window = pane.session + ":" + pane.window
        target = window + "." + pane.id
        self.run("if-shell", "-F", "-t", target, condition,
                 "unlink-window -k -t " + shlex.quote(window))

    def add_sidebar(self, content_id: str) -> str:
        pane = self.pane(content_id)
        width = sidebar_width(pane.window_width, pane.preferred_sidebar_width)
        sidebar = self.run(
            "split-window", "-hbf", "-d", "-l", str(width),
            "-t", content_id, "-P", "-F", "#{pane_id}", self.helper("sidebar", "run"),
        )
        try:
            self.run("set-option", "-p", "-t", sidebar, "@workspace-sidebar", "1")
            self.run("set-option", "-w", "-t", pane.window, "@workspace-last-content", content_id)
            self.run("select-pane", "-t", content_id)
        except WorkmuxError:
            self.run("kill-pane", "-t", sidebar)
            raise
        return sidebar

    def new_workspace(self, label: str, directory: str) -> str:
        label = validate_label(label)
        directory = validate_directory(directory)
        existing = self.workspaces()
        server_has_sessions = bool(self.run("list-sessions", "-F", "#{session_id}", missing_ok=True))
        dedicated = self.run("show-options", "-sqv", "@workmux-configured", missing_ok=True) == "1"
        if any(workspace.label == label for workspace in existing):
            raise WorkmuxError(f"Workspace already exists: {label}")
        prefixes = [workspace.name.split("-", 1)[0] for workspace in existing]
        numbers = [int(prefix) for prefix in prefixes if prefix.isascii() and prefix.isdigit()]
        name = f"{max(numbers, default=0) + 1:06d}-{label}"
        # Keep the bootstrap window large enough even when no client is attached.
        content = self.run("new-session", "-d", "-s", name, "-c", directory,
                           "-x", "120", "-y", "40", "-P", "-F", "#{pane_id}")
        session = self.pane(content).session
        try:
            for option, value in (("@workmux", "1"), ("@workspace-label", label),
                                  ("@workspace-dir", directory)):
                self.run("set-option", "-t", session, option, value)
            integrated = self.run("show-options", "-sqv", "@workmux-integrated") == "1"
            if integrated:
                self.run("set-option", "-t", session, "@workmux-enabled", "1")
            elif server_has_sessions and not dedicated:
                self.enable(content)
                return session
            else:
                self._configure_defaults()
            self.add_sidebar(content)
        except WorkmuxError:
            self.run("kill-session", "-t", session)
            raise
        return session

    def new_tab(self, target: str, name: str) -> str:
        validate_label(name)
        pane = self.pane(target)
        workspace = self.require_workspace(pane.session)
        content = self.run("new-window", "-d", "-t", workspace.id + ":", "-n", name,
                           "-c", workspace.directory, "-P", "-F", "#{pane_id}")
        try:
            self.add_sidebar(content)
        except WorkmuxError:
            self.run("kill-window", "-t", self.pane(content).window)
            raise
        self.run("select-window", "-t", self.pane(content).window)
        return content

    def toggle_sidebar(self, target: str) -> None:
        pane = self.pane(target)
        self.require_workspace(pane.session)
        if pane.sidebar:
            content = self.content_or_none(pane)
            if content is None:
                return  # The sidebar may briefly be alone while its tab is closing.
            destination = content.id
        else:
            self.run("set-option", "-w", "-t", pane.window, "@workspace-last-content", pane.id)
            sidebars = [item for item in self.panes(pane.window) if item.sidebar]
            if not sidebars:
                raise WorkmuxError("No sidebar in this window. Create tabs with workmux tab new.")
            destination = sidebars[0].id
        self.run("select-pane", "-t", destination)

    def split(self, target: str, direction: str) -> str:
        pane = self.pane(target)
        self.require_workspace(pane.session)
        content = self.content(pane)
        created = self.run("split-window", "-h" if direction == "right" else "-v",
                           "-t", content.id, "-c", content.directory, "-P", "-F", "#{pane_id}")
        self.run("set-option", "-w", "-t", pane.window, "@workspace-last-content", created)
        return created

    def retile(self, target: str, preset: str) -> None:
        if preset == "balance":
            self.balance(target)
            return
        pane = self.pane(target)
        self.require_workspace(pane.session)
        panes = self.panes(pane.window)
        sidebars = [item for item in panes if item.sidebar]
        if len(sidebars) != 1:
            raise WorkmuxError("Retiling requires exactly one sidebar in the window.")
        sidebar = sidebars[0]
        content_ids = [item.id for item in panes if not item.sidebar]
        saved = self.run("show-options", "-wqv", "-t", pane.window, "@workspace-pane-order").split()
        order = [pane_id for pane_id in saved if pane_id in content_ids]
        order.extend(pane_id for pane_id in content_ids if pane_id not in order)
        if preset in ("next", "previous"):
            current = self.run("show-options", "-wqv", "-t", pane.window, "@workspace-layout")
            index = LAYOUTS.index(current) if current in LAYOUTS else 0
            preset = LAYOUTS[(index + (1 if preset == "next" else -1)) % len(LAYOUTS)]
        width, height = map(int, self.run(
            "display-message", "-p", "-t", pane.window, "#{window_width} #{window_height}",
        ).split())
        main_width = self.run("display-message", "-p", "-t", pane.window, "#{main-pane-width}")
        main_height = self.run("display-message", "-p", "-t", pane.window, "#{main-pane-height}")
        try:
            layout = workspace_layout(
                preset, sidebar.id, order, width, height,
                sidebar_width(width, pane.preferred_sidebar_width), main_width, main_height,
            )
        except ValueError as error:
            raise WorkmuxError(str(error)) from error

        self._apply_layout(pane, panes, layout,
                           "set-option", "-w", "-t", pane.window, "@workspace-layout", preset, ";",
                           "set-option", "-w", "-t", pane.window, "@workspace-pane-order", " ".join(order))

    def balance(self, target: str) -> None:
        pane = self.pane(target)
        self.require_workspace(pane.session)
        panes = self.panes(pane.window)
        sidebars = [item for item in panes if item.sidebar]
        if len(sidebars) != 1:
            raise WorkmuxError("Balancing requires exactly one sidebar in the window.")
        scrollbars = self.run("display-message", "-p", "-t", pane.id, "#{pane-scrollbars}")
        if scrollbars not in ("", "off", "0"):
            raise WorkmuxError("Pane balancing with scrollbars is unsupported; use a named workmux layout.")
        content = self.content(pane)
        border = self.run("display-message", "-p", "-t", pane.id, "#{pane-border-status}")
        raw = self.run("display-message", "-p", "-t", pane.id, "#{window_layout}")
        try:
            layout = protect_sidebar(parse_layout(raw), sidebars[0].id)
            needs_balance = content_needs_balance(layout, content.id, border)
        except ValueError as error:
            raise WorkmuxError(str(error)) from error
        # Native -E climbs to ancestors until it finds an uneven group. Do not
        # call it when all content ancestors are even: it would resize the sidebar.
        tail = ("select-layout", "-E", "-t", content.id) if needs_balance else ()
        self._apply_layout(pane, panes, layout, *tail)

    def _apply_layout(self, pane: Pane, panes: list[Pane], layout: Cell, *tail: str) -> None:
        actual = [item.id for item in panes]
        if set(actual) != set(layout.pane_ids()):
            raise WorkmuxError("Panes changed while arranging the layout. Retry the operation.")
        commands = []
        if pane.zoomed:
            commands.extend(["resize-pane", "-Z", "-t", pane.id, ";"])
        # Legacy select-layout assigns cells by pane-list order, ignoring serialized IDs.
        # Arrange that order to match the tree, including mirrored main-pane layouts.
        for index, desired in enumerate(layout.pane_ids()):
            if actual[index] != desired:
                other = actual.index(desired)
                commands.extend(["swap-pane", "-d", "-s", desired, "-t", actual[index], ";"])
                actual[index], actual[other] = actual[other], actual[index]
        active = next(item.id for item in panes if item.active)
        commands.extend([
            "select-layout", "-t", pane.window, layout.dump(), ";",
            "select-pane", "-t", active,
        ])
        if tail:
            commands.extend([";", *tail])
        self.run(*commands)

    def select_workspace(self, session: str, source_pane: str, client: str | None = None) -> None:
        self.require_workspace(session)
        source = self.pane(source_pane)
        self.require_workspace(source.session)
        if client is None:
            clients = self.run("list-clients", "-t", source.session, "-F", "#{client_name}").splitlines()
            if len(clients) != 1:
                raise WorkmuxError("Workspace selection requires exactly one attached client in this session.")
            client = clients[0]
        self.run("switch-client", "-c", client, "-t", session)
        # Retain the destination's selected tab and active content pane.
        active = self.pane(session + ":")
        if active.sidebar:
            content = self.content_or_none(active)
            if content is not None:
                self.run("select-pane", "-t", content.id)

    def select_tab(self, session: str, window: str, source_pane: str, client: str | None = None) -> None:
        self.require_workspace(session)
        source = self.pane(source_pane)
        self.require_workspace(source.session)
        # A session-qualified ID rejects stale rows or a window from another workspace.
        target = session + ":" + window
        panes = self.panes(target)
        if not panes or any(pane.session != session or pane.window != window for pane in panes):
            raise WorkmuxError("This tab no longer belongs to the selected workspace.")
        if client is None:
            clients = self.run("list-clients", "-t", source.session, "-F", "#{client_name}").splitlines()
            if len(clients) != 1:
                raise WorkmuxError("Tab selection requires exactly one attached client in this session.")
            client = clients[0]
        preferred = next((tab.pane for tab in self.attention_tabs()
                          if tab.session == session and tab.id == window), None)
        # Select the destination tab before switching sessions, avoiding an unrelated
        # tab being focused (and having its native alerts acknowledged) on the way.
        self.run("select-window", "-t", target)
        if source.session != session:
            self.run("switch-client", "-c", client, "-t", session)
        active = self.pane(target)
        content = next((pane for pane in panes if pane.id == preferred and not pane.sidebar), None)
        content = content or self.content_or_none(active)
        if content is not None:
            self.run("select-pane", "-t", content.id)

    def repair_width(self, target: str, previous: Pane | None = None) -> Pane:
        pane = self.pane(target)
        if pane.sidebar and not pane.zoomed:
            count = self.run("display-message", "-p", "-t", pane.id, "#{window_panes}")
            if count == "1":
                return pane  # Empty tabs are closed by the sidebar; never resize a sole pane.
            desired = sidebar_width(pane.window_width, pane.preferred_sidebar_width)
            if pane.width != desired:
                manually_resized = (
                    previous is not None
                    and pane.window_width == previous.window_width
                    and pane.preferred_sidebar_width == previous.preferred_sidebar_width
                    and pane.width != previous.width
                )
                if manually_resized:
                    self.run("set-option", "-g", "@workmux-sidebar-width", str(pane.width))
                    return replace(pane, preferred_sidebar_width=pane.width)
                self.run("resize-pane", "-t", pane.id, "-x", str(desired))
                return replace(pane, width=desired)
        return pane

    def _default_config(self) -> str:
        context = ["--pane", "#{pane_id}"]

        def binding(key: str, *args: str) -> str:
            # Successful CLI output would otherwise open tmux's output view in the source pane.
            command = self.helper(*args, *context, portable=True) + " >/dev/null"
            # tmux configuration quoting is separate from shell quoting.
            quoted = '"' + command.replace("\\", "\\\\").replace('"', '\\"') + '"'
            return f"bind-key {key} run-shell -b {quoted}"

        layout_bindings = [
            binding("M-1", "layout", "even-horizontal"),
            binding("M-2", "layout", "even-vertical"),
            binding("M-3", "layout", "main-horizontal"),
            binding("M-4", "layout", "main-vertical"),
            binding("M-5", "layout", "tiled"),
            binding("M-6", "layout", "main-horizontal-mirrored"),
            binding("M-7", "layout", "main-vertical-mirrored"),
            binding("Space", "layout", "next"),
            binding("M-Space", "layout", "previous"),
            binding("E", "layout", "balance"),
        ]
        return "\n".join([
            "# Internal defaults for a dedicated workmux server.",
            "set-option -g status-position top",
            "set-option -g mouse on",
            "set-option -g status-left ''",
            "set-option -g status-right ''",
            "set-option -g status-justify left",
            "set-option -g window-status-format ' #I:#W '",
            "set-option -g window-status-current-format ' #I:#W '",
            "set-option -g window-status-current-style 'bold,reverse'",
            "set-option -g allow-rename off",
            "bind-key Up switch-client -p",
            "bind-key Down switch-client -n",
            "bind-key Left previous-window",
            "bind-key Right next-window",
            binding("c", "tab", "new"),
            binding("s", "sidebar", "toggle-focus"),
            binding('%', "split", "right"),
            binding('\\\"', "split", "down"),
            *layout_bindings,
            "",
        ])

    def _configure_defaults(self) -> None:
        configured = self.run("show-options", "-sqv", "@workmux-configured")
        if configured != "1":
            self.run("source-file", "-", input=self._default_config())
            self.run("set-option", "-s", "@workmux-configured", "1")
