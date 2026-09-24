---
title: Transport and Downlink
level: sub-spec
parent: spec-rns-hermes-endpoint.md
subsystem: transport-downlink
status: DRAFT
---

# Transport and Downlink

## Scope

This document owns two layers underneath the turn logic: the Reticulum and LXMF transport
the bridge speaks, and the downlink queue that tracks outbound messages and their
acknowledgements. It does not cover how a turn is driven (turn lifecycle sub-spec), the
approval gate or step watcher (approval and step-push sub-spec), or the Hermes CLI
contract (Hermes CLI interaction contract sub-spec).

Status: DRAFT, as-built from code reading on 2026-09-23, including reading into the
installed RNS and LXMF library sources where the bridge inherits behaviour rather than
implementing it.

Privacy note: the repository is public. No identity hash, hostname, or endpoint appears in
this document. Files holding secrets are named; their contents are not.

## Part A: Transport

### 1. Initialisation

The bridge starts Reticulum, loads its identity, and registers an LXMF delivery
destination. Secret material lives in gitignored locations: the identity file and the
environment file. The control token file sits in the LXMF storage directory
(`~/.lxmf/storage/control_token` by default, per `HERMES_MESH_TOKEN_FILE`).

The identity is the bridge's presence on the mesh. Everything about authorisation and
routing is expressed in terms of the destination hash derived from it.

### 2. Inbound

An inbound LXMF message arrives on the Reticulum event loop thread and is validated before
it reaches the bridge's turn logic: link state, signature validity, and duplicate
suppression.

The handler runs in the event loop thread, which is why dispatch to a worker pool happens
immediately (see the turn lifecycle sub-spec). Anything slow in the callback delays all
transport, not just the message being handled.

**Stamp enforcement is off in this deployment.** LXMF supports message stamps as an
anti-spam measure, enforced by a router setting. `enforce_stamps` is False here, so an
inbound message with an invalid or missing stamp is accepted rather than dropped. The
enforcement point, when enabled, is in the router's delivery path.

This is worth a deliberate decision rather than being inherited. For a public project
whose README other operators follow, the default posture on spam resistance should be
chosen, not accidental. It is not a vulnerability in itself, and it does not bypass the
ACL, which remains the actual access control. It does mean the transport layer is not
doing the filtering it is capable of.

### 3. Outbound

Outbound sends request a method, DIRECT here, and the router resolves a path. If no path
is known for the destination, the message is queued in the router's pending outbound set,
and path selection runs later: direct first, then propagation, then opportunistic.

This matters for expectations. A send that returns successfully has been *accepted*, not
delivered. The bridge's own queue tracks acknowledgement separately, described below. An
unreachable peer therefore has two distinct failure points: the router failing to find any
path, and a path existing but no acknowledgement coming back.

### 4. Path discovery

Path discovery uses a broadcast path request, answered by any peer that already knows a
route, rather than a lookup that queries the local identity store. The practical
consequence is timing: after a restart, the bridge may not know how to reach a peer until
an announce arrives or a request is answered, and that delay appears to the user as
slowness rather than as an error.

## Part B: The downlink queue

### 5. Per-recipient state

`DownlinkTracker` (`src/hermes_reticulum/core/downlink.py`) assigns monotonic sequence
numbers per destination and tracks outstanding sends. Each outstanding entry records the
recipient and the dispatch time. There is a cap on how many sequences may be outstanding
at once (`MAX_OUTSTANDING`), so a peer that stops acknowledging cannot cause unbounded
growth.

### 6. Acknowledgements

Acknowledgements are matched by sequence number and recorded idempotently
(`note_outcome`, `downlink.py:178`): a repeated ack for the same sequence is a no-op
rather than a double count. A terminal outcome pops the entry and increments a counter.

Idempotency here is not a nicety. Radio links produce duplicate deliveries, and a counter
that double-counts on a repeat makes every downstream statistic misleading.

### 7. Timeouts, and the absence of retry

Timeouts are detected lazily rather than by a timer thread: the sweep
(`_sweep_timeouts_locked`, `downlink.py:215`) runs when the next sequence is allocated and
walks the outstanding set for entries older than the acknowledgement timeout. For each
expired entry it pops it, increments the `timeout` counter, and logs at INFO.

It does not resend. There is no code path in the downlink tracker or in the bridge that
re-sends a timed-out or failed sequence. This was confirmed by reading the code rather
than inferred from behaviour.

One qualification: the LXMF router below may re-attempt a message that is still in its own
pending queue. That is router-internal behaviour with its own lifecycle, and it is not a
bridge-level retry of a sequence the bridge has already given up on. The two should not be
conflated when reasoning about whether a message arrived.

The gap this leaves: for a message the peer must act on, such as a clarify question or an
approval request, there is no mechanism to notice that it never arrived and try again. The
message is counted as timed out and forgotten.

### 8. Pacing and multi-part messages

Sends to the same recipient are paced at a minimum interval, `MIN_CHUNK_INTERVAL_MS`
(default 500 ms, overridable by environment variable). Pacing is per recipient, so two
peers do not delay each other. The wait happens *before* a send, and the clock is updated
only on a successful send, so a failed send does not consume the interval.

Long messages are split (`split_message`, `adapter.py:42`) at the chunk size, respecting
sentence and word boundaries where possible, and multi-part sends are tagged with
`[p<N> i/N]` so a recipient can detect a missing part without any protocol change. The
numbering is applied by the splitter itself.

Note the ordering interaction: the splitter numbers the parts, and the downlink paces
them. A slow link with a large step push therefore produces a sequence of numbered
messages arriving over time, which is the intended experience and also the reason pacing
and chunk size are worth tuning together.

### 9. What happens to a never-acknowledged message

It times out, increments the counter, and is dropped from the outstanding set. No record
is retained per message after that point, and there is no cleanup thread.

The only operator-visible state is `stats()` (`downlink.py:203`), a dictionary of counters
such as sends, acknowledgements, and timeouts. An operator can therefore see that
something timed out, but not which message, to whom, or what it contained. If diagnosing
message loss matters, that is the obvious gap to close first.

## Open questions and unknowns

Whether the router's internal retry is sufficient in practice on a lossy link, or whether
absent bridge-level retry produces user-visible loss.
What the appropriate stamp enforcement posture is for this project.
Whether an announce arriving mid-send can cause a duplicate delivery that the idempotent
ack path does not cover.

## Invariants observed

Access control is enforced above this layer, not by it.
A send returning successfully means accepted, not delivered.
A sequence is never resent by the bridge once it has timed out.
Acknowledgements are idempotent by sequence number.
Pacing is per recipient and is not consumed by a failed send.
