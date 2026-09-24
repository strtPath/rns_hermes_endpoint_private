# Findings — clarify branch, tool calls not appearing on the mesh (2026-09-23)

**Status:** root cause CONFIRMED and FIXED. Regression test included (red on the
old code, green on the fix). Full suite green except one unrelated pre-existing
failure.

## Reported symptom

On the `clarify` branch, tool calls made by the agent do not appear on the
mesh. The final reply arrives, but the per-tool progress messages that
step-through mode used to deliver are missing.

## Root cause

`chat()` started the step watcher with a possibly-null session id:

```python
watcher_sid = self._resume_id or self._resolve_session_id()
threading.Thread(target=self._run_step_watcher,
                 args=(watcher_sid, watcher_stop), ...).start()
```

On the **first turn of a brand-new session** there is no `_resume_id`, and
`_resolve_session_id()` returns `None` because the session does not exist yet.
The child creates it; this Hermes build has no `--create-if-missing`, so
`chat()` cannot pre-create it either. The watcher then ran its queries with
`session_id = NULL`, which matches no row. Every tool call of that turn was
dropped while the log showed the tell-tale:

```
step-mode ON, push set, sid=None resume_id=None
step-watcher start sid=None
step-watcher stop sid=None pushed=0
```

The session only became resolvable after the turn adopted and titled it, so
later turns on the same session watch fine. That is why the bug reads as
intermittent rather than "always broken": the first message of every fresh
session loses its tool steps, and everything after it looks healthy.

### Live evidence

`~/.hermes/logs/mesh-bridge-step.log`, 2026-09-23:

- **06:45:39** turn: `sid=None`, `pushed=0`. Session
  `20260923_064541_22ce28` really ran two tools (`terminal` at 06:46:56,
  `web_search` at 06:47:07) per `state.db`; neither was pushed.
- **09:32:38** turn: `sid=20260923_064541_22ce28` (now adopted, so the id
  resolved) — the watcher watched properly. That turn happened to make no tool
  calls, so `pushed=0` there is correct, not a failure.

Every `sid=None` watcher start in the log pairs with `pushed=0`; runs with a
resolved sid push normally (the 2026-08-29 entries show `pushed=13`, `pushed=6`,
`pushed=47`).

## Fix

Two changes in `src/hermes_reticulum/core/hermes_client.py`:

1. **Lazy sid resolution in the watcher** (`_run_step_watcher`). `sid` is now
   `str | None`; when it is None the watcher polls `_resolve_session_id()` each
   second until the adopted session appears, then streams it normally. This
   keeps the watcher live during the turn (so steps stream as they happen)
   while tolerating a session that does not exist yet. Bounded by the turn's
   own lifetime.

2. **Time-window row selection instead of an id seed.** Rows are selected by
   `timestamp >= turn_start - 1s` and deduped by pushed row id. The old
   `last_pushed = MAX(id)` seed was racy in the other direction too: the
   watcher starts *before* the child is spawned, and the user prompt row is
   written asynchronously, so a seed snapshot could land on the prior turn's
   last row and the first poll could then skip the batch. The time window is
   immune to when the snapshot is taken. Where the `messages` table has no
   `timestamp` column the watcher falls back to the old id-seed rule (detected
   once via `_messages_has_column`) rather than silently matching nothing.

Moving dedup from an advancing watermark to a set of pushed row ids also fixes
a latent ordering issue: if a tool *result* row appears after the assistant row
that issued it was first seen, the old watermark dropped the result from the
pushed body. With the id set the result map can refresh without re-pushing.

## Tests

`tests/test_hermes_client.py`:

- `TestStepWatcherFirstTurnSid.test_first_turn_streams_after_sid_resolves` —
  starts the watcher with `sid=None`, then creates and titles the session and
  writes a tool call. **Fails on the old code**
  (`AssertionError: 0 != 1 : first-turn tool was not streamed: []`) and passes
  on the fix. Direct regression test for the reported bug.
- `TestStepWatcherRowSelection` — three tests covering late-written rows,
  no-prior-turn replay (the 2026-08-29 recap guarantee), and push-once dedup
  across polls.

## Verification

- `./venv/bin/python -m unittest tests.test_hermes_client` → 37 tests, OK.
- `./venv/bin/python -m unittest discover -s tests` → 145 tests, 1 failure.
  That failure is `test_tool_emoji.TestParityWithInstalledGateway
  .test_table_has_no_undeclared_extras` (`setup_mcp` not registered by the
  installed gateway) and **also fails on pristine HEAD** — unrelated to this
  change; the installed gateway's tool registry drifted.
- **Live path exercised** (the new code runs wherever the edited `src/` is
  imported, and the tests drive the real watcher). The step log shows the fix
  resolving a null sid and pushing:

  ```
  11:59:05 step-watcher start sid=None since=1790179145.723
  11:59:06 step-watcher resolved sid=sess-first (was None)
  11:59:06   push_step(terminal) → 30 chars
  11:59:06 step-watcher stop sid=sess-first pushed=1
  ```

  The `no timestamp column, using id seed` fallback also fired for fixtures
  without the column, which is why the pre-existing watcher marker tests pass.

## Full end-to-end check on the mesh (still to do)

The unit and log evidence above prove the watcher now resolves and pushes. To
confirm on the wire:

1. Restart the bridge so it re-imports `src/`.
2. From the mesh, send a message that starts a **fresh** session and uses a
   tool.
3. Expect `step-watcher start sid=None`, then
   `step-watcher resolved sid=... (was None)`, then `push_step(<tool>)` lines,
   and `pushed=N` matching the turn's tool count — and the tool message
   arriving on the mesh client.

## PII note

Session ids here are local SQLite keys, not RNS identities. Do not add mesh peer
identity hashes — the repo is public and the findings-doc convention forbids
full or truncated identity values.
