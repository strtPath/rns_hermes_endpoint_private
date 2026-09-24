# Findings — Downlink reliability, RSSI/SNR profiler, SIGTERM clean exit

**Date:** 2026-08-30
**Commit:** `1322e89` (branch `feat/slash-commands-and-model-pin`)
**Live PID after restart:** 902155 (identity `<redacted>` stable)

---

## What was broken

1. **Downlink was fire-and-forget.** `bridge.send_reply` called
   `router.handle_outbound(lxm)` and logged "Reply dispatched (N bytes)" the
   instant the LXMessage was handed to the RNS router. No ack, no receipt, no
   sequence. Over opportunistic LoRa the tail of a chunk burst is silently
   dropped and the bridge has no signal about it. The profiler logged
   `rssi=None, snr=None` on every classification and defaulted to the
   `tcp_default` 1 Gbit profile — a blind assumption, not a measurement.

2. **`systemctl stop/restart` hung.** The SIGTERM handler in
   `run_forever()` only set `self._running = False`; the RNS C-level
   event-loop threads kept the process alive. `systemctl --user stop`
   sent SIGTERM, the process ignored it, and the stop phase never
   completed. Workaround was `kill -9 <pid>` (documented in
   `references/stop-restart-sigterm-hang.md`).

3. **Approval timeout was uncommitted.** `control_server.py` had a 900s
   default + `HERMES_MESH_APPROVAL_TIMEOUT` env override in the working
   tree but not in git.

---

## What landed (commit `1322e89`)

### 1. Downlink ack + sequence + pacing (`core/downlink.py` — new module)

**`DownlinkTracker`:**
- `next_seq(recipient_hex)` — allocates a monotonic outbound sequence id
  and records the dispatch wall-clock time. One per LXMessage chunk.
- `pace_wait(recipient_hex, interval_ms)` — sleeps in the worker thread
  until at least `interval_ms` (default 500 ms, overridable via
  `HERMES_CHUNK_INTERVAL_MS`) has elapsed since the last chunk to the
  same recipient. Keeps the one-second burst from landing again.
- `note_outcome(seq, outcome)` — increments per-recipient counters
  (`delivered` / `propagated` / `failed` / `timeout`) for `/status` and
  journalctl rollups.
- `stats(recipient_hex | None)` — returns the counters.

**`sequence_chunks(parts, tag)`** prefixes each part of a multi-part
push with `[<tag> i/N]` so the recipient (Columba, or a human) can detect
a dropped tail without any protocol change. Tag is short (`p1`, `p2`, …)
so it costs a few bytes per ~368-byte LXMF content block.

**`bridge.send_reply`** now:
1. Allocates `seq = self.downlink.next_seq(recipient_hex)`.
2. Calls `lxm.register_delivery_callback(...)` — the LXMF-level callback
   that fires on RNS first-hop proof (`DELIVERED`), `SENT` (out on the
   network), or `FAILED`.
3. Logs `Reply dispatched to <hex> seq=<N> (bytes)` at dispatch.
4. `_on_outbound` (the callback) logs
   `Downlink ack seq=<N> → <hex8> state=<DELIVERED|SENT|FAILED> after <elapsed>s`
   so journalctl shows, per chunk: dispatched → acked (or failed) and
   after how many seconds.

This is the ground truth the profiler and the operator were missing.
"dispatched" is no longer the last word we log about an outbound chunk.

**Caveat (unchanged from before):** first-hop `DELIVERED` is *not*
end-to-end. It means the first hop (usually the rnode or first relay)
accepted the packet with a signed proof. A LoRa burst can still drop the
tail between rnode and phone with the first hop showing DELIVERED. The
end-to-end app-level ack (bridge → Columba → "yes I got this message")
is a Tier 5 feature and needs a protocol decision on the phone side.

### 2. Real RSSI/SNR into the profiler (`core/profiler.py`)

`ChannelMetrics.from_lxmessage` now reads:
- `RNS.Transport.local_client_rssi_cache[src_bytes]`
- `RNS.Transport.local_client_snr_cache[src_bytes]`

before falling back to `message.rssi` / `message.snr` attributes, then to
`None`. The Transport caches are populated by incoming-packet receipts on
the LoRa air interface (`RNS/Interfaces/RNodeInterface.py` feeds
`r_stat_rssi` from the radio; `RNS/Transport.py` stores per-destination
values in the dicts). So the profiler's LoRa vs TCP and
constrained-vs-standard classification now uses **measured** signal
instead of the blind `tcp_default` 1 Gbit assumption.

### 3. Per-recipient burst pacing

`push_reply` now calls `self.downlink.pace_wait(recipient_hex,
MIN_CHUNK_INTERVAL_MS)` between chunks to the same recipient (500 ms
floor). This is the same shape the old code had (a flat `time.sleep(0.5)`)
but it is now tracked per-recipient and overridable via
`HERMES_CHUNK_INTERVAL_MS`. The per-profile `send_delay_ms` still applies
on the uplink-response path in `_process_and_reply` (a different code
path).

### 4. SIGTERM clean shutdown (`core/bridge.py`)

`run_forever()`'s SIGTERM/SIGINT handler now calls `RNS.exit(0)`.
`RNS.exit` (in `RNS/__init__.py:352`) does:
```python
def exit(code=0):
    global exit_called
    if not exit_called:
        exit_called = True
        Reticulum.exit_handler()   # Transport.detach_interfaces + exit_handler,
                                   # identity save, log detach
        os._exit(code)             # hard process exit
```
The handler wraps it in try/except and falls back to `os._exit(1)` if
RNS is not fully initialised. The process now actually terminates on
SIGTERM, so `systemctl --user stop` completes.

**Verified:** `systemctl --user stop hermes-reticulum` returned in 60 ms
(was: hang until `kill -9`). Bridge restarted cleanly, identity stable,
ACL + control server + LXMF destination all re-announced.

### 5. Approval timeout (900s + env override) — now committed

`control_server.py`:
- `DEFAULT_APPROVAL_TIMEOUT = 900.0`
- `HERMES_MESH_APPROVAL_TIMEOUT` env override (float parse, ValueError
  warning fallback).

Keeps the gate window ≥ the plugin's `MESH_GATE_TIMEOUT` per
`references/approval-gate-timeout.md`.

---

## Tests

`venv/bin/python -m pytest tests/ -q` → **73 passed, 1 pre-existing
failure** (`test_slow_turn_with_fresh_marker_completes` — fails on clean
HEAD too, not introduced by this commit). The one failure is a known
flaky test around the liveness-marker generation counter, unrelated to
downlink / profiler / signal work.

## Verification (live)

- Bridge PID 902155, active since 11:47:06 EDT.
- `ACL loaded: allow_all=False, allowed=2, blocked=0`
- `Liveness guard: 600s`
- `Control endpoint listening on 127.0.0.1:8471`
- `Bridge ready — LXMF address: <redacted>`
- `Bridge running. Press Ctrl+C to stop.`
- `systemctl stop` returned cleanly (60 ms), no hang.

## Still open

- **End-to-end app-level ack** (bridge → Columba → "received"). Needs a
  protocol decision on the phone side. Tier 5.
- **Real RSSI/SNR into the *downlink* profile** (the uplink side is now
  measured; the downlink still uses the uplink-classified profile for
  pacing, which is fine for now but could be refined by reading the
  rnode's own signal back to us on ack receipts).
- **Per-recipient burst pacing keyed on measured loss** — the current
  500 ms floor is a constant; a smarter version would adapt based on the
  `note_outcome` counters (e.g. back off if `failed` > `delivered` for a
  recipient).
- **Pre-existing test failure** `test_slow_turn_with_fresh_marker_completes`
  (unrelated to this commit; liveness-marker generation counter test).
