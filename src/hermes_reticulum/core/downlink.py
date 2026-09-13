"""Downlink reliability — outbound pacing, sequence tracking, first-hop acks.

Re-lands the reverted feature from commit 1322e89 with explicit fixes for
each known regression candidate (see docs/mesh-bridge-findings-2026-09-12-
reminder-tool-calls-never-arrived.md, "Re-land (v2)" section).
"""

import logging
import os
import threading
import time

logger = logging.getLogger("hermes_reticulum.downlink")

MIN_CHUNK_INTERVAL_MS = int(os.environ.get("HERMES_CHUNK_INTERVAL_MS", "500"))
ACK_TIMEOUT_S = int(os.environ.get("HERMES_DOWNLINK_ACK_TIMEOUT_S", "300"))

# Reverse map: state value (0x04, 0x08, 0xFF, ...) → name string.
# LXMF.LXMessage.states is a LIST whose values are NOT contiguous indices
# (SENT=0x04 is index 3, DELIVERED=0x08 is index 4, FAILED=0xFF is index 7).
# The original commit's bug: states[0x08] → IndexError. We use a hardcoded
# map instead — the values are stable constants in the LXMF 1.1.1 source.
_STATE_MAP: dict[int, str] = {
    0x00: "GENERATING",
    0x01: "OUTBOUND",
    0x02: "SENDING",
    0x04: "SENT",
    0x08: "DELIVERED",
    0xFD: "REJECTED",
    0xFE: "CANCELLED",
    0xFF: "FAILED",
}

_STATE_OUTCOME = {
    "DELIVERED": "delivered",
    "SENT": "propagated",
    "FAILED": "failed",
}

# Block content budget (LXMF 1.1.1): 368 bytes max content per link block.
# A 1500-char STEP_CHUNK_CHARS part ≈ 1500 UTF-8 bytes → ~4 blocks.
# Prefix overhead is negligible relative to block boundaries.
_BLOCK_CONTENT_BUDGET = 368


def _state_name(state: int) -> str:
    return _STATE_MAP.get(state, f"state_{state:#x}")


def _state_outcome(state: int) -> str:
    return _STATE_OUTCOME.get(_state_name(state), "unknown")


def sequence_chunks(parts: list[str], tag: str) -> list[str]:
    """Prefix each part with ``[tag i/N] `` so recipients can spot a dropped
    tail without protocol changes. Single-part lists are returned unchanged.
    """
    if len(parts) <= 1:
        return parts
    n = len(parts)
    out = []
    for i, part in enumerate(parts, 1):
        prefix = f"[{tag} {i}/{n}] "
        encoded = prefix + part
        # Never let the prefix push a part over a single-block boundary.
        # If the part itself is over budget, truncate the part (not the prefix).
        max_bytes = _BLOCK_CONTENT_BUDGET - len(prefix.encode("utf-8"))
        if max_bytes <= 0:
            out.append(encoded)
            continue
        raw = part.encode("utf-8")
        if len(raw) > max_bytes:
            # Truncate on a UTF-8 codepoint boundary.
            truncated = raw[:max_bytes]
            # Drop any incomplete trailing codepoint.
            while truncated:
                try:
                    truncated.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    truncated = truncated[:-1]
            encoded = prefix + truncated.decode("utf-8")
        out.append(encoded)
    return out


class DownlinkTracker:
    """Per-recipient outbound pacing + first-hop ack tracking.

    Threading model: all state is guarded by ``self._lock``. The LXMF
    delivery callback fires on the RNS event-loop thread; pacing sleeps
    happen on the *calling* thread (the thread pool worker, the step-watcher
    thread, or the ThreadingHTTPServer handler thread). The lock is held
    only for the brief state reads/writes, never across a sleep.
    """

    MAX_OUTSTANDING = 256

    def __init__(self, ack_timeout_s: int | None = None):
        self._lock = threading.Lock()
        self._outbound: dict[int, tuple[str, float]] = {}
        self._next_seq = 0
        self._last_send: dict[str, float] = {}
        self._counters: dict[str, int] = {
            "delivered": 0,
            "propagated": 0,
            "failed": 0,
            "timeout": 0,
            "unknown": 0,
        }
        self._push_counter = 0
        self._ack_timeout_s = ack_timeout_s if ack_timeout_s is not None else ACK_TIMEOUT_S

    # ------------------------------------------------------------------
    # Sequence allocation
    # ------------------------------------------------------------------

    def next_seq(self) -> int:
        """Allocate a monotonically increasing outbound sequence number.

        Also runs the lazy ack-timeout sweep: any seq older than
        ``ack_timeout_s`` that never got a callback is counted as
        ``timeout`` and logged.
        """
        with self._lock:
            self._sweep_timeouts_locked()
            self._next_seq += 1
            seq = self._next_seq
            self._outbound[seq] = ("", time.monotonic())
            if len(self._outbound) > self.MAX_OUTSTANDING:
                oldest = min(self._outbound, key=lambda s: self._outbound[s][1])
                del self._outbound[oldest]
            return seq

    def register_dispatch(self, seq: int, recipient_hex: str) -> None:
        """Record the recipient for a dispatched seq (called from send_reply
        after a successful handle_outbound)."""
        with self._lock:
            if seq in self._outbound:
                _, t = self._outbound[seq]
                self._outbound[seq] = (recipient_hex, t)

    # ------------------------------------------------------------------
    # Pacing
    # ------------------------------------------------------------------

    def pace_wait(self, recipient_hex: str, interval_ms: int | None = None) -> float:
        """Sleep until at least ``interval_ms`` has passed since the last
        *successful* send to this recipient. Returns the sleep duration.

        Design: the send time is recorded by ``record_send`` (called from
        send_reply on success), NOT here. This means a failed send does not
        consume pacing budget.
        """
        interval_ms = interval_ms if interval_ms is not None else MIN_CHUNK_INTERVAL_MS
        interval_s = interval_ms / 1000.0
        with self._lock:
            last = self._last_send.get(recipient_hex)
        if last is None:
            return 0.0
        elapsed = time.monotonic() - last
        wait = interval_s - elapsed
        if wait <= 0:
            return 0.0
        time.sleep(wait)
        return wait

    def record_send(self, recipient_hex: str) -> None:
        """Record a successful send. Called from send_reply after
        handle_outbound returns without exception."""
        with self._lock:
            self._last_send[recipient_hex] = time.monotonic()

    # ------------------------------------------------------------------
    # Outcome tracking
    # ------------------------------------------------------------------

    def note_outcome(self, seq: int, outcome: str) -> None:
        """Record a first-hop outcome for a dispatched seq. Idempotent:
        a second call for the same seq is a no-op (the seq was already
        popped)."""
        with self._lock:
            if seq not in self._outbound:
                return
            self._outbound.pop(seq, None)
        if outcome in self._counters:
            self._counters[outcome] += 1

    # ------------------------------------------------------------------
    # Push tagging
    # ------------------------------------------------------------------

    def next_push_tag(self) -> str:
        """Allocate a per-push monotonic tag (``p<N>``) for sequence_chunks."""
        with self._lock:
            self._push_counter += 1
            return f"p{self._push_counter}"

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            outstanding = len(self._outbound)
            counters = dict(self._counters)
        counters["outstanding"] = outstanding
        counters["next_seq"] = self._next_seq
        return counters

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sweep_timeouts_locked(self) -> None:
        """Count and log any dispatched seq that never got a callback within
        ``ack_timeout_s``. Must be called with ``self._lock`` held."""
        now = time.monotonic()
        expired = []
        for seq, (recipient, t) in self._outbound.items():
            if now - t > self._ack_timeout_s:
                expired.append((seq, recipient))
        for seq, recipient in expired:
            self._outbound.pop(seq, None)
            self._counters["timeout"] += 1
            logger.info(
                "Downlink ack seq=%d → %s state=timeout (no first-hop ack within %ds)",
                seq, recipient[:16] if recipient else "?", self._ack_timeout_s,
            )
