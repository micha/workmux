"""Shared snapshot, publisher election, and bounded window-status writes."""

import unittest
from unittest.mock import Mock, patch

from workmux.core import ActivitySnapshot, Tab, Tmux, WorkmuxError
from workmux.status import (ANIMATION_INTERVAL, INDICATOR_OPTION, WORKING_FRAMES,
                            WindowIndicators, working_indicator)


def row(pane, *, session="$0", window="@0", sidebar="", dead="0", managed="1",
        indicator="", flags="*", report="", name="work", title="", pane_index=""):
    return (f"{managed}\t{indicator}\t{session}\t{window}\t1\t{flags}\t{pane}\t"
            f"{sidebar}\t{dead}\t{report}\t{pane_index}\t{title}\t{name}")


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
        self.assertEqual(snapshot.tabs, [Tab("$0", "@0", 1, "work", "attention", "%4", agents=(
            Tab("$0", "@0", 1, "work", "attention", "%4"),
            Tab("$0", "@0", 1, "name\twith tab", "working", "%3")))])

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
    def test_working_frames_use_a_shared_one_second_phase(self):
        expected = ("○", "◔", "◑", "◕", "●", "◕", "◑", "◔")
        self.assertEqual(WORKING_FRAMES, expected)
        self.assertEqual(ANIMATION_INTERVAL, 1.0)
        for index, glyph in enumerate(expected):
            with self.subTest(index=index):
                self.assertEqual(working_indicator(float(index)), glyph)
                self.assertEqual(working_indicator(index + 0.999), glyph)
        self.assertEqual(working_indicator(float(len(expected))), expected[0])

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
            self.assertEqual(indicators.animating, pane == "%1")
            indicators.publish(tmux, 0)
        tmux.run.assert_called_once()

    def test_working_windows_share_one_batch_per_frame(self):
        keys = (("$0", "@0"), ("$1", "@2"))
        static = ("$1", "@3")
        states = {**dict.fromkeys(keys, "working"), static: "finished"}
        indicators = WindowIndicators("%1")
        indicators.update(self.snapshot(states))
        tmux = Mock()
        for index, glyph in enumerate(WORKING_FRAMES):
            now = index * ANIMATION_INTERVAL + 0.001
            indicators.publish(tmux, now)
            self.assertEqual(tmux.run.call_count, index + 1)
            expected = {**dict.fromkeys(keys, glyph), static: "✓"}
            self.assertEqual(indicators.values, expected)
            batch = tmux.run.call_args.kwargs["input"]
            self.assertEqual(batch.count("set-option -w -t"), 3 if index == 0 else 2)
            self.assertIn(f"#{{!=:#{{{INDICATOR_OPTION}}},{glyph}}}", batch)
            self.assertNotIn("refresh-client", batch)
            self.assertNotIn("status-interval", batch)
            self.assertNotIn("#[", batch)
            indicators.update(self.snapshot(states, values=expected))
            indicators.publish(tmux, now + ANIMATION_INTERVAL / 2)
            self.assertEqual(tmux.run.call_count, index + 1)

    def test_skipped_phases_with_the_same_glyph_do_not_write_again(self):
        key = ("$0", "@0")
        indicators = WindowIndicators("%1")
        indicators.update(self.snapshot({key: "working"}))
        tmux = Mock()
        indicators.publish(tmux, ANIMATION_INTERVAL + 0.001)
        indicators.publish(tmux, 7 * ANIMATION_INTERVAL + 0.001)
        self.assertEqual(indicators.values[key], "◔")
        tmux.run.assert_called_once()

    def test_static_symbols_and_idle_clearing_write_only_on_change(self):
        values = {("$0", "@0"): "", ("$0", "@1"): "", ("$0", "@2"): "", ("$0", "@3"): "stale"}
        states = {("$0", "@0"): "attention", ("$0", "@1"): "finished", ("$0", "@2"): "alert"}
        indicators = WindowIndicators("%1")
        indicators.update(self.snapshot(states, values=values))
        tmux = Mock()
        self.assertFalse(indicators.animating)
        indicators.publish(tmux, 0)
        expected = dict(zip(values, ("⚠", "✓", "◆", "")))
        self.assertEqual(indicators.values, expected)
        indicators.update(self.snapshot(states, values=expected))
        indicators.publish(tmux, ANIMATION_INTERVAL)
        tmux.run.assert_called_once_with("source-file", "-", input=tmux.run.call_args.kwargs["input"])

    def test_handoff_reads_existing_values_and_clears_stale_indicators(self):
        indicators = WindowIndicators("%2")
        tmux = Mock()
        key = ("$0", "@0")
        states = {key: "working"}
        frame = WORKING_FRAMES[1]
        indicators.update(self.snapshot(states))
        indicators.publish(tmux, ANIMATION_INTERVAL)
        indicators.update(self.snapshot(states, publisher="%2", values={key: frame}))
        indicators.publish(tmux, ANIMATION_INTERVAL)
        tmux.run.assert_not_called()
        indicators.update(self.snapshot({}, publisher="%2", values={key: frame}))
        self.assertFalse(indicators.animating)
        indicators.publish(tmux, ANIMATION_INTERVAL)
        self.assertEqual(indicators.values, {key: ""})
        tmux.run.assert_called_once()
        indicators.update(self.snapshot(states))
        indicators.publish(tmux, ANIMATION_INTERVAL)
        tmux.run.assert_called_once()

    def test_races_are_best_effort_and_retry_without_claiming_success(self):
        indicators = WindowIndicators("%1")
        indicators.update(self.snapshot({("$0", "@0"): "working"}))
        tmux = Mock()
        tmux.run.side_effect = WorkmuxError("can't find window: @0")
        indicators.publish(tmux, 0)
        self.assertEqual(indicators.values[("$0", "@0")], "")
        tmux.run.side_effect = None
        indicators.publish(tmux, 0)
        self.assertEqual(indicators.values[("$0", "@0")], WORKING_FRAMES[0])
        batch = tmux.run.call_args.kwargs["input"]
        self.assertIn("@workspace-sidebar", batch)
        self.assertIn("@workmux", batch)
        self.assertIn("pane_dead", batch)


if __name__ == "__main__":
    unittest.main()
