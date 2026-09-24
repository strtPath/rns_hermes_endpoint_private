# 2026-08-27 — Bridge liveness-heartbeat + bridge-message-labeling spec

Spec for two things the bridge is missing:

1. A **liveness heartbeat** so the liveness guard can tell a *wedged* model
   from a *slow-but-working* one (today it can't, because the `hermes chat
   -q` child is silent for the whole turn).
2. **Labeling of bridge-originated mesh messages** so a peer can tell an
   agent reply from a human's message (and, secondarily, so the operator can
   see which replies were auto-generated).

Also documents a *root cause* found 2026-08-27: the "gateway hiccup" is a bug
in our own `mesh-tool-events` hook (blocking HTTP inside the gateway asyncio
loop), not a hermes/state.db issue.

Related:
- `mesh-bridge-findings-2026-08-22-error-code-9.md` — the code -9 / liveness
  history this builds on.
- `mesh-bridge-findings-2026-08-26-bridge-pipe-guard-and-gateway-watchdog.md`
  — the gateway watchdog tuning.

---

## Part 0 — The 2026-08-27 "gateway hiccup" (root cause, already found)

**Symptom.** Mid-turn the Hermes gateway "hiccupped" (a session dropped) and
the user saw it around 13:28–13:32.

**Root cause (confirmed in journald, gateway PID 129609).** It is *not* the
hermes database. It is our own hook:

1. `agent:step` fires on every tool batch in every mesh session, including
   this very conversation (`mesh-reticulum-1787840453`).
2. For risky tools the hook does a **blocking**
   `urllib.request.urlopen(..., timeout=600.0)` POST to the bridge control
   server — *from inside the gateway's asyncio event loop* (see
   `~/.hermes/hooks/mesh-tool-events/handler.py`, `_post`, and the two
   `timeout=600.0` gate calls).
3. The control server's `/step` handler is synchronous and, when relaying the
   gate to the mesh, blocks on the (synchronous) LXMF send. On a mesh peer
   that's AFK/offline that handler can sit for many minutes holding the
   control server.
4. So the hook's `urlopen` parks for up to 600 s, and the **whole gateway
   event loop is blocked the entire time** — the smoking gun is the repeating
   `WARNING discord.gateway: Shard ID None heartbeat blocked for more than
   N seconds` (10 s → 100 s) from 13:28:55 to 13:31:47.
5. When the loop unblocks, the burst of overdue heartbeats hits Discord, the
   client 429s, and the active session's connection drops = the "hiccup".

**Two independent bugs, one visible symptom:**
- *Hook side (the event-loop killer):* sync HTTP in an async loop + 600 s
  timeouts. Must never block the gateway loop.
- *Control-server side (what makes each call hang):* the `/step` handler does
  a blocking LXMF relay with no bounded timeout, so an offline peer turns a
  "report" into a multi-minute stall.

**Immediate mitigations (do first, unblock now):**
- Hook: drop the blocking POST from the event loop — dispatch the control-
  server POST onto a short-lived worker thread (or an `asyncio`-safe
  non-blocking client) with a *bounded* timeout (e.g. 5 s for reports, 30 s
  for gates). A hook that can't reach the bridge must degrade to "skip", never
  stall the loop.
- Control server: bound the LXMF relay (connect+read timeout), and do the
  blocking mesh send **off the request thread** so a stuck relay can't wedge
  the handler.
- Optional, belt-and-braces: keep the gateway watchdog relaxed (see the
  2026-08-26 findings doc) until the hook no longer blocks the loop.

This part is *diagnosis + mitigation*; the real fix for the liveness problem
is Part 1.

---

## Part 1 — Liveness heartbeat

### Problem
The bridge runs `hermes chat -q … -Q` as a child and wraps it in
`_run_with_liveness_guard`. The guard kills the child on **zero
stdout+stderr for `HERMES_LIVENESS_TIMEOUT` (default 600 s)**. In `-q`/`-Q`
mode the child prints *nothing* until the final reply (banners go to stderr
at startup only), so the guard is really a **max-turn wall clock**, and it
cannot distinguish:

- a **wedged** model (no progress, should be killed), from
- a **slow-but-working** model (a 71-message session, 34 assistant turns, on a
  local 27B-class model can legitimately take > 600 s).

Today, on a slow turn the child is SIGKILL'd at 600 s, and `chat()` retries
once — but the retry re-runs the *same* work (the prompt is already
persisted), re-burns a full window, and fails identically. Net effect: the
user waits 2 × the window and still gets
`⏱️ Turn exceeded the liveness window (600s)`.

### Already done in this pass (committed)
`HermesClient.chat()` now measures the child's wall time (`_last_run_ms`) and
gates the retry on `_guard_kill_worth_retrying(run_ms)`:

- child died **before** consuming a full window (`run_ms < N*1000`) → treated
  as wedged → retry once (resume) — *this is the correct, useful case*;
- child ran a **full window** (`run_ms ≥ N*1000`) → treated as slow, not
  wedged → **no retry**, honest "ran past the liveness window — re-send to
  continue" reply.

This stops the guaranteed-double-wait, but the underlying "silent child"
problem remains: a *genuinely* wedged 600 s+ turn still gets killed and
reported, and a slow turn still dies at the window instead of completing.

### Goal
Let the child emit **heartbeat bytes while it works**, so the liveness guard
becomes a *stall* detector instead of a wall clock. The guard kills on
`no output for N seconds`, which is exactly right **once** the child
produces periodic output during a turn.

### Design options (ranked)

**Option A — bridge-side heartbeat via a state-file watcher (recommended).**
No hermes change, no new CLI flag.

- The `agent:step` hook (already fires per tool batch) and/or the bridge write
  a monotonic "still working" marker to a small file, e.g.
  `~/.hermes/.reticulum-turn-alive` (last-activity timestamp + session id).
- A new bridge-side watcher thread, started inside
  `_run_with_liveness_guard`, polls that file once per second. Each fresh
  timestamp *touches the liveness clock* (same `_touch()` the stream readers
  use).
- The guard then means: *kill only if the model made no tool progress AND
  emitted no bytes for N seconds.* A working model keeps the file warm; a
  wedged one stops touching it and gets killed.

  *Caveat:* `agent:step` only fires after a tool **batch**, so a single very
  long tool call (one giant `terminal` run) won't itself refresh the marker.
  Acceptable: the N-second window is per *turn*, and the realistic wedge is a
  stalled model, not a 10-minute single tool. If needed, also have the hook
  write the marker at batch *start* (pre-batch), not just after.

**Option B — `hermes chat -q --json-lines` (or `--stream`).**
If the hermes CLI is ever given a line-oriented streaming flag, each emitted
line is already a natural heartbeat (the existing stream readers `_touch()`
the clock). Cleanest signal, but blocked on an upstream CLI feature. Track it;
adopt if it lands.

**Option C — raise `HERMES_LIVENESS_TIMEOUT`.**
Only a band-aid. It makes the wall clock longer but still kills slow-but-
working turns, and widens the wedge-detection lag. Not a fix; list it as a
runtime knob, not the solution.

**Recommendation:** ship **Option A** now (fork-only, no upstream dependency,
directly serves the "wedged vs slow" distinction), and keep **B** on the
roadmap. Leave the retry gate from "Already done" in place as a second layer.

### Spec — Option A

1. **Marker file** `HERMES_TURN_ALIVE_FILE` (default
   `~/.hermes/.reticulum-turn-alive`). Written as JSON:
   `{"session": <title>, "ts": <float monotonic-ish wall time>, "phase":
   "tool"|"model"}`.
   - Hook writes on every `agent:step` (pre- and post-batch).
   - Bridge writes `phase:"model"` when it spawns the child and on each
     retry, so a turn that never reaches a tool still has a start marker.
2. **Watcher** in `_run_with_liveness_guard`: a daemon thread, 1 s poll, reads
   the file, and if `ts` is within `liveness_timeout` of now it calls
   `_touch()`. It must be **scoped to this child's session** (match on
   `session` in the marker) so a *different* bridge turn's hook firing can't
   keep this child alive.
3. **Reset** the marker at child spawn and on `_kill_process`, so a dead
   turn's stale marker can't leak into the next turn.
4. **Guard semantics unchanged** (`kill on no *touch* for N s`); the only
   change is that "touch" now also comes from the marker file, not just the
   streams.
5. **Config:** `HERMES_TURN_ALIVE_FILE` (path), reuse
   `HERMES_LIVENESS_TIMEOUT` (N). Add a debug log on each marker-driven touch
   so it's observable in journald.
6. **Failure mode:** if the marker file is absent (hook disabled), the
   watcher does nothing and behavior falls back to today's (bytes-only)
   guard. No regression.

### Test plan (Option A)
- Unit: a child that sleeps and never writes the marker is killed at N; a
  child that rewrites the marker every second is **not** killed (use a short
  `HERMES_LIVENESS_TIMEOUT` + a fake child script).
- Unit: a marker for a *different* session does not keep this child alive.
- Integration: 200 s turn on the local model (marker refreshed by hook)
  completes instead of dying at 600 s; a wedged child (no marker) still dies
  at N.

---

## Part 2 — Bridge-message labeling

### Problem
The mesh peer can't tell an **agent** reply from a **human** message, and the
operator can't tell which replies were auto-generated. Every message on the
delivery destination is just text.

### Requirement
Label every **bridge-originated** outbound message (agent final replies,
`🔧` tool pushes, step-through full steps, hold/recap lines) with a compact,
parseable marker, so:
- a mesh client can visually distinguish agent vs human,
- the operator can audit "this was the agent",
- the marker is stable (not a random string) and cheap on LoRa bandwidth.

### Design
Label at the **dispatch seam** in `LXMFBridge` (`core/bridge.py`), because
the bridge is the *only* sender on this identity — anything leaving this
destination is, by construction, agent/bridge output. A human's message
arrives via the *inbound* callback and is never re-sent through the outbound
seam, so it is never labeled.

**Marker format** (prefix, single line, ≤ ~14 chars):

```
⚙︎ agent:<short> :: <text>
```

where `<short>` is a 3–4 char kind tag:
- `agent:rep` — normal final reply
- `agent:tool` — `🔧` tool event / recap push
- `agent:step` — step-through full step chunk
- `agent:hold` — hold/gate notice
- `agent:sys` — system/control notice (errors, access-denied, etc.)

The `::` separator is the parse point for clients. If a client doesn't
understand the marker it still reads as plain text (`⚙︎ agent:rep :: hi`) —
graceful degradation.

**Implementation points:**
- Add a single helper `LXMFBridge._label(text, kind)` that prepends the
  marker (idempotent: don't double-label if already prefixed) and route every
  outbound send through it (`send_reply`, `push_reply`, and the
  `_on_tool_step`/`_on_full_step` push callbacks in `cli.py`).
- Keep the marker **outside** the `split_message` chunking math: label once on
  the first chunk, and *number* continuation chunks `[n/N]` as today so a
  client can reassemble. (Label the first chunk; continuation chunks carry
  the chunk index only.)
- Do **not** put the marker in the LLM prompt or in the stored reply text —
  only in the bytes actually sent over LXMF. The DB/stored reply stays clean.

### Spec
1. `bridge.py`: add `KIND_TAGS = {"reply":"rep","tool":"tool","step":"step",
   "hold":"hold","sys":"sys"}` and `def _label(self, text, kind="reply") ->
   str` (idempotent, chunk-aware).
2. Route outbound sends through `_label`:
   - final reply → `kind="reply"`
   - `🔧` tool push / recap → `"tool"`
   - step-through chunk → `"step"`
   - hold/gate, errors, access-denied → `"sys"` / `"hold"`
3. `cli.py`: `_on_tool_step` / `_on_full_step` pass the right `kind` through
   to the bridge push.
4. **Inbound** (`_on_lxmf_message`) must **strip** a leading `⚙︎ agent:… :: `
   marker before handing text to the LLM, so an echo (user forwarding the
   agent's own message, or a relay) can't leak the marker into the prompt.
5. Config: `HERMES_MESH_LABEL` (default on; set `0` to disable for a client
   that can't handle the prefix).

### Test plan
- Unit: `send_reply` output begins with the expected marker and is not
  double-labeled on a second call.
- Unit: `_label` is idempotent and chunk-aware (only first chunk labeled;
  chunks numbered).
- Unit: inbound strip removes the marker and passes clean text to the handler.
- Integration: peer receives an agent reply, sees `⚙︎ agent:rep :: …`; a
  human reply is absent of the marker.

---

## Sequencing & rollout
1. **Now (mitigate the hiccup):** hook non-blocking bounded POST + control-
   server bounded/off-thread LXMF relay (Part 0).
2. **Next (stop the double-wait):** the already-committed conditional retry
   (Part 1 "Already done").
3. **Then (real liveness fix):** Option A heartbeat (Part 1).
4. **Then (operator UX):** message labeling (Part 2).

Each step is independent and individually testable; none blocks the next
except that 1 should land before 3 (so the hook no longer wedges the loop
while the heartbeat work is in flight).
