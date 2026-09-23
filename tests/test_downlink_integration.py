"""Integration test for the downlink re-land (v2).

Exercises the real ``LXMFBridge.push_reply`` code path. The LXMessage
constructor is mocked (no radio in the test env), but the pacing, tagging,
and dispatch-log logic are real.

The ack assertion is intentionally absent: real LoRa first-hop acks require
a real RNode radio. The unit tests (``test_downlink.py``) cover the ack
state-mapping and timeout-sweep logic in isolation.
"""

import logging
import re
import time
from unittest.mock import MagicMock, patch

from hermes_reticulum.core.downlink import sequence_chunks


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord):
        self.records.append(record)


def _make_test_bridge():
    """Create a bridge with a mocked router (no RNS)."""
    from hermes_reticulum.core.bridge import LXMFBridge
    from hermes_reticulum.core.downlink import DownlinkTracker

    bridge = LXMFBridge.__new__(LXMFBridge)
    bridge.router = MagicMock()
    bridge.destination = MagicMock()
    bridge.downlink = DownlinkTracker()
    return bridge


def _patch_send():
    """Patch RNS.Identity.recall, RNS.Destination, and the LXMessage
    constructor so send_reply works without a real RNS instance."""
    mock_identity = MagicMock()
    mock_dest = MagicMock()
    mock_lxm = MagicMock()
    p_recall = patch(
        "hermes_reticulum.core.bridge.RNS.Identity.recall",
        return_value=mock_identity,
    )
    # Mock RNS.Destination so the real constructor doesn't reject the mock.
    p_dest = patch(
        "hermes_reticulum.core.bridge.RNS.Destination",
        return_value=mock_dest,
    )
    # Mock the LXMessage class so the constructor returns our mock.
    p_lxmsg = patch(
        "hermes_reticulum.core.bridge.LXMF.LXMessage",
        return_value=mock_lxm,
    )
    p_recall.start()
    p_dest.start()
    p_lxmsg.start()
    return p_recall, p_dest, p_lxmsg, mock_lxm


def _unpatch(p_recall, p_dest, p_lxmsg):
    p_recall.stop()
    p_dest.stop()
    p_lxmsg.stop()


def test_push_reply_three_chunks_dispatch():
    """Send a 4000-char message (→ 3 parts via split_message) via
    push_reply and assert 3 dispatch lines with monotonic seq."""
    bridge = _make_test_bridge()
    handler = _CaptureHandler()
    bridge_logger = logging.getLogger("hermes_reticulum.bridge")
    bridge_logger.addHandler(handler)
    bridge_logger.setLevel(logging.DEBUG)

    try:
        p_recall, p_dest, p_lxmsg, mock_lxm = _patch_send()
        try:
            bridge.push_reply("abcd1234", "x" * 4000)
        finally:
            _unpatch(p_recall, p_dest, p_lxmsg)

        dispatch_lines = [
            r for r in handler.records
            if "Reply dispatched" in r.getMessage() and "seq=" in r.getMessage()
        ]
        assert len(dispatch_lines) == 3, (
            f"Expected 3 dispatch lines, got {len(dispatch_lines)}: "
            f"{[r.getMessage() for r in dispatch_lines]}"
        )

        seqs = [
            int(re.search(r"seq=(\d+)", r.getMessage()).group(1))
            for r in dispatch_lines
        ]
        assert seqs == sorted(seqs), f"seqs not monotonic: {seqs}"

        # Verify the dispatch log format.
        for r in dispatch_lines:
            assert r.getMessage().startswith("Reply dispatched to ")
            assert "seq=" in r.getMessage()
            assert "bytes" in r.getMessage()

        # Verify the multi-part push was tagged with [p1 i/N].
        # The tagging is in the message content, not the log line.
        # Check that sequence_chunks was called (via the tag in downlink).
        assert bridge.downlink._push_counter == 1, (
            "Expected push tag p1 to be allocated"
        )

    finally:
        bridge_logger.removeHandler(handler)


def test_sequence_chunks_tagging_in_push():
    """Verify that sequence_chunks produces correct tags for a 3-part push."""
    parts = sequence_chunks(["hello", "world", "foo"], "p1")
    assert parts[0].startswith("[p1 1/3] ")
    assert parts[1].startswith("[p1 2/3] ")
    assert parts[2].startswith("[p1 3/3] ")
    assert parts[0].endswith("hello")
    assert parts[1].endswith("world")
    assert parts[2].endswith("foo")


def test_pacing_interval_enforced():
    """Verify that push_reply paces between chunks.

    Uses a 50ms interval and 3 chunks (2 gaps) → expects ≥ 100ms total.
    """
    bridge = _make_test_bridge()
    handler = _CaptureHandler()
    bridge_logger = logging.getLogger("hermes_reticulum.bridge")
    bridge_logger.addHandler(handler)
    bridge_logger.setLevel(logging.DEBUG)

    try:
        p_recall, p_dest, p_lxmsg, mock_lxm = _patch_send()
        try:
            with patch(
                "hermes_reticulum.core.bridge.MIN_CHUNK_INTERVAL_MS",
                50,
            ):
                start = time.monotonic()
                bridge.push_reply("abcd1234", "x" * 4000)
                elapsed = time.monotonic() - start
        finally:
            _unpatch(p_recall, p_dest, p_lxmsg)

        # 2 gaps × 50ms = 100ms minimum (allow 15ms jitter).
        assert elapsed >= 0.085, (
            f"Pacing not enforced: elapsed={elapsed:.3f}s < 85ms"
        )

    finally:
        bridge_logger.removeHandler(handler)
