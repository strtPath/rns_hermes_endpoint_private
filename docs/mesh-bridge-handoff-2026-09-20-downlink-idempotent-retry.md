# Downlink Idempotent-Retry — Handoff (2026-09-20)

Branch `feat/downlink-idempotent-retry` off `dev`. NOT to be merged to dev
until it has been in the field a couple of days. 4 commits ahead of dev,
working tree clean (all source/tests committed). All 256 tests pass.

## What this branch does
Makes the RNS downlink retry path (a) **idempotent** — one stable 16-hex
`mid` per reply, shared by all chunks and every retry, distinct from the
per-chunk `seq` — and (b) **transport-aware** — server-routed TCP recipients
get a safe retry on a strong loss signal; direct-propagation recipients keep
conservative TTL-aware gating (Phase 2, not yet built).

## Commits (dev +4)
- 9c89115 Task 1 — stable per-message ID registry (`new_message_id`,
  `register_message`, `attach_seq`, `seq_for_message`, `message_for_seq`).
- 7ce833b Task 2a — thread `mid` through `push_reply` → `send_reply(mid=...)`.
- 9278ab1 Task 2b — tag chunks in-band `[tag m<MID> i/N] ` via
  `sequence_chunks(parts, tag, message_id=...)`; mid minted BEFORE chunking.
- 2f727ad Task 3 — conservative retry: `should_retry` / `begin_retry` /
  `note_outcome` + bridge `_retry_sweep` daemon loop.

## Design invariants (already implemented + tested)
- One mid per reply, minted before chunking; every chunk + every retry shares
  it. Client dedupes on mid, NOT on seq.
- `seq` is per-chunk and per-attempt; a retry is a fresh seq under the same mid.
- Retry is CONSERVATIVE: only a first-hop FAILED outcome (`note_outcome`
  normalizes to lowercase "failed") authorizes a retry. A bare silent timeout
  (no FAILED callback) does NOT. Never blind-retry.
- Bounded: `HERMES_DOWNLINK_MAX_RETRIES` (default 3), backoff
  `HERMES_DOWNLINK_RETRY_BACKOFF_S` (default 60s).
- `begin_retry(mid)` returns `(recipient, attempts, chunk_count, orig_seq)`;
  it authorizes only — it does NOT allocate a seq. The bridge's `send_reply`
  allocates the fresh dispatch seq, links it to the mid via `attach_seq`, and
  re-saves the chunk text under the new seq. (Earlier bug: begin_retry
  pre-allocated a seq AND send_reply allocated another → double seq. Fixed by
  making begin_retry authorization-only.)
- Retry text is sourced from `orig_seq` (first dispatch) so retries re-send
  the identical body.
- Multi-part push: every chunk carries `[p<N> m<MID> i/N] `; single-part stays
  untagged (backward compatible).
- `send_reply(..., mid: str | None = None)` is optional → all existing
  positional call sites unchanged.

## Files touched
- src/hermes_reticulum/core/downlink.py — mid registry, sequence_chunks mid
  prefix, retry state (`_messages`, `_seq_to_message`, `_chunk_text`,
  `MAX_OUTSTANDING=256`), `should_retry`/`begin_retry`/`note_outcome`/
  `save_chunk_text`/`chunk_text`/`pending_retry_mids`/`retry_backoff_s`/
  `retry_count`/`message_chunk_count`/`message_recipient`/`new_message_id`/
  `register_message`/`attach_seq`/`seq_for_message`/`message_for_seq`/
  `next_push_tag`/`record_send`/`pace_wait`/`register_dispatch`/`next_seq`.
- src/hermes_reticulum/core/bridge.py — `send_reply(mid=)`, `push_reply`
  mid-before-chunking, `_retry_sweep`, `_retry_loop` (daemon thread),
  lifecycle in `run_forever`/`stop()` (thread join 2s).
- tests/test_downlink.py — Task 1 + chunk-tagging + RetryPolicy (6 tests).
- tests/test_downlink_integration.py — mid threading + retry-sweep test.

## Test command (important)
pytest is ONLY in `venv/` (NOT `.venv`, which doesn't exist; system python
has no pytest). Canonical run:
    venv/bin/python -m pytest tests/ -q
256 passing as of this handoff.

## Env knobs
- HERMES_DOWNLINK_MAX_RETRIES (default 3)
- HERMES_DOWNLINK_RETRY_BACKOFF_S (default 60s)

## Routing fact (drives the design)
Downlink to the phone is SERVER-ROUTED TCP:
path_interface_at_send = TCPInterface[<propagation-server-host>:<port>],
path_hops_at_send = 2. So TTL/`local_hops_delta` obfuscation is NOT the loss
cause here; retry over the same reliable TCP interface is safe on a strong
loss signal. TTL-aware gating is only needed for direct-propagation
recipients (Phase 2).

## Still open / next steps
1. Field-test the branch for a couple of days (user's requirement) before
   merging to dev. Do NOT merge yet.
2. PRIMARY OPEN QUESTION: why did seq 188 time out (silent state=timeout, no
   first-hop ACK) when seq 189-192 on the SAME TCP path acked in ~1s each?
   Likely a transient TCP/bridge/server hiccup or a lost first-hop ACK on the
   TCP leg, not mesh propagation loss. No bridge-log corroboration found
   (journalctl grep empty; no path_* field handling in the repo).
3. Phase 2 (per plan): client-side dedupe spec (owner = phone app, spec-only,
   no in-repo code); transport-aware retry gating for direct-propagation
   recipients; RNode-side delivery ACK is OUT OF SCOPE.
4. Plan doc: .hermes/plans/2026-09-19_downlink-retry-idempotent-ids.md
   (Tasks 4+ remaining = recap/replay idempotent + Phase 2 items).
5. Findings doc: docs/mesh-bridge-findings-2026-09-19-downlink-retry-
   idempotency.md (updated to reflect server-routed routing + conservative retry).

## Incident values (for reference)
- seq 188 = silent state=timeout, no first-hop ACK.
- seq 189-192 acked ~1s each on the same TCP path.
- Forwarded MeshChatX msg id 1506: dest = phone RNS identity, src = bridge
  identity (both redacted), timestamp 1789919968.

## Constraints carried forward
- No code changes to the phone-side chat client (dedupe = spec-only).
- No RNode-side delivery ACK implementation.
- No merge to dev until field-tested.
