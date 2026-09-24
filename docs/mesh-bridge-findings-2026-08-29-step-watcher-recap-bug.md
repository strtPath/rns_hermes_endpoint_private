# Step-watcher recap bug — 2026-08-29 (replay of prior turns' tool steps)

**Symptom.** With `/steps on`, every new mesh turn re-sends the tool calls from
**previous** turns (the whole session's tool history) before the current turn's
first step arrives. User saw the entire prior session's tool calls replayed as
separate `💻` messages at the start of each turn.

**Root cause** (`hermes_client.py::_run_step_watcher`, shipped in `c087da2`):

```python
last_pushed = 0
...
last_id = SELECT MAX(id) FROM messages WHERE session_id = ?
...
rows = SELECT ... WHERE session_id = ? AND id > last_pushed
```

`last_pushed` starts at **0**, but the watcher is started fresh for every
`chat()` call. `state.db` `messages` rows are **session-global** — the whole
conversation history lives under one `session_id`. So on the 2nd+ turn of a
session, `MAX(id)` is far ahead of 0, the tail query returns *every* assistant
row with `tool_calls` in the session, and the watcher pushes the entire prior
tool history again.

**Why it wasn't caught:** the verification checklist only tested the first turn
(fresh session → no prior rows → looked correct).

**Fix (2026-08-29):** seed `last_pushed` at watcher start with the
**MAX message id already in the session** (i.e. "everything that existed before
this turn"). New rows only appear after the user message is persisted, so
seed-at-start is safe and simple:

```python
def _run_step_watcher(self, sid, stop_evt):
    db_path = ...
    # Seed with existing rows so we only push THIS turn's tool I/O.
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT MAX(id) FROM messages WHERE session_id = ?", (sid,)
        ).fetchone()
        last_pushed = row[0] if row and row[0] else 0
        conn.close()
    except (sqlite3.Error, OSError):
        last_pushed = 0
    ...
```

The `while` loop's own `MAX(id)` check then only ever sees rows created
*after* the watcher started (the current turn's).

**Edge cases considered:**
- Turn 1 of a brand-new session: MAX is 0 or the user prompt row — either way
  the seed is correct (user prompt has no `tool_calls`).
- Compaction / session re-write: if the DB is rebuilt with new ids below the
  seed, we'd skip them. Not a regression (today's behavior pushes them all,
  which is the bug). Acceptable.
- Concurrent turns on the same session: `_turn_lock` serializes per
  HermesClient, so no overlap.

**Verify:** with `/steps on`, send a 2nd+ message that uses a tool in an
existing session → only the *current* turn's `💻` messages arrive; `pushed=N`
in `mesh-bridge-step.log` matches the current turn's tool count.
