"""Shared snapshot, publisher election, and bounded window-status writes."""

import unittest
from unittest.mock import Mock, patch

from workmux.core import ActivitySnapshot, Tab, Tmux, WorkmuxError
from workmux.status import (ANIMATION_INTERVAL, BRAILLE_WHEEL, INDICATOR_OPTION,
                            WindowIndicators)


def row(pane, *, session="$0", window="@0", sidebar="", dead="0", managed="1",
        indicator="", flags="*", report="", name="work", title=""):
    return (f"{managed}\t{indicator}\t{session}\t{window}\t1\t{flags}\t{pane}\t"
            f"{sidebar}\t{dead}\t{report}\t{title}\t{name}")


class ActivitySnapshotTests(unittest.TestCase):
    def test_one_snapshot_reuses_aggregation_and_elects_oldest_live_owned_sidebar(self):
        output = "\n".join([
            row("%10", sidebar="1", indicator="old"),
            row("%2", sidebar="1", window="@1"),
            row("%1", sidebar="1", dead="1", window="@2"),
            row("%0", sidebar="1", managed="", window="@3"),
            row("%3", report='{"state":"working"}', indicator="old", name="name\twith tab"),
            row("%4", flags="!", report='{"state":"attention"}', indicator="old"),
            "malformed",
        ])
        with patch.object(Tmux, "run", return_value=output) as run:
            snapshot = Tmux().activity_snapshot()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[:3], ("list-panes", "-a", "-F"))
        self.assertEqual(snapshot.publisher, "%2")
        self.assertEqual(snapshot.indicators, {("$0", "@0"): "old", ("$0", "@1"): "",
                                                ("$0", "@2"): ""})
        self.assertEqual(snapshot.tabs, [Tab("$0", "@0", 1, "work", "attention", "%4")])

    def test_empty_server_and_unmanaged_sidebars_have_no_publisher(self):
        for output in ("", row("%0", sidebar="1", managed="0")):
            with patch.object(Tmux, "run", return_value=output):
                snapshot = Tmux().activity_snapshot()
            self.assertIsNone(snapshot.publisher)
            self.assertEqual(snapshot.indicators, {})

    def test_window_name_tabs_survive_extended_snapshot(self):
        with patch.object(Tmux, "run", return_value=row("%0", report='{"state":"working"}',
                                                        name="name\twith tab")):
            snapshot = Tmux().activity_snapshot()
        self.assertEqual(snapshot.tabs[0].name, "name\twith tab")


class WindowIndicatorTests(unittest.TestCase):
    def snapshot(self, states, *, publisher="%1", values=None):
        tabs = [Tab(session, window, index, "name", state)
                for index, ((session, window), state) in enumerate(states.items())]
        return ActivitySnapshot(tabs, publisher, values if values is not None else
                                {key: "" for key in states})

    def test_only_one_publisher_writes_across_many_sidebars(self):
        snapshot = self.snapshot({("$0", "@0"): "working"})
        tmux = Mock()
        for pane in ("%0", "%1", "%2", "%10"):
            indicators = WindowIndicators(pane)
            indicators.update(snapshot)
            indicators.publish(tmux, 0)
        tmux.run.assert_called_once()

    def test_all_working_windows_share_one_batch_per_frame(self):
        keys = (("$0", "@0"), ("$1", "@2"))
        indicators = WindowIndicators("%1")
        indicators.update(self.snapshot({**dict.fromkeys(keys, "working"), ("$1", "@3"): "finished"}))
        tmux = Mock()
        for frame, glyph in enumerate(BRAILLE_WHEEL):
            indicators.publish(tmux, frame * ANIMATION_INTERVAL)
            self.assertEqual(tmux.run.call_count, frame + 1)
            for key in keys:
                self.assertEqual(indicators.values[key], glyph)
            batch = tmux.run.call_args.kwargs["input"]
            self.assertEqual(batch.count(INDICATOR_OPTION), 3 if frame == 0 else 2)
            self.assertNotIn("refresh-client", batch)
            self.assertNotIn("status-interval", batch)
            self.assertNotIn("#[", batch)
            indicators.publish(tmux, frame * ANIMATION_INTERVAL + 0.01)
            self.assertEqual(tmux.run.call_count, frame + 1)
        self.assertEqual(indicators.values[("$1", "@3")], "✓")

    def test_static_symbols_and_idle_clearing_write_only_on_change(self):
        values = {("$0", "@0"): "", ("$0", "@1"): "", ("$0", "@2"): "", ("$0", "@3"): "stale"}
        states = {("$0", "@0"): "attention", ("$0", "@1"): "finished", ("$0", "@2"): "alert"}
        indicators = WindowIndicators("%1")
        indicators.update(self.snapshot(states, values=values))
        tmux = Mock()
        indicators.publish(tmux, 0)
        expected = dict(zip(values, ("⚠", "✓", "●", "")))
        self.assertEqual(indicators.values, expected)
        for frame in range(1, 10):
            indicators.update(self.snapshot(states, values=expected))
            indicators.publish(tmux, frame * ANIMATION_INTERVAL)
        tmux.run.assert_called_once_with("source-file", "-", input=tmux.run.call_args.kwargs["input"])

    def test_handoff_reads_existing_values_and_clears_stale_indicators(self):
        indicators = WindowIndicators("%2")
        tmux = Mock()
        indicators.update(self.snapshot({("$0", "@0"): "working"}))
        indicators.publish(tmux, 0)
        tmux.run.assert_not_called()
        indicators.update(self.snapshot({}, publisher="%2", values={("$0", "@0"): "✓"}))
        indicators.publish(tmux, 0)
        self.assertEqual(indicators.values, {("$0", "@0"): ""})
        tmux.run.assert_called_once()
        indicators.update(self.snapshot({("$0", "@0"): "working"}))
        indicators.publish(tmux, 0)
        self.assertEqual(tmux.run.call_count, 1)

    def test_races_are_best_effort_and_retry_without_claiming_success(self):
        indicators = WindowIndicators("%1")
        indicators.update(self.snapshot({("$0", "@0"): "working"}))
        tmux = Mock()
        tmux.run.side_effect = WorkmuxError("can't find window: @0")
        indicators.publish(tmux, 0)
        self.assertEqual(indicators.values[("$0", "@0")], "")
        tmux.run.side_effect = None
        indicators.publish(tmux, 0)
        self.assertEqual(indicators.values[("$0", "@0")], BRAILLE_WHEEL[0])
        batch = tmux.run.call_args.kwargs["input"]
        self.assertIn("@workspace-sidebar", batch)
        self.assertIn("@workmux", batch)
        self.assertIn("pane_dead", batch)


if __name__ == "__main__":
    unittest.main()
