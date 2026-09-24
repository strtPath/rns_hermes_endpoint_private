# Mesh bridge: operator veto misclassified as wedged-model → infinite deny loop

Date: 2026-08-29
Branch: feat/slash-commands-and-model-pin
Commit: 70bfcb5 (never auto-retry a veto) + 5e4d021 (idempotent on_deny)

## Symptom

After the user sent `great. received. switching to loRa radio for this
message` on a fresh mesh session, the bridge log printed, on a steady
~30-40s cadence, for roughly 45 minutes:

```
18:58:22 mesh denied tool — aborting in-flight child
19:09:50 mesh denied tool — aborting in-flight child
19:10:07 mesh denied tool — aborting in-flight child
19:10:42 mesh denied tool — aborting in-flight child
... (continues) ...
19:47:39 mesh denied tool — aborting in-flight child
```

No `hermes chat` child process was ever alive during the loop
(`pgrep` showed only `rnsd`/`nomadnet`/the gateway), and the bridge
process itself sat at ~12% CPU. The user saw "propagated, no response"
— the turn never produced a final reply.

A second, independent loss: the user's *duplicate* "great..." message
never reached the bridge at all (only 3 LXMF messages received since the
17:29 restart: `/new`, the test message, one "great..."). That is the
uplink no-receipt gap (fire-and-forget `handle_outbound`), not this bug.

## Root cause

`cli._deny_veto()` funnels every operator veto (gate timeout, `/deny`,
pre-exec gate refusal) through `hermes.stop()`, which set:

```
self._stop_requested = True
self._guard_killed    = False
```

In `chat()`, the auto-retry branch is gated on `self._guard_killed`:

```
if result is not None and self._guard_killed:
    if self._guard_kill_worth_retrying(run_ms):   # run_ms < window
        ... resume the SAME message ...
```

`_guard_killed` is False for a veto, so the *intended* path (report
"⏹️ Stopped") was correct — but the veto kept re-arriving on an
already-dead child. Each `on_deny` re-fired `stop()`, and because the
gate state / message re-entry re-driven the turn, the "early death"
(`run_ms < 600s window`) resumed the same prompt, which re-hit the same
non-safe tool, re-opened the 120s approval gate, timed out (deny-by-
default), and re-fired `on_deny`. Net effect: an infinite
kill → resume → gate-timeout → deny cycle with no model work happening.

The core defect: **an operator timeout/deny was being re-driven as if
it were a wedged model.** A denied tool must terminate the turn, not be
retried — re-running it re-denies identically.

## Fix (commit 70bfcb5)

Added `self._deny_veto` to distinguish a mesh operator veto from a
liveness-guard kill:

- `__init__`: `self._deny_veto = False`
- `stop()`: sets `self._deny_veto = True` (a veto, even on an already-dead
  child)
- liveness `_watchdog()` kill: sets `self._deny_veto = False` (a real
  guard kill still qualifies for the early-death resume-retry)
- `chat()`: resets `self._deny_veto = False` at turn start, and the retry
  branch becomes `if ... and self._guard_killed and not self._deny_veto`

A veto now ends the turn honestly ("⏹️ Stopped before the turn
finished") and is never auto-resumed. A genuine liveness guard kill
still gets the single early-death retry.

## Verified

- After `systemctl --user restart hermes-reticulum` with the fix (PID 731703):
  **zero** "mesh denied tool" events in the 45s window, where previously
  there was one every ~40s.
- Follow-up (5e4d021): the loop re-surfaced at a slower ~3-8 min cadence,
  driven by `control_server._relay_loop` re-firing `on_deny` with
  `child_running=False` (no in-flight child). `_deny_veto` now no-ops when
  no child is running, so a stale/re-fired veto can't re-open the gate.
  After that fix (PID 740094): **zero** events over 4m03s, bridge idle and
  healthy.

## Residual / still open

The upstream *driver* that intermittently re-opens a gate with no live turn
is not yet identified (the gateway hook `mesh-tool-events` and plugin
`mesh-tool-gate` are the only HTTP clients to the control endpoint; the hook
diag log is empty, suggesting an internal relay re-fire rather than an
external POST). The idempotency guard makes it harmless, but the source
should be traced if it recurs.

## Still open (separate issues, not this fix)

1. **No LoRa delivery receipt** — `handle_outbound` is fire-and-forget;
   "dispatched" ≠ "arrived". This is why both the user's uplink duplicate
   and earlier downlink tails were lost silently. Needs chunk ack/sequence.
2. **Approval gate UX** — deny-by-default after 120s is correct for safety,
   but the mesh peer should get a clear "⏸️ waiting for /approve — this
   tool needs your OK" so the user can approve instead of the turn dying.
   The `on_gate_open` push exists; verify it reaches the phone.
3. **`rssi=None, snr=None, method=1`** (opportunistic) — the profiler is
   blind to downlink signal. Feeding real RSSI/SNR into the profiler is
   still needed to diagnose the downlink loss.

## Related

- docs/mesh-bridge-findings-2026-08-29-step-watcher-recap-bug.md (recap
  replay — fixed 8f9d0fb)
- docs/mesh-bridge-findings-2026-08-29-downlink-burst-loss-and-recap-replay.md
- docs/mesh-bridge-findings-2026-08-22-error-code-9.md (liveness guard SIGKILL)
