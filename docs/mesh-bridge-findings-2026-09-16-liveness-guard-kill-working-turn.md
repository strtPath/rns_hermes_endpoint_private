# Liveness guard killed working turns — marker never refreshed mid-turn

**Date:** 2026-09-16
**Branch:** fix/liveness-marker-mid-turn-touch
**Files:** `src/hermes_reticulum/core/hermes_client.py`, `tests/test_hermes_client.py`

## Symptom

The RNS bridge would stop a session even though the model was still working and
emitting tool calls. On the mesh this surfaced as a turn ending mid-work with
the "Turn exceeded the liveness window" message, on a turn that was clearly
still progressing.

## Root cause

The liveness guard in `_run_with_liveness_guard` keeps a child alive by polling
`_marker_alive()` once per second. A "fresh" marker (same `session`, matching
`gen`, mtime within the window) touches the liveness clock, turning the guard
from a max-turn wall clock into a stall detector. The design assumed the
gateway's `agent:step` hook writes that marker on every tool batch.

But the hook **never fires for a mesh turn**: the child is a `hermes chat -q`
CLI process, and the hook only runs in the gateway. Tracing the writers of
`write_turn_alive_marker`, it is called exactly once — at spawn,
`phase="model"`. In `-q` mode the child is silent for the whole turn, so during
tool work there were no stdout/stderr bytes to touch the bytes-only clock
either.

Net effect: a working `-q` turn longer than `HERMES_LIVENESS_TIMEOUT` (default
600s) that had tool calls but no stdout bytes had a stale marker and no
bytes, so the watchdog killed it. "Still working and emitting tool calls" was
invisible to the guard.

The CLI-side step watcher (`_run_step_watcher`) is the in-process component that
*does* see each new tool row for the child (it polls `state.db`) — but it only
pushed the tool messages to the mesh and never refreshed the liveness marker.
The signal the guard needed was already flowing in the process; it just wasn't
being used.

## Fix

1. **Watcher refreshes the marker on each new tool row.** In `_run_step_watcher`,
   the moment it confirms `last_id > last_pushed` (new tool rows for this turn),
   it now calls `write_turn_alive_marker(phase="tool")`. `write_turn_alive_marker`
   writes the current `session_name` and `gen`, and the watchdog's
   `_marker_alive()` already scopes on exactly those — so this correctly keeps
   *this* child's liveness clock warm, and a different turn's or the gateway's
   session still cannot keep ours alive. Best-effort: a write failure degrades
   to the bytes-only guard (the documented fallback), never raises. Called only
   when a new row actually appears, not on every 1s poll.

2. **Hard cap safety net.** Added `HERMES_TURN_HARD_CAP` (default 21600s = 6h,
   `0` disables), read in `__init__` next to `liveness_timeout`. The watchdog now
   also kills when the turn has exceeded this absolute wall clock, even if the
   marker keeps trickling fresh. This is a *different* detector from the
   relative silence window: it stops a turn that keeps emitting tool rows forever
   from running unbounded. Reuses the same kill path
   (`_stop_requested`, `_guard_killed=True`, clear `_deny_veto`, `_kill_process`,
   `clear_turn_alive_marker`).

3. **Scoping diagnostic.** `_log_marker_scoping_diag()` logs the marker's
   `session`+`gen` vs the child's at the silence-window kill, so a scoping
   mismatch (e.g. a resumed child whose `gen` was bumped after the marker was
   written) is distinguishable from a true stall (marker present and matched but
   stale) in the bridge log. Best-effort, never raises.

## What is NOT changed

- The retry logic (`_guard_kill_worth_retrying`) and the deny-veto path in
  `cli.py` — untouched. A veto still never auto-retries.
- The pre-execution gate — untouched.

## Verification

- `tests/test_hermes_client.py`: 22 passed (15 existing + 7 new). New tests:
  hard-cap env default/override/zero; step watcher writes a `phase="tool"`
  marker scoped to the child when new tool rows appear; no marker refresh with
  no new rows; scoping diag does not raise (no marker / mismatch).
- Full suite: `pytest tests/` → 175 passed.
- `py_compile` clean on both changed files; module imports; `turn_hard_cap`
  default confirmed live (21600).

## Follow-ups (not done, deliberate)

- If kills still cluster on resume, the new scoping log is the first thing to
  read: it will say SCOPING MISMATCH vs TRUE STALL.
- A findings doc here is for the fork. Upstream PR should land this on the
  public repo once it has been exercised on a real long `-q` turn.
