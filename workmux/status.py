"""Shared activity symbols and batched, plain tmux window-status publication."""

from .core import ActivitySnapshot, Tmux, WorkmuxError, quote_tmux


BRAILLE_WHEEL = ("⣷", "⣯", "⣟", "⡿", "⢿", "⣻", "⣽", "⣾")
ANIMATION_INTERVAL = 0.125
INDICATORS = {"working": BRAILLE_WHEEL[0], "attention": "⚠", "finished": "✓", "alert": "●"}
INDICATOR_OPTION = "@workmux-agent-indicator"


def working_indicator(now: float) -> str:
    """Select a time-based animation frame, independent of input and polling."""
    return BRAILLE_WHEEL[int(now / ANIMATION_INTERVAL) % len(BRAILLE_WHEEL)]


class WindowIndicators:
    """Only the oldest live owned sidebar publishes, with automatic failover.

    All sidebars already snapshot the server for their activity trees. Reusing
    that snapshot elects one publisher without locks, leases, or a new daemon.
    Static states write only on change; working windows share one batch/frame.
    """

    def __init__(self, pane_id: str):
        self.pane_id = pane_id
        self.values: dict[tuple[str, str], str] = {}
        self.states: dict[tuple[str, str], str] = {}

    def update(self, snapshot: ActivitySnapshot) -> None:
        self.values = dict(snapshot.indicators) if snapshot.publisher == self.pane_id else {}
        self.states = {(tab.session, tab.id): tab.state for tab in snapshot.tabs
                       if (tab.session, tab.id) in self.values}

    def publish(self, tmux: Tmux, now: float) -> None:
        desired = {key: working_indicator(now) if self.states.get(key) == "working"
                   else INDICATORS.get(self.states.get(key, ""), "") for key in self.values}
        changed = {key: value for key, value in desired.items() if value != self.values[key]}
        if not changed:
            return
        commands = []
        for (session, window), value in changed.items():
            target = session + ":" + window
            # Recheck management in tmux, not just the older Python snapshot.
            command = f"set-option -w -t {target} {INDICATOR_OPTION} {quote_tmux(value)}"
            commands.append(f'if-shell -F -t {target} "#{{==:#{{@workmux}},1}}" {quote_tmux(command)}')
        # A removed/replaced publisher must not keep writing from its cached
        # snapshot. set-option itself schedules tmux's status redraw, even when
        # status-interval is zero; no extra client queries/refreshes are needed.
        owned = "#{&&:#{==:#{@workmux},1},#{&&:#{==:#{@workspace-sidebar},1},#{!:#{pane_dead}}}}"
        batch = f"if-shell -F -t {self.pane_id} {quote_tmux(owned)} {quote_tmux(' ; '.join(commands))}\n"
        try:
            tmux.run("source-file", "-", input=batch)
        except WorkmuxError:
            # Panes/windows may close mid-frame. Retry from the next snapshot;
            # status decoration must never interrupt sidebar navigation.
            return
        self.values.update(changed)
