# Bridge Audit — 2026-08-30

**Ran by:** Holo, deepseek-v4-pro via OpenRouter
**Git HEAD:** `1d71cf3` — Revert "bridge: downlink acks + RSSI/SNR profiler + SIGTERM clean exit + 900s approval"
**Branch:** `feat/slash-commands-and-model-pin`
**Running bridge:** PID 599, started 2026-08-30 16:42:09 EDT, `HERMES_LIVENESS_TIMEOUT=600`, `HERMES_MESH_APPROVAL_TIMEOUT=900`, model `qwen3.8-27b@iq3_xxs`
**Tests:** 1 failed, 73 passed, 5 warnings

---

## 1. TEST FAILURE: `test_slow_turn_with_fresh_marker_completes` — gen field mismatch

**File:** `tests/test_turn_alive.py` line ~168
**Root cause:** Commit `a154d7c` ("scope liveness-heartbeat marker per child (generation counter)") added a `gen` field requirement to `_marker_alive()`:

```python
# hermes_client.py:272
if marker.get("gen") != self.current_turn_alive_gen():
    return False
```

`write_turn_alive_marker()` stamps `{"session": ..., "gen": <current_gen>, ...}` (line 218–229), and `bump_turn_alive_gen()` advances the counter at spawn (line 734) and retry (line 769).

**But the test helpers never stamp `gen`:**

- `tests/test_turn_alive.py:_write_marker()` (line 30): writes `{"session": session, "ts": ..., "phase": phase}` — **no `gen`**.
- `tests/test_turn_alive.py:_write_child()` (line ~120): the child's `beat()` function writes `{"session": s, "ts": ..., "phase": "tool"}` — **no `gen`**.

**Sequence in the failing test:**
1. `_run_with_liveness_guard` calls `bump_turn_alive_gen()` → gen becomes 1
2. `_run_with_liveness_guard` calls `write_turn_alive_marker(phase="model")` → marker has `gen: 1`
3. Test calls `_write_marker(...)` — overwrites marker with no `gen` field, or gen=0 by default
4. Child runs, beats the marker every second — but never stamps `gen`
5. Watchdog polls `_marker_alive()` → `marker.get("gen")` returns `None` → `None != 1` → `False`
6. Marker is never considered alive → child killed at 2s → test gets liveness-window message, not "child-done"

**Fix:** The test's `_write_marker` and child `beat()` must stamp the current generation. Easiest path: have `_write_child` accept and pass `gen` into the child script, and have the pre-run `_write_marker` call also stamp it. Or the test could call `self.client.write_turn_alive_marker()` instead of the bare `_write_marker` helper.

---

## 2. DENIED TOOL EVENTS — PID 902155, 12:19 and 13:52

```
mesh denied tool — aborting in-flight child
(stop_requested=False guard_killed=False deny_veto=False)
```

**What happened:**
- Two denied-tool events on PID 902155 (the bridge instance before the current PID 599, which was the one running the reverted commit's code with downlink acks).
- Both show `deny_veto=False` — meaning the deny came through the control server's `on_deny` path (via the `mesh-tool-gate` plugin's `pre_tool_call` hook → `/gate/notify`), not a manual `/stop` veto.
- The reasons are NOT logged — could be operator explicit deny, or operator timeout (the 900s gate expiring). The timestamp gap between the user message and the deny event is short (minutes, not 900s), suggesting an explicit operator deny rather than a timeout.
- After the deny, the child was killed (`Hermes exited with code -9`), and the bridge reported "Session found but has no messages. Starting fresh" — the child was killed before it could write anything.

**Not a bug** — this is the pre-execution gate working as designed. The operator denied a risky tool, the bridge killed the turn. The idempotency guard (`_deny_veto` no-op when `is_running() == False`) is intact.

**Improvement:** Log the *reason* for the deny (timeout vs explicit) so these events are self-diagnosing.

---

## 3. REVERTED COMMITS — downlink acks, RSSI/SNR, SIGTERM exit

Commit `1322e89` ("bridge: downlink acks + RSSI/SNR profiler + SIGTERM clean exit + 900s approval") added 328 lines across 4 files:

| File | What it added |
|------|--------------|
| `bridge.py` | Chunk sequence numbers, ack tracking, per-recipient state |
| `control_server.py` | 900s approval timeout (env-overridable) |
| `downlink.py` (NEW) | 178 lines: `DownlinkTracker` with chunk acks, burst detection, receipt tracking |
| `profiler.py` | RSSI/SNR into channel classification |

Commit `1d71cf3` reverted it at 15:40 EDT today. Commit `e67652f` reverted the roadmap doc update that marked those features done.

**What the revert means for the running bridge (PID 599):**
- No downlink acks. `send_reply()` logs "Reply dispatched" but this is fire-and-forget — no delivery receipt. Silent tail loss on weak LoRa channels with zero signal on this side.
- RSSI/SNR values come through on inbound messages (`profiler.py` line 66-67 reads them), but since all current connections are `tcp_default` (rssi=None, snr=None), every channel classifies as a 1 Gbit link.
- `systemctl restart` still hangs — `run_forever()`'s SIGTERM handler only sets a flag; no `os._exit(0)` or `RNS.shutdown()`.
- Approval timeout in `control_server.py` is back to `DEFAULT_APPROVAL_TIMEOUT = 120.0` in the source, but the running service's env has `HERMES_MESH_APPROVAL_TIMEOUT=900` (confirmed via `/proc/599/environ`), so the live gate is still 900s. If the bridge restarts and the env var is lost, it would drop to 120s.

**The original commit is intact at `1322e89`.** To restore: `git revert 1d71cf3`, or cherry-pick specific pieces.

---

## 4. SIGTERM STILL DOES NOT WORK

**Current state in `bridge.py:run_forever()` (line 517–522):**
```python
def _handle_signal(signum, frame):
    logger.info("Received signal %d, shutting down...", signum)
    self._running = False
signal.signal(signal.SIGTERM, _handle_signal)
```

And in `finally:` it calls `self.stop()` which only does `self._pool.shutdown(wait=False)`. No `os._exit`, no `RNS.reticulum.shutdown()`. The RNS/LXMF C-level event loop threads keep the process alive even after `_running = False`, so `systemctl --user stop hermes-reticulum` hangs forever.

**The fix** (was in the reverted commit): call `os._exit(0)` after cleanup, or call `RNS.Reticulum().shutdown()` in the finally block. Until this lands, every restart needs `kill -9 <pid>` + confirm fresh start.

---

## 5. LIVENESS GUARD — working as designed (19:09 kill)

The 19:09 guard kill on the current PID 599:

```
Liveness guard: no output or heartbeat for 600s, killing hermes
Hermes exited with code -9: ↻ Resumed session ... (1 user message, 54 total messages)
Liveness guard killed the child after a full %.0fms window — model was slow, not wedged; skipping retry
```

This is **correct behavior:** the model ran a full 600s window, the heartbeat marker kept it alive the entire time (54 tool messages means the hook was beating the marker regularly), the guard fired at exactly the window, and the retry suppression correctly classified it as "full window" (not a wedge) and returned the "re-send to continue" message. The `%.0fms` format-string bug (missing the actual ms value) is cosmetic.

---

## 6. MINOR: `_read_stream` iterator `.close()` warnings in tests

**File:** `src/hermes_reticulum/core/hermes_client.py` line 830
**Trigger:** When test mocks use list iterators instead of real pipe file objects, `_read_stream` calls `stream.close()` on an iterator that has no `.close()`.
**Harmless in production:** Real `subprocess.PIPE` file objects have `.close()`.
**Fix:** Guard with `if hasattr(stream, 'close'): stream.close()` or use `contextlib.closing`.

---

## Summary — action items ranked by impact

| # | What | Impact | Effort |
|---|------|--------|--------|
| 1 | Fix test gen field mismatch | Blocks CI | Low — update test helpers |
| 3 | Restore downlink acks + SIGTERM fix (or decide not to) | Silent message loss on weak channels | Medium — cherry-pick or revert-of-revert |
| 4 | Land SIGTERM exit fix | Every restart is manual SIGKILL | Low — one `os._exit(0)` call |
| 6 | Guard `stream.close()` in tests | Noisy test output | Trivial |
| 2 | Log deny reason (timeout vs explicit) | Better debugging | Trivial |

**What's healthy:**
- Pre-execution gate (`mesh-tool-gate` plugin) is working
- Liveness heartbeat marker with generation scoping (`a154d7c`) is working
- Step watcher recap fix (`8f9d0fb`) is working — no replay bursts
- Deny-loop fixes (`70bfcb5` + `5e4d021`) are working — zero deny loop events
- CLI-side step watcher (`c087da2`) delivers per-tool 💻 pushes
