# Findings — "replies came through, tool calls didn't" (2026-09-12 18:11 reminder request)

**Symptom.** User on RNS: replies arrive, tool-call step messages never do.
Reproduces the 2026-08-29 downlink burst loss profile exactly.

## What actually happened (today, 18:11:07–18:12:45)

User asked for a reminder to tell Ivan who's working overtime. Bridge was
healthy (no errors, no SIGKILL, no denials). The turn did 9 tool calls:
cronjob search/describe + the actual cronjob_manage create via tool_call.
The final reply (614 chars → 620-byte chunk) dispatched at 18:12:45.

But the step pushes look like this in journalctl:

```
18:11:27  66 bytes   18:11:31  40 bytes   18:11:34  65 bytes
18:11:39  40 bytes   18:11:43  74 bytes   18:11:47  40 bytes   18:11:51  47 bytes
18:12:16  717 bytes  18:12:25  132 bytes  18:12:45  620 bytes (final reply)
```

Four 40-byte chunks in the first 30s — that's the short step messages
(💻 + tool name + one-line result, 37–74 chars per push_step log). Then a
25s gap, then the 717-byte cronjob_manage tool_call (the actual reminder
creation) + 132-byte follow-up. User received none of the 40-byte steps.

## Root cause (known, from 2026-08-29 — never fixed)

`bridge.send_reply` is fire-and-forget: "Reply dispatched (N bytes)" logs
the instant `router.handle_outbound(lxm)` returns, which only means the
LXMessage entered the LXMRouter outbound queue. No delivery receipt on the
LoRa downlink. Over a weak/opportunistic channel the tail of a burst sheds
packets silently.

The 2026-08-29 downlink-ack commit (`1322e89`: per-chunk
`register_delivery_callback` + `[tag i/N]` sequence prefixes + 500ms
per-recipient pacing + real RSSI/SNR) was **reverted** the same day
(`1d71cf3`) — and today's live code (PID 489785) has none of it: no
`seq=` in dispatch logs, no `Downlink ack` lines, no `core/downlink.py`.
So we are flying blind again: no per-chunk delivery state, no pacing on
the step-push path (step chunks go through a different path than
`_process_and_reply`, so the profile `send_delay_ms` doesn't apply either),
and no loss signal in journalctl.

**Why 40-byte chunks die but the 620-byte reply survived:** not size —
the 40-byte chunks landed in a 4-in-24s burst on a channel with no
RSSI/SNR measurement (profiler: `tcp_default, rssi=None, snr=None`),
which is the exact worst-case downlink profile from the 2026-08-29
findings. The final reply was a single chunk 25s later, when the channel
happened to be clear.

## The reminder DID get created

The cronjob_manage call went through and the job was created
(tool_call push 717 bytes at 18:12:16). The user just never saw the
tool-call message — the tool ran, the visibility failed. This is the
2026-09-05 "flagged tool visibility" problem's opposite: a *successful*
tool call that's invisible, same root cause (no delivery feedback).

## Fix path (options)

1. **Re-land `1322e89`** (downlink acks + sequence + pacing). The revert
   reason is unknown from git — likely the callback API or the revert was
   part of the 08-30 cleanup before re-landing pieces individually.
   The SIGTERM/900s parts were re-landed in `36d94bb` (08-31). Only the
   downlink-ack + RSSI parts are missing. Re-land those two pieces.
2. **Minimum viable:** add `register_delivery_callback` to
   `bridge.send_reply` + per-chunk `seq` in the dispatch log + a 500ms
   `pace_wait` on the step-push path (hermes_client `_push_step` →
   bridge send). Without sequence tags the user can't tell which chunk
   was lost, but journalctl gets per-chunk DELIVERED/FAILED state.
3. **End-to-end app ack** (Columba → bridge "got message N") — Tier 5,
   needs a protocol decision on the phone side.

## Open questions

- Why was `1322e89` reverted on 08-30? (No commit message beyond "Revert.")
- Is the step-push path (hermes_client `_push_step` → bridge) subject to
  the profile's `send_delay_ms`? Currently no — that delay is only in
  `_process_and_reply`. Step bursts need their own pacing.

## Re-land (v2) — 2026-09-12

Re-landed the downlink reliability feature on branch `fix/downlink-acks-v2`
(commit TBD), designed around each known regression candidate from the
original `1322e89`.

### A — `states[state]` index bug (FIXED)

**Found:** The original `_on_outbound` did `LXMF.LXMessage.states[state]`,
using the state *value* (e.g. `0x08`) as a list *index* into an 8-element
list. `states[8]` → IndexError → caught → fallback `state_8`. So the
original NEVER produced a real state name for DELIVERED.

**Fix:** Hardcoded reverse map `{0x08: "DELIVERED", 0x04: "SENT", 0xFF:
"FAILED", ...}` at module level in `downlink.py`. No list indexing.

### B — Sequence tag format + block budget (VERIFIED)

**Found:** `[p<N> i/N] ` prefix is ~12 chars. Block content budget is 368
bytes (LXMF 1.1.1: `PLAIN_PACKET_MAX_CONTENT = PLAIN_PACKET_MDU -
LXMF_OVERHEAD + DESTINATION_LENGTH`). A 1500-char STEP_CHUNK_CHARS part
fits in ~4 blocks with the prefix.

**Fix:** `sequence_chunks` in `downlink.py` truncates the *part* (not the
prefix) if `len((prefix + part).encode('utf-8'))` would exceed 368 bytes.
UTF-8 codepoint boundary respected.

### C — `include_ticket=True` + first chunk after restart (VERIFIED)

**Found:** `LXMRouter.generate_ticket` is a stored-ticket lookup (not a
stamp burn). Tickets persist to disk via `save_available_tickets` and load
on startup (`available_tickets` dict). The first chunk after restart has
tickets available.

**Fix:** No change needed. `include_ticket=True` stays. If ticket
generation ever fails, LXMessage catches it internally (logged to RNS,
message continues without ticket).

### D — Pacing thread interactions (VERIFIED)

**Found:** `pace_wait` sleeps on the *calling* thread. Three call paths:
(a) bridge `_process_and_reply` thread pool — fine. (b) cli `_step_push`
(step-watcher thread, inline in poll loop) — a 500ms sleep per step chunk
is negligible vs the poll interval. (c) cli `_on_full_step`
(ThreadingHTTPServer handler thread) — each request gets its own thread,
a 500ms sleep doesn't block other requests.

**Fix:** No change needed. All three paths are safe.

### E — Ack timeout sweep (NEW)

**Found:** The original had no timeout path. A chunk that never gets a
first-hop ack (silent LoRa loss) never fires the callback, so its seq
stays in `_outbound` until the 256-entry prune. "No ack" is
indistinguishable from "acked but log scrolled off."

**Fix:** Lazy sweep on each `next_seq` call in `DownlinkTracker`. Any seq
older than `HERMES_DOWNLINK_ACK_TIMEOUT_S` (default 300s) that never got
a callback is counted `timeout` and logged:
`Downlink ack seq=%d → %s state=timeout (no first-hop ack within %ds)`.
No new thread.

### F — Log-format consumers (VERIFIED)

**Found:** No consumers of the `Reply dispatched to` log format in
`tests/` or `src/` (grep-verified). The new format adds `seq=%d` —
backward-compatible (existing parsers that match `Reply dispatched to`
still work).

**Fix:** None needed.

### Pacing correctness fix (FIXED)

**Found:** The original `pace_wait` was called in `push_reply` *before*
`send_reply`. If `send_reply` returned False (unknown recipient),
`pace_wait` had already advanced `_last_send`, corrupting the pacing clock
for the next real send.

**Fix:** `record_send` is called from `send_reply` *after* a successful
`handle_outbound`. `pace_wait` only *reads* the last send time (never
sets it). A failed send does not consume pacing budget.

### New env vars

- `HERMES_CHUNK_INTERVAL_MS` (keep original name, default 500).
- `HERMES_DOWNLINK_ACK_TIMEOUT_S` (new, default 300).

### Log-line formats (for operator grep)

- Dispatch: `Reply dispatched to %s seq=%d (%d bytes)`
- Ack: `Downlink ack seq=%d → %s state=%s (%s)`
- Timeout: `Downlink ack seq=%d → %s state=timeout (no first-hop ack within %ds)`

### What we could NOT verify in this env

- Real LoRa first-hop acks (no RNode radio on this box). The ack
  state-mapping and timeout-sweep logic are unit-tested in isolation.
  The operator should watch for `Downlink ack` lines in journalctl after
  deploy — if they don't appear, the callback isn't firing (check the
  LXMF delivery callback path).
