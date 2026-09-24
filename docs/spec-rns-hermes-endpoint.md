---
title: rns_hermes_endpoint
level: main-spec
status: DRAFT
---

# rns_hermes_endpoint

## 1. What this system is

A bridge between a Reticulum mesh network and a Hermes AI agent. A message arrives over
LXMF from an authorised peer, drives a Hermes agent turn, and the answer goes back over
the mesh.

The point of the project is that the link may be a radio link: slow, lossy, and operated
by someone who is not sitting at a terminal. Every design decision in the bridge is a
consequence of that, and several of the current problems are places where the code has not
fully accepted it.

## 2. The parts

**The bridge process** runs Reticulum and LXMF, holds the mesh identity, enforces the
peer allowlist, owns the downlink queue, and hosts the control server. It is the long-lived
process, managed by a user-level systemd unit.

**The gateway process** runs alongside Hermes' normal gateway and hosts the mesh-tool-gate
plugin that intercepts consequential tool calls.

**The Hermes child** is a short-lived subprocess, one per turn, spawned by the bridge. It
does the actual agent work: model call, tool calls, final answer. This boundary is the
most consequential fact about the architecture, and section 5 explains why.

**state.db** is the shared surface between the bridge and the child. The child writes its
message and tool rows there; the step watcher reads them. It is a coupling point that
neither process strictly owns.

## 3. The shape of a turn

A message arrives on the mesh and is validated. The allowlist is checked before anything
else, deny by default. The turn is dispatched to a worker thread so the transport event
loop stays free. The peer's session is resolved, a Hermes child is spawned in one-shot
mode, and a watcher thread starts polling `state.db` so tool calls can be relayed to the
peer as they happen. When the child finishes, its output is extracted and sent back. Every
one of those stages is documented in a sub-spec; the point here is the shape.

Two properties of that shape are worth holding onto. First, one turn is a subprocess
lifetime, which means the bridge's unit of work is something it must monitor and can lose.
Second, the bridge and the child communicate through a database, not through a
conversation, which means the bridge observes the turn rather than participating in it.

## 4. Sub-specs

**Hermes CLI interaction contract** owns the spawn command, what interface the bridge
actually drives, what that interface guarantees, and what it refuses to do. Read this
first when asking "why doesn't feature X work end to end".

**Turn lifecycle** owns the path of one message: inbound, concurrency, the phases of a
turn, outbound delivery, failure modes.

**Approval gate and step push** owns the pre-tool gate, its timeout chain, the deny veto,
step-through mode, and the watcher's deferral rules.

**Transport and downlink** owns Reticulum and LXMF behaviour and the outbound queue.

## 5. The central design tension

The bridge drives the Hermes CLI in its *programmatic* mode while expecting *interactive*
behaviour from it.

This is the single most important thing in this document. The bridge spawns
`hermes chat -q <message> -Q`, which selects Hermes' one-shot headless path. That path is
designed around the assumption that no human is present, and it acts on that assumption in
ways that are not obvious from the outside. Most visibly, it installs a clarify callback
that refuses to wait and tells the model to choose for itself.

The bridge, meanwhile, has a live human on the other end of the mesh. It pushes the
clarify question, arms its gate, captures the answer, and injects it into a turn that
already finished. Every step in that sequence is faithfully implemented and logged. The
outcome is still wrong, because the two halves disagree about whether a user exists.

This is not a bug in either component. Hermes' one-shot behaviour is correct for cron
jobs and scripts. The bridge's expectations are reasonable for a messaging integration.
They are simply incompatible, and the mismatch was never noticed because nothing wrote
down what interface was being driven.

The general lesson, which is the reason this documentation set exists: a feature can be
implemented correctly at every layer and still fail, when the layers hold different
assumptions and no document states them.

## 6. Decision points for the architecture review

These are recorded, not resolved. Each is a place where the current behaviour is a choice
that was never consciously made, or was made under constraints that no longer hold.

### 6.1 The child interface

The bridge needs "run one turn, with a live user reachable". Hermes offers interactive
mode (needs a TTY and an answering UI) and one-shot mode (assumes no user). There is no
supported flag combination for "one-shot output, live user". The seam that would allow it,
`agent.clarify_callback`, is settable in-process and is used by the gateway and the TUI,
but it cannot be reached from outside the child, and no plugin hook fires at agent
construction.

Options, from smallest to largest: find or add a supported way to supply a clarify
callback to a one-shot child; drive the child over a PTY; embed the agent in-process in
the bridge. This decision gates the clarify feature and any future feature that needs to
pause mid-turn.

### 6.2 No downlink retry

A message the bridge considers timed out is counted and forgotten. For a radio link this
is a poor fit: clarify questions and approval requests are exactly the messages that must
arrive, and there is no mechanism to notice they did not.

### 6.3 Model naming has no single source of truth

Three places can specify a model, with silent precedence: the bridge's `HERMES_MODEL`
environment variable, the Hermes config default, and the delegation model. All three have
been observed pointing at names the inference server did not serve, at different times,
producing failures that looked like something else entirely.

### 6.4 Stamp enforcement is off

Inbound validation accepts messages with invalid stamps. The ACL still governs access, but
the transport layer is not doing the filtering it can.

### 6.5 Timeouts are spread across processes

The gate's effective behaviour depends on values in two environment files and one config
file, in two processes that load configuration independently. There is a diagnostic that
warns on mismatch, which is a good pattern, but the underlying spread is a standing
source of error.

### 6.6 The watcher observes rather than participates

Tool calls are relayed by polling a database that the child writes to. This works and is
loosely coupled, but it makes the bridge's knowledge of a turn a step behind at all times,
and it couples the bridge to a schema it does not own.

## 7. Standing invariants

Collected from the sub-specs, listed here because they are cross-cutting:

Access control is enforced before session resolution.
A turn captures its peer locally; no shared mutable peer field is read after a thread
starts.
The approval gate is fail-closed: no verdict means the tool does not run.
A message the bridge has given up on is never resent by the bridge.
A send returning successfully means accepted, not delivered.
Clarify is delivered on sight and never deferred behind its own result row.

## 8. How to read this set

Start with this document for the shape, then the CLI contract if you are chasing an
end-to-end behaviour, then the sub-spec for whichever layer you are about to change. The
notes files in `docs/_notes-*.md` are the raw research behind the sub-specs, kept because
they carry file:line citations that the polished documents deliberately avoid, since line
numbers go stale.

Convention: sub-specs describe behaviour, the main spec records decisions. When you change
a behaviour, update the sub-spec in the same commit. When you settle a decision in section
6, move it out of the open list and record the choice here.

## 9. Open questions

Whether the PTY route for the child is viable at all, or whether embedding is the only
real option.
Whether the router's internal retry makes the absent bridge-level retry acceptable in
practice, or whether message loss is already occurring silently.
How much of this documentation survives the next significant change to the Hermes CLI
surface, which the bridge does not control.
