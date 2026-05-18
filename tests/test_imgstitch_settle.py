from __future__ import annotations

import threading
import unittest

from semi_auto_probe.app import ProbeApp
from semi_auto_probe.config import ProbeConfig


class WaitEvent:
    def __init__(self) -> None:
        self.timeout = None

    def wait(self, timeout: float | None = None) -> bool:
        self.timeout = timeout
        return False


class ImgStitchSettleTests(unittest.TestCase):
    def test_wait_after_imgstitch_motion_uses_configured_ms(self) -> None:
        app = ProbeApp.__new__(ProbeApp)
        app.probe_config = ProbeConfig(imgstitch_settle_ms=175)
        app.imgstitch_stop_event = WaitEvent()

        ProbeApp._wait_after_imgstitch_motion(app)

        self.assertAlmostEqual(app.imgstitch_stop_event.timeout, 0.175)

    def test_wait_after_imgstitch_motion_skips_zero_ms(self) -> None:
        app = ProbeApp.__new__(ProbeApp)
        app.probe_config = ProbeConfig(imgstitch_settle_ms=0)
        app.imgstitch_stop_event = threading.Event()

        ProbeApp._wait_after_imgstitch_motion(app)

        self.assertFalse(app.imgstitch_stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
