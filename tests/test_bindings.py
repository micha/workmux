import unittest

from workmux.bindings import BindingParser, UnsupportedBinding, command_options, wrap_binding


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.parser = BindingParser()

    def test_preserves_key_table_repeat_and_unicode_note(self):
        line = 'bind-key -r -N "note café" -T custom M-v splitw -dh -c "#{pane_current_path}"'
        binding = self.parser.parse(line)
        self.assertEqual((binding.table, binding.key, binding.command), ("custom", "M-v", "split-window"))
        flags, values, positional = command_options(binding)
        self.assertEqual(flags, {"d", "h"})
        self.assertEqual(values, {"c": 2})
        self.assertEqual(positional, [])
        self.assertEqual(binding.header + binding.body, line)

    def test_wrapping_preserves_native_fallback_and_header(self):
        binding = self.parser.parse("bind-key -r -T prefix M-1 select-layout even-horizontal")
        wrapped = wrap_binding(binding, "workmux dispatch --binding test --pane '#{pane_id}'")
        self.assertTrue(wrapped.startswith(binding.header))
        self.assertIn("@workmux-enabled", wrapped)
        self.assertIn("{ select-layout even-horizontal }", wrapped)
        self.assertEqual(self.parser.identity(wrapped), ("prefix", "M-1"))
        with self.assertRaises(UnsupportedBinding):
            command_options(self.parser.parse(wrapped))

    def test_opaque_menus_and_templates_are_not_direct_workspace_actions(self):
        for body in (
            'display-menu "Split" v "split-window -h"',
            'if-shell -F "#{mouse_any_flag}" "display-menu Split v split-window" "send-keys -M"',
            'run-shell "tmux new-window"',
        ):
            with self.subTest(body=body):
                self.assertTrue(self.parser.opaque_outer_command("bind-key v " + body))
        self.assertFalse(self.parser.opaque_outer_command("bind-key v split-window -f"))
        self.assertFalse(self.parser.opaque_outer_command("bind-key v { split-window; new-window }"))

    def test_balance_flag_cannot_be_combined_with_another_layout_action(self):
        for command in ("select-layout -En", "select-layout -Ep", "select-layout -E tiled"):
            with self.subTest(command=command), self.assertRaises(UnsupportedBinding):
                command_options(self.parser.parse("bind-key X " + command))

    def test_serialization_padding_does_not_change_ownership(self):
        first = "bind-key -r -T prefix M-1 select-layout even-horizontal"
        second = "bind-key    -r   -T prefix    M-1   select-layout even-horizontal"
        self.assertTrue(self.parser.equivalent(first, second))
        self.assertFalse(self.parser.equivalent(first, first.replace("horizontal", "vertical")))

    def test_native_arguments_are_not_lost(self):
        binding = self.parser.parse('bind-key c new-window -d -n "my tab" -c "/tmp/my project" "sleep 999"')
        flags, values, positional = command_options(binding)
        self.assertEqual(flags, {"d"})
        self.assertEqual(binding.args[values["n"]], "my tab")
        self.assertEqual(binding.args[values["c"]], "/tmp/my project")
        self.assertEqual(positional, ["sleep 999"])

    def test_select_layout_and_cycling_are_supported(self):
        for command in ("select-layout tiled", "select-layout -E", "select-layout -n", "select-layout -p",
                        "select-layout", "next-layout", "previous-layout"):
            with self.subTest(command=command):
                command_options(self.parser.parse("bind-key X " + command))

    def test_explicit_targets_and_whole_window_splits_are_not_silently_changed(self):
        for command in ("split-window -f", "split-window -t %4", "new-window -t other:",
                        "split-window -Z", "select-layout -o", "select-layout custom"):
            with self.subTest(command=command), self.assertRaises(UnsupportedBinding):
                command_options(self.parser.parse("bind-key X " + command))

    def test_quoted_and_compound_commands_are_not_approximated(self):
        for body in ('"new-window; select-pane -t 0"', "{ new-window; display-message done }",
                     "new-window ; display-message done"):
            with self.subTest(body=body), self.assertRaises(UnsupportedBinding):
                self.parser.parse("bind-key X " + body)

    def test_unrelated_operations_are_not_candidates(self):
        for command in ("display-message unchanged", "send-keys hello", "select-pane -L"):
            with self.subTest(command=command), self.assertRaises(UnsupportedBinding):
                command_options(self.parser.parse("bind-key X " + command))

    def test_tokens_distinguish_identical_keys_in_different_tables(self):
        first = self.parser.parse("bind-key -T prefix X new-window")
        second = self.parser.parse("bind-key -T root X new-window")
        self.assertNotEqual(first.token, second.token)
