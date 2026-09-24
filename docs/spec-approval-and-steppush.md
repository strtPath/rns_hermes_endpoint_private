---
title: Approval Gate and Step Push
level: sub-spec
parent: spec-rns-hermes-endpoint.md
subsystem: approval-steppush
status: DRAFT
---

# Approval Gate and Step Push

## Scope

This document owns two coupled subsystems: the pre-tool approval gate that can block a
tool call until a peer approves or denies it, and the step watcher that relays tool calls
to the peer as they happen. They are documented together because the watcher is the only
thing that arms the clarify gate, so their behaviour is interlocked.

Not covered here: the spawn of the Hermes child and its output parsing (Hermes CLI
interaction contract sub-spec), the end-to-end message flow and outbound queue (turn
lifecycle sub-spec), or transport details (transport and downlink sub-spec).

Status: DRAFT, as-built from code reading on 2026-09-23.

## Part A: The approval gate

### 1. What the gate is for

The bridge runs an agent with real tool access, driven by messages arriving over a radio
link. Some tools are consequential enough that a human should agree before they run. The
gate intercepts those calls, asks the peer, and blocks until told what to do. Its default
posture is to deny: if no answer arrives, the tool does not run.

### 2. Where it lives, and the two-process split

The gate spans two processes, and this is the part that makes it hard to reason about.

The plugin side is `mesh-tool-gate` (in `~/.hermes/plugins/`), loaded into the gateway
process. It hooks `pre_tool_call` and owns `MESH_GATE_TIMEOUT` (`__init__.py:119`,
default 900). When it decides a call needs approval, it blocks the tool call and asks the
control server.

The control server side is `src/hermes_reticulum/core/control_server.py`, running in the
bridge process. It owns the approval wait and the `HERMES_MESH_APPROVAL_TIMEOUT` value,
and it reports that value at `/status` as `approval_timeout_s`.

So the two halves are configured from different places and can disagree, which is exactly
what the validation below guards against.

### 3. The timeout chain and the required ordering

Three values are involved, and the ordering between them is not the ordering a reader
would guess:

`MESH_GATE_TIMEOUT`, plugin side, default 900. How long the plugin waits for a verdict
before failing closed.

`HERMES_MESH_APPROVAL_TIMEOUT`, control server side, default 900. How long the control
server waits for the peer to answer.

`plugins.hook_callback_timeout`, in the Hermes config. How long Hermes waits for the hook
callback to return. Code default is 30, which is far too short for a gate that expects a
human on a radio link; the deployment sets 490.

The required relationship is **`MESH_GATE_TIMEOUT >= HERMES_MESH_APPROVAL_TIMEOUT`**, that
is, the plugin's window must be at least as long as the control server's. The reasoning:
the control server is holding the question, so the plugin must not give up on it first.

This is enforced as a diagnostic rather than a hard failure.
`_validate_timeout_against_control_server` (`__init__.py:800`) compares the plugin's value
against the value the control server *reports*, not against the bridge's environment,
because the two processes load configuration independently. On a mismatch it logs a loud
warning (`__init__.py:851`) and does not raise, so a control server that is not up yet at
boot degrades to the gate's own fail-closed behaviour instead of breaking plugin load.

And `hook_callback_timeout` must be at least the plugin's window, or a verdict arriving
late is rejected by Hermes after fail-closed has already blocked the call. This is the
trap for anyone deploying this: the config default is 30 seconds, which is long enough for
a local terminal prompt and hopeless for a mesh round trip.

Summary of the chain, shortest to longest: hook callback must exceed the plugin window,
and the plugin window must be at least the control server's.

### 4. Why the approval is keyed by thread name

The control server keys each approval by the bridge's mesh thread name, taken from
`sessions.title` in `state.db` (for example `mesh-reticulum-<epoch>`), not by Hermes' own
session key.

This was a genuine bug. The plugin used to POST `get_current_session_key()`, a
Hermes-side identifier, which the control server could never match against anything it
knew. The approval request went out, the verdict came back, and the two could not be
correlated.

The lesson generalises: when two processes have to agree on an identifier, the identifier
must be one both of them actually possess. A thread name is visible to both; a
process-local session key is not.

### 5. The deny veto

`_deny_veto` (`src/hermes_reticulum/cli.py`) is the choke point for a mesh denial. It ends
the turn and suppresses the retry, so a peer saying "no" does not cause the same turn to
be re-driven.

The critical guard: a veto raised when **no child is running** is ignored
(`hermes.is_running()` false, debug-logged). This exists because of a real incident. A
veto used to re-fire against an already-dead child, re-driving the same prompt, which
re-hit the same non-safe tool, which timed out, which denied by default, which killed and
resumed, in a loop that ran for roughly 45 minutes with no live child at all.

Two layers of idempotency now guard it: the veto suppresses the retry, and a veto with no
in-flight child no-ops. Worth knowing that the upstream driver of the re-fire was never
identified; the guards make it harmless rather than fixing whatever kept triggering it.

## Part B: Step-through mode and the watcher

### 6. What step-through mode is

Step-through mode relays each tool call, and its output, to the peer as its own message,
so a user on the mesh sees the agent working rather than waiting in silence for a final
answer.

It is opt-in and tracked by a state file, `~/.hermes/.reticulum-step-mode`, which is the
source of truth rather than an in-process flag (see the comment at
`hermes_client.py:1320`). That choice matters: it means the setting survives a restart and
is visible to any process that asks.

Clarify is separate and independent: `_clarify_enabled()` (`hermes_client.py:398`) reads
`HERMES_MESH_CLARIFY`, which **defaults to on** (`"1"`), and can be disabled with `0`. The
separation was a deliberate fix: the clarify gate is armed by the watcher, so tying the
watcher to step mode made the clarify round trip dead in the default configuration where
step mode is off. The watcher now runs whenever a push callback exists, and only the
*step pushes* are gated on step mode.

### 7. How the watcher reads tool calls

The watcher polls the Hermes `state.db` for the session's message rows. It cannot be
pushed events, because the child is a separate process.

Row selection is by time window, `timestamp >= turn_start - 1s`, plus a set of
already-pushed row ids for dedup. An earlier design seeded on a `MAX(id)` watermark, which
was fragile across a session boundary. There is a fallback to id seeding when the messages
table has no timestamp column, detected by probing the schema
(`_messages_has_column`).

### 8. The deferral rule, and why it is subtle

A tool call lives in an assistant row; its output lives in a separate result row. If the
watcher pushed the assistant row the moment it saw it, the user would receive the call
with no output, and the output would arrive as a second orphaned message or not at all.
So the watcher **defers**: it holds an assistant row until the matching result row
appears, then pushes the pair together.

The state involved:

`results_by_cid` maps a call id to its result text.
`seen_result_cids` records call ids whose result **row** has actually been seen.
`still_deferred` holds rows waiting on a result.
`pushed_cids` dedupes at the level of individual calls, not rows.

The distinction between `seen_result_cids` and `results_by_cid` is the subtle part, and it
existed as a bug. A tool can legitimately return empty output. If deferral keyed on "the
result text is non-empty", an empty result would look identical to "the result has not
arrived yet", the row would defer forever, and the call would never be pushed at all. The
fix is to track the *row's presence* separately from its *text*, and defer only on the
former.

The per-call dedup exists for a similar reason: one assistant row can carry several calls,
and if one call's result lands before its sibling's, the completed call must not be
re-pushed on every poll while waiting.

### 9. The clarify special case

A `clarify` call is pushed **on sight**, exempt from the deferral rule
(`hermes_client.py:833`). The reason is a deadlock that the deferral rule would otherwise
create: a clarify call blocks the agent until answered, so its result row cannot exist
until after the answer, and the answer cannot arrive until the question is pushed. Waiting
for the result row means waiting forever.

So clarify bypasses deferral, is pushed immediately, and arms the gate only after the push
succeeds.

### 10. How a push reaches the peer

The chain: `hermes_client` calls its push callback, which the CLI wires at `cli.py:291`
to `_step_push`, which looks up the peer for the session and calls
`bridge.push_reply(peer, text, identity)`. `push_reply` chunks at 1500 characters, tags
multi-part messages, and paces between parts. Full details in the turn lifecycle and
transport sub-specs.

## Current values in this deployment

Listed with key names only; values live in gitignored files.

`MESH_GATE_TIMEOUT` - code default 900, set in the repo env file.
`HERMES_MESH_APPROVAL_TIMEOUT` - code default 900, set in the repo env file.
`plugins.hook_callback_timeout` - 490 in the Hermes config, code default 30.
`HERMES_MESH_CLARIFY` - code default on ("1"); the step-mode file is also set to 1.
`MESH_GATE_TRIAGE` - default off, unset here.
`MESH_GATE_TRIAGE_CONF` - default 0.6.
`MESH_GATE_TRIAGE_EXEC` - default 0, not honoured.
`HERMES_MESH_CONTROL_URL` - default http://127.0.0.1:8471.
`HERMES_MESH_TOKEN_FILE` - default points into the LXMF storage directory.
`HERMES_CHUNK_INTERVAL_MS` - default 500.

## Open questions and unknowns

Whether the gate's triage thresholds are exercised at all in this deployment, given
triage is off.
Whether an approval request survives a bridge restart mid-wait.
How often the watcher's one-second poll interval produces a visible delay to the peer on
a fast turn.

## Invariants observed

The gate is fail-closed: no verdict means no tool execution.
A clarify question is never deferred behind its own result row.
The clarify gate arms only after a successful push.
A deny-veto with no running child does nothing.
An assistant row is not pushed until its result row is present, and an empty result still
counts as present.
