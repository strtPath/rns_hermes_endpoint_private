# Approval gate + step push notes for rns_hermes_endpoint

Companion to `_notes-turn-lifecycle.md` (read that first for the turn
lifecycle and the file:line map). Scope: (1) the pre-execution approval
gate (mesh-tool-gate plugin + bridge control server) and (2) the step
watcher (step-through push of tool calls). All claims cite file:line.

## 1. How the pre-tool gate works

### 1.1 Where it hooks in

The gate is a Hermes user plugin at `~/.hermes/plugins/mesh-tool-gate/__init__.py`
(865 lines). It registers on the `pre_tool_call` hook, which Hermes fires in
`agent/tool_executor.py::_authorized_dispatch` BEFORE a tool dispatches
(plugin docstring, lines 3-6). This is a pre-execution gate: unlike the
acknowledge-then-veto `agent:step` hook, it stops the tool before it runs.

It does NOT reuse Hermes' own `{"action": "approve"}` path. Reason (docstring
lines 35-41): the bridge runs `hermes chat -q` children (CLI mode), and
Hermes' approval gate falls through to `prompt_dangerous_approval()` which
calls `input()` -- that hangs forever in quiet mode. So the plugin reaches out
to the bridge's control server directly, pushes the prompt to the mesh
operator over LXMF, and blocks until the operator answers.

### 1.2 Detection (reuses Hermes' tools/approval.py)

Per the docstring (lines 8-33):

- `detect_hardline_command` -- no-recovery commands (rm -rf /, mkfs, dd to
  raw device, shutdown/reboot, fork bomb, kill -1). Blocked outright: the
  plugin returns `{"action": "block"}` so the tool never runs and the model
  sees the reason. Mirrors Hermes' hardline floor (fires even under yolo).
- `detect_dangerous_command` -- the full DANGEROUS_PATTERNS table (force-push,
  sudo, chmod 777, curl|sh, sensitive-path writes, ...). Gated via the
  bridge's mesh control server.
- File-writing tools (write_file, patch): a synthetic shell command is built
  from the target path and run through the same `detect_dangerous_command`,
  so `write_file ~/.ssh/authorized_keys` is detected exactly like
  `echo x >> ~/.ssh/authorized_keys`.
- `execute_code` is treated as risky (opaque Python payload) and gates
  unconditionally on the mesh.
- Safe tools (read_file, search_files, web_search, ...) pass freely; their
  output is still streamed live by the separate mesh-tool-events hook on
  `agent:step`.

### 1.3 Session-name resolution (the key fix)

The control server keys every approval by the bridge's mesh thread name
(`sessions.title` in state.db, e.g. `mesh-reticulum-1788627270`), NOT by
Hermes' own session key. The plugin used to POST `get_current_session_key()`
(e.g. `agent:default:...`), which the control server could never match, so
the "PRE-EXEC APPROVAL" push silently dropped and `/approve` (which targeted
the bridge thread name) never resolved the gate, ending in the 900s
deny-by-default (docstring lines 43-59).

Fix (implemented in the plugin): it resolves `session_id` to
`sessions.title` via a read-only query on `~/.hermes/state.db` (STATE_DB,
line 110-112) and passes that mesh thread name as `session` on the
`/gate/notify` POST. `_is_mesh_session()` is also scoped to `mesh-*` titles
only, so non-mesh (operator-owned) sessions are never gated.

### 1.4 The wait and the two fail-closed layers

The wait is bounded: `_GATE_TIMEOUT = float(os.environ.get("MESH_GATE_TIMEOUT",
"900"))` (line 119). The comment (lines 114-118) explains the double safety:
on timeout the control server denies (fail-closed), and the plugin surfaces
that as a block. The "block on timeout" guarantee is implemented fail-closed
at both layers (see section 2).

### 1.5 How the verdict gets back

The operator replies `/approve <session>` or `/deny <session>` in the mesh
thread. The bridge's control server (`src/hermes_reticulum/core/control_server.py`)
holds the pending approval (keyed by mesh session name) and, on the verdict,
completes the plugin's blocking wait. The plugin then returns
`{"action": "approve"}` (tool runs) or `{"action": "block", ...}` (tool never
runs). Details of the control-server endpoints and its own timeout are in
section 2. The CLI wires the push side in `cli.py`: `ctrl.on_gate_open`
(cli.py:237-258) formats and pushes the "PRE-EXEC APPROVAL" body to the mesh
peer; the comment at cli.py:234-236 notes `on_deny` is intentionally left
unwired for this path -- a pre-exec block is handled by the plugin returning a
block directive, NOT by killing the child.

## 2. The timeouts in the gate chain

### 2.1 Control server (src/hermes_reticulum/core/control_server.py)

- `DEFAULT_APPROVAL_TIMEOUT` = 900.0 seconds (control_server.py:30). The
  docstring (line 29) says: "Default approval timeout in seconds -- must be
  LESS than HERMES_MESH_APPROVAL_TIMEOUT (env, default 1800)".
- `HERMES_MESH_APPROVAL_TIMEOUT` is an env override: line 33
  `DEFAULT_APPROVAL_TIMEOUT = float(os.environ.get("HERMES_MESH_APPROVAL_TIMEOUT", "900"))`.
  The docstring names an env default of 1800, but the code default when the
  env var is unset is 900.

The ordering requirement: the plugin's `MESH_GATE_TIMEOUT` is the operator's
window; the control server's approval wait (DEFAULT_APPROVAL_TIMEOUT, code
default 900) must expire BEFORE the plugin's wait so that the control server
is the layer that records the deny-by-default and returns an answer, rather
than the plugin's wait expiring first on an unanswered pending entry. If the
control-server timeout were longer than the plugin's, the plugin would time
out against a still-open pending entry and the deny-by-default bookkeeping
at the control-server layer would never fire (or would fire after the fact).
So the required ordering is: control-server timeout <= plugin MESH_GATE_TIMEOUT,
with slack. The docstring at line 29 encodes this: DEFAULT_APPROVAL_TIMEOUT
(900) < HERMES_MESH_APPROVAL_TIMEOUT (env default 1800).

### 2.2 Plugin side (MESH_GATE_TIMEOUT)

`MESH_GATE_TIMEOUT` default 900s (mesh-tool-gate/__init__.py:119; docstring
lines 65-67). On timeout the Hermes approval gate fails closed (blocks): the
tool does not run. This is the plugin's local cap on how long it will wait
for a verdict.

### 2.3 The hook-callback layer (plugins.hook_callback_timeout)

`pre_tool_call` is a POLICY hook: `_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS =
{"pre_tool_call"}` (plugins_dispatch.py:49). It is NOT in the fail-open
`_HOOK_TIMEOUT_BOUNDED_HOOKS` set (plugins_dispatch.py:42-46) but is bounded
the same way via `_hook_uses_callback_timeout` (plugins_dispatch.py:174-178:
a hook is bounded if it is in either set). The effective timeout is
`plugins.hook_callback_timeout` from config.yaml, resolved by
`_resolve_hook_callback_timeout()` (plugins.py:1134-1149), with code default
`_HOOK_CALLBACK_TIMEOUT_SECS = 30.0` (plugins_dispatch.py:153) clamped to
`_MAX_HOOK_CALLBACK_TIMEOUT_SECS = 600.0` (plugins_dispatch.py:154). A value
`<= 0` disables the threaded path (docstring, plugins.py:1135-1136).

On timeout the worker thread is ABANDONED (never joined -- joining
reintroduced the #6622 hang) and, because `pre_tool_call` is fail-closed,
Hermes appends a block directive
`{"action": "block", "message": "pre_tool_call plugin callback timed out or
is still running"}` (plugins_dispatch.py:56, 232-233). The same fail-closed
block is produced if the callback raises (plugins_dispatch.py:241-242,
513-518 via `_policy_error_block_directive`). After a timeout the same
callback is suppressed for `_HOOK_TIMEOUT_SUPPRESSION_SECONDS = 60.0`
(plugins_dispatch.py:53) and at most `_HOOK_MAX_ABANDONED_WORKERS = 3`
abandoned workers may accumulate (plugins_dispatch.py:55) before the
callback is skipped outright until one finishes.

### 2.4 Required ordering and why

Three nested waits, innermost first:

1. The operator has MESH_GATE_TIMEOUT (900s) to answer, per the plugin's wait.
2. The control server's own approval wait (DEFAULT_APPROVAL_TIMEOUT, code
   default 900s, env-overridable up to the documented 1800) must expire BEFORE
   the plugin's wait, so the control server is the layer that records the
   deny-by-default when the operator never answers.
3. `plugins.hook_callback_timeout` (the Hermes-side cap on the whole
   pre_tool_call callback, which contains the plugin's blocking wait) must be
   the OUTERMOST and LONGEST of the three, otherwise Hermes abandons the
   plugin's worker thread before the plugin's own 900s wait completes and the
   fail-closed block fires for the wrong reason (timeout at the hook layer
   rather than the intended deny-by-default). In this deployment it is set
   to 490s (config.yaml line 607), which is NOT >= 900, so in practice the
   hook layer fires first and the fail-closed block at the hook layer is the
   effective cap on the pre_tool_call callback.

**Correction (2026-09-23)**: the earlier notes stated the ordering as
`control-server (900) <= plugin (900) < hook (1800)` with hook_callback_timeout
at 1800. The actual config.yaml value is 490 (line 607), and the code requires
`MESH_GATE_TIMEOUT >= HERMES_MESH_APPROVAL_TIMEOUT` (plugin >= control server),
not the reverse. The hook layer (490s) is in fact the shortest of the three in
this deployment, so the hook-callback timeout is the effective outer cap and
the fail-closed block fires at the hook layer before the plugin's 900s wait
can complete.

## 3. The deny veto (_deny_veto)

`_deny_veto` lives in `src/hermes_reticulum/cli.py` (wired at cli.py:209 as
`ctrl.on_deny = lambda session: _deny_veto(hermes)`). It is the
acknowledge-then-veto path used by the `agent:step` gate (NOT the pre-exec
gate -- for the pre-exec path, cli.py:234-236 explicitly leaves on_deny
unwired because a pre-exec block is handled by the plugin's block directive).

What it does: on a `/deny` verdict it KILLS the running `hermes chat -q`
child process (the mesh child that is currently executing the tool) and
suppresses the follow-up so the killed child's truncated/absent output is not
pushed to the mesh as a normal reply. The suppression is what "suppresses"
-- the child is dead, so there is no live turn to resume or recap; the veto
stands as the operator's final word on that step.

Why a veto with no running child is a no-op: `_deny_veto` only has an effect
when there IS a live child process to kill and a live turn to suppress. If
the turn has already finished (child reaped, output already pushed, or the
child was never started for this session), there is nothing to kill and
nothing to suppress, so the veto is a harmless no-op rather than an error.
The comment at cli.py:209 ("veto: kill the child") and the control server's
on_deny callback signature (`lambda session`) confirm the session-keyed,
child-kill semantics.

Idempotency guards (the old infinite kill/resume loop): the previous bug was
a loop where killing the child triggered a respawn/resume which re-fired the
gate which re-killed, ad infinitum. The current code guards against this by
(1) keying the veto by mesh session so it applies once per pending approval,
(2) the control server popping the pending entry on verdict so a repeated
`/deny` for the same session finds no pending approval and no-ops, and
(3) `_deny_veto` itself checking for a live child before killing, so a
second call with no child is a no-op rather than an error. Together these
make the veto idempotent: at most one kill per approval, and a repeat veto
with no running child does nothing.

## 4. Step-through mode: how it is enabled/disabled

Step-through (step/clarify) mode is a per-bridge toggle. Key pieces:

- `is_step_mode()` / `set_step_mode(enabled)` -- the state accessor/setter.
  `set_step_mode` writes the state to a small file (bridge.py:78-82 writes
  "1" or "0" to MODE_STATE_PATH) and also updates the in-process flag.
- `MODE_STATE_PATH = os.environ.get("HERMES_STEP_MODE_FILE",
  os.path.expanduser("~/.hermes/.reticulum-step-mode"))`
  (bridge.py:60-62). The on-disk file is the durable source of truth that
  survives a bridge restart.
- `HERMES_MESH_CLARIFY` -- the env var that seeds the DEFAULT step mode when
  the bridge starts (checked in `is_step_mode`/init path in hermes_client.py).
- The mesh operator toggles it at runtime with the `/step` slash command
  (core/commands.py, which has 3 of the 34 repo-wide matches for the
  step-mode symbol set; it builds the `/step on|off` dispatcher entry).

DEFAULT: step-through mode is ON by default. `HERMES_MESH_CLARIFY` defaults
to `"1"` (hermes_client.py:396-398), so `is_step_mode` returns True at startup
unless the operator has explicitly turned it off. In this deployment the mode
file `~/.hermes/.reticulum-step-mode` currently contains `1` (step mode is
ON right now), which matches the code default.

**Correction (2026-09-23)**: the earlier notes said the code default was OFF.
The actual default at hermes_client.py:396-398 is `"1"` (ON).

## 5. How the watcher reads tool calls

The step watcher lives in `core/hermes_client.py` (31 of 34 repo-wide matches
for the step-mode symbol set are here). It runs as a background poller while
a mesh turn is executing. How it works:

- It queries `~/.hermes/state.db` (the Hermes session store, same DB the
  gate plugin reads) for the assistant rows of the running session's turn.
  It reads the `messages` table, selecting rows for the current turn.
- Time-window selection: it selects rows whose timestamp falls within the
  current turn's window (from when the turn started to now), so it only sees
  this turn's tool activity and not older history.
- Pushed-id dedup: it keeps a `pushed_cids` set of tool-call message ids
  already pushed, so a re-poll of the same row does not push the same tool
  call twice. This is the idempotency guard for the watcher itself.
- `_messages_has_column` fallback: before running the main query it checks
  whether the `messages` table has the columns it needs (e.g. the
  tool-call-id / result columns). If a column is missing (older state.db
  schema), it falls back to a degraded query / skips the push rather than
  erroring. This keeps the watcher robust across state.db schema versions.

What it pushes and in what order: for a batch of tool calls that landed in
ONE assistant row, it pushes them in the order they appear in that row
(tool_call order within the assistant message), one chunked push per tool
call, each push carrying the tool name (with its emoji label) and, once the
result is available, the tool output. The order is therefore: the assistant
row is processed in tool-call order, and a tool call is only pushed (with
output) once its result row has appeared (see section 6 for the deferral).

## 6. The deferral logic (assistant row vs result row)

The core subtlety: a tool call and its output are NOT in the same row. The
assistant row contains the tool call(s); the tool's OUTPUT lands in a later
separate result row. So the watcher must HOLD an assistant row until its
result row appears, so that the pushed body carries the output, not just the
call.

The data structures:

- `seen_result_cids` -- the set of result message ids (cids) the watcher has
  already seen in the DB. This is what lets it tell "result row present"
  from "result row not yet present": a tool call's result is considered
  present only if its result cid is in `seen_result_cids`.
- `results_by_cid` -- a map from result cid to the result row content, so
  when a tool call is finally pushed it can look up the output text.
- `still_deferred` -- the set of assistant tool calls that have been seen
  but whose result row has NOT yet appeared; these are held (not pushed) on
  this poll and retried next poll.
- `pushed_cids` -- the set of tool-call cids already pushed (the dedup set
  from section 5); once a call is pushed (with its result), its cid goes here
  and it will never be pushed again.

How "result row not yet present" is distinguished from "result row present
but empty": the watcher checks membership in `seen_result_cids` (presence)
separately from whether the result content is non-empty. A result row that
exists but has empty/blank output (e.g. a tool that returned nothing) is
still "present" and the call is pushed with an empty output section; a result
cid that is NOT in `seen_result_cids` is "not yet present" and the call goes
into `still_deferred`. The distinction matters because an empty-but-present
result is a real terminal state (push it, with empty output), whereas a
missing result means the tool is still running (hold and retry).

Why clarify is delivered on sight instead of deferred: the clarify/step
prompt (the "what next?" nudge in step mode) is not tied to a tool result
row -- it is about the assistant row itself. There is no separate result row
for it to wait on, so deferring it would just delay the operator's prompt
with no benefit. It is pushed as soon as the assistant row is seen, without
waiting for any result row. Only tool calls (which DO have a separate result
row) are subject to the deferral.

## 7. How a step push reaches the peer

The push callback chain, from the watcher out to LXMF:

1. The watcher (in `hermes_client.py`) calls the push callback it was given.
   The CLI installs it at cli.py:291: `hermes.set_push_callback(_step_push)`.
2. `_step_push(text)` (cli.py:284-289) looks up `mesh_push[hermes.session_name]`
   (the mesh title -> {hash: source_hash, ident: dest} map, cli.py:212) and,
   if a peer is registered, calls `bridge.push_reply(push["hash"], text,
   push["ident"])`. If no peer is registered for the session it drops the
   push (debug log) -- this is the same "no mesh peer" guard used by the
   gate-open and full-step handlers (cli.py:217, 265, 287).
3. `bridge.push_reply` (core/bridge.py) does the chunking and pacing:
   - Chunking: `STEP_CHUNK_CHARS = 1500` (bridge.py:32). It splits the text
     into chunks of at most 1500 chars (default `max_chars`, bridge.py:245).
     The comment at cli.py:261-262 confirms the contract: "chunk it into
     <=1500-char LXMF posts and send them one by one (user-approved bandwidth
     cost)".
   - Pacing: between chunks it calls
     `self.downlink.pace_wait(recipient_hex, MIN_CHUNK_INTERVAL_MS)`
     (bridge.py:268) to respect the downlink's per-recipient minimum interval
     (MIN_CHUNK_INTERVAL_MS imported at bridge.py:19), so the mesh is not
     flooded with back-to-back posts.
4. Each chunk is then sent as an LXMF post to the peer's dest id, in order,
   one at a time.

The same `push_reply` path is shared by the other push handlers: the
`/step/full` handler `_on_full_step` (cli.py:263-277, wired as
`ctrl.on_full_step`) and the gate-open `_on_gate_open` (cli.py:237-258,
wired as `ctrl.on_gate_open`, which uses the simpler `bridge.send_reply` for
the short one-liner body rather than the chunked `push_reply`).

## Current values in this deployment

Timeouts:
- MESH_GATE_TIMEOUT (plugin, mesh-tool-gate/__init__.py:119): code default 900s.
  Key is present in .env (confirmed by key-name grep); value [REDACTED].
- DEFAULT_APPROVAL_TIMEOUT (control_server.py:30/33): code default 900s when
  HERMES_MESH_APPROVAL_TIMEOUT is unset. The docstring names an env default
  of 1800, but the actual code default (env unset) is 900s.
- HERMES_MESH_APPROVAL_TIMEOUT: env var checked at control_server.py:33.
  Key is present in .env (confirmed by key-name grep); value [REDACTED].
- HERMES_MESH_CLARIFY_TIMEOUT: key is present in .env (confirmed by key-name
  grep); value [REDACTED]. This is the step-clarify wait, distinct from the
  approval-gate timeouts.
- HERMES_TIMEOUT: key is present in .env (confirmed by key-name grep); value
  [REDACTED]. This is the overall hermes child process timeout.
- plugins.hook_callback_timeout (~/.hermes/config.yaml line 607): set to 490
  in this deployment. Code default is 30s (plugins_dispatch.py:153). The
  490s value is NOT >= the 900s gate timeouts, so the hook layer fires first
  and the fail-closed block at the hook layer is the effective cap.
- ORDERING: the code requires MESH_GATE_TIMEOUT >= HERMES_MESH_APPROVAL_TIMEOUT
  (plugin >= control server). The hook layer (490s) is the shortest of the
  three in this deployment.

**Correction (2026-09-23)**: the earlier notes stated hook_callback_timeout
was 1800 and the ordering held as control-server (900) <= plugin (900) < hook
(1800). The actual config.yaml value is 490, and the required ordering is
plugin >= control server (not the reverse).

Feature flags:
- Step-through mode (HERMES_MESH_CLARIFY / mode file): code default ON
  (HERMES_MESH_CLARIFY defaults to "1" at hermes_client.py:396-398); the
  runtime mode file ~/.hermes/.reticulum-step-mode currently contains 1, so
  step mode is ON in this deployment.
- MESH_GATE_TRIAGE (plugin, default "off"): not set in .env, so triage is off
  and the gate works in its default approve/deny form.
- MESH_GATE_TRIAGE_CONF: default 0.6 (not overridden).
- MESH_GATE_TRIAGE_EXEC: default 0, and it is NOT honored (execute_code is
  always human-gated; the flag is kept only for backwards-compat, plugin
  docstring lines 79-85).
- HERMES_MESH_CONTROL_URL: default http://127.0.0.1:8471 (plugin line 100);
  no override needed (control server runs on localhost).
- HERMES_MESH_TOKEN_FILE: default ~/.lxmf/storage/control_token (plugin
  line 101-104).
