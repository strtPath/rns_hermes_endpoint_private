# Finding: "❌ Error (code -9)" on fresh mesh session (2026-08-22)

**Status: fixed + extended (2026-08-29).** The 2026-08-22 liveness-guard fix
(HermesClient liveness guard → honest "⏱️ …" reply, `HERMES_LIVENESS_TIMEOUT`
env override, one-shot resume-retry) held. A second source of `code -9` was
found and fixed on 2026-08-29: an *explicit* `HermesClient.stop()` (manual
`/stop`, or a mesh operator denying a gated tool via `on_deny`) left
`_guard_killed` unset, so the exit path fell through to
`_error_reply(-9)` and printed the cryptic `❌ Error (code -9)`. Fix:
`stop()` now forces `_guard_killed = False` and the exit path checks
`_stop_requested and not _guard_killed` first, replying
`⏹️ Stopped before the turn finished.` instead. A denied tool therefore
reports as *stopped*, not as an error — and the deny itself is now logged
by the bridge (`mesh denied tool … — aborting in-flight child`,
`cli._deny_veto`) because the control server logs neither deny path.
Tests: `tests/test_hermes_client.py` (9 passing).

## Symptom
User created a new mesh session (`/new`) and sent a test message. The bridge
replied `❌ Error (code -9)` on two consecutive attempts (11:53 and 12:01 EDT).
A third attempt at 12:04 (after the user's own Telegram session had freed the
GPU) presumably succeeded — the mesh thread now has a full 2-turn history.

## Timeline (from journalctl --user -u hermes-reticulum + ~/.hermes/logs/agent.log)
- 11:49:50 — `/new` received; thread bumped to `mesh-reticulum-1787413790`.
- 11:50:48 — test message received; `chat()` spawns
  `hermes chat -q ... -c mesh-reticulum-1787413790 --create-if-missing`.
- 11:50:50 — child creates session `20260822_115050_766a63` in state.db
  (source='cli', 2 messages = user prompt + wrapper).
- 11:51:00 — child hits the API; **no `API call #1` completion line is ever
  logged for this session** — the request never returned.
- 11:53:55 — liveness guard (180s, no stdout/stderr) kills the child.
  `proc.returncode == -9` (SIGKILL by our own guard, not the kernel OOM).
  → user sees `❌ Error (code -9): <last stderr line>`.
- 11:58:01 — user resends; bridge resumes the now-pinned session (correct
  Tier 1.1 behavior), same stall, same kill at 12:01:08.
- 12:02:01 — user starts the Telegram session (this investigation) on the same
  host; that API call completes in 137.8s.
- 12:04 — third mesh attempt runs after the GPU is idle and succeeds.

## Root cause (two compounding factors)

### 1. The 180s liveness guard is a wall clock, not a stream-idle clock
`HermesClient` only refreshes the liveness clock when **stdout/stderr bytes
arrive** (`_read_stream` → `_touch()`). But in `-q` mode `hermes chat` emits
its startup banners ("Loaded environment variables", "Plugin ... registered",
"Session ... found", "↻ Resumed session ...") to **stderr**, and *that is the
last thing the child ever writes*. The actual model response is the only
stdout. So the guard is really measuring "time since the child started", not
"time since the model produced output".

On the local ROCm server (`qwen3.8-27b@iq3_xxs`) a single turn can take 100–950s:
- 2026-08-21 22:02: API call latency 634.4s
- 2026-08-21 22:55: 844.4s
- 2026-08-21 21:48: 943.7s
- 2026-08-22 12:06 (idle GPU): 137.8s — still over the 180s default if two
  sessions run concurrently

So any turn that exceeds `HERMES_LIVENESS_TIMEOUT` (180s default) is guaranteed
to be SIGKILL'd by the bridge itself and reported as `code -9`.

### 2. Concurrent agent sessions serialize on the same single-GPU model
On 2026-08-22 at 11:51, this host was simultaneously running:
- the mesh CLI child (this failed attempt),
- the hermes-gateway Telegram session,
- background housekeeping (mem_trim) in both.

Two `hermes chat` processes hitting the same local 27B model at 64k context
means the second one effectively waits for the first to finish its turn.
That's why the Aug 21 mesh test (15:13, ~4 min) succeeded but the Aug 22
attempt (11:51, while the gateway session was mid-turn) did not, and why the
third attempt at 12:04 — with the GPU finally idle — succeeded.

The stderr captured in the error log confirms the child had passed startup
and reached the model: "Session ... found but has no messages. Starting
fresh." / "↻ Resumed session ... (1 user message, 1 total messages)".

## Not a bug in session continuity
`/new` → `-c <thread> --create-if-missing` → pin-by-ID resume worked exactly as
designed (Tier 1.1). The session row, message rows, and resume behavior in
state.db are all correct. The -9 is unrelated to session resolution.

## Recommendations (fix ordering)
1. **Raise the default liveness timeout** to ≥600s (or make it a config value
   next to `timeout`). The guard still catches genuinely wedged processes;
   180s is simply below the realistic single-turn latency on a 27B local model.
2. **Distinguish "guard killed it" from "hermes crashed".** Today the kill
   makes `returncode == -9` and the user sees a cryptic `code -9`. If
   `self._stop_requested` / watchdog fired, the reply should be
   `⏱️ Turn exceeded the liveness window (180s) — model may be busy; retrying…`
   and ideally the message should be **retried once** (the prompt is already
   in `set_last_prompt`, and the session row exists, so resume is cheap).
3. **Optional: serialize bridge turns on the model.** A simple `asyncio`/thread
   lock around `_run_with_liveness_guard` per `HERMES_MODEL` would make the
   bridge wait for an in-flight turn instead of racing it (the mesh is a
   single-operator channel; queueing is acceptable and predictable).
4. **Document the wall-clock semantics** of the liveness guard in the
   `subprocess-liveness-guard` skill note: it measures stream idleness, and
   `-q` children go silent for the entire model turn.

## Repro / verification commands
```bash
journalctl --user -u hermes-reticulum --no-pager --since "2026-08-22 11:49"
grep -n "20260822_115050_766a63" ~/.hermes/logs/agent.log
sqlite3 ~/.hermes/state.db "SELECT id,title,source,message_count FROM sessions WHERE title LIKE 'mesh-reticulum-1787413790%';"
```

## Status
- [x] Root cause identified
- [x] Fix 1 (timeout default) — implemented in hermes_client.py
- [x] Fix 2 (guard-kill message + auto-retry) — implemented in hermes_client.py
- [x] Fix 3 (per-model serialization) — implemented in hermes_client.py
- [x] Unit tests: tests/test_hermes_client.py (7 tests, pass)
- [ ] Verify: send `/new` + test message on mesh with the gateway session
      busy; expect a delayed-but-successful reply instead of `code -9`.

## Implemented (2026-08-22)

All three fixes landed in `src/hermes_reticulum/core/hermes_client.py`:

1. `liveness_timeout` default raised 180s → 600s; `HERMES_LIVENESS_TIMEOUT`
   env override still honored (0 = off).
2. `_run_with_liveness_guard` sets `self._guard_killed` when the watchdog
   (or `/stop`) SIGKILLs the child; the exit path returns
   `⏱️ Turn exceeded the liveness window (600s) — the model may be busy or
   stalled. Please retry.` instead of the cryptic `code -9`.
   `chat()` detects a guard-kill, waits 5s, re-resolves the session, and
   retries the same command exactly once (resume by pinned ID, or
   `-c <thread> --create-if-missing` on a fresh thread).
3. `self._turn_lock` serializes turns per `HermesClient` instance. A queued
   message now waits for the in-flight turn before its child is spawned, so
   the per-subprocess liveness clock never starts while it's still queued.
   Each instantiation is per-process (CLI + plugin adapter), so this is
   true per-model serialization on a single-model mesh.

Tests: `tests/test_hermes_client.py` — 7 cases (default/override, flag reset,
honest-message path, lock serialization, retry-once, no-retry-on-crash).
Run with `./venv/bin/python -m unittest tests.test_hermes_client -v`.

Deploy: `systemctl --user restart hermes-reticulum`.
