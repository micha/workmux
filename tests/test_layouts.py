import json
import unittest

from workmux.layouts import (
    LAYOUTS, Cell, content_needs_balance, parse_layout, protect_sidebar, split_sizes, workspace_layout,
)


class LayoutTests(unittest.TestCase):
    def assert_partition(self, cell: Cell):
        self.assertGreaterEqual(cell.width, 2)
        self.assertGreaterEqual(cell.height, 2)
        if cell.pane is not None:
            self.assertFalse(cell.children)
            return
        self.assertGreaterEqual(len(cell.children), 2)
        offset = cell.x if cell.axis == "h" else cell.y
        for child in cell.children:
            if cell.axis == "h":
                self.assertEqual((child.x, child.y, child.height), (offset, cell.y, cell.height))
                offset += child.width + 1
            else:
                self.assertEqual((child.x, child.y, child.width), (cell.x, offset, cell.width))
                offset += child.height + 1
            self.assert_partition(child)
        self.assertEqual(offset - 1, cell.x + cell.width if cell.axis == "h" else cell.y + cell.height)

    def test_legacy_layout_round_trips_every_preset(self):
        for preset in LAYOUTS:
            layout = workspace_layout(preset, "%9", ["%1", "%2", "%3"], 120, 40, 24)
            self.assertEqual(parse_layout(layout.dump()), layout)

    def test_json_v2_geometry_uses_stable_pane_ids_not_indexes(self):
        layout = workspace_layout("even-horizontal", "%9", ["%4"], 120, 40, 24)

        def encoded(cell):
            value = dict(w=cell.width, h=cell.height, x=cell.x, y=cell.y)
            if cell.pane:
                return dict(value, t="p", I=cell.pane, i=0)
            return dict(value, t=cell.axis, c=[encoded(child) for child in cell.children])

        self.assertEqual(parse_layout(json.dumps({"V": 2, "L": encoded(layout)})), layout)

    def test_bad_current_layouts_are_rejected(self):
        layout = workspace_layout("tiled", "%0", ["%1"], 120, 40, 24)
        for value in ("", "0000," + layout.serialize(), layout.dump() + "trailing",
                      '{"V":3,"L":{}}', '{"V":2,"L":{"t":"p","w":-1}}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_layout(value)

    def test_protecting_sidebar_preserves_flat_geometry_and_ids(self):
        sidebar = Cell(24, 40, 0, 0, "%9")
        left = Cell(20, 40, 25, 0, "%1")
        right = Cell(74, 40, 46, 0, "%2")
        flat = Cell(120, 40, 0, 0, axis="h", children=(sidebar, left, right))
        protected = protect_sidebar(flat, "%9")
        self.assertEqual(protected.children[0], sidebar)
        self.assertEqual(protected.children[1].children, (left, right))
        self.assertEqual(protected.pane_ids(), flat.pane_ids())
        self.assertTrue(content_needs_balance(protected, "%1"))

    def test_balanced_content_does_not_ascend_to_sidebar_parent(self):
        layout = workspace_layout("even-horizontal", "%0", ["%1", "%2"], 120, 40, 24)
        self.assertFalse(content_needs_balance(layout, "%1"))
        self.assertFalse(content_needs_balance(layout, "%2"))
        single = workspace_layout("tiled", "%0", ["%1"], 120, 40, 24)
        self.assertFalse(content_needs_balance(single, "%1"))

    def test_balance_search_uses_only_ancestors_of_selected_content(self):
        layout = Cell(120, 40, 0, 0, axis="h", children=(
            Cell(24, 40, 0, 0, "%0"),
            Cell(95, 40, 25, 0, axis="h", children=(
                Cell(47, 40, 25, 0, "%1"),
                Cell(47, 40, 73, 0, axis="v", children=(
                    Cell(47, 10, 73, 0, "%2"), Cell(47, 29, 73, 11, "%3"),
                )),
            )),
        ))
        self.assertFalse(content_needs_balance(layout, "%1"))
        self.assertTrue(content_needs_balance(layout, "%2"))
        with self.assertRaises(ValueError):
            content_needs_balance(layout, "%99")

    def test_misplaced_sidebar_is_not_guessed_or_rearranged(self):
        vertical = Cell(120, 40, 0, 0, axis="v", children=(
            Cell(120, 20, 0, 0, "%0"), Cell(120, 19, 0, 21, "%1"),
        ))
        with self.assertRaises(ValueError):
            protect_sidebar(vertical, "%0")

    def test_all_presets_partition_content_without_including_sidebar(self):
        for preset in LAYOUTS:
            for count in range(1, 10):
                for width, height in ((120, 40), (161, 53)):
                    with self.subTest(preset=preset, count=count, size=(width, height)):
                        panes = [f"%{index}" for index in range(count)]
                        layout = workspace_layout(preset, "%99", panes, width, height, 24)
                        self.assert_partition(layout)
                        self.assertEqual(layout.children[0], Cell(24, height, 0, 0, "%99"))
                        self.assertCountEqual(layout.pane_ids(), ["%99", *panes])
                        self.assertEqual(layout.children[1].x, 25)

    def test_mirrored_main_pane_is_last_in_traversal_but_keeps_identity(self):
        for preset in ("main-horizontal-mirrored", "main-vertical-mirrored"):
            layout = workspace_layout(preset, "%0", ["%1", "%2", "%3"], 120, 40, 24)
            self.assertEqual(layout.pane_ids(), ["%0", "%2", "%3", "%1"])

    def test_percentage_main_sizes_are_relative_to_content(self):
        layout = workspace_layout("main-vertical", "%0", ["%1", "%2"], 120, 40, 24,
                                  main_width="50%")
        self.assertEqual(layout.children[1].children[0].width, 47)
        layout = workspace_layout("main-horizontal", "%0", ["%1", "%2"], 120, 40, 24,
                                  main_height="50%")
        self.assertEqual(layout.children[1].children[0].height, 19)

    def test_portable_checksum_matches_known_tmux_layout(self):
        # Independently recorded from tmux, for a two-pane workspace window.
        layout = workspace_layout("tiled", "%1", ["%0"], 120, 40, 24)
        self.assertEqual(layout.serialize(), "120x40,0,0{24x40,0,0,1,95x40,25,0,0}")
        self.assertEqual(layout.dump(), "8ce1,120x40,0,0{24x40,0,0,1,95x40,25,0,0}")

    def test_invalid_or_impossible_layouts_fail_before_applying(self):
        for preset, panes, width, height in (
            ("unknown", ["%1"], 120, 40),
            ("tiled", [], 120, 40),
            ("tiled", ["%1", "%1"], 120, 40),
            ("tiled", ["%0"], 120, 40),
            ("even-horizontal", ["%1", "%2", "%3"], 30, 40),
            ("even-vertical", ["%1", "%2", "%3"], 120, 4),
            ("main-horizontal", ["%1", "%2"], 120, 4),
        ):
            with self.subTest(preset=preset, panes=panes), self.assertRaises(ValueError):
                workspace_layout(preset, "%0", panes, width, height, 24)

    def test_even_sizes_differ_by_at_most_one_cell(self):
        for count in range(1, 20):
            for length in range(count * 3, 150):
                sizes = split_sizes(length, count)
                self.assertEqual(sum(sizes) + count - 1, length)
                self.assertLessEqual(max(sizes) - min(sizes), 1)
