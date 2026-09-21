"""Regression tests for the gate-timeout relationship check.

The pre-exec gate has two bridge-side timeouts (MESH_GATE_TIMEOUT and
HERMES_MESH_APPROVAL_TIMEOUT) that must satisfy MESH_GATE_TIMEOUT >=
HERMES_MESH_APPROVAL_TIMEOUT, or the plugin's blocking POST fails closed
before the operator can answer. These pin that contract so a config that
breaks it fails loudly at startup instead of silently capping the operator
window at runtime.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hermes_reticulum.cli import _validate_gate_timeouts  # noqa: E402


class TestGateTimeoutValidation(unittest.TestCase):
    def setUp(self):
        self._saved = {
            var: os.environ.pop(var, None)
            for var in ("MESH_GATE_TIMEOUT", "HERMES_MESH_APPROVAL_TIMEOUT")
        }
        self._exit_calls = []
        self._orig_exit = sys.exit
        sys.exit = self._exit_calls.append

    def tearDown(self):
        sys.exit = self._orig_exit
        for var, value in self._saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value

    def test_valid_equal_values_passes(self):
        os.environ["MESH_GATE_TIMEOUT"] = "480"
        os.environ["HERMES_MESH_APPROVAL_TIMEOUT"] = "480"
        _validate_gate_timeouts()
        self.assertEqual(self._exit_calls, [], "should not exit on valid config")

    def test_valid_gate_greater_passes(self):
        os.environ["MESH_GATE_TIMEOUT"] = "590"
        os.environ["HERMES_MESH_APPROVAL_TIMEOUT"] = "480"
        _validate_gate_timeouts()
        self.assertEqual(self._exit_calls, [], "gate>approval is valid")

    def test_gate_less_than_approval_exits(self):
        os.environ["MESH_GATE_TIMEOUT"] = "120"
        os.environ["HERMES_MESH_APPROVAL_TIMEOUT"] = "900"
        _validate_gate_timeouts()
        self.assertEqual(len(self._exit_calls), 1, "misconfig must exit(1)")

    def test_defaults_are_valid(self):
        for var in ("MESH_GATE_TIMEOUT", "HERMES_MESH_APPROVAL_TIMEOUT"):
            os.environ.pop(var, None)
        _validate_gate_timeouts()
        self.assertEqual(self._exit_calls, [], "defaults (900/900) are valid")

    def test_non_numeric_exits(self):
        os.environ["MESH_GATE_TIMEOUT"] = "abc"
        os.environ["HERMES_MESH_APPROVAL_TIMEOUT"] = "480"
        _validate_gate_timeouts()
        self.assertEqual(len(self._exit_calls), 1, "non-numeric must exit(1)")

    def test_zero_exits(self):
        os.environ["MESH_GATE_TIMEOUT"] = "0"
        os.environ["HERMES_MESH_APPROVAL_TIMEOUT"] = "0"
        _validate_gate_timeouts()
        self.assertEqual(len(self._exit_calls), 1,
                         "zero would deny every gated action immediately")

    def test_negative_exits(self):
        os.environ["MESH_GATE_TIMEOUT"] = "-1"
        os.environ["HERMES_MESH_APPROVAL_TIMEOUT"] = "-1"
        _validate_gate_timeouts()
        self.assertEqual(len(self._exit_calls), 1,
                         "negative Event.wait(-1) returns immediately — invalid")

    def test_nan_exits(self):
        os.environ["MESH_GATE_TIMEOUT"] = "nan"
        os.environ["HERMES_MESH_APPROVAL_TIMEOUT"] = "480"
        _validate_gate_timeouts()
        self.assertEqual(len(self._exit_calls), 1, "NaN is not a valid timeout")

    def test_infinity_exits(self):
        os.environ["MESH_GATE_TIMEOUT"] = "inf"
        os.environ["HERMES_MESH_APPROVAL_TIMEOUT"] = "480"
        _validate_gate_timeouts()
        self.assertEqual(len(self._exit_calls), 1, "infinity is not a valid timeout")

    def test_decimal_values_pass(self):
        # Non-integer finite positive values are legitimate (e.g. 0.5).
        os.environ["MESH_GATE_TIMEOUT"] = "30.5"
        os.environ["HERMES_MESH_APPROVAL_TIMEOUT"] = "30.5"
        _validate_gate_timeouts()
        self.assertEqual(self._exit_calls, [], "finite positive decimals are valid")


if __name__ == "__main__":
    unittest.main()
