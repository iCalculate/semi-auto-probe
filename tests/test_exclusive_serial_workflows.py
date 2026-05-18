from __future__ import annotations

import threading
import unittest
from types import MethodType

from semi_auto_probe.app import ProbeApp


class DummyVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class ExclusiveSerialWorkflowTests(unittest.TestCase):
    def make_app_shell(self) -> ProbeApp:
        app = ProbeApp.__new__(ProbeApp)
        app.serial_client = object()
        app.home_signal_enabled = True
        app.home_signal_stop_event = threading.Event()
        app.home_signal_thread = None
        app.home_signal_button_var = DummyVar("Stop Home Signals")
        app.status_var = DummyVar()
        app.autofocus_status_var = DummyVar("Running")
        app.imgstitch_status_var = DummyVar("Running")
        app.autofocus_restore_home_signal = False
        app.autofocus_restore_realtime = False
        app.autofocus_running = False
        app.motion_busy = False
        app.focus_lock = threading.Lock()
        app.autofocus_run_end_time = None
        app.imgstitch_restore_home_signal = False
        app.imgstitch_restore_realtime = False
        app.imgstitch_running = False
        app.imgstitch_focus_sampling_required = False
        app._home_signal_worker = lambda: None
        return app

    def test_autofocus_pauses_home_polling_and_remembers_restore(self) -> None:
        app = self.make_app_shell()

        app.disable_home_signal_polling()
        app.autofocus_restore_home_signal = True

        self.assertTrue(app.home_signal_stop_event.is_set())
        self.assertFalse(app.home_signal_enabled)
        self.assertEqual(app.home_signal_button_var.get(), "Home Signals")
        self.assertTrue(app.autofocus_restore_home_signal)

    def test_home_polling_can_be_restored_after_exclusive_workflow(self) -> None:
        app = self.make_app_shell()
        app.home_signal_enabled = False
        app.autofocus_restore_home_signal = True

        app.toggle_home_signal_polling()

        self.assertTrue(app.home_signal_enabled)
        self.assertFalse(app.home_signal_stop_event.is_set())
        self.assertEqual(app.home_signal_button_var.get(), "Stop Home Signals")
        app.disable_home_signal_polling()

    def test_autofocus_done_restores_home_polling(self) -> None:
        app = self.make_app_shell()
        app.home_signal_enabled = False
        app.autofocus_restore_home_signal = True
        restored = []

        def restore_home(self) -> None:
            restored.append(True)
            self.home_signal_enabled = True

        app.toggle_home_signal_polling = MethodType(restore_home, app)

        ProbeApp._handle_worker_event(app, ("autofocus_done",))

        self.assertTrue(restored)
        self.assertTrue(app.home_signal_enabled)
        self.assertFalse(app.autofocus_restore_home_signal)

    def test_imgstitch_finished_restores_home_polling(self) -> None:
        app = self.make_app_shell()
        app.home_signal_enabled = False
        app.imgstitch_restore_home_signal = True
        restored = []

        def restore_home(self) -> None:
            restored.append(True)
            self.home_signal_enabled = True

        app.toggle_home_signal_polling = MethodType(restore_home, app)

        ProbeApp._handle_worker_event(app, ("imgstitch_finished",))

        self.assertTrue(restored)
        self.assertTrue(app.home_signal_enabled)
        self.assertFalse(app.imgstitch_restore_home_signal)


if __name__ == "__main__":
    unittest.main()
