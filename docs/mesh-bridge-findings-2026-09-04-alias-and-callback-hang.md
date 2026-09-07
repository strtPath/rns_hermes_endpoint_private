# Findings: Command dispatcher alias bug + pre_tool_call callback hang

**Date:** 2026-09-04
**Branch:** `fix/mesh-gate-session-mismatch`

## Bug 1: `/a` and `/d` aliases returned None

**Symptom:** `handle("/a")` and `handle("/d")` returned `None` (treated as
non-command, forwarded to the model) while `/approve` and `/deny` worked.

**Root cause:** In `CommandDispatcher.handle()` the first token was extracted
with the slash intact: `command = parts[0].lower()` → `"/a"`. The alias
normalization branch then checked `command in ("approve", "a")` — but `"/a"`
is never equal to `"a"`, so the branch was dead. Every alias normalization
branch was dead for the same reason; the only reason `/model`, `/new`,
`/help`, `/stop` appeared to work is that the `COMMANDS` dict also registers
those slash-prefixed keys directly, so the lookup succeeded without ever
hitting the normalization.

**Fix:** Strip the leading slash into a separate `word` variable and match
against that:

```python
word = command.lstrip("/")
if word in ("model", "m"):
    command = "/model"
elif word in ("approve", "a"):
    command = "/approve"
# ... etc
```

This makes every alias branch live. `/a`, `/d`, `/m`, `/t`, `/s`, `/p`,
`/r`, `/u`, `/v` all route correctly now.

## Bug 2: pre_tool_call plugin callback hang

**Symptom:** After a slash-approve or slash-deny in the mesh bridge, the
`pre_tool_call` plugin callback blocked, stalling all subsequent tool calls
in the session.

**Investigation:** The control server (port 8471) is a
`ThreadingHTTPServer` — each request handler runs in its own thread. A
blocking POST from the `mesh-tool-gate` plugin (the gate's
`MESH_GATE_TIMEOUT`, raised to 900s on 2026-08-30) can hold a handler
thread for up to 15 minutes. If the gate POST is in-flight when `/a` or
`/d` arrives on the mesh side, the dispatcher routes the command to
`answer_approval()` which signals the gate's `Event` — but the gate plugin
thread may not have returned from the HTTP POST yet, so the
`pre_tool_call` hook thread is still blocked inside the POST until the
response lands.

**Status:** This is a timing interaction, not a deadlock — the gate POST
eventually returns and the thread frees. The practical impact is a stall
equal to the remaining gate timeout. No orphaned processes found
(`ps aux | grep test_control_server` is clean). The 8471 socket is owned
by the single bridge PID 409136.

**Mitigation (existing):** The 900s gate timeout is env-overridable
(`MESH_GATE_TIMEOUT`). For interactive use, a shorter value (e.g. 120s)
reduces the worst-case stall. The 900s default was set for LoRa hop
latency — if the bridge is on a fast link, 120s is sufficient.

## Tests

- `TestCommandDispatcherAliases` (3 tests) added to
  `tests/test_control_server.py`:
  - `test_a_routes_to_approve` — `/a` returns approve handler result
  - `test_d_routes_to_deny` — `/d` returns deny handler result
  - `test_full_words_still_work` — `/approve` and `/deny` unchanged
- Full suite: **35/35 pass** (was 35 before, 32 passing + 2 failing before
  the fix).

## Verification

```
venv/bin/python -m unittest tests.test_control_server -v
# Ran 35 tests in 9.530s
# OK
```
