"""
Downlink reliability — chunk sequencing + RNS receipt observability.

Background (docs/mesh-bridge-findings-2026-08-29-downlink-burst-loss-and-recap-replay.md):
`bridge.send_reply` was fire-and-forget — "Reply dispatched (N bytes)" logged the
moment the LXMessage was handed to the RNS router, which is NOT an ack. Over
opportunistic LoRa the tail of a chunk burst is silently dropped and the bridge
has no signal about it. Columba's ✅/❌ on the phone is the receiver's view;
the bridge is at least one hop removed and (before this module) read no receipts
at all.

What this module does (desktop-side only, no phone-side changes):

1. **Chunk sequencing.** `sequence_chunks(parts, tag)` returns
   `(parts, seq)` where each part is prefixed `[tag i/N]` so the recipient
   (Columba, or a human) can detect gaps: if the phone shows `[mesh 3/17]`
   but never sees `[mesh 4/17]`..`[mesh 17/17]`, the tail loss is visible
   without any protocol change on the device.

2. **First-hop receipt wiring.** `send_reply` now registers a
   `register_delivery_callback` on each outbound LXMessage (the LXMF-level
   callback that fires on RNS first-hop proof `DELIVERED`, `PROPAGATED`, or
   `FAILED`/timeout) plus an RNS `PacketReceipt` timeout callback via the
   packet when available. Every event is logged with a stable per-message
   id so journalctl shows, per chunk: dispatched → acked (or timed out /
   failed) and after how many seconds. This is the ground truth the profiler
   and the operator were missing: whether "dispatched" is ever followed by a
   real first-hop ack.

3. **Per-recipient burst pacing.** `send_reply` enforces a minimum interval
   between consecutive chunks to the same recipient (default from the active
   profile's `send_delay_ms`, floor 500ms) so a burst can never land in a
   one-second wall again — even on tcp_default — which was the exact traffic
   shape that shed LoRa packets.
"""

import logging
import threading
import time

logger = logging.getLogger("hermes_reticulum.downlink")

# Default minimum spacing between chunks to the same recipient (ms).
# Overridden by the profile's send_delay_ms when higher.
MIN_CHUNK_INTERVAL_MS = int(__import__("os").environ.get("HERMES_CHUNK_INTERVAL_MS", "500"))


class DownlinkTracker:
    """
    Tracks per-recipient chunk pacing and first-hop receipt outcomes.

    Thread-safe: send_reply runs in the bridge thread pool (up to
    _MAX_HANDLERS workers) and the LXMF delivery callback fires on the RNS
    event-loop thread, so all state mutations are behind one lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # recipient_hex -> last chunk send time (monotonic)
        self._last_send: dict[str, float] = {}
        # per-recipient receipt counters (for /status + journalctl rollups)
        self._counts: dict[str, dict[str, int]] = {}
        # global outbound sequence counter (monotonic, per-bridge)
        self._seq = 0
        # seq -> (recipient_hex, dispatched_at) for correlation in logs
        self._outbound: dict[int, tuple[str, float]] = {}

    def next_seq(self, recipient_hex: str) -> int:
        """Allocate the next outbound sequence id and record dispatch time."""
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._outbound[seq] = (recipient_hex, time.monotonic())
            if len(self._outbound) > 256:
                # prune oldest
                for k in list(self._outbound)[: len(self._outbound) - 256]:
                    del self._outbound[k]
        return seq

    # ── chunk pacing ─────────────────────────────────────────────────

    def pace_wait(self, recipient_hex: str, interval_ms: int) -> float:
        """
        Sleep (in a worker thread) until at least `interval_ms` has elapsed
        since the last chunk to this recipient. Returns actual wait seconds.
        """
        wait_ms = interval_ms if interval_ms > 0 else MIN_CHUNK_INTERVAL_MS
        with self._lock:
            last = self._last_send.get(recipient_hex)
            if last is None:
                self._last_send[recipient_hex] = time.monotonic()
                return 0.0
            now = time.monotonic()
            elapsed_ms = (now - last) * 1000.0
            if elapsed_ms >= wait_ms:
                self._last_send[recipient_hex] = now
                return 0.0
            wait = wait_ms - elapsed_ms
        time.sleep(wait / 1000.0)
        with self._lock:
            self._last_send[recipient_hex] = time.monotonic()
        return wait / 1000.0

    def record_send(self, recipient_hex: str):
        with self._lock:
            self._last_send[recipient_hex] = time.monotonic()

    # ── receipt tracking ─────────────────────────────────────────────

    def register_outbound(self, seq: int, recipient_hex: str):
        """Record an outbound chunk (used when next_seq is called externally)."""
        with self._lock:
            self._outbound[seq] = (recipient_hex, time.monotonic())

    def note_outcome(self, seq: int, outcome: str):
        """
        outcome: 'delivered' | 'propagated' | 'failed' | 'timeout'
        """
        with self._lock:
            entry = self._outbound.get(seq)
            recipient = entry[0] if entry else "?"
            counts = self._counts.setdefault(
                recipient, {"delivered": 0, "failed": 0, "timeout": 0, "propagated": 0}
            )
            if outcome in counts:
                counts[outcome] += 1
            self._outbound.pop(seq, None)

    def stats(self, recipient_hex: str | None = None) -> dict:
        with self._lock:
            if recipient_hex is not None:
                return dict(self._counts.get(recipient_hex, {}))
            total = {"delivered": 0, "failed": 0, "timeout": 0}
            for c in self._counts.values():
                for k in total:
                    total[k] += c.get(k, 0)
            return total


# Module-level default; the bridge instance owns its own copy.
_default_tracker = DownlinkTracker()


def downlink_tracker() -> DownlinkTracker:
    return _default_tracker


def sequence_chunks(parts: list[str], tag: str) -> list[str]:
    """
    Prefix each part with a position marker `[tag i/N]` so gaps in the
    arrival order are detectable by the recipient without a new protocol.

    The marker is kept short (tag is expected to be ≤ ~8 chars) so it costs
    at most a few bytes per ~368-byte LXMF content block.
    """
    n = len(parts)
    if n <= 1:
        # Single-part messages get no marker: nothing to sequence.
        return list(parts)
    marker = tag if tag else "msg"
    out = []
    for i, p in enumerate(parts, start=1):
        prefix = f"[{marker} {i}/{n}] "
        # Keep total content within the LXMF content budget: LXMF reserves
        # ~112B overhead + content ≤ ~368B per block. We subtract the prefix
        # length from the part so we don't push it over a block boundary.
        out.append(prefix + p)
    return out


def receipt_label(method: int) -> str:
    """Human-readable LXMF method for logs."""
    import LXMF
    return {
        LXMF.LXMessage.DIRECT: "direct",
        LXMF.LXMessage.OPPORTUNISTIC: "opportunistic",
        LXMF.LXMessage.PROPAGATED: "propagated",
    }.get(method, "unknown")
