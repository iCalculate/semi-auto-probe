from __future__ import annotations

import os
import threading
import unittest
from unittest.mock import patch
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

    def test_admin_mode_requires_config_token(self) -> None:
        app = ProbeApp.__new__(ProbeApp)
        app.admin_mode_enabled = False
        app.admin_token_var = DummyVar("wrong")
        app.admin_mode_status_var = DummyVar("Admin mode locked")
        app.status_var = DummyVar()
        app.serial_client = None
        app.set_xyz_zero_button = None
        app.set_autofocus_z_zero_button = None

        with patch.dict(os.environ, {"SEMI_AUTO_PROBE_ADMIN_TOKEN": "secret-token"}, clear=False):
            ProbeApp.enable_admin_mode(app)
            self.assertFalse(app.admin_mode_enabled)
            self.assertIn("invalid token", app.admin_mode_status_var.get())

            app.admin_token_var.set("secret-token")
            ProbeApp.enable_admin_mode(app)

        self.assertTrue(app.admin_mode_enabled)
        self.assertEqual(app.admin_token_var.get(), "")


if __name__ == "__main__":
    unittest.main()
