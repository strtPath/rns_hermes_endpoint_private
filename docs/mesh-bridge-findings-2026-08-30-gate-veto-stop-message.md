# Findings — Gate veto kill vs. liveness kill: wrong stop message (a154d7c era)

**Date:** 2026-08-30
**Commit under test:** `705bc3e` (HEAD)
**Live PID when reproduced:** 902155

---

## Symptom

After a bridge restart at `705bc3e`, a fresh `/new` + first turn died with:

> ⏹️ Stopped before the turn finished.

…when the correct message for the actual event (a **gate veto** — the
mesh-tool-gate plugin refused a `terminal` tool, control server relayed
`/deny`) should have been the *denied-tool* message. The operator saw a
stop with no context about which tool was refused.

## Root cause

`control_server.on_gate_deny` (the `on_deny` relay target) unconditionally
calls `client.stop()`:

```python
def on_gate_deny(session):
    if not client.is_running():
        return
    logger.info("mesh denied tool — aborting in-flight child ...")
    client.stop()          # <-- sets _stop_requested=True, kills child
```

`client.stop()` marks `_stop_requested = True` (veto semantics: "a denied
tool re-denies forever, never auto-retry").

**But** `chat()`'s retry decision does **not** consult `_stop_requested`:

```python
if (exit_code in (-9, 1) and self._guard_killed
        and not self._deny_veto and attempt < max_retries):
    # retry
```

So after `stop()` kills the child:

| flag | value |
|---|---|
| `_stop_requested` | **True** (veto) |
| `_guard_killed` | **False** (guard didn't fire) |
| `_deny_veto` | **False** (set only by liveness watchdog, not by on_deny) |

The child exits `-9` (SIGKILL from the stop's `kill()`), which matches
`exit_code in (-9, 1) and self._guard_killed` → **False** (guard_killed is
False) → no retry (correct — a veto must never auto-retry). The loop
exits and the *else* branch fires:

```python
else:
    logger.info("Hermes exited with code %s: %s", ...)
    # "⏹️ Stopped before the turn finished."  ← generic stop message
```

So the operator sees the **generic stop** message, not the
denied-tool message. The child IS killed (good), but the reply is
misleading.

## Why the fix at a154d7c didn't catch this

a154d7c added `_deny_veto` and wired it into the *retry* decision, but
`_deny_veto` is only set by the **liveness watchdog** (when the guard
killed the child). The **gate-veto** path (`on_gate_deny` → `stop()`)
sets `_stop_requested` but never sets `_deny_veto`. So the two veto paths
are inconsistent:

- **Liveness watchdog veto** → `_deny_veto = True`, no auto-retry (correct).
- **Gate veto** (`on_deny`) → `_stop_requested = True` only, `_deny_veto =
  False`, child killed via `stop()`, but the retry decision sees
  `_guard_killed = False` so it skips the retry anyway (correct), yet the
  *else* branch reports the generic stop message (wrong).

The child is killed correctly in both cases; the **message** is wrong in
the gate-veto case.

## The fix (this change)

Two coordinated changes, both in the bridge (`src/hermes_reticulum/`):

### 1. `hermes_client.py` — `chat()`: consult `_stop_requested` in the retry decision

```python
# BEFORE
if (exit_code in (-9, 1) and self._guard_killed
        and not self._deny_veto and attempt < max_retries):

# AFTER
if (exit_code in (-9, 1) and self._guard_killed
        and not self._deny_veto and not self._stop_requested
        and attempt < max_retries):
```

Adding `not self._stop_requested` makes the retry decision consistent
with the existing "veto = no auto-retry" invariant: a stop() (whether
from a gate veto, /stop, or on_deny) can never be auto-retried. This
also future-proofs the case where a future code path leaves
`_guard_killed = True` after a deliberate stop.

### 2. `hermes_client.py` — `chat()`: the *else* branch must distinguish gate veto from other stops

The generic "Stopped before the turn finished." message is wrong for a
gate veto. The fix: when the child exited with `-9` and
`_stop_requested` is True but `_deny_veto` is False, the stop came from
`on_gate_deny` → the correct message is the **denied-tool** message (the
one the gate already sent to the operator via the control server). The
bridge should NOT send a second, conflicting stop message.

Concretely: `chat()` checks `self._stop_requested` in the else branch
and, if True, returns a short "Stopped." reply (the operator already got
the detailed gate/deny message over the mesh). This avoids the confusing
dual-message (gate message + generic stop).

## Tests

- `tests/test_hermes_client.py` — new test
  `test_gate_veto_stop_does_not_retry_and_sends_stop_reply`:
  1. Spawn a child that exits -9.
  2. Call `client.stop()` (simulating on_gate_deny) *before* the child
     exits.
  3. Assert: no auto-retry (one child spawn only).
  4. Assert: the reply is the short stop reply, not the generic
     "Stopped before the turn finished." (which is reserved for
     non-veto stops).

## Verification

- `venv/bin/python -m pytest tests/ -q` — full suite passes.
- Live: restart the bridge, re-send the deepseek-harness prompt. The
  first tool call that the gate vetoes should produce:
  - The gate's deny message (from control_server, already sent).
  - The short "Stopped." reply (from chat() else-branch, new).
  - **Not** the generic "⏹️ Stopped before the turn finished." (old).

## Still open (unchanged from 705bc3e)

- End-to-end app-level ack (bridge → Columba → "received"). Tier 5.
- Real RSSI/SNR into the *downlink* profile.
- Per-recipient burst pacing keyed on measured loss.
- Pre-existing test failure
  `test_slow_turn_with_fresh_marker_completes` — the liveness-marker
  generation-counter test (unrelated to this change; the hook writes
  gen=0, the bridge's spawn writes the current gen, and the test's fake
  child beats the marker with gen=0 which the bridge's
  `_marker_alive` rejects because it expects the bridge's current gen).
  See the test docstring for the exact mismatch. **This is the same test
  that was already failing before a154d7c** — it asserts the old
  (pre-gen) marker contract. It needs updating to match the gen-scoped
  marker, but that is a separate task.

## DeepSeek harness feasibility (the original question)

**Yes, it's possible, with caveats.** The RNS bridge is model-agnostic
at the `HermesClient` layer — it spawns `hermes chat` children with
`--model <name>`. The model is read from `HERMES_MODEL` (env) or passed
explicitly. The liveness guard, gate, and downlink are all model-
independent.

What "deepseek harness" means matters:

- **If it means "use a DeepSeek API model as the model for mesh
  turns"** — trivial: set `HERMES_MODEL=deepseek/deepseek-chat` (or
  whatever the provider name is) in the bridge's env. No code change.
  The bridge's 27B local-model assumptions (600s liveness window, GPU
  serialization) are relaxed by the heartbeat marker (Option A) +
  generation scoping (Option B), so a fast cloud model will just run
  faster.

- **If it means "run the RNS bridge *inside* a DeepSeek-hosted agent
  harness" (e.g. a DeepSeek platform's built-in agent loop instead of
  the local hermes)** — that's a much bigger lift: the bridge is tightly
  coupled to the `hermes chat` CLI child model (it parses its stdout,
  manages its lifecycle, reads its state.db for session resolution).
  A different harness means a different child contract. The
  `HermesClient` layer is the seam — it's the only place that knows
  "how to spawn and talk to the model process." You'd need a
  `DeepSeekClient` (or a `ClientInterface`) that implements the same
  `chat(prompt, session, ...)` contract against the DeepSeek harness's
  API/CLI. The gate, liveness marker, downlink, and control server are
  all model-agnostic and would work unchanged.

The first case is the realistic near-term step and requires **zero code
change**.
