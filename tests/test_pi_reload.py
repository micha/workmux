"""Autosizing, client validation, and result delivery for Pi reload popups."""

from pathlib import Path
from subprocess import CompletedProcess
import unittest
from unittest.mock import Mock, patch

from workmux.core import Tmux, WorkmuxError
from workmux.pi_reload import _popup_size, reload_popup


class PiReloadPopupTests(unittest.TestCase):
    def test_size_fits_display_columns_and_caps_each_axis(self):
        # Wide characters occupy two cells; a combining accent adds no cell.
        self.assertEqual(_popup_size("作業 e\u0301\nshort", 120, 40), (8, 5))
        self.assertEqual(_popup_size("x" * 200, 101, 40), (85, 4))
        self.assertEqual(_popup_size("\n".join(["abc"] * 100), 120, 41), (5, 30))

    def test_popup_uses_completed_table_and_current_size_and_cleans_up(self):
        client = "/dev/pts/17"
        less = "/tmp/bin ' $; /less"
        table = "界" * 26 + "\n" + "\n".join(["result $(echo no); ' &"] * 10)
        client_info = iter((f"{client}\t0\t123\t20\t10", f"{client}\t0\t123\t120\t40"))
        tmux = Mock(spec=Tmux)
        outputs = []
        with (patch("workmux.pi_reload._popup_less", return_value=less),
              patch("workmux.pi_reload.reload_table", return_value=table) as reload_table):
            def run(*args):
                if args[0] == "display-message":
                    self.assertEqual(args[1:4], ("-p", "-c", client))
                    return next(client_info)
                self.assertEqual(args[0], "display-popup")
                reload_table.assert_called_once_with(tmux)
                options = {arg: args[args.index(arg) + 1] for arg in ("-c", "-T", "-w", "-h")}
                self.assertEqual(options, {"-c": client, "-T": "Pi reload", "-w": "54", "-h": "14"})
                env = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-e")
                self.assertEqual(env["WORKMUX_PI_RELOAD_LESS"], less)
                output = Path(env["WORKMUX_PI_RELOAD_OUTPUT"])
                outputs.append(output)
                self.assertEqual(output.read_text(encoding="utf-8"), table + "\n")
                self.assertNotIn("$(echo no)", args[-1])
                return ""

            tmux.run.side_effect = run
            reload_popup(tmux, client)
        self.assertEqual([call.args[0] for call in tmux.run.call_args_list],
                         ["display-message", "display-message", "display-popup"])
        self.assertEqual(len(outputs), 1)
        self.assertFalse(outputs[0].parent.exists())

    def test_missing_or_unsupported_less_is_rejected_before_reload(self):
        for executable in (None, "/usr/bin/less"):
            with self.subTest(executable=executable):
                tmux = Mock(spec=Tmux)
                with (patch("workmux.pi_reload.shutil.which", return_value=executable),
                      patch("workmux.pi_reload.subprocess.run",
                            return_value=CompletedProcess([], 0, stdout=b"no mouse option")),
                      patch("workmux.pi_reload.reload_table") as reload_table):
                    with self.assertRaisesRegex(WorkmuxError, "requires less with --mouse support"):
                        reload_popup(tmux, "/dev/pts/17")
                reload_table.assert_not_called()
                tmux.run.assert_not_called()

    def test_nonmatching_and_control_clients_are_rejected_before_reload(self):
        for client_info in ("/dev/pts/18\t0\t123\t120\t40", "/dev/pts/17\t1\t123\t120\t40"):
            with self.subTest(client_info=client_info):
                tmux = Mock(spec=Tmux)
                tmux.run.return_value = client_info
                with (patch("workmux.pi_reload._popup_less", return_value="/usr/bin/less"),
                      patch("workmux.pi_reload.reload_table") as reload_table):
                    with self.assertRaisesRegex(WorkmuxError, "Not an attached tmux terminal client"):
                        reload_popup(tmux, "/dev/pts/17")
                reload_table.assert_not_called()
                tmux.run.assert_called_once()

    def test_client_replaced_during_reload_does_not_receive_popup(self):
        tmux = Mock(spec=Tmux)
        tmux.run.side_effect = ["/dev/pts/17\t0\t123\t120\t40", "/dev/pts/17\t0\t456\t120\t40"]
        with (patch("workmux.pi_reload._popup_less", return_value="/usr/bin/less"),
              patch("workmux.pi_reload.reload_table", return_value="No running Pi panes.") as reload_table):
            with self.assertRaisesRegex(WorkmuxError, "client changed while reloading Pi"):
                reload_popup(tmux, "/dev/pts/17")
        reload_table.assert_called_once_with(tmux)
        self.assertEqual([call.args[0] for call in tmux.run.call_args_list],
                         ["display-message", "display-message"])
