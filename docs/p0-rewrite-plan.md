# P0 Rewrite Plan — SIGTERM + Approval Timeout (minimal-comment style)

**Date:** 2026-08-31
**Branch:** `feat/slash-commands-and-model-pin` @ `1d71cf3`
**Style rule:** comments only where code is non-obvious. No design-decision records in
code (those live in findings docs). Docstrings: one line max for trivial methods,
Args/Returns only when a param isn't self-evident.

---

## 1. SIGTERM clean exit (`bridge.py`)

**Current state:** `run_forever()` L509 → `_handle_signal()` L518 sets
`self._running = False`. RNS C-level event-loop threads keep the process alive after
the Python loop exits. `systemctl stop` hangs; needs `kill -9`.

**Fix (cherry-pick from `1322e89`, rewritten minimal):**

In `_handle_signal()`:
```python
def _handle_signal(self, signum, frame):
    self._running = False
    # RNS C-level threads outlive the Python loop; force-exit after clean shutdown.
    threading.Thread(target=self._clean_exit, daemon=True).start()

def _clean_exit(self):
    try:
        self.stop()          # shut down thread pool, close router
        import reticulum as rns
        rns.exit(0)           # tears down RNS C event loop + all threads
    except Exception:
        os._exit(0)          # last resort if RNS.exit hangs
```

**Why a thread:** `signal.signal` handlers run on the main thread; calling `RNS.exit()`
directly from the handler can deadlock if the RNS C loop holds the GIL. A daemon
thread lets the signal return immediately, then shuts down cleanly.

**What NOT to touch:** `stop()` L499 already sets `_running=False` and shuts the pool —
keep it. The new code just adds the RNS teardown after it.

**Verification:**
- Unit: mock `rns.exit`, assert it's called on SIGTERM.
- Integration: real `systemctl restart rns-hermes-bridge` completes in <10s (was: hangs).
- Regression: bridge still starts, receives LXMF, replies (loopback test).

---

## 2. Approval timeout 900s + env override (`control_server.py`)

**Current state:** `DEFAULT_APPROVAL_TIMEOUT = 120.0` at L48. Live env has
`HERMES_MESH_APPROVAL_TIMEOUT=900` but source doesn't read it — if the env var is ever
lost, gate drops to 120s silently.

**Fix (cherry-pick from `1322e89`, rewritten minimal):**

```python
# L48 area:
DEFAULT_APPROVAL_TIMEOUT = float(os.getenv("HERMES_MESH_APPROVAL_TIMEOUT", "900"))
```

That's it. One line. The env var name matches what `.env` already sets. No separate
constant + override dance — the default IS 900, and the env var overrides it if present.

**What NOT to touch:** `request_approval()` L296 takes a `timeout` param that defaults
to `DEFAULT_APPROVAL_TIMEOUT`. The call sites in `_handle_post` pass it through. No
change needed there.

**Verification:**
- Unit: set env var, assert `DEFAULT_APPROVAL_TIMEOUT == 900.0`; unset, assert same (default).
- Integration: trigger a gate via `/gate/notify`, confirm it blocks ~900s before deny-by-default.
- Regression: `/approve` and `/deny` still resolve the gate immediately.

---

## 3. Comment cleanup (same files, same pass)

While touching these files, strip the worst offenders per the audit:

### `control_server.py` (28% → target <15%)
- **L1-27 module docstring:** cut to 3 lines. The "Approval gate" section restates what
  `request_approval()` documents. Keep only: "Local HTTP control endpoint for mesh
  bridge. Token-authenticated, localhost-only."
- **L121-133 class docstring:** delete the Usage example (trivial 3-step lifecycle).
  One line: "Threaded local HTTP server for tool-step events and control ops."
- **L170-181 bounded queue comment:** cut to one line: `# Bounded queue: mesh-bound side effects offloaded from HTTP handler thread.`
- **L303-311 request_approval docstring:** cut to 2 lines. Return values are in the signature.
- **L431-441 pre-exec gate comment:** delete entirely. The code dispatches to
  `on_gate_open` + `request_approval` — self-evident.

### `bridge.py` (30% → target <15%)
- **L38-43 step-through ContextVar comment:** delete. Code is `ContextVar(...)` declarations.
- **L66-83 StepThroughManager docstring:** cut to 2 lines. The 3 bullets are documented
  on the methods themselves.
- **L235-246 push_reply docstring:** cut to 1 line: "Push a proactive LXMF message, split if long."
- **L411-422 send_reply docstring:** delete Args/Returns block. Param names are self-evident.

### `hermes_client.py` (31% → target <15%) — BIGGEST WIN
- **L110-136 turn_alive_file comment (27 lines):** cut to 2 lines:
  `# Heartbeat marker file, scoped per-child via generation counter.`
  The design rationale belongs in the findings doc it references.
- **L321-330 "WHY THIS EXISTS" block (10 lines):** delete. Method names + their own
  docstrings cover it.
- **L373-379 seed-at-MAX comment (7 lines):** cut to 1 line:
  `# Seed at session MAX(id) so we only push this turn's rows.`
- **L626-640 guard-kill retry comment (15 lines):** cut to 2 lines:
  `# Single retry only if child died early (< full liveness window). Slow model != wedged.`
- **L885-890 marker reset comment (6 lines):** delete. Code says exactly what's happening.

**Rule for all files:** if a comment explains *why* and the why isn't obvious from the
code, keep it (one line max). If it explains *what*, delete — the code is the doc.

---

## 4. Execution order

1. **Branch:** `fix/p0-sigterm-timeout-cleanup` off current HEAD.
2. **Commit 1:** SIGTERM clean exit (`bridge.py`). Run tests + loopback.
3. **Commit 2:** Approval timeout env override (`control_server.py`). Run tests.
4. **Commit 3:** Comment cleanup (all files). No behavior change — `git diff` should show
   only comment/docstring deletions. Run full test suite to confirm zero regressions.
5. **Verify:** `systemctl restart` completes <10s. Loopback test passes. Full suite green.

**Do NOT bundle:** downlink tracker rebuild (P1) goes in a separate branch/commit after P0
is verified and merged.
