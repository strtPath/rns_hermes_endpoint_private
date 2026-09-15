# Findings — tool recap shows prior turns' tools, not the current turn's (2026-09-15)

**Symptom.** On the Reticulum mesh, a turn that used no tools at all was
answered with a footer listing tool calls from an earlier turn of the same
session. Observed 2026-09-15 ~10:52 in session 20260915_094700_3817d3
(thread "mesh-reticulum"): the user's message "nope that was because i
forgot to load the model on that pc" produced a reply with no tool calls of
its own, yet the reply carried a tool-recap footer naming tools that had
run ~8 minutes earlier in a different turn.

The operator reported: "the tools that you did previously are showing up in
the recap in this message you sent just now as well."

## Reproduction (exact, from live state.db)

Session 20260915_094700_3817d3, message ids from `~/.hermes/state.db`:

- 713–714: turn 1 "/good morning" — no tools.
- 715: turn 2 "test reticulum message how do you hear me?" (user message).
- 716: user message for the SAME turn stored again (duplicate; see note
  below — a /retry re-sends the original prompt text).
- 717: assistant row, content empty, **no tool_calls column** in the row as
  persisted by this turn's model run? No — see below: the model's
  tool-calling rows for this turn are 719, 724, 730, ...
- Tool rows for turn 2: 718 (search_files), 720 (search_files), 721
  (read_file), 723 (terminal), 725 (terminal).
- 726: turn 2 final reply ("I hear you fine — ...").
- 727: user message for turn 3 ("nope that was because i forgot to load
  the model...").
- 728: turn 3 final reply — no tool rows exist between 727 and 728.

Step mode was OFF for every turn (bridge step log:
`step watcher NOT started (step_mode=False, push=True)`), so
`_with_tool_recap()` ran on turn 3. It called `tool_recap(limit=8)`, which
returns the last 8 tool calls in the session **regardless of which turn
produced them**. Result: turn 3's (tool-free) reply was footed with
`🔧 search_files, search_files, read_file, terminal, terminal` — tools from
turn 2.

That matches the operator's report exactly.

## Root cause

`HermesClient.tool_recap()` in `src/hermes_reticulum/core/hermes_client.py`
(line ~520):

```python
rows = conn.execute(
    "SELECT content, tool_calls FROM messages "
    "WHERE session_id = ? AND role = 'assistant' "
    "ORDER BY id ASC",
    (sid,),
).fetchall()
...
return out[-limit:]
```

The query is session-scoped only. There is no bound to "rows created during
this turn". The footer is then unconditionally appended in
`_with_tool_recap()` (line ~1129), which only suppresses the recap in step
mode (line ~1140) — with step mode off, ANY non-empty session history
becomes the footer, even when the current turn ran zero tools.

Consequences:

1. **Stale tool attribution.** A tool-free turn is footed with previous
   turns' tools. The footer implies "these tools just ran", which is false.
2. **Cross-turn bleed on every multi-turn session.** Once a session has any
   tool history, every later reply carries a footer derived from that
   history, not from the reply's own turn.
3. **The failure is invisible in the common case.** With `/steps` ON the
   operator gets per-tool 💻 pushes from the CLI-side watcher
   (`_run_step_watcher`), which correctly seeds at `MAX(id)` at watcher
   start (see the comment at line ~401 referencing the 2026-08-29 "recap
   bug"), so live tool delivery is correct — the *footer* is the only
   misbehaving surface, and with step mode on it is suppressed anyway.
   With step mode OFF (the default), the footer is the only tool
   visibility, and it is wrong.

## Why the watcher doesn't have the same bug

`_run_step_watcher` (line ~390) seeds `last_pushed` at the session's
`MAX(id)` before the child spawns, so it only ever pushes rows created
during the current turn. `tool_recap()` has no equivalent anchor — it
re-reads the whole session on every reply.

## Suggested fix

Scope the recap to the current turn. Options, in order of preference:

1. **Anchor on the user message row.** `chat()` knows the turn's prompt;
   before spawning, record the session's current `MAX(id)` (like the
   watcher does). After the child returns, query:

   ```sql
   SELECT tool_calls FROM messages
   WHERE session_id = ? AND role = 'assistant' AND id > ?
   ```

   and build the recap only from that tail. Zero-tool turn → empty recap →
   no footer. No schema change, same DB, same read-only connection pattern.
   The anchor must be taken BEFORE `_run_with_liveness_guard` spawns the
   child (the user row is persisted by the child; the pre-spawn MAX(id) is
   the last row of previous turns, which is exactly the boundary we want).
   Note the duplicate user row observed at id 716 (a /retry re-sends the
   prompt): the anchor is still correct, because it is taken before the
   child runs, so all rows of the retried turn are above it.

2. **Fallback anchor: `started_at`/turn timestamp.** If row ids are not
   trusted, compare a per-turn start timestamp against a message-timestamp
   column — but state.db `messages` has no per-row timestamp in this
   schema (v0.19.0), so option 1 is the robust one.

3. **Minimum viable:** suppress the footer entirely when the turn ran no
   tools. Cheap, but it still shows the wrong footer when a turn ran tools
   AND the session has older tool history (the footer would then show the
   *last* 8 tools, mixing this turn's with older ones). Not recommended as
   the final fix; fine as a stopgap.

Also worth a line in the docs: `tool_recap()`'s docstring says "Recap of
the last tool calls in the current mesh session" — "current session" is
the trap; it should say "current turn".

## Verification plan

- Unit test: seed a temp state.db with two turns (turn A: 2 tool calls;
  turn B: no tools). Call the fixed `tool_recap` with the turn-B anchor →
  expect `[]`; with the turn-A anchor → expect the 2 tools.
- Manual: on the mesh, with step mode OFF, send a tool-free message after a
  tool-heavy turn; confirm no `🔧` footer. Then send a tool-heavy message;
  confirm the footer lists only that turn's tools.

## Related / adjacent

- 2026-08-29 "step watcher recap bug" (seed-at-MAX-id fix) — same class of
  bug (unbounded session-history replay), already fixed for the live
  watcher path. This is the sibling bug in the recap-fallback path.
- Downlink ack findings (2026-09-12) are unrelated to this; footer size is
  small enough that chunking is not a factor here.

## Environment

- Hermes v0.19.0 (pipx via mise), bridge running from repo checkout on the
  main profile, announce name "gorycensus".
- state.db: ~/.hermes/state.db (messages: id, role, tool_name, tool_calls,
  tool_call_id, content; sessions: id, title, started_at, source, cwd).
- No per-row timestamp on `messages` in this schema.
