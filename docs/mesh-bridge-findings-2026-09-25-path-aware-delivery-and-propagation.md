---
title: Path-aware delivery and propagation policy
date: 2026-09-25
status: implemented
---

# Path-aware delivery and propagation policy

## What was wrong

Two defects in the plugin's outbound path, both found from one symptom: a turn
completed, the model ran, and nothing arrived.

**1. `send_to()` reported success for a message that never left.**

`LXMRouter.handle_outbound()` queues a message; it returns while the router is
still deciding whether a route exists. The transport returned `True`
immediately after that call, so a cancelled message was indistinguishable from
a delivered one. Nothing was logged, nothing failed, and every dropped reply
looked sent. This is the defect that made the underlying problem take an hour
to find instead of a minute.

The real failure, seen when the message state was actually watched:

```
state -> OUTBOUND
state -> CANCELLED          (about one second later)
```

`MAX_PATHLESS_TRIES = 1` in LXMF: one attempt, no path, cancelled.

**2. `desired_method=DIRECT` had no path awareness, and DIRECT does not ask
for one.**

RNS path state expires. A peer that has not announced recently has no path, and
only `OPPORTUNISTIC` sends trigger a path request inside `handle_outbound`. So a
DIRECT reply to a phone whose announce had aged out was guaranteed to cancel,
no matter how reachable the phone actually was.

Measured on the live mesh during diagnosis:

```
has_path: False
hops_to:  128          # 128 is the unreachable sentinel
identity cached: False
```

Then, after the peer announced from its own client:

```
PATH APPEARED after 178.0s  hops=2
```

A path at two hops, for a peer that had been unreachable a moment earlier. The
transport was fine; the path state was simply cold.

## What changed

### Path probe before the method is chosen

`_probe_path()` asks `has_path()`, and when the answer is no it calls
`request_path()` and waits (default 15s, `PATH_WAIT_SECONDS`) for the path to
appear. A warm path pays nothing. This is the missing step: DIRECT alone never
requests a path.

### Method from reachability

`_select_method()`:

- path present -> `DIRECT`. The message travels end to end and a real delivery
  receipt comes back.
- no path, propagation node configured -> `PROPAGATED`. The reply parks on the
  node and reaches an offline peer, which is the normal state for a phone.
- no path, propagation off or unconfigured -> `DIRECT` anyway, with a warning.
  It will fail honestly rather than pretend.

### Terminal-state check before reporting success

`_await_outcome()` watches the message for `OUTCOME_GRACE_SECONDS` (default
2.5s) and treats `FAILED`, `REJECTED` and `CANCELLED` as failure. `DELIVERED`
and `SENT` are success — `SENT` because a propagated message is complete once
the node accepts it, which is the whole point of propagating.

The loop always checks the state once before the deadline, so a zero grace
period (used by tests) cannot report a synchronously-cancelled message as
delivered. That edge was found by a failing test, not by inspection.

### Propagation policy

New setting `RETICULUM_PROPAGATION_NODE`, read through the existing
extra -> scoped env -> default chain:

- `auto` (default) — leave LXMF autopeering alone (max depth 4).
- `off` — disable autopeering and clear any pinned node. A pathless reply fails
  instead of parking on a third party.
- a 32-character hex destination hash — pin that node.

Pinning matters because autopeering only sees nodes that happen to sync with us
and gives no say over which node carries our traffic. A self-hosted node, or one
the operator trusts, has to be named.

An invalid value raises at startup. This setting decides whether a reply reaches
an offline peer, so a typo must not quietly change the policy.

### Chunk pacing

`send()` now paces multi-part replies, using the transport's own report of
whether the last send went direct:

- direct: no gap (`INTER_CHUNK_DIRECT_SECONDS = 0.0`)
- propagated: 2s between parts (`INTER_CHUNK_PROPAGATED_SECONDS = 2.0`)

A burst of chunks is the fastest way to congest a LoRa link, and through a node
the message still has to sync, so back-to-back chunks there spend airtime for
nothing. The first part is never delayed.

A transport that cannot answer the question is treated as direct. Inventing a
delay from an absent answer would slow every send to fix a problem that may not
exist.

## What was deliberately not changed

**The announce interval stays at 60 minutes.** A shorter cadence is the obvious
"fix" for a cold path and it is the wrong one: the mesh is shared, and a
gateway re-announcing more often than the operator's own client is bad
etiquette. The path wait plus propagation covers the cold-path case without
adding traffic.

## Verification status

- Unit: 75 tests across the transport and adapter suites, both directions
  (direct, propagated, off, no node, cancelled, failed, sent, pacing).
  Non-vacuity checked by removing the outcome check (two tests fail) and the
  pacing call (one test fails).
- Live, verified: the send path reaches the transport; path state for a peer
  was observed going from unreachable to 2 hops after the peer announced.
- Live, NOT verified: end-to-end propagated delivery. Every propagation node
  visible from this host reported `hops: 128` at the time of writing — stale
  announce memory with no live path — so a propagated message could be
  dispatched but its arrival could not be confirmed. Do not read the unit tests
  as proving delivery over the mesh.

## Operational notes

When a reply does not arrive, the log now says why. Look for:

- `Message to <hash>.. was cancelled before leaving` — no path, no accepted
  propagation. The reply did not leave this host.
- `No path to peer and no propagation node configured` — the setting is absent
  and the reply will fail. Set `RETICULUM_PROPAGATION_NODE`.
- `Path to peer appeared after a request (hops=N)` — the wait worked; the peer
  was reachable but had not announced recently.

Absence of any of these, with a turn that reached `response ready`, points at
the gateway rather than the transport.
