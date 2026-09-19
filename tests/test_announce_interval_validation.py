"""Regression tests for rejecting non-finite announce intervals.

RETICULUM_ANNOUNCE_INTERVAL and /announce <minutes> both flow through
float() and range checks that previously accepted nan and inf (every
comparison with nan is False, so nan passed the < 0 and min checks; inf
created a timer whose wait never completes). These pin that non-finite
values are rejected BEFORE they mutate scheduler state.
"""
import math
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestAnnounceGuard(unittest.TestCase):
    """The announce() method guard rejects non-finite cadences up front."""

    def _bare_bridge(self):
        from hermes_reticulum.core.bridge import LXMFBridge

        b = LXMFBridge.__new__(LXMFBridge)
        b._announce_lock = mock.MagicMock()
        b._running = True
        b.destination = mock.MagicMock()
        b.announce_interval_min = 30.0
        return b

    def test_nan_rejected_by_announce(self):
        b = self._bare_bridge()
        with self.assertRaises(ValueError):
            b.announce(interval_min=math.nan)

    def test_inf_rejected_by_announce(self):
        b = self._bare_bridge()
        with self.assertRaises(ValueError):
            b.announce(interval_min=math.inf)

    def test_negative_inf_rejected_by_announce(self):
        b = self._bare_bridge()
        with self.assertRaises(ValueError):
            b.announce(interval_min=-math.inf)

    def test_state_not_mutated_on_reject(self):
        b = self._bare_bridge()
        before = b.announce_interval_min
        with self.assertRaises(ValueError):
            b.announce(interval_min=math.inf)
        self.assertEqual(
            b.announce_interval_min, before,
            "a rejected interval must not update the scheduler cadence",
        )

    def test_finite_zero_still_allowed(self):
        # 0 = disable periodic re-announce; must NOT be rejected by the
        # finite guard (it is a legitimate value).
        b = self._bare_bridge()
        b.announce_interval_min = 30.0
        # announce() with 0 won't start a timer (interval==0 path), but must
        # not raise on the finiteness check. destination exists so it returns
        # cleanly; we only assert no ValueError surfaces.
        try:
            b.announce(interval_min=0.0)
        except ValueError:
            self.fail("interval=0 must be accepted by the finite guard")


class TestAnnounceEnvParse(unittest.TestCase):
    """The LXMFBridge constructor rejects non-finite RETICULUM_ANNOUNCE_INTERVAL.

    The env-parse guard fires at the very start of __init__ (before any RNS
    machinery is constructed), so a ValueError here can be asserted with a
    bare constructor call.
    """

    def _construct(self):
        from hermes_reticulum.core.bridge import LXMFBridge

        return LXMFBridge(display_name="test")

    def test_env_nan_rejected(self):
        with mock.patch.dict(
            os.environ,
            {"RETICULUM_ANNOUNCE_INTERVAL": "nan"},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                self._construct()

    def test_env_inf_rejected(self):
        with mock.patch.dict(
            os.environ,
            {"RETICULUM_ANNOUNCE_INTERVAL": "inf"},
            clear=False,
        ):
            with self.assertRaises(ValueError):
                self._construct()

    def test_env_valid_passes(self):
        with mock.patch.dict(
            os.environ,
            {"RETICULUM_ANNOUNCE_INTERVAL": "30"},
            clear=False,
        ):
            # A valid finite value must not raise at the announce-parse guard.
            try:
                b = self._construct()
            except ValueError:
                self.fail("valid RETICULUM_ANNOUNCE_INTERVAL=30 must not raise")
            self.assertEqual(b.env_announce_interval_min, 30.0)


if __name__ == "__main__":
    unittest.main()
