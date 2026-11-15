"""Opt-in adaptation of a running tmux session; configuration files stay untouched."""

from dataclasses import dataclass
import json
import re

from .bindings import (
    ALIASES, BindingParser, UnsupportedBinding, binding_token, command_options, quote_tmux, wrap_binding,
)
from .core import Tmux, WorkmuxError


STATE_OPTION = "@workmux-binding-state"
FOCUS_HOOK_OPTION = "@workmux-sidebar-focus-hook"
FOCUS_HOOK = (
    'if-shell -F "#{&&:#{==:#{@workmux},1},'
    '#{&&:#{==:#{@workspace-sidebar},1},#{&&:#{pane_active},#{>:#{window_panes},1}}}}" '
    '{ select-pane -l }'
)


def ensure_focus_hooks(tmux: Tmux, windows: list[str]) -> None:
    """Append owned runtime hooks without shadowing inherited user hooks.

    A local hook array overrides inherited hooks, so extend existing local arrays
    as well as the global default. Never create a local array merely to add ours.
    Native last-pane selection uses the hook's stable source context, avoiding
    a delayed helper stealing focus after a click has selected another tab.
    Only one sidebar is allowed per tab, so its previous pane is content.
    """
    tmux.run("wait-for", "-L", "workmux-focus-hooks")
    added = []
    try:
        # pane-focus-in is window/pane scoped. Unlike the hook itself, our user
        # option marker needs explicit -w to avoid defaulting to session scope.
        scopes = [("-gw",)]
        for window in windows:
            scopes.append(("-w", "-t", window))
            scopes.extend(("-p", "-t", pane.id) for pane in tmux.panes(window) if pane.sidebar)
        for scope in scopes:
            # The marker also remembers intentional removal/replacement: enabling
            # again must not resurrect a hook the user or a plugin has changed.
            if tmux.run("show-options", "-qv", *scope, FOCUS_HOOK_OPTION):
                continue
            current = tmux.run("show-hooks", *scope, "pane-focus-in")
            if not current and scope != ("-gw",):
                continue
            indexes = re.findall(r"^pane-focus-in\[(\d+)\] ", current, re.MULTILINE)
            hook = f"pane-focus-in[{max(map(int, indexes), default=-1) + 1}]"
            # -o refuses to replace an entry installed by a concurrent plugin.
            # Capture canonical text in the same command queue for safe rollback.
            applied = tmux.run("set-option", "-o", *scope, hook, FOCUS_HOOK,
                               ";", "show-options", "-qv", *scope, hook)
            added.append((scope, hook, applied))
            tmux.run("set-option", *scope, FOCUS_HOOK_OPTION, hook)
    except (WorkmuxError, KeyboardInterrupt):
        for scope, hook, applied in reversed(added):
            if tmux.run("show-options", "-qv", *scope, hook) == applied:
                tmux.run("set-hook", "-u", *scope, hook)
            if tmux.run("show-options", "-qv", *scope, FOCUS_HOOK_OPTION) == hook:
                tmux.run("set-option", "-u", *scope, FOCUS_HOOK_OPTION)
        raise
    finally:
        tmux.run("wait-for", "-U", "workmux-focus-hooks")


@dataclass(frozen=True)
class EnableResult:
    sidebars_added: int
    bindings_adapted: int
    warnings: tuple[str, ...]


class Integration:
    def __init__(self, tmux: Tmux):
        self.tmux = tmux
        self.parser = BindingParser()

    def state(self) -> dict:
        raw = self.tmux.run("show-options", "-sqv", STATE_OPTION)
        if not raw:
            return {"version": 1, "bindings": {}}
        try:
            state = json.loads(raw)
            if state["version"] != 1 or not isinstance(state["bindings"], dict):
                raise ValueError("unsupported state")
            for token, entry in state["bindings"].items():
                if not re.fullmatch(r"[0-9a-f]{24}", token) or not isinstance(entry, dict):
                    raise ValueError("invalid binding record")
                if not all(isinstance(entry.get(field), str) for field in ("original", "applied")):
                    raise ValueError("invalid binding fields")
                if binding_token(*self.parser.identity(entry["original"])) != token:
                    raise ValueError("binding identity mismatch")
            return state
        except (KeyError, TypeError, ValueError) as error:
            raise WorkmuxError(
                "Invalid workmux binding state. Restore the server state before enabling again."
            ) from error

    def current_bindings(self, warnings: list[str] | None = None) -> dict[tuple[str, str], str]:
        result = {}
        for line in self.tmux.run("list-keys").splitlines():
            try:
                result[self.parser.identity(line)] = line
            except (UnsupportedBinding, ValueError):
                if warnings is not None and not self.parser.opaque_outer_command(line) and any(
                    re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", line) for name in ALIASES
                ):
                    warnings.append("Unrecognized workspace-operation binding syntax; left unchanged")
                continue  # Unknown unrelated tmux/plugin syntax is never rewritten.
        # Executable list-keys output omits notes. Query only candidates and our
        # wrappers individually, so multiline notes cannot be mistaken for other keys.
        for identity, line in list(result.items()):
            table, key = identity
            try:
                candidate = self.parser.parse(line).command in ALIASES.values()
            except UnsupportedBinding:
                candidate = False
            if not candidate and not ("workmux" in line and "dispatch" in line):
                continue
            try:
                raw = self.tmux.run("list-keys", "-N", "-T", table, "-P", "", key)
            except WorkmuxError as error:
                if str(error).startswith("unknown key:"):
                    continue  # With -N, an existing key without a note is not listed.
                raise
            if raw.startswith(key + " ") or raw.startswith(key + "\t"):
                note = raw[len(key):].lstrip(" \t")
                result[identity] = line.replace("bind-key", "bind-key -N " + quote_tmux(note), 1)
        return result

    def binding_plan(self):
        warnings = []
        current = self.current_bindings(warnings)
        old = self.state()["bindings"]
        records = {}
        changes = []
        for identity, line in current.items():
            table, key = identity
            # Look up ownership by the original identity, not text matching our command name.
            token = binding_token(table, key)
            previous = old.get(token)
            owned = previous and self.parser.equivalent(line, previous["applied"])
            original = previous["original"] if owned else line
            try:
                binding = self.parser.parse(original)
                if binding.command not in ALIASES.values():
                    if previous and token in line and "workmux" in line and "dispatch" in line:
                        records[token] = {**previous, "modified": True}
                        warnings.append(f"{table}/{key}: modified workmux wrapper left unchanged")
                    continue
                command_options(binding)
            except (UnsupportedBinding, ValueError) as error:
                if previous and token in line and "dispatch" in line:
                    records[token] = {**previous, "modified": True}
                if not self.parser.opaque_outer_command(original) and any(
                    re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", original) for name in ALIASES
                ):
                    warnings.append(f"{table}/{key}: {error}; left unchanged")
                continue
            command = self.tmux.helper("dispatch", "--binding", token, "--pane", "#{pane_id}", portable=True)
            # Use the invoking server even for bindings shared with other sessions.
            wrapped = wrap_binding(binding, command)
            if not owned:
                changes.append(wrapped)
            records[token] = {"original": original, "applied": line}
        return records, changes, warnings

    def validate_windows(self, session: str) -> list[str]:
        windows = self.tmux.run("list-windows", "-t", session, "-F", "#{window_id}").splitlines()
        links = self.tmux.run("list-windows", "-a", "-F", "#{session_id} #{window_id}").splitlines()
        if any(owner != session and window in windows for owner, window in map(str.split, links)):
            raise WorkmuxError("This session shares windows with another session. Unlink them before enabling workmux.")
        for window in windows:
            panes = self.tmux.panes(window)
            if sum(pane.sidebar for pane in panes) > 1:
                raise WorkmuxError("A window has multiple workmux sidebars; remove the extra sidebar first.")
        return windows

    def enable(self, target: str, client: str | None = None) -> EnableResult:
        source = self.tmux.pane(target)
        if client:
            attached = self.tmux.run("display-message", "-p", "-c", client, "#{session_id}")
            if attached != source.session:
                raise WorkmuxError("The supplied client is not attached to the source pane's session.")
        self.tmux.run("wait-for", "-L", "workmux-enable")
        try:
            return self.enable_locked(source.session)
        finally:
            self.tmux.run("wait-for", "-U", "workmux-enable")

    def enable_locked(self, session: str) -> EnableResult:
        windows = self.validate_windows(session)
        records, changes, warnings = self.binding_plan()
        # Mark only the chosen session; never change prefix, styling, mouse, or unrelated options.
        label = self.tmux.run("show-options", "-qv", "-t", session, "@workspace-label")
        label = label or self.tmux.run("display-message", "-p", "-t", session, "#{session_name}")
        active = self.tmux.pane(session + ":")
        directory = self.tmux.run("show-options", "-qv", "-t", session, "@workspace-dir")
        content = self.tmux.content_or_none(active)
        directory = directory or (content.directory if content is not None else active.directory)
        options = {"@workmux": "1", "@workmux-enabled": "1", "@workspace-label": label,
                   "@workspace-dir": directory}
        previous = {}
        for option in options:
            present = self.tmux.run("show-options", "-q", "-t", session, option)
            previous[option] = self.tmux.run("show-options", "-qv", "-t", session, option) if present else None
        prior_state = self.tmux.run("show-options", "-sqv", STATE_OPTION)
        prior_integrated = self.tmux.run("show-options", "-sqv", "@workmux-integrated")
        added = []
        restore = [records[binding_token(*self.parser.identity(line))]["applied"] for line in changes]
        try:
            for option, value in options.items():
                self.tmux.run("set-option", "-t", session, option, value)
            for window in windows:
                panes = self.tmux.panes(window)
                if any(pane.sidebar for pane in panes):
                    continue
                active = next(pane for pane in panes if pane.active)
                if active.zoomed:
                    self.tmux.run("resize-pane", "-Z", "-t", active.id)
                try:
                    added.append(self.tmux.add_sidebar(active.id))
                finally:
                    if active.zoomed:
                        self.tmux.run("resize-pane", "-Z", "-t", active.id)
            if changes:
                self.tmux.run("source-file", "-", input="\n".join(changes) + "\n")
            # Capture canonical serialization and verify that no concurrent plugin
            # overwrote the wrappers before claiming ownership of them.
            current = self.current_bindings()
            for entry in records.values():
                if entry.get("modified"):
                    continue
                identity = self.parser.identity(entry["original"])
                binding = self.parser.parse(entry["original"])
                command = self.tmux.helper("dispatch", "--binding", binding.token, "--pane", "#{pane_id}",
                                           portable=True)
                expected = wrap_binding(binding, command)
                if identity not in current or not self.parser.equivalent(expected, current[identity]):
                    raise WorkmuxError(
                        "Bindings changed while enabling. Run workmux enable again after plugins finish."
                    )
                entry["applied"] = current[identity]
            self.tmux.run("set-option", "-s", STATE_OPTION, json.dumps({"version": 1, "bindings": records}))
            self.tmux.run("set-option", "-s", "@workmux-integrated", "1")
            ensure_focus_hooks(self.tmux, windows)
        except (WorkmuxError, KeyboardInterrupt):
            rollback = []
            current = self.current_bindings()
            for original in restore:
                binding = self.parser.parse(original)
                identity = (binding.table, binding.key)
                command = self.tmux.helper("dispatch", "--binding", binding.token, "--pane", "#{pane_id}",
                                           portable=True)
                if identity in current and self.parser.equivalent(wrap_binding(binding, command), current[identity]):
                    rollback.append(original)
            if rollback:
                self.tmux.run("source-file", "-", input="\n".join(rollback) + "\n")
            for sidebar in added:
                self.tmux.run("kill-pane", "-t", sidebar)
            for option, value in previous.items():
                if value is None:
                    self.tmux.run("set-option", "-u", "-t", session, option)
                else:
                    self.tmux.run("set-option", "-t", session, option, value)
            for option, value in ((STATE_OPTION, prior_state), ("@workmux-integrated", prior_integrated)):
                if value:
                    self.tmux.run("set-option", "-s", option, value)
                else:
                    self.tmux.run("set-option", "-su", option)
            raise
        return EnableResult(len(added), len(records), tuple(dict.fromkeys(warnings)))

    def dispatch(self, token: str, target: str) -> None:
        # No binding can consume half-written state during enable/reconcile.
        self.tmux.run("wait-for", "-L", "workmux-enable")
        try:
            self.dispatch_locked(token, target)
        finally:
            self.tmux.run("wait-for", "-U", "workmux-enable")

    def dispatch_locked(self, token: str, target: str) -> None:
        pane = self.tmux.pane(target)
        if self.tmux.run("display-message", "-p", "-t", pane.id, "#{@workmux-enabled}") != "1":
            raise WorkmuxError("Enable workmux in this session before dispatching an adapted binding.")
        entry = self.state()["bindings"].get(token)
        if not entry:
            raise WorkmuxError("Binding state is missing. Run workmux enable again.")
        try:
            binding = self.parser.parse(entry["original"])
            flags, values, positional = command_options(binding)
        except UnsupportedBinding as error:
            raise WorkmuxError("This adapted binding is unsupported. Run workmux enable again.") from error
        content = self.tmux.content_or_none(pane) if binding.command == "new-window" else self.tmux.content(pane)
        context = content or pane
        args = list(binding.args)
        for flag, index in values.items():
            if flag in ("c", "n"):
                args[index] = self.tmux.run("display-message", "-p", "-t", context.id, "--", args[index])
        if binding.command == "new-window":
            if content is None and "c" not in values:
                workspace = self.tmux.require_workspace(pane.session)
                args = ["-c", workspace.directory or pane.directory, *args]
            created = self.tmux.run("new-window", "-t", pane.session + ":", "-P", "-F", "#{pane_id}", *args)
            self.tmux.add_sidebar(created)
            ensure_focus_hooks(self.tmux, [self.tmux.pane(created).window])
        elif binding.command == "split-window":
            created = self.tmux.run("split-window", "-t", content.id, "-P", "-F", "#{pane_id}", *args)
            if "d" not in flags:
                self.tmux.run("set-option", "-w", "-t", pane.window, "@workspace-last-content", created)
        else:
            if "E" in flags:
                self.tmux.balance(pane.id)
                return
            if binding.command == "next-layout" or "n" in flags:
                preset = "next"
            elif binding.command == "previous-layout" or "p" in flags:
                preset = "previous"
            elif positional:
                preset = positional[0]
            else:
                preset = self.tmux.run("show-options", "-wqv", "-t", pane.window, "@workspace-layout")
                preset = preset or "even-horizontal"
            self.tmux.retile(pane.id, preset)
