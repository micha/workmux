"""Shared activity symbols and batched, plain tmux window-status publication."""

from .core import ActivitySnapshot, Tmux, WorkmuxError, quote_tmux


INDICATORS = {"working": "♥", "attention": "⚠", "finished": "✓", "alert": "◆"}
INDICATOR_OPTION = "@workmux-agent-indicator"


class WindowIndicators:
    """Only the oldest live owned sidebar publishes, with automatic failover.

    The shared activity snapshot elects the publisher without a separate poll.
    The working heart is steady in the tab bar; only changed glyphs are written.
    """

    def __init__(self, pane_id: str):
        self.pane_id = pane_id
        self.values: dict[tuple[str, str], str] = {}
        self.states: dict[tuple[str, str], str] = {}

    def update(self, snapshot: ActivitySnapshot) -> None:
        self.values = dict(snapshot.indicators) if snapshot.publisher == self.pane_id else {}
        self.states = {(tab.session, tab.id): tab.state for tab in snapshot.tabs
                       if (tab.session, tab.id) in self.values}

    def publish(self, tmux: Tmux) -> None:
        desired = {key: INDICATORS.get(self.states.get(key, ""), "") for key in self.values}
        changed = {key: value for key, value in desired.items() if value != self.values[key]}
        if not changed:
            return
        commands = []
        for (session, window), value in changed.items():
            target = session + ":" + window
            # Recheck live management and the current glyph: a shared snapshot
            # can predate a publish, but must not cause an extra status redraw.
            changed_glyph = f"#{{!=:#{{{INDICATOR_OPTION}}},{value}}}"
            guard = f"#{{&&:#{{==:#{{@workmux}},1}},{changed_glyph}}}"
            command = f"set-option -w -t {target} {INDICATOR_OPTION} {quote_tmux(value)}"
            commands.append(f"if-shell -F -t {target} {quote_tmux(guard)} {quote_tmux(command)}")
        # A removed/replaced publisher must not keep writing from its cached
        # snapshot. set-option itself schedules tmux's status redraw, even when
        # status-interval is zero; no extra client queries/refreshes are needed.
        owned = "#{&&:#{==:#{@workmux},1},#{&&:#{==:#{@workspace-sidebar},1},#{!:#{pane_dead}}}}"
        batch = f"if-shell -F -t {self.pane_id} {quote_tmux(owned)} {quote_tmux(' ; '.join(commands))}\n"
        try:
            tmux.run("source-file", "-", input=batch)
        except WorkmuxError:
            # Panes/windows may close mid-publish. Retry from the next snapshot;
            # status decoration must never interrupt sidebar navigation.
            return
        self.values.update(changed)
