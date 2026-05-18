from __future__ import annotations

import unittest

from semi_auto_probe.app import ProbeApp


class FocusProcessingGateTests(unittest.TestCase):
    def make_app_shell(self) -> ProbeApp:
        app = ProbeApp.__new__(ProbeApp)
        app.current_page = "Main"
        app.autofocus_running = False
        app.imgstitch_focus_sampling_required = False
        return app

    def test_focus_scores_are_disabled_outside_autofocus_page(self) -> None:
        app = self.make_app_shell()

        self.assertFalse(ProbeApp._should_process_focus_scores(app))

    def test_focus_scores_are_enabled_for_autofocus_page_or_active_sampling(self) -> None:
        app = self.make_app_shell()
        app.current_page = "AutoFocus"
        self.assertTrue(ProbeApp._should_process_focus_scores(app))

        app.current_page = "Main"
        app.autofocus_running = True
        self.assertTrue(ProbeApp._should_process_focus_scores(app))

        app.autofocus_running = False
        app.imgstitch_focus_sampling_required = True
        self.assertTrue(ProbeApp._should_process_focus_scores(app))


if __name__ == "__main__":
    unittest.main()
