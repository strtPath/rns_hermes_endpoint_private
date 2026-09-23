"""Unit tests for the downlink reliability module (re-land of 1322e89).

Covers: sequence_chunks, DownlinkTracker (next_seq, pace_wait, note_outcome,
timeout sweep), _on_outbound state mapping (the reverse-map bug fix), and
send_reply callback registration.
"""

import logging
import time
from unittest.mock import MagicMock, patch

from hermes_reticulum.core.downlink import (
    DownlinkTracker,
    _state_name,
    _state_outcome,
    sequence_chunks,
)

# ---------------------------------------------------------------------------
# sequence_chunks
# ---------------------------------------------------------------------------

class TestSequenceChunks:
    def test_single_part_no_prefix(self):
        assert sequence_chunks(["hello"], "p1") == ["hello"]

    def test_multi_part_prefixes(self):
        parts = sequence_chunks(["a", "b", "c"], "p1")
        assert parts[0].startswith("[p1 1/3] ")
        assert parts[1].startswith("[p1 2/3] ")
        assert parts[2].startswith("[p1 3/3] ")

    def test_multi_part_order_preserved(self):
        parts = sequence_chunks(["a", "b", "c"], "p1")
        assert parts[0].endswith("a")
        assert parts[1].endswith("b")
        assert parts[2].endswith("c")

    def test_multi_part_correct_n(self):
        parts = sequence_chunks(["x", "y"], "p9")
        assert "1/2" in parts[0]
        assert "2/2" in parts[1]

    def test_over_budget_part_truncated_not_prefix(self):
        # A part longer than the block content budget minus the prefix.
        long_part = "A" * 400
        parts = sequence_chunks([long_part], "p1")
        # Single part → no prefix, returned as-is.
        assert parts == [long_part]

    def test_over_budget_multi_part_truncates_part(self):
        # 300-char part with a 12-char prefix → fits in 368.
        # 400-char part → truncated to fit.
        parts = sequence_chunks(["B" * 400, "C" * 10], "p1")
        # First part: prefix "[p1 1/2] " (9 chars) + truncated part.
        assert len(parts[0].encode("utf-8")) <= 368
        assert parts[0].startswith("[p1 1/2] ")
        # Second part: prefix "[p1 2/2] " (9 chars) + "C" * 10.
        assert len(parts[1].encode("utf-8")) <= 368
        assert parts[1].startswith("[p1 2/2] ")


# ---------------------------------------------------------------------------
# next_seq
# ---------------------------------------------------------------------------

class TestNextSeq:
    def test_monotonic(self):
        t = DownlinkTracker()
        seqs = [t.next_seq() for _ in range(10)]
        assert seqs == list(range(1, 11))

    def test_records_dispatch_time(self):
        t = DownlinkTracker()
        t.next_seq()
        with t._lock:
            assert 1 in t._outbound
            _, ts = t._outbound[1]
            assert ts > 0

    def test_prunes_to_256(self):
        t = DownlinkTracker()
        for _ in range(300):
            t.next_seq()
        with t._lock:
            assert len(t._outbound) <= 256

    def test_register_dispatch_sets_recipient(self):
        t = DownlinkTracker()
        seq = t.next_seq()
        t.register_dispatch(seq, "abcd1234")
        with t._lock:
            recipient, _ = t._outbound[seq]
            assert recipient == "abcd1234"


# ---------------------------------------------------------------------------
# pace_wait
# ---------------------------------------------------------------------------

class TestPaceWait:
    def test_first_call_returns_zero(self):
        t = DownlinkTracker()
        start = time.monotonic()
        wait = t.pace_wait("recip", 500)
        elapsed = time.monotonic() - start
        assert wait == 0.0
        assert elapsed < 0.1

    def test_second_call_sleeps(self):
        t = DownlinkTracker()
        t.record_send("recip")
        start = time.monotonic()
        wait = t.pace_wait("recip", 200)
        elapsed = time.monotonic() - start
        assert wait > 0
        assert elapsed >= 0.15  # ~200ms minus scheduling jitter

    def test_failed_send_does_not_corrupt_pacing(self):
        """The key pacing fix: a failed send (no record_send) must not
        advance the pacing clock.

        Scenario:
        - Successful send at t=0 (record_send called).
        - A FAILED send at t=50ms (NO record_send — the bug in the original).
        - At t=100ms, the next pace_wait should sleep until t=100ms
          (100ms since the last *successful* send), NOT until t=150ms
          (100ms since the failed send).

        If the failed send had corrupted the clock, the next pace_wait
        would sleep until t=150ms instead of t=100ms.
        """
        t = DownlinkTracker()
        # Successful send at t=0.
        t.record_send("recip")
        # Simulate a failed send: no record_send call.
        # (In the original bug, pace_wait was called before send_reply,
        # so a failed send would have advanced the clock.)
        time.sleep(0.05)  # t=50ms
        # Now at t=50ms, call pace_wait with a 100ms interval.
        # If the clock is at t=0 (correct), it sleeps 50ms (until t=100ms).
        # If the clock was corrupted to t=50ms (bug), it sleeps 100ms
        # (until t=150ms).
        start = time.monotonic()
        t.pace_wait("recip", 100)
        elapsed = time.monotonic() - start
        # Correct behavior: ~50ms sleep (100ms - 50ms elapsed since t=0).
        # Buggy behavior: ~100ms sleep (100ms - 0ms since corrupted clock).
        assert elapsed < 0.09, (
            f"Pacing clock corrupted: elapsed={elapsed:.3f}s > 90ms. "
            f"Expected ~50ms (100ms interval - 50ms already elapsed)."
        )
        assert elapsed >= 0.04, (
            f"Expected ~50ms sleep, got {elapsed:.3f}s"
        )

    def test_pacing_per_recipient(self):
        t = DownlinkTracker()
        t.record_send("recip1")
        # Different recipient: no pacing.
        start = time.monotonic()
        wait = t.pace_wait("recip2", 500)
        elapsed = time.monotonic() - start
        assert wait == 0.0
        assert elapsed < 0.1


# ---------------------------------------------------------------------------
# note_outcome
# ---------------------------------------------------------------------------

class TestNoteOutcome:
    def test_increments_counters(self):
        t = DownlinkTracker()
        seq = t.next_seq()
        t.register_dispatch(seq, "abcd")
        t.note_outcome(seq, "delivered")
        assert t.stats()["delivered"] == 1

    def test_pops_seq(self):
        t = DownlinkTracker()
        seq = t.next_seq()
        t.note_outcome(seq, "delivered")
        with t._lock:
            assert seq not in t._outbound

    def test_unknown_seq_noop(self):
        t = DownlinkTracker()
        t.note_outcome(9999, "delivered")
        assert t.stats()["delivered"] == 0

    def test_double_call_idempotent(self):
        t = DownlinkTracker()
        seq = t.next_seq()
        t.register_dispatch(seq, "abcd")
        t.note_outcome(seq, "delivered")
        t.note_outcome(seq, "delivered")  # Second call: no-op.
        assert t.stats()["delivered"] == 1

    def test_all_outcomes_tracked(self):
        for outcome in ["delivered", "propagated", "failed", "timeout", "unknown"]:
            t = DownlinkTracker()
            seq = t.next_seq()
            t.note_outcome(seq, outcome)
            assert t.stats()[outcome] == 1


# ---------------------------------------------------------------------------
# _on_outbound state mapping (the reverse-map bug fix)
# ---------------------------------------------------------------------------

class TestStateMapping:
    def test_state_name_delivered(self):
        """DELIVERED=0x08 must map to 'DELIVERED', not 'state_8' (the bug)."""
        assert _state_name(0x08) == "DELIVERED"

    def test_state_name_sent(self):
        assert _state_name(0x04) == "SENT"

    def test_state_name_failed(self):
        assert _state_name(0xFF) == "FAILED"

    def test_state_name_unknown(self):
        assert _state_name(0x42) == "state_0x42"

    def test_state_outcome_delivered(self):
        assert _state_outcome(0x08) == "delivered"

    def test_state_outcome_sent(self):
        assert _state_outcome(0x04) == "propagated"

    def test_state_outcome_failed(self):
        assert _state_outcome(0xFF) == "failed"

    def test_state_outcome_unknown(self):
        assert _state_outcome(0x42) == "unknown"


# ---------------------------------------------------------------------------
# Timeout sweep
# ---------------------------------------------------------------------------

class TestTimeoutSweep:
    def test_expired_seq_counted_timeout(self):
        t = DownlinkTracker(ack_timeout_s=1)
        seq = t.next_seq()
        t.register_dispatch(seq, "abcd")
        # Manually age the dispatch time.
        with t._lock:
            t._outbound[seq] = ("abcd", time.monotonic() - 2.0)
        # Next next_seq triggers the sweep.
        t.next_seq()
        assert t.stats()["timeout"] == 1

    def test_fresh_seq_not_timed_out(self):
        t = DownlinkTracker(ack_timeout_s=300)
        t.next_seq()  # Triggers sweep.
        assert t.stats()["timeout"] == 0

    def test_timeout_logged(self, caplog):
        caplog.set_level(logging.INFO, logger="hermes_reticulum.downlink")
        t = DownlinkTracker(ack_timeout_s=1)
        seq = t.next_seq()
        t.register_dispatch(seq, "abcd")
        with t._lock:
            t._outbound[seq] = ("abcd", time.monotonic() - 2.0)
        t.next_seq()
        assert any(
            "state=timeout" in r.message and "no first-hop ack" in r.message
            for r in caplog.records
        )

    def test_no_new_thread(self):
        """The sweep is lazy (on next_seq), not a daemon thread."""
        t = DownlinkTracker(ack_timeout_s=1)
        t.next_seq()
        # No thread was started.
        import threading
        assert not any(
            th.name.startswith("downlink") for th in threading.enumerate()
        )


# ---------------------------------------------------------------------------
# send_reply callback registration
# ---------------------------------------------------------------------------

class TestSendReplyCallback:
    """Test that send_reply registers a delivery callback and that
    invoking it produces the 'Downlink ack' log."""

    def _make_bridge(self):
        from hermes_reticulum.core.bridge import LXMFBridge
        b = LXMFBridge.__new__(LXMFBridge)
        b.router = MagicMock()
        b.destination = MagicMock()
        b.downlink = DownlinkTracker()
        return b

    def test_register_delivery_callback_called(self):
        b = self._make_bridge()
        mock_lxm = MagicMock()
        with patch("hermes_reticulum.core.bridge.LXMF") as mock_lxmf:
            mock_lxmf.LXMessage.return_value = mock_lxm
            mock_lxmf.LXMessage.DIRECT = 0
            with patch("hermes_reticulum.core.bridge.RNS") as mock_rns:
                mock_rns.Identity.recall.return_value = MagicMock()
                mock_rns.Destination.return_value = MagicMock()
                result = b.send_reply("abcd1234", "hello")
        assert result is True
        mock_lxm.register_delivery_callback.assert_called_once()
        cb = mock_lxm.register_delivery_callback.call_args[0][0]
        assert callable(cb)

    def test_invoking_callback_produces_ack_log(self, caplog):
        caplog.set_level(logging.INFO, logger="hermes_reticulum.bridge")
        b = self._make_bridge()
        mock_lxm = MagicMock()
        with patch("hermes_reticulum.core.bridge.LXMF") as mock_lxmf:
            mock_lxmf.LXMessage.return_value = mock_lxm
            mock_lxmf.LXMessage.DIRECT = 0
            with patch("hermes_reticulum.core.bridge.RNS") as mock_rns:
                mock_rns.Identity.recall.return_value = MagicMock()
                mock_rns.Destination.return_value = MagicMock()
                b.send_reply("abcd1234", "hello")
        # Grab the callback and invoke it with a fake DELIVERED message.
        cb = mock_lxm.register_delivery_callback.call_args[0][0]
        fake_msg = MagicMock()
        fake_msg.state = 0x08  # DELIVERED
        cb(fake_msg)
        assert any(
            "Downlink ack" in r.message and "state=delivered" in r.message
            for r in caplog.records
        )

    def test_failed_send_does_not_advance_pacing(self):
        """A failed send (send_reply returns False) must not call
        record_send, so the pacing clock is preserved."""
        b = self._make_bridge()
        # Force handle_outbound to raise.
        b.router.handle_outbound.side_effect = Exception("send failed")
        with patch("hermes_reticulum.core.bridge.LXMF") as mock_lxmf:
            mock_lxmf.LXMessage.return_value = MagicMock()
            mock_lxmf.LXMessage.DIRECT = 0
            with patch("hermes_reticulum.core.bridge.RNS") as mock_rns:
                mock_rns.Identity.recall.return_value = MagicMock()
                mock_rns.Destination.return_value = MagicMock()
                result = b.send_reply("abcd1234", "hello")
        assert result is False
        # No record_send was called.
        assert "abcd1234" not in b.downlink._last_send
