# Spec Part 1 — Liveness-Heartbeat Marker (Option A): Implementation Notes

_Date: 2026-08-28. Branch: `feat/slash-commands-and-model-pin`. Commit: `bef429b`._
_Spec: `docs/spec-2026-08-27-liveness-heartbeat-and-message-labeling.md` (Option A section)._

## What shipped

The liveness guard in `_run_with_liveness_guard` is no longer a pure wall
clock. A per-turn **marker file** (default `~/.hermes/.reticulum-turn-alive`,
override `HERMES_TURN_ALIVE_FILE`) carries
`{"session": <thread title>, "ts": <time.time()>, "phase": "model"|"tool"}`.
A fresh marker (same session, mtime within `liveness_timeout`) touches the
liveness clock, so the guard becomes a **stall detector** instead of a
max-turn wall clock:

- A slow-but-working model (hook beating the marker on every `agent:step`)
  runs to completion even on 600s+ turns.
- A truly wedged model (no marker, no output) is still killed at N seconds.

## Code changes (`src/hermes_reticulum/core/hermes_client.py`)

| Location | Change |
|----------|--------|
| `__init__` | `self.turn_alive_file` from `HERMES_TURN_ALIVE_FILE` env (default `~/.hermes/.reticulum-turn-alive`). |
| `write_turn_alive_marker(phase)` | Atomic write (tmp + `os.replace`) of `{session, ts, phase}`. Best-effort; never raises. |
| `clear_turn_alive_marker()` | `os.unlink`; swallows `FileNotFoundError`/`OSError`. |
| `_marker_alive()` | True iff file exists, mtime within `liveness_timeout`, **and** `session` matches `self.session_name`. Mtime is authoritative (survives a read racing the writer's swap). |
| `_run_with_liveness_guard` — spawn | `clear_turn_alive_marker()` (reset stale) + `write_turn_alive_marker(phase="model")` (fresh start marker). |
| `_run_with_liveness_guard` — watcher | `_marker_alive()` → `_touch()` (touches the liveness clock). Scoped to this child's session so a different bridge turn's hook can't keep ours alive. |
| `_run_with_liveness_guard` — kill path | `clear_turn_alive_marker()` after `_kill_process()` (belt-and-suspenders). |
| `_kill_process()` | `clear_turn_alive_marker()` (covers `/stop` and external kills, per spec item 3). |

## Design decisions (deltas from the spec)

1. **Clean exit leaves the start marker in place.** Spec item 3 says reset
   "at child spawn and on `_kill_process`" — a clean exit is *neither*, so
   the `phase="model"` start marker persists after a clean run. This is
   harmless: the next spawn resets it, and a kill clears it. (The test
   `test_spawn_resets_marker` asserts content — session + phase — not
   wall-clock freshness, because the child takes ~2s to exit and the test
   liveness_timeout is 2s, so mtime freshness is a timing race.)

2. **The hook is the steady-state beater.** The bridge's start marker covers
   at most one full window of pure model thinking before the first tool
   batch. After that, the `agent:step` hook (`~/.hermes/hooks/mesh-tool-events/
   handler.py`) rewrites the marker on every tool batch (pre- and
   post-batch), scoped to mesh sessions only (resolved via `state.db`).

3. **Session scoping is the safety valve.** `_marker_alive()` matches on
   `marker["session"] == self.session_name`. A different bridge turn's hook
   firing cannot keep this child alive. Covered by
   `test_foreign_session_marker_does_not_keep_alive`.

## Tests (`tests/test_turn_alive.py`, 14 tests, all passing)

- **TestMarkerHelpers** (7): roundtrip, corrupt JSON, stale mtime, foreign
  session, no file, default path, env override.
- **TestGuardWithMarker** (4, end-to-end vs a fake child script):
  - `test_slow_turn_with_fresh_marker_completes` — child beats the marker
    every 1s through a 3s silent sleep (> 2s window); completes with
    `child-done`.
  - `test_wedged_child_without_marker_killed` — child sleeps 8s, no marker;
    killed at ~2s with the honest guard-kill reply.
  - `test_foreign_session_marker_does_not_keep_alive` — child beats a
    *different* session; still killed.
  - `test_spawn_resets_marker` — stale foreign marker present before run;
    after a clean exit the marker holds this client's session + `phase="model"`.
- **TestHookMarkerWriter** (2): atomic write + silent failure on bad path;
  `agent:start` dispatch routes mesh → marker, non-mesh → no marker.

## Bridge status

Restarted 2026-08-28 09:36 EDT via `systemctl --user restart
hermes-reticulum.service`. Clean shutdown of the old PID, new PID up at
09:36:33. Control endpoint on 127.0.0.1:8471, liveness guard 600s, LXMF
address `<<redacted>>` re-announced, identity preserved
from `~/.lxmf/storage/hermes_identity`.

## Known gaps / follow-ups

- **Integration test (spec test plan, last bullet):** "200s turn on the local
  model (marker refreshed by hook) completes instead of dying at 600s" — not
  yet run end-to-end on the live bridge. The unit tests cover the logic;
  a real 200s+ turn on the 27B model would be the confirmation.
- **Option B** (`hermes chat --json-lines` streaming) remains on the roadmap
  as the cleaner long-term signal; blocked on an upstream CLI feature.
- **Message-labeling half of the spec** (inbound marker strip + reply
  labeling) is **Part 2** and not yet implemented. See the spec doc for the
  full Part 2 scope.
