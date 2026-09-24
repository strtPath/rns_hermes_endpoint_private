---
title: Intended Interaction Model
level: sub-spec
parent: spec-rns-hermes-endpoint.md
subsystem: intended-interaction
status: DRAFT
---

# Intended Interaction Model

## Scope

This document states how the bridge was *imagined* to work, compares that against how it
currently works, and assesses what is reachable. It is the target that the architecture
review refines against. Sub-specs describe what the system does today; this one describes
what it was wanted to do and what the gap costs.

Not covered here: the as-built behaviour of any subsystem. See the CLI interaction
contract, turn lifecycle, approval and step-push, and transport sub-specs.

Status: DRAFT, 2026-09-23. The intended model below is the user's own description, in
their words, recorded before any assessment.

## 1. The intended model

The bridge should interact with the Hermes CLI the way a person does at a terminal. The
user types something, Hermes responds. The bridge is not required to understand Hermes'
internals; it relays text in one direction and text back in the other, and keeps the
conversation alive between messages.

When the agent needs a decision mid-task, it asks. The task stays open and the agent
waits. When the user answers, the agent continues from where it was and finishes the
work. The task is not abandoned and restarted; it is paused and resumed.

Live view matters. Tool calls should stream to the user as they happen, both for
visibility and so a turn can be interrupted. The user is explicit that abort is best
effort, not guaranteed: mesh latency means the interruption may arrive after a tool has
already run. The value is in *seeing* the work, with interruption as a helpful extra
rather than a safety guarantee.

## 2. What the bridge actually does

The bridge spawns a one-shot child per turn: `hermes chat -q <message> -Q`. The child
answers and exits. There is no conversation held open, no pause, and no wait.

When the agent needs a decision mid-task, Hermes' single-query path installs a callback
that refuses to wait and instructs the model to choose for itself. So the agent does not
wait, because it has been told not to. It picks the first recommended option and carries
on.

The bridge separately pushes the clarify question to the mesh, arms a gate, captures the
answer, and injects it into the *next* turn. Every step works. The task the answer
referred to has already finished.

Live view does work, and is the part of the intended model that is realised: the step
watcher relays tool calls, and the pre-tool approval gate can genuinely block a
consequential action.

## 3. The gap, stated precisely

Three of the four properties of the intended model are missing, and they are all
consequences of the same root:

Conversation continuity. Missing. Each turn is a fresh process. Continuity is
reconstructed through session resumption, which is the machinery behind the
fresh-session adoption complexity and a recurring source of bugs.

Pause and resume. Missing. The task cannot be held open because the process that would
hold it exits at the end of its turn.

Agent-initiated questions. Missing, and for the same reason: a process that has already
decided no user exists cannot pause to ask one.

Live view. Present. Unaffected by the others.

## 4. Is the intended model viable

Short answer: yes, with one unresolved obstacle, and the obstacle is not the one it
first appears to be.

### 4.1 The conversational part is viable in principle, with a caveat

Hermes has a genuine interactive mode. `hermes chat` without `-q` runs a session that stays
alive across turns, so a bridge could in principle hold it open, send a message, read the
response, and keep the session for the next message.

This is closer to the intended model than the current design, and it removes a category
of complexity rather than adding one: no per-turn spawn, no session adoption heuristic,
no guessing a session id that does not exist yet, no retry-on-early-death. The session
simply persists.

**Caveat, found on reading the code:** the interactive path is not a line-oriented REPL.
`cli.py:1365` builds a prompt_toolkit `Application` with a layout, key bindings, and a
renderer that deliberately manipulates the terminal cursor and scrollback. It is a
full-screen terminal application that expects to own the display.

That does not make it impossible to drive, but it does mean the naive "write a line, read
a line" approach will not work, and the PTY route described below is closer to mandatory
than optional. This should be settled by experiment, not by reading: the open question in
section 7 covers it.

### 4.2 The mid-task question is the obstacle

In interactive mode, the clarify prompt is a prompt_toolkit modal. It wants a real
terminal to draw in. Handing it a pipe produces the same "no user available" outcome the
bridge sees today.

So the mode only solves the question if one of these also happens: drive the child
through a PTY and speak enough terminal protocol to answer the modal; embed the agent
in-process in the bridge and set the clarify callback directly, the way the gateway and
the TUI do; or get a supported way for a non-TTY session to declare that a live user
exists.

Until one of those is settled, switching to interactive mode buys conversation
continuity and pause-resume for ordinary turns, and does not by itself fix clarify.

### 4.3 The cost, and why it is a mode rather than a replacement

A persistent session holds a process open. Measured on the reference machine: a Hermes
CLI process is roughly 300 to 400 MB resident.

That machine has 3 GB of total RAM with about 1 GB available, and already runs several
gateway processes of comparable size. One persistent bridge session would consume a
meaningful fraction of what is left, and a mesh session plus a Telegram session would
consume two.

The current spawn-per-turn design costs nothing between turns, which is its real virtue
and is invisible until you consider a small machine.

This is a deployment-shape decision, not a correctness one. The recommendation is to
support both:

Spawn-per-turn, the default, for constrained hosts. Keeps the current architecture and
its costs.

Persistent session, opt-in, for hosts with headroom. Simpler internals, true pause and
resume, at a steady memory cost.

Since the project is public and other operators have other hardware, exposing the
tradeoff is more useful than choosing for them. The configuration should name the memory
cost in its documentation so the choice is informed.

## 5. What live view means for the design

The user's framing should be recorded explicitly, because it prevents
over-engineering: abort is best effort. On a lossy, high-latency link, an interruption
request may arrive after the tool has run. The feature's primary value is visibility.

Consequences for design:

The step watcher stays as a streaming path, and its latency is a usability question, not
a correctness one.

The pre-tool approval gate remains the only mechanism that truly blocks before an action
runs. It should not be demoted on the grounds that abort already covers interrupting.

Interruption should be offered on every turn and never promised as a guarantee in user
facing text.

## 6. What this implies for the architecture review

The decision points in the main spec can now be weighed against a stated target:

The child interface (main spec 6.1) is the gating decision. Whichever way it goes
determines whether pause-and-resume is possible at all.

Downlink retry (6.2) matters more under the intended model than the current one, because
a paused task waiting on a question is a task that hangs if the question never arrives.

Watcher-observes-via-polling (6.6) is compatible with the intended model but becomes
less necessary if the child is embedded in-process, since then the bridge has the
events directly.

## 7. Open questions and unknowns

Which of the three mid-task options is cheapest in practice. The PTY route is the least
invasive but the most fragile; the embed is the most capable but the largest change.
What the interactive session's output actually looks like over a pipe, which needs a
real experiment rather than a reading of the code.
Whether a persistent session's memory cost grows over a long conversation, which would
matter on constrained hosts regardless of the baseline.

## 8. Invariants of the intended model

The task is held open while waiting for an answer; it is paused, not restarted.
An interruption is best effort and is never promised.
Seeing the work is the primary value of streaming; stopping it is secondary.
