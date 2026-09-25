"""
Delivery-mapping tests for the Reticulum platform adapter (spec section 10
seams + the ledger-independence case).

Covers:
- DELIVERED receipt -> success=True.
- FAILED after retries -> success=False with a real error_kind.
- SENT (propagated) -> success=True AND an entry in the pending set.
- Packet refusal -> retryable=True.
- Ledger independence: a retryable=True result recorded while the adapter is
  down stays in the adapter's own pending set across disconnect()/connect()
  on the same instance and is still reported unconfirmed. This is the case
  the gateway's boot sweep skips and abandons at 24h, so the adapter's own
  record is the only assertion that can fail here.
- Chunking: long messages split into the expected number of parts, each
  within budget, prefixes correct, single part unchanged, UTF-8 boundaries
  respected (multi-byte characters).
- Unclassified failure -> unknown, never a benign value.
- Over-budget content -> too_long.
- Empty / degenerate input -> log and send nothing (no zero-length packet).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from hermes_reticulum.plugin import delivery
from hermes_reticulum.plugin.adapter import (
    FakeTransport,
    ReticulumPlatformAdapter,
)

# ── Fixtures ───────────────────────────────────────────────────────────────


class PlatformConfig:
    """Minimal stand-in for the gateway's PlatformConfig dataclass."""

    def __init__(self, extra=None):
        self.extra = extra or {}
        self.enabled = True
        self.token = None
        self.api_key = None
        self.home_channel = None


def make_adapter(transport=None):
    adapter = ReticulumPlatformAdapter(PlatformConfig())
    if transport is not None:
        adapter._transport_factory = lambda: transport
    return adapter


# A fake transport that lets us script the delivery-state callback. The real
# LXMF transport fires a state through the adapter's _record_receipt seam;
# this fake lets tests drive that seam directly.
class ScriptedTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.refuse = False

    def send_to(self, destination_hash, payload):
        self.sent.append((destination_hash, payload))
        return not self.refuse


# LXMF state values (from hermes_reticulum.plugin.delivery).
DELIVERED = delivery.STATE_DELIVERED
SENT = delivery.STATE_SENT
FAILED = delivery.STATE_FAILED
REJECTED = delivery.STATE_REJECTED  # unrecognised → unknown


# ── Send-result mapping (spec section 6 / 10) ───────────────────────────────


def test_delivered_receipt_maps_to_success():
    pending = delivery.PropagationPendingSet()
    result = delivery.map_receipt(pending, DELIVERED, "0" * 32, "hello")
    assert result.success is True
    assert result.error is None
    # DELIVERED is confirmed, so no pending entry.
    assert len(pending) == 0
    assert pending.stats()["delivered"] == 1


def test_failed_receipt_maps_to_failure_with_error_kind():
    pending = delivery.PropagationPendingSet()
    result = delivery.map_receipt(
        pending, FAILED, "0" * 32, "hello", reason="route dropped after 5 attempts"
    )
    assert result.success is False
    assert result.retryable is True
    assert result.error_kind == "transient"
    # FAILED is not confirmed delivery; no pending entry.
    assert len(pending) == 0
    assert pending.stats()["failed"] == 1


def test_failed_receipt_default_reason_still_transient():
    pending = delivery.PropagationPendingSet()
    result = delivery.map_receipt(pending, FAILED, "0" * 32, "hello")
    assert result.success is False
    assert result.retryable is True
    assert result.error_kind == "transient"
    assert result.error is not None


def test_propagated_sent_maps_to_success_and_pending_entry():
    pending = delivery.PropagationPendingSet()
    result = delivery.map_receipt(pending, SENT, "0" * 32, "hello")
    assert result.success is True
    assert result.error is None
    # Propagated: recorded in the pending set, unconfirmed.
    assert len(pending) == 1
    entry = pending.all_pending()[0]
    assert entry.destination == "0" * 32
    assert entry.content == "hello"
    assert entry.sequence == 1
    assert pending.stats()["propagated"] == 1
    assert pending.stats()["unconfirmed"] == 1


def test_propagated_sequence_increments_per_destination():
    pending = delivery.PropagationPendingSet()
    delivery.map_receipt(pending, SENT, "0" * 32, "one")
    delivery.map_receipt(pending, SENT, "0" * 32, "two")
    entries = pending.pending_for("0" * 32)
    assert [e.sequence for e in entries] == [1, 2]
    assert [e.content for e in entries] == ["one", "two"]


def test_unrecognised_state_maps_to_unknown():
    pending = delivery.PropagationPendingSet()
    # REJECTED / GENERATING / OUTBOUND / SENDING / CANCELLED are not
    # DELIVERED/SENT/FAILED → unclassified → unknown, never a benign default.
    for state in (REJECTED, 0x00, 0x01, 0x02, 0xFE, 0x09):
        result = delivery.map_receipt(pending, state, "0" * 32, "x")
        assert result.success is False
        assert result.error_kind == "unknown", "state %#x" % state
    assert len(pending) == 0


def test_unrecognised_state_keeps_reason():
    pending = delivery.PropagationPendingSet()
    result = delivery.map_receipt(pending, REJECTED, "0" * 32, "x", reason="stamp invalid")
    assert result.success is False
    assert result.error_kind == "unknown"
    assert result.error == "stamp invalid"


def test_over_budget_content_is_too_long():
    result = delivery.map_chunk_overflow(5000)
    assert result.success is False
    assert result.retryable is False
    assert result.error_kind == "too_long"


# ── Adapter-level: the send() seam drives the pending set ───────────────────


@pytest.mark.asyncio
async def test_send_through_adapter_records_propagated_entry():
    adapter = make_adapter(ScriptedTransport())
    await adapter.connect()
    result = await adapter.send("0" * 32, "hello mesh")
    # The fake transport accepted the packet → node acceptance (SENT).
    assert result.success is True
    # The send is recorded as unconfirmed in the adapter's own pending set.
    entries = adapter.unconfirmed_for("0" * 32)
    assert len(entries) == 1
    assert entries[0].content == "hello mesh"
    assert adapter.delivery_stats["unconfirmed"] == 1
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_send_packet_refusal_is_retryable_transient():
    transport = ScriptedTransport()
    transport.refuse = True
    adapter = make_adapter(transport)
    await adapter.connect()
    result = await adapter.send("0" * 32, "hello")
    assert result.success is False
    assert result.retryable is True
    assert result.error_kind == "transient"
    # Refusal means nothing reached the transport; no pending entry.
    assert len(adapter.unconfirmed_for("0" * 32)) == 0
    await adapter.disconnect()


# ── Ledger independence (spec section 10, highest value) ────────────────────


@pytest.mark.asyncio
async def test_pending_set_survives_disconnect_and_reconnect():
    """A retryable/propagated result recorded while connected stays in the
    adapter's own pending set after disconnect() then connect() on the same
    instance, and is still reported unconfirmed.

    This is the case the gateway's boot sweep skips and abandons at 24h, so
    the adapter's own record is the only assertion that can fail here.
    The set is constructed in __init__ (NOT in connect()), so it survives.
    """
    adapter = make_adapter(ScriptedTransport())
    await adapter.connect()
    # Record a propagated send while connected.
    result = await adapter.send("0" * 32, "the reply")
    assert result.success is True
    assert len(adapter.unconfirmed_for("0" * 32)) == 1

    # Simulate the ledger case: the adapter goes down, comes back up.
    await adapter.disconnect()
    await adapter.connect()

    # The entry must still be there, still unconfirmed.
    entries = adapter.unconfirmed_for("0" * 32)
    assert len(entries) == 1
    assert entries[0].content == "the reply"
    assert entries[0].sequence == 1
    assert adapter.delivery_stats["unconfirmed"] == 1
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_pending_set_survives_full_reconnect_flag():
    """Same guarantee via connect(is_reconnect=True), the path the gateway's
    reconnect watcher uses after a transport death."""
    adapter = make_adapter(ScriptedTransport())
    await adapter.connect()
    await adapter.send("0" * 32, "persisted")
    assert len(adapter.unconfirmed_for("0" * 32)) == 1

    await adapter.disconnect()
    assert await adapter.connect(is_reconnect=True) is True
    assert len(adapter.unconfirmed_for("0" * 32)) == 1
    await adapter.disconnect()


# ── Chunking (ported from core/downlink.py) ─────────────────────────────────


def test_sequence_chunks_single_part_unchanged():
    assert delivery.sequence_chunks(["hello"], "p1") == ["hello"]


def test_sequence_chunks_multi_part_prefixes_correct():
    parts = delivery.sequence_chunks(["aa", "bb", "cc"], "p1")
    assert parts == ["[p1 1/3] aa", "[p1 2/3] bb", "[p1 3/3] cc"]


def test_sequence_chunks_respects_utf8_boundary():
    # A multi-byte character must not be split mid-codepoint.
    part = "é" * 200  # 200 × 2 bytes = 400 bytes, over budget once prefix added
    out = delivery.sequence_chunks([part], "p1")
    # Single part: returned unchanged (no prefix for single-part).
    assert out == [part]

    # Force multi-part so the prefix + truncation logic engages, and verify
    # the result decodes cleanly as UTF-8 (no corrupted codepoints).
    big = "é" * 500
    out = delivery.sequence_chunks([big], "p1")
    # With a single part there is no split; but verify the truncation helper
    # directly on a multi-byte chunk that would be cut mid-byte.
    chunks = delivery._utf8_codepoint_chunks(big, 10)
    for c in chunks:
        c.encode("utf-8")  # must round-trip
    assert "".join(chunks) == big
    assert all(len(c.encode("utf-8")) <= 10 for c in chunks)


def test_sequence_chunks_truncation_never_splits_codepoint():
    # Build a part whose UTF-8 bytes are just over budget so truncation fires,
    # and confirm no part contains a lone continuation byte.
    prefix = "[p1 1/2] "
    budget = delivery._BLOCK_CONTENT_BUDGET
    max_bytes = budget - len(prefix.encode("utf-8"))
    # A 3-byte emoji repeated so the byte length exceeds max_bytes.
    part = "🚀" * ((max_bytes // 3) + 2)
    out = delivery.sequence_chunks([part, "tail"], "p1")
    assert out[0].startswith(prefix)
    body = out[0][len(prefix):]
    body.encode("utf-8")  # must be valid UTF-8 (no split codepoint)
    assert len(body.encode("utf-8")) <= max_bytes


def test_chunking_produces_no_empty_parts():
    # A message that would chunk to a single empty part must not produce one.
    assert delivery._utf8_codepoint_chunks("", 10) == []
    # Whitespace-only is handled upstream (log + send nothing); the chunker
    # still must not emit an empty part for non-empty input.
    chunks = delivery._utf8_codepoint_chunks("   ", 10)
    assert chunks == ["   "]


def test_long_ascii_message_splits_into_expected_parts():
    # 2000 ASCII chars → ceil(2000 / 368) = 6 parts, each within budget.
    text = "a" * 2000
    chunks = delivery._utf8_codepoint_chunks(text, delivery._BLOCK_CONTENT_BUDGET)
    assert len(chunks) == 6
    for c in chunks:
        assert 0 < len(c.encode("utf-8")) <= delivery._BLOCK_CONTENT_BUDGET
    assert "".join(chunks) == text


def test_long_multibyte_message_splits_and_reassembles():
    text = "é" * 2000  # 4000 bytes
    chunks = delivery._utf8_codepoint_chunks(text, delivery._BLOCK_CONTENT_BUDGET)
    # ceil(4000 / 368) = 11 parts.
    assert len(chunks) == 11
    for c in chunks:
        assert 0 < len(c.encode("utf-8")) <= delivery._BLOCK_CONTENT_BUDGET
    assert "".join(chunks) == text


# ── Empty / degenerate input ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_content_sends_nothing():
    transport = ScriptedTransport()
    adapter = make_adapter(transport)
    await adapter.connect()
    result = await adapter.send("0" * 32, "   ")
    assert result.success is False
    assert result.error_kind == "bad_format"
    # Nothing reached the transport: no zero-length packet.
    assert transport.sent == []
    await adapter.disconnect()


# ── chunk_for_send: the seam joining splitter to prefixer ─────────────────
# These are the tests that were missing. The pieces existed and were tested
# individually, but nothing joined them to adapter.send(), so a long reply
# went out as one oversized packet. A test per piece cannot catch that.


def test_chunk_for_send_short_message_unchanged():
    assert delivery.chunk_for_send("hello", "p1") == ["hello"]


def test_chunk_for_send_splits_long_message():
    text = "a" * 2000
    parts = delivery.chunk_for_send(text, "p1")
    assert len(parts) > 1
    # Every part must fit the block budget INCLUDING its prefix.
    assert all(
        len(p.encode("utf-8")) <= delivery._BLOCK_CONTENT_BUDGET for p in parts
    )


def test_chunk_for_send_parts_reassemble_to_original():
    """Prefixes are overhead, not loss: stripping them must give the original."""
    text = "the quick brown fox " * 100
    parts = delivery.chunk_for_send(text, "p1")
    assert len(parts) > 1
    stripped = []
    for p in parts:
        assert p.startswith("[p1 ")
        stripped.append(p.split("] ", 1)[1])
    assert "".join(stripped) == text


def test_chunk_for_send_multibyte_survives():
    text = "héllo wörld 🎉 " * 200
    parts = delivery.chunk_for_send(text, "p1")
    assert len(parts) > 1
    assert all("\ufffd" not in p for p in parts)
    assert all(
        len(p.encode("utf-8")) <= delivery._BLOCK_CONTENT_BUDGET for p in parts
    )


def test_chunk_for_send_empty_returns_no_parts():
    assert delivery.chunk_for_send("", "p1") == []
