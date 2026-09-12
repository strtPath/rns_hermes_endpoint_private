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

- Why was `1322e89` reverted on 08-30? (No commit message beyond "Revert".)
- Is the step-push path (hermes_client `_push_step` → bridge) subject to
  the profile's `send_delay_ms`? Currently no — that delay is only in
  `_process_and_reply`. Step bursts need their own pacing.
