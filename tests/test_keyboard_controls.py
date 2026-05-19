from __future__ import annotations

import unittest

from semi_auto_probe.app import ProbeApp
from semi_auto_probe.config import KEYBOARD_MOTION_SCHEME_WASD_QE, ProbeConfig


class DummyVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def set(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value


class KeyboardControlsTests(unittest.TestCase):
    def make_app_shell(self, scheme: str | None = None) -> ProbeApp:
        app = ProbeApp.__new__(ProbeApp)
        app.probe_config = ProbeConfig()
        if scheme is not None:
            app.probe_config.keyboard_motion_scheme = scheme
        return app

    def test_default_keyboard_scheme_uses_arrows_and_page_keys(self) -> None:
        app = self.make_app_shell()

        bindings = ProbeApp._keyboard_bindings_for_configured_scheme(app)

        self.assertEqual(bindings["Right"], ("X", False))
        self.assertEqual(bindings["Left"], ("X", True))
        self.assertEqual(bindings["Prior"], ("Z", False))
        self.assertEqual(bindings["Next"], ("Z", True))

    def test_wasd_keyboard_scheme_uses_wasd_and_qe(self) -> None:
        app = self.make_app_shell(KEYBOARD_MOTION_SCHEME_WASD_QE)

        bindings = ProbeApp._keyboard_bindings_for_configured_scheme(app)

        self.assertEqual(bindings["d"], ("X", False))
        self.assertEqual(bindings["a"], ("X", True))
        self.assertEqual(bindings["w"], ("Y", False))
        self.assertEqual(bindings["s"], ("Y", True))
        self.assertEqual(bindings["q"], ("Z", False))
        self.assertEqual(bindings["e"], ("Z", True))

    def test_cycle_jog_step_uses_configured_levels_for_axis(self) -> None:
        app = self.make_app_shell()
        app.jog_step_levels = {"X": (2, 4), "Y": (1,), "Z": (1,)}
        app.step_vars = {"X": DummyVar("2")}
        app.status_var = DummyVar()

        ProbeApp.cycle_jog_step(app, "X")

        self.assertEqual(app.step_vars["X"].get(), "4")
        ProbeApp.cycle_jog_step(app, "X")
        self.assertEqual(app.step_vars["X"].get(), "2")

    def test_numeric_input_validators_reject_wrong_type(self) -> None:
        self.assertTrue(ProbeApp._integer_text_allowed("123", minimum=0, maximum=1000))
        self.assertFalse(ProbeApp._integer_text_allowed("12.3", minimum=0, maximum=1000))
        self.assertFalse(ProbeApp._integer_text_allowed("-1", minimum=0, maximum=1000))
        self.assertTrue(ProbeApp._float_text_allowed("12.3", minimum=0, maximum=1000))
        self.assertFalse(ProbeApp._float_text_allowed("12.3.4", minimum=0, maximum=1000))
        self.assertFalse(ProbeApp._float_text_allowed("-1", minimum=0, maximum=1000))
        self.assertTrue(ProbeApp._jog_step_text_allowed("1, 10; 1000"))
        self.assertFalse(ProbeApp._jog_step_text_allowed("1, ten"))


if __name__ == "__main__":
    unittest.main()
