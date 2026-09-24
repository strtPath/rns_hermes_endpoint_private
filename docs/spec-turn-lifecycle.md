---
title: Turn Lifecycle
level: sub-spec
parent: spec-rns-hermes-endpoint.md
subsystem: turn-lifecycle
status: DRAFT
---

# Turn Lifecycle

## Scope

This document owns the path a single turn takes: an LXMF message arriving, being
routed to a peer's session, driving a Hermes child, and producing a reply. It covers
inbound handling, concurrency, the phases of `chat()`, outbound delivery, and failure
behaviour. It does not cover the CLI contract itself (Hermes CLI interaction contract
sub-spec), the approval gate or step watcher internals (approval and step-push
sub-spec), or the transport and queue layers (transport and downlink sub-spec).

Status: DRAFT. As-built description, written from code reading on 2026-09-23. Where the
code behaves in a way that is likely to surprise a new reader, that is called out
explicitly rather than smoothed over.

## 1. Inbound

An LXMF message arrives on the Reticulum event loop thread and lands in
`LXMFBridge._on_lxmf_message` (`src/hermes_reticulum/core/bridge.py:427`). The docstring
notes the thread: this callback runs inside the RNS event loop, not a worker thread, so
anything slow here delays the whole transport.

The handler does not process the turn inline. It hands the work to a thread pool
(`bridge.py:322`), sized by `_MAX_HANDLERS`. Dispatch is what keeps the event loop free.

### 1.1 Peer identity and the ACL

A peer is identified by its RNS/LXMF destination hash, referred to throughout as
`source_hash`. This is the identity used for authorisation, for routing a session, and
for deciding who a reply goes back to.

Access control is deny-by-default. `cli.handle_message` consults
`acl.AccessControl.is_allowed` (`src/hermes_reticulum/core/acl.py`). A peer not on the
allowlist is rejected before any turn starts, so an unauthorised sender cannot spend
model time or reach a session.

The active peer is recorded per turn in the handler (`cli.py:320`), which sets it on the
client before the turn runs. The reason this is a per-turn value and not a single shared
field is covered under concurrency below.

### 1.2 Why this ordering matters

Authorisation happens before session resolution. That is the correct order: it means a
rejected peer cannot create a session, cannot resume one, and cannot observe whether a
given session exists. Worth preserving deliberately, because the reverse ordering would
leak that a session exists to an unauthorised peer.

## 2. Concurrency

### 2.1 The pool

`LXMFBridge` owns a `ThreadPoolExecutor` (`bridge.py:182`, created at `bridge.py:322`).
Multiple turns can run at once, bounded by the worker count. This is the single most
important fact about the bridge's design, because almost every shared-state bug in this
codebase traces back to it.

### 2.2 Pacing is per recipient

`DownlinkTracker.pace_wait` (`src/hermes_reticulum/core/downlink.py:147`) enforces a
minimum interval between sends to the same recipient, defaulting to
`MIN_CHUNK_INTERVAL_MS` (`downlink.py:15`, overridable via `HERMES_CHUNK_INTERVAL_MS`,
default 500 ms). The counter is keyed per recipient, so two peers are paced independently.
The lock is held only around the clock bookkeeping, not across the send.

### 2.3 The same-peer collision, and the fix that made it safe

Two turns from the same peer can overlap, and two turns from different peers almost
certainly will. The original design stored the current peer in a single shared field on
the client. With a thread pool that is a race: peer B's message arriving mid-turn would
overwrite the field, and peer A's clarify question would then be armed for the wrong peer,
while A's actual answer would be rejected.

The current code captures the peer as a **turn-local** at the start of `chat()` and
threads it into the watcher as `turn_peer`, rather than reading a shared field later. The
parameter exists specifically because the bridge fans out on a thread pool (see the
docstring at `hermes_client.py:662`).

This is the pattern to copy for any new shared state introduced here: capture per turn,
pass explicitly, never read a shared mutable field after the thread starts.

## 3. The turn

`HermesClient.chat()` (`hermes_client.py:1287`) drives one turn. Its phases, in order:

Session resolution. The client works out which Hermes session this peer's conversation
belongs to. On a first turn of a new session the id is genuinely not known yet, because
the child creates the session when it runs and this Hermes build has no
`--create-if-missing`. The client therefore starts an untitled session and adopts it
afterwards. See the CLI contract sub-spec for the spawn details.

Step-mode prefix. If step-through mode is on, the outgoing prompt is prefixed with
`_step_prompt_prefix()`, which instructs the model that its tool calls are being relayed
to the user and it should not restate them.

Child spawn. A `hermes chat -q ... -Q` subprocess is started. Details in the CLI contract
sub-spec; the important property here is that the bridge's model of the turn is a
subprocess it must monitor.

Watcher start. The step watcher thread starts before the child finishes, so tool calls
can be pushed to the peer as they happen rather than after the fact.

Waiting and extraction. The bridge waits on the child, reading stdout and stderr. The
liveness watchdog runs concurrently.

Post-processing. The reply is extracted, the session is adopted or titled if this was a
fresh one, and the answer is delivered back to the peer.

### 3.1 Background threads

Three things run off the main turn thread: the stdout reader, the stderr reader, and the
liveness watchdog. The step watcher is a fourth when it is active. Each of these holds a
reference to turn-local state rather than consulting shared fields.

## 4. Outbound

There are two distinct send paths, and confusing them is a common source of "why did my
message go out twice" or "why did the reply never arrive".

`Bridge.push_reply` (`bridge.py:239`) is the proactive, multi-part path. It splits long
text with `split_message` (`adapter.py:42`) at `STEP_CHUNK_CHARS` (1500, `bridge.py:32`),
tags multi-part sends with `[p<N> i/N]`, and paces between parts. This is the path used
for step pushes and clarify questions.

`Bridge.send_reply` (`bridge.py:475`) is the atomic single send. It carries the final
answer for a turn, and it is also the point at which send timing is recorded for pacing.

The distinction that matters: `push_reply` is for "here is something happening while you
wait", `send_reply` is for "here is your answer".

### 4.1 The downlink queue, and the retry that does not exist

`DownlinkTracker` assigns monotonic sequence numbers, tracks acknowledgements
idempotently, and detects timeouts lazily. The timeout sweep
(`downlink.py:215`) pops the expired sequence, increments a counter, and logs at INFO.
That is all it does.

There is **no bridge-level retry**. A sequence that is never acknowledged is counted and
forgotten. The transport below may retry on its own while a message is still in its
pending queue, but the bridge itself never resends a timed-out sequence. This was
confirmed by reading the code rather than inferred from behaviour.

For a chat reply this is tolerable, since the peer will usually say something if nothing
arrives. For a clarify question, or a step push on a lossy link, it is a real gap: the
question can simply never be seen, and the sender has no mechanism to discover that.

The only operator-visible state is `DownlinkTracker.stats()` (`downlink.py:203`), a set of
counters. There is no cleanup thread and no per-message record retained after resolution.

## 5. Clarify

The round trip, as built:

The step watcher spots a `clarify` tool call in the session's message rows. It renders
the question and choices (`_format_clarify`, `hermes_client.py:468`), pushes it to the
peer, and only then arms the gate (`_push_step_clarify`, `hermes_client.py:616`). The
ordering is deliberate: an undelivered question must not consume the peer's next
unrelated message as though it were an answer.

`arm_clarify_wait` records which peer was asked. When a message later arrives,
`capture_clarify_answer` (`hermes_client.py:509`) accepts it as the answer only if the
sender matches that peer. A different peer's message is treated as a normal turn.

The captured answer is injected into the next prompt as a prefix, via
`pop_clarify_answer`.

### 5.1 The gap this design does not close

Everything above describes the bridge's half of the round trip, and the bridge's half
works. The half that does not work is on the other side of the process boundary: the
Hermes child, running in `-q` single-query mode, has already been told that no user
exists and has already answered the question itself.

So the gate arms, the answer arrives, the answer is captured, and it is injected into a
turn that has long since finished. The bridge is faithfully routing an answer to a
question the child stopped caring about. Full explanation in the CLI contract sub-spec.

This is why the feature reads as "working but wrong" rather than broken: every log line
is correct, every message is delivered, and the outcome is still not what the user asked
for.

## 6. Failure modes

Child dies early. A single retry is allowed, on the reasoning that an early death is more
likely a transient startup failure than a real fault. The retry is suppressed when a
deny-veto is in force, so a user saying "no" does not cause the turn to be re-driven.

Child dies late. No retry. The turn ends and the peer is told, if the death is
distinguishable from a normal end.

Guard kill. The liveness watchdog can kill the child itself. Historically this was
reported to the peer as an opaque exit code, which read as a crash rather than a guard
action. The findings document for the original incident is
`docs/mesh-bridge-findings-2026-08-22-error-code-9.md`.

Approval denied. Handled through `_deny_veto`, described in the approval and step-push
sub-spec. The short version: the veto ends the turn and suppresses the retry, and a veto
raised when no child is running is ignored, which is the guard against the historical
retry loop.

Empty output. A child that produces no text is not treated as success. Historically the
step watcher had a related bug where a genuinely empty tool result was confused with a
result that had not arrived yet; that is covered in the approval and step-push sub-spec.

## 7. Seams and coupling points

Every place the bridge depends on a detail of another system:

The Hermes CLI's spawn flags and output format. If the CLI changes how it prints the
final response or the session id, extraction breaks silently.
The `state.db` schema. The step watcher queries `messages` directly, including a
`_messages_has_column` probe to handle a missing timestamp column.
The Hermes config file. Model selection and hook timeouts are read from it.
The mesh-tool-gate plugin's protocol with the control server, including the thread-name
keying of approvals.
The installed RNS and LXMF libraries, whose routing and stamp behaviour the bridge
inherits rather than controls.

## 8. Open questions and unknowns

How the child's stdout is parsed in detail, and how tolerant that parsing is to format
changes. Not yet documented to the same standard as the rest of this document.
Whether the fresh-session adoption path can race with the watcher on a very fast first
turn.
Whether the LXMF router's internal retry is sufficient in practice on a lossy link, or
whether the absent bridge-level retry produces user-visible message loss.

## 9. Invariants observed

Authorisation happens before session resolution, always.
Every turn captures its peer locally at the start; no code path reads a shared peer field
after the thread has started.
An unacknowledged sequence is never resent by the bridge.
The clarify gate is armed only after a successful push.
