# RNS restart: out-of-order and delayed delivery after interface change

Session: 2026-09-23. Observed during live Tier 3 clarify testing.

## Symptom

When the phone-side RNS is restarted (e.g. app killed and relaunched, or
Tailscale link drops and re-establishes), the phone gets a new RNS
destination identity. During the transition window:

1. A message sent from the phone before the restart may arrive at the
   bridge **after** a newer message, because the old identity's in-flight
   messages were queued at a propagation server and delivered after the
   new identity's direct messages.

2. A message sent from the phone after the restart may be delivered via
   a propagation server (hop count > 0) instead of the direct path,
   arriving slower than expected.

3. The bridge receives both the stale (pre-restart) and fresh
   (post-restart) messages and processes them in arrival order, which
   may be the reverse of send order.

## Root cause

RNS/LXMF message delivery is **at-least-once, no-order-guaranteed**.
When a destination identity changes (new RNS keypair), the old
identity's in-flight messages are not cancelled. They sit at
propagation servers until the server's delivery retry logic fires,
which can be seconds to minutes later. The new identity's messages
take the direct path (hop 0) and arrive first.

This is not a bridge bug. It is inherent to the RNS addressing model:
a destination is an RNS destination hash, and a restart generates a
new hash. The old hash's messages are orphaned to the propagation
servers.

## What the bridge does about it

- **Inbound**: the bridge processes messages in arrival order. There is
  no sequence-number reordering on the inbound path (LXMF does not
  provide one). The bridge cannot distinguish a stale pre-restart
  message from a legitimate post-restart one by content alone.
- **Outbound**: the downlink tracker (`core/downlink.py`) allocates
  monotonic sequence numbers per send, but these are for ack tracking,
  not for ordering guarantees. The `[tag i/N]` prefix lets the
  *recipient* spot a dropped tail, but does not reorder out-of-order
  single messages.

## Practical impact

- If the user sends two messages in quick succession across a restart,
  the agent may see them in reverse order.
- A clarify question pushed to the phone may arrive after the user's
  reply (if the push was in-flight when the phone restarted).
- The 30-minute (now 1-hour) clarify answer window is long enough that
  a delayed clarify question does not expire before the user answers.

## Mitigations (user-side)

- After a phone RNS restart, wait ~10-15s before sending a new message.
  This lets in-flight propagation messages drain.
- If a message arrives out of order, just re-send it. The agent sees
  duplicates as repeated prompts, which is benign (the agent may repeat
  itself, but the clarify/steer state is idempotent).
- The clarify answer window (1 hour) is long enough that a delayed
  clarify question does not expire.

## Possible bridge-side improvements (not yet implemented)

1. **Inbound dedup**: hash the message content + sender identity + a
   short time window. If the same content arrives twice within 5s,
   drop the duplicate. This handles the case where a message is
   delivered via both direct path and propagation server.

2. **Stale-message suppression**: if the sender identity changed
   recently (track last-seen identity per peer), suppress messages
   from the old identity for a short window (e.g. 30s) after the new
   identity first appears. This is heuristic and can suppress
   legitimate messages if the user rapidly switches identities.

3. **Sequence-aware inbound**: LXMF messages carry a ticket (timestamp
   + nonce). If the ticket is older than a recent message from the
   same peer, log a warning. This does not reorder, but makes the
   out-of-order event visible in the bridge log for debugging.

None of these are implemented. They are noted for future work if the
out-of-order issue becomes frequent enough to warrant the complexity.

## Clarify timeout change (same session)

The clarify answer window was raised from 30 min (1800s) to 1 hour
(3600s) in `hermes_client.py` (`CLARIFY_ANSWER_TIMEOUT_S`, env
`HERMES_MESH_CLARIFY_TIMEOUT`). The clarify gate is a standalone
mechanism (not coupled to the three-layer approval gate), so this
change does not affect the 480/480/490 gate alignment. Rationale:
the user reported the 30-minute window felt too short, especially
given mesh delivery delays and the possibility of a delayed clarify
question after an RNS restart.
