# Downlink burst loss + recap replay — 2026-08-29 (replies "dispatched" but never arrive)

**Symptom.** User reported: over the RNS mesh, messages stopped arriving on the
phone (columba on rnode) even though channel bandwidth was visibly in use and
the desktop llama.cpp was generating tokens. The user's last two sent messages
were *"this was the last message to get through to me"* (16:54) and
*"hello?"* (17:04). No replies reached the phone.

## What my end actually did (journalctl, hermes-reticulum)

The bridge was **healthy** — no errors, no SIGKILL, no code -9, no restarts,
no dropped sends. Both user messages were received, processed, and got full
reply bursts dispatched to the phone peer (identity redacted):

- 16:37:57 "I think you can commit..." → 13 chunks, streamed 16:37→16:40 (normal)
- 16:54:57 "last message to get through" → **17 chunks, all in 16:54:58** (instant)
- 17:04:12 "hello?" → **24 chunks, all in 17:04:13** (instant), then 17:04:51

Two tells:
1. **`handle_outbound` is fire-and-forget.** "Reply dispatched (N bytes)" logs
   the instant the LXMessage is handed to the RNS router — it does *not* mean
   the message reached the peer. RNS LXMF over LoRa has no delivery receipt,
   so a chunk can be "dispatched" on this side and silently lost on the
   downlink to the rnode.
2. **The instant one-second burst is the unfixed recap bug.** Each new turn
   re-pushed the *entire prior session's tool history* (17, then 24 chunks in
   one second) instead of just the current turn's steps. A 24-chunk burst in
   one second over opportunistic LoRa (`rssi=None, snr=None, method=1`) is the
   worst-case downlink profile and sheds packets — the tail of each burst was
   lost before the phone got it. The bigger "hello?" burst (24) was *more*
   broken than the earlier one (17), which matches "it got worse, not better".

Note: the desktop llama.cpp burning tokens was serving the **Telegram** chat,
not the mesh session — the mesh replies were pre-built cache replay, not fresh
generation, hence the instant burst.

## Root cause (the bug, now fixed)

`hermes_client.py::_run_step_watcher` started `last_pushed = 0` on **every**
`chat()` turn. `state.db` `messages` are session-global, so on the 2nd+ turn
the tail query `WHERE id > 0` returned the whole session's assistant
tool-call rows and re-pushed them all. Described in
`docs/mesh-bridge-findings-2026-08-29-step-watcher-recap-bug.md` (which
specified the fix) but **the fix was never actually applied to the live tree**
— line 323 was still `last_pushed = 0` at the time of this incident.

## Fix (commit 8f9d0fb, applied + service restarted)

Seed `last_pushed` at the session's existing `MAX(id)` at watcher start, so
only rows created *after* the watcher started (i.e. this turn) are pushed.
Safe: the user message is persisted before the watcher runs.

## What is NOT solved (open)

- **No delivery receipt on the LoRa downlink.** "dispatched" still means
  nothing about arrival. The real fix is a chunk ack / sequence-number scheme
  so the bridge knows a reply actually landed (see the known-gap note in
  memory). Until then, burst loss on weak/opportunistic channels will still
  drop tails silently.
- **Opportunistic channel with no RSSI/SNR** (`method=1`, `rssi=None`): the
  bridge is flying blind on signal. Worth surfacing real RSSI/SNR from the
  rnode so the profiler can rate-limit burst pacing instead of `tcp_default`
  (1 Gbit assumption).

## Verify

- Send a 2nd+ message with `/steps on` on an existing session → only the
  *current* turn's `💻` messages arrive (no replay burst); `pushed=N` in
  `mesh-bridge-step.log` matches the current turn's tool count.
- Watch `journalctl -u hermes-reticulum` — per-turn chunk bursts should no
  longer be one-second walls of 15–25 identical chunks.

## TODO (next, on the fork before upstream)
- [ ] Chunk ack / sequence scheme for the downlink (no more silent tail loss).
- [ ] Real RSSI/SNR into the channel profiler so burst pacing is honest.
- [ ] Consider per-recipient burst pacing (small sleep between chunks) even on
      tcp_default, to stop saturating the opportunistic LoRa channel.
