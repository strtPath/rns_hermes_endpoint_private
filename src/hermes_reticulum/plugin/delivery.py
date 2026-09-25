"""Delivery handling for the Reticulum platform adapter (spec section 6).

Owns three things the adapter delegates:

- **Send-result mapping** (:func:`map_receipt`): translate an LXMF delivery
  state into a gateway ``SendResult``. The mapping is a translation, not a
  design problem (spec section 3): ``DELIVERED`` → success, ``FAILED`` after
  retries → failure with a real ``error_kind``, ``SENT`` (propagated) →
  success plus an entry in the pending set.
- **The propagation pending set** (:class:`PropagationPendingSet`): the
  authoritative delivery record for propagated sends. The gateway's ledger
  is wrong in both directions on a mesh (spec section 6), so this set is
  the record that stays true. It is constructed once in ``__init__`` and
  survives ``disconnect()``/``connect()`` on the same adapter instance —
  never rebuilt in ``connect()``.
- **Chunking** (:func:`sequence_chunks`): the port of the downlink
  ``_BLOCK_CONTENT_BUDGET`` and ``[tag i/N]`` prefix logic from
  ``core/downlink.py``.

The adapter never writes to the ledger and never re-sends on its own
initiative (spec section 16 invariant).
"""

import logging
import threading
from dataclasses import dataclass

from gateway.platforms.base import SendResult

logger = logging.getLogger("hermes_reticulum.delivery")

# LXMF 1.1.1 delivery-state values (hardcoded; the LXMF state list values are
# NOT contiguous indices — see core/downlink.py ``_STATE_MAP``).
STATE_GENERATING = 0x00
STATE_OUTBOUND = 0x01
STATE_SENDING = 0x02
STATE_SENT = 0x04          # node accepted (propagated) — no further signal
STATE_DELIVERED = 0x08     # direct delivery confirmed
STATE_REJECTED = 0xFD
STATE_CANCELLED = 0xFE
STATE_FAILED = 0xFF

# Outcome names, matching core/downlink.py ``_STATE_OUTCOME``.
_OUTCOME_DELIVERED = "delivered"
_OUTCOME_PROPAGATED = "propagated"
_OUTCOME_FAILED = "failed"

# Error kinds the adapter emits (the full set: too_long, bad_format,
# forbidden, not_found, rate_limited, transient, unknown).
_ERR_TRANSIENT = "transient"
_ERR_TOO_LONG = "too_long"
_ERR_UNKNOWN = "unknown"


# ── Chunking (ported from core/downlink.py) ────────────────────────────────


# Block content budget (LXMF 1.1.1): 368 bytes max content per link block.
# Carried over verbatim from core/downlink.py:43 — do not re-derive.
_BLOCK_CONTENT_BUDGET = 368


def sequence_chunks(parts: list, tag: str) -> list:
    """Prefix each part with ``[tag i/N] `` so recipients can spot a dropped
    tail without protocol changes. Single-part lists are returned unchanged.

    Ported from ``core/downlink.py:sequence_chunks``: the prefix is counted
    against the per-block budget and the part is truncated on a UTF-8
    codepoint boundary if the prefix would push it over budget.
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


def _utf8_codepoint_chunks(text: str, budget: int) -> list:
    """Split ``text`` on UTF-8 codepoint boundaries into parts of at most
    ``budget`` bytes. No empty parts are produced for non-empty input."""
    if not text:
        return []
    out = []
    cur = []
    cur_bytes = 0
    for ch in text:
        b = len(ch.encode("utf-8"))
        if cur and cur_bytes + b > budget:
            out.append("".join(cur))
            cur = []
            cur_bytes = 0
        cur.append(ch)
        cur_bytes += b
    if cur:
        out.append("".join(cur))
    return out


# ── Propagation pending set ────────────────────────────────────────────────


@dataclass(frozen=True)
class PendingEntry:
    """One unconfirmed (propagated) send.

    ``destination`` is the destination hash; ``content`` is the text that was
    sent; ``sequence`` is the per-destination monotonic sequence number.
    """
    destination: str
    content: str
    sequence: int


class PropagationPendingSet:
    """Authoritative record of propagated (unconfirmed) sends (spec section 6).

    Constructed once in the adapter's ``__init__`` and kept across
    ``disconnect()``/``connect()`` on the same instance. The gateway's ledger
    is wrong in both directions on a mesh, so this set — not the ledger — is
    the delivery record a user can inspect to answer "did that message
    actually go".

    The set NEVER writes to the ledger and NEVER re-sends on its own
    initiative (spec section 16 invariant). It only records and reports.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: list = []
        self._counters = {
            "delivered": 0,
            "propagated": 0,
            "failed": 0,
        }
        # Per-destination monotonic sequence.
        self._seq: dict = {}

    # ── Record ────────────────────────────────────────────────────────

    def record_propagated(self, destination: str, content: str) -> PendingEntry:
        """Record a propagated (``SENT``) send and return the new entry."""
        with self._lock:
            self._seq[destination] = self._seq.get(destination, 0) + 1
            seq = self._seq[destination]
            entry = PendingEntry(destination=destination, content=content,
                                  sequence=seq)
            self._entries.append(entry)
            self._counters["propagated"] += 1
            return entry

    def note_delivered(self, destination: str, content: str) -> None:
        """Record a confirmed (``DELIVERED``) send (no pending entry)."""
        with self._lock:
            self._counters["delivered"] += 1

    def note_failed(self, destination: str, content: str) -> None:
        """Record a failed send (no pending entry)."""
        with self._lock:
            self._counters["failed"] += 1

    # ── Inspect ───────────────────────────────────────────────────────

    def pending_for(self, destination: str) -> list:
        """Unconfirmed entries for a destination, oldest first."""
        with self._lock:
            return [e for e in self._entries if e.destination == destination]

    def all_pending(self) -> list:
        """All unconfirmed entries, oldest first."""
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict:
        """Counters plus the unconfirmed count."""
        with self._lock:
            counters = dict(self._counters)
        counters["unconfirmed"] = len(self)
        return counters


# ── Send-result mapping ──────────────────────────────────────────────────────


_STATE_NAMES = {
    STATE_GENERATING: "GENERATING",
    STATE_OUTBOUND: "OUTBOUND",
    STATE_SENDING: "SENDING",
    STATE_SENT: "SENT",
    STATE_DELIVERED: "DELIVERED",
    STATE_REJECTED: "REJECTED",
    STATE_CANCELLED: "CANCELLED",
    STATE_FAILED: "FAILED",
}


def _state_name(state: int) -> str:
    """Map an LXMF state value to its name string (unknown → ``state_0xNN``)."""
    return _STATE_NAMES.get(state, f"state_{state:02x}")


def state_name(state) -> str:
    """Public form of :func:`_state_name` for callers outside this module.

    LXMF exposes state constants but no name function of its own, so this
    module owns the mapping both ways (:func:`state_from_name` is the inverse).
    A non-integer state yields ``state_none`` rather than raising.
    """
    if not isinstance(state, int):
        return "state_none"
    return _state_name(state)


def state_from_name(name: str) -> int:
    """Map an LXMF state NAME string back to its value.

    The transport reports outcomes as names (``LXMF.LXMessage.state_name``),
    so the adapter needs the inverse of :func:`_state_name` to reach
    :func:`map_receipt`. An unknown name maps to ``STATE_FAILED``: a state we
    cannot classify must not be reported as delivered.
    """
    if not isinstance(name, str):
        return STATE_FAILED
    for value, label in _STATE_NAMES.items():
        if label == name.upper():
            return value
    return STATE_FAILED


_STATE_OUTCOMES = {
    STATE_DELIVERED: _OUTCOME_DELIVERED,
    STATE_SENT: _OUTCOME_PROPAGATED,
    STATE_FAILED: _OUTCOME_FAILED,
}


def _state_outcome(state: int) -> str:
    """Map an LXMF state value to a delivery outcome string."""
    return _STATE_OUTCOMES.get(state, "unknown")


def map_receipt(
    pending: PropagationPendingSet,
    state: int,
    destination: str,
    content: str,
    reason: str | None = None,
) -> SendResult:
    """Translate an LXMF delivery-state callback into a gateway ``SendResult``.

    - ``DELIVERED`` → ``success=True`` (direct delivery confirmed).
    - ``SENT`` (propagated) → ``success=True`` and an entry in ``pending``.
      No per-message signal arrives afterwards, so the pending set is the
      record of what was actually confirmed.
    - ``FAILED`` → ``success=False`` with ``retryable=True`` and
      ``error_kind="transient"`` (peer unreachable / route dropped).
    - Anything else (``GENERATING``/``OUTBOUND``/``SENDING``/``REJECTED``/
      ``CANCELLED``/unrecognised) → ``success=False`` with
      ``error_kind="unknown"``. Never a benign default.

    ``error_kind`` is always set explicitly — the gateway's
    ``classify_send_error`` substring table is built for API-server wording and
    would misclassify LXMF strings.
    """
    outcome = _state_outcome(state)

    if outcome == _OUTCOME_DELIVERED:
        pending.note_delivered(destination, content)
        return SendResult(success=True)

    if outcome == _OUTCOME_PROPAGATED:
        entry = pending.record_propagated(destination, content)
        logger.info(
            "Reticulum: propagated send recorded (seq=%d); no further "
            "per-message signal will arrive — the pending set is the record.",
            entry.sequence,
        )
        return SendResult(success=True)

    if outcome == _OUTCOME_FAILED:
        pending.note_failed(destination, content)
        err = reason if reason else "delivery failed after retries"
        return SendResult(
            success=False,
            error=err,
            retryable=True,
            error_kind=_ERR_TRANSIENT,
        )

    # Unrecognised / in-flight states: never a benign default.
    err = reason if reason else f"unrecognised delivery state 0x{state:02x}"
    return SendResult(
        success=False,
        error=err,
        retryable=False,
        error_kind=_ERR_UNKNOWN,
    )


def map_chunk_overflow(total_bytes: int) -> SendResult:
    """Over-budget content: ``success=False, retryable=False, too_long``."""
    return SendResult(
        success=False,
        error="message exceeds the single-packet content budget",
        retryable=False,
        error_kind=_ERR_TOO_LONG,
    )


def chunk_for_send(text: str, tag: str) -> list:
    """Split ``text`` for transmission, returning ready-to-send parts.

    This is the seam that joins the two halves of the ported chunking logic,
    which are useless apart: :func:`_utf8_codepoint_chunks` splits on codepoint
    boundaries, :func:`sequence_chunks` adds the ``[tag i/N]`` prefixes.

    The prefix must be budgeted BEFORE splitting, not after. Splitting at the
    full budget and letting ``sequence_chunks`` re-truncate silently drops the
    bytes between the two cut points: the parts still look well-formed and each
    fits the budget, but the reassembled text is shorter than the original.

    That is why this predicts the prefix width up front and splits against
    ``budget - prefix``. The part count feeds back into the prefix width, so a
    first pass at the widest plausible prefix (``i/N`` with two-digit numbers)
    decides the split, and the exact prefix is applied afterwards. Parts then
    fit the budget with no re-truncation and no loss.
    """
    if not text:
        return []
    # Widest prefix this send could need: longest tag, two-digit index/total.
    # Overestimating only makes the parts slightly shorter; underestimating
    # would force sequence_chunks to truncate, which is the lossy path.
    reserve = len((f"[{tag} 99/99] ").encode())
    budget = _BLOCK_CONTENT_BUDGET - reserve
    if budget <= 0:
        return []
    parts = _utf8_codepoint_chunks(text, budget)
    if len(parts) > 1:
        parts = sequence_chunks(parts, tag)
    return parts
