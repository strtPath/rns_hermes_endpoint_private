# Findings: downlink retry needs idempotent message IDs and client-side dedupe

**Date:** 2026-09-19
**Status:** analyzed — design open, no code written
**Related:** `docs/mesh-bridge-findings-2026-08-29-downlink-burst-loss-and-recap-replay.md`, `docs/mesh-bridge-findings-2026-09-12-reminder-tool-calls-never-arrived.md`

## Incident

2026-09-19, mesh session `20260919_203241_bf6f7f` (anonymized: session id kept,
identity hex redacted below). User sent "yes" over RNS at
10:03:58 (opportunistic, `rssi=None, snr=None, method=1` — phone link cold,
first message in ~12h). Bridge dispatched the reply at 10:10:41 as **seq 188,
1039 bytes** (single chunk, largest of the session). The downlink ack sweep
marked it at 10:16:40:

```
Downlink ack seq=188 state=timeout (no first-hop ack within 300s)
```

User reported no reply arrived on the phone. Every other dispatch that session
(seq 189, 190, 191, 192) acked `DELIVERED` within 1s.

## What the ack actually tells us

`DownlinkTracker` (`core/downlink.py`) registers a per-chunk
`register_delivery_callback` and classifies the first-hop outcome:
`DELIVERED` / `SENT` (propagated) / `FAILED`. The timeout sweep fires when no
callback arrives within `HERMES_DOWNLINK_ACK_TIMEOUT_S` (default 300s).

**Critical scope limitation: this is a first-hop ack, not an end-to-end
delivery receipt.** "no first-hop ack" means the node directly connected to us
(the propagation server) did not confirm receipt. It does NOT tell us:

- whether the message was dropped before reaching the propagation server,
- whether the propagation server received it but its ack back to us was lost,
- whether the propagation server forwarded it to the phone but the phone is
  slow / offline / the forward was delayed.

All three look identical to the bridge: no first-hop ack within the window.

## The retry hazard: at-least-once duplication

A naive "resend on timeout" creates at-least-once delivery over a lossy,
out-of-order-capable mesh. The concrete confusing case:

1. seq 188 dispatched; propagation server receives it, forwards to phone, but
   the forward is slow (phone opportunistic, queued, or the ack path is lossy).
2. 300s pass with no first-hop ack; bridge times out seq 188 and **retries**,
   dispatching the same reply content as a **new seq**.
3. The retry reaches the phone first (faster path, or the original was stuck).
   Phone shows the reply.
4. The original seq 188 then arrives at the phone late. Phone shows the reply
   **again** — a duplicate, possibly out of order relative to other messages.

LXMF has no end-to-end ack to the final recipient, so the bridge cannot
distinguish "original lost" from "original in flight" at the moment it decides
to retry. A blind retry therefore risks duplicates whenever the original was
actually in flight.

## Why seq 188 was a prime loss target

- Phone link was cold/opportunistic (first message in ~12h, `method=1`,
  `rssi=None, snr=None`) — exactly the profile that sheds packets.
- 1039 bytes in a single chunk — the largest dispatch of the session — rather
  than small paced chunks. A single large chunk on a weak link is the worst
  downlink profile (see the 2026-08-29 burst-loss findings).
- **First-hop obfuscation (RNS `local_hops_delta`) was enabled on the phone
  (mesh chat X).** This is a real RNS mechanism (randomized advertised hop
  distance affects the TTL RNS assigns to outbound packets, and a TTL-expired
  packet produces no first-hop ack and no delivery anywhere). **However, the
  mesh chat X JSON for the last successfully-delivered message shows
  `path_interface_at_send = TCPInterface[<propagation-server-host>:<port>]` and
  `path_hops_at_send = 2`** — the downlink is carried over a **persistent TCP
  connection to the propagation server**, not direct RNS propagation. A TCP
  leg does not expire by hop count, so the TTL/obfuscation mechanism does NOT
  explain the seq 188 timeout on this deployment. It is recorded here as a
  general RNS risk that would apply if a recipient were reached by direct
  propagation, but it is not the cause of this incident.

## Design: make retries idempotent + dedupe on the client + TTL-aware

To retry safely, messages must be **idempotent and detectable** at the
recipient, AND the retry must fix the *cause* of the loss (which, when
`local_hops_delta` is on, may be an insufficient TTL, not just a dropped
packet).

### Retry: server-routed TCP needs no TTL correction; keep TTL-aware gating only for direct-propagation recipients

For **this deployment** the downlink is server-routed TCP
(`path_interface_at_send = TCPInterface[<propagation-server-host>:<port>]`,
`path_hops_at_send = 2`). A persistent TCP leg does not expire by hop count, so
a retry over the same interface has **no TTL dimension to get wrong** — it can
re-send safely and more aggressively (on first-hop `FAILED` or a timeout) without
any TTL/route correction. The conservative gating that refuses a blind retry on
an obfuscated-hop link therefore does **not** apply here; the retry for a
server-routed recipient is simple and safe.

The TTL-aware gating is still worth keeping as a **general safeguard** for the
case where a recipient IS reached by direct RNS propagation (no TCP interface),
because there a `local_hops_delta`-induced short TTL can make a blind retry
useless (same TTL, same drop). So the retry policy branches on the recipient's
observed transport:

- **Server-routed (TCP interface present):** retry on `FAILED` or timeout; no
  TTL correction needed; re-chunk smaller to reduce burst loss.
- **Direct propagation (no TCP interface, RNS propagation):** do NOT blind-retry
  on a bare silent timeout when hop obfuscation is on; require a learned true
  hop count / TTL correction or a deliberate route via a server first.

### Bridge side (our repo)

1. **Stable message ID per logical reply.** Assign each reply a stable ID
   (reuse the original `seq`, or a UUID). Every chunk of that reply — including
   all retries — carries the **same** ID. A retry does NOT mint a new ID; it
   re-sends under the original ID.
2. **Tag chunks with the ID.** Extend the existing `[p<N> i/N]` chunk prefix to
   also carry the message ID, e.g. `[p<N> m<id> i/N] `. The client can then group
   chunks by `m<id>` and detect that a late-arriving chunk set is a duplicate
   of one already received.
3. **Retry only on strong loss signals (fallback).** If the phone client cannot
   dedupe, prefer gating retries on stronger evidence than a bare first-hop
   timeout: channel was opportunistic/cold, first hop reported `FAILED` (not a
   silent timeout), or the message was a single large chunk that is known to be
   loss-prone. Otherwise, prefer re-chunking the reply smaller and re-sending
   (reduces burst loss) over a blind full-message retry.

### Client side (rnode / columba on the phone)

4. **Dedupe by message ID.** The phone client drops any chunk set whose `m<id>`
   it has already fully (or partially, per a policy) received. This is what
   actually eliminates the "retry arrives, then original arrives late" duplicate.
5. **Reassemble by per-message sequence.** Within a given `m<id>`, order chunks
   by the `i/N` index so out-of-order arrival reassembles correctly, and a
   complete set suppresses the other copy.

### The dependency

Full dedupe **requires the phone client to honor the ID tag.** The phone runs
columba on rnode; if that client is not under our control, the bridge can still
make retries idempotent and tag them, but the duplicate-suppression only works
if the client reads the tag. If it cannot be modified, the only safe bridge-side
behavior is conservative retry gating (item 3) plus smaller re-chunking — we
cannot unilaterally prevent a duplicate that the client will render.

## Open questions

- **Why did seq 188 time out when seq 189-192 on the same TCP path acked in
  1s?** This is now the primary open question. The transport is server-routed
  TCP (persistent TCP leg to the propagation server, 2 hops), so it is not a
  TTL/obfuscation issue.
  The timeout with no first-hop ack on a reliable TCP leg points to a transient
  TCP/bridge or propagation-server hiccup (server not acking, connection
  blip, or the bridge's first-hop callback not firing), not a mesh propagation
  loss.
- **How much control do we have over the phone (columba/rnode) client?** Can it
  be patched to dedupe by an ID tag, or is it fixed? This decides whether the
  fix is bridge-only (conservative) or bridge+client (full idempotency).
- **Message ID format:** reuse `seq` vs. a fresh UUID per logical reply. `seq`
  is monotonic and already unique per dispatch, but a retry reuses the original
  seq — need to confirm the client treats "same seq, new dispatch" as a
  duplicate, not a new message. A UUID per logical reply is unambiguous but
  must fit in the 368-byte block budget alongside content.
- **Retry policy:** max retries, backoff, and whether to re-chunk smaller on
  retry. (For server-routed recipients the policy can be simpler/more
  aggressive since there is no TTL dimension; for direct-propagation recipients
  the conservative/TTL-aware gating applies.)
- **Partial receipt:** if the client got chunks 1-3 of a 5-chunk reply and then
  the retry re-sends all 5, does the client keep 1-3 and only need 4-5, or does
  it treat the retry as a fresh full set? Policy needed.

## Next step

Plan (see `.hermes/plans/` companion doc): bridge-side idempotent-retry design
(stable ID + chunk tagging + conservative retry gating) and the client-side
dedupe requirement, with the phone-client-control question called out as the
blocking open item before committing to a blind retry.
