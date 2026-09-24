# 2026-08-26 — Bridge control-server broken-pipe guard + gateway watchdog tuning

## Symptom
- User reported "messages haven't been going through to Telegram or the Reticulum
  bridge."
- Investigation (2026-08-26) found the bridge was actually *alive and replying*
  (journald shows `Reply dispatched` for the 10:44 "you there?" message). The
  real failures were:
  1. **Gateway crash loop** — the Hermes gateway was hard-exiting with code 75
     ("missed 3 consecutive liveness probes") every few hours. Each restart
     dropped in-flight Telegram messages and, on the mesh side, broke the
     bridge's in-flight control-socket write.
  2. **Bridge control-server noise** — every gateway restart surfaced a full
     `BrokenPipeError` traceback in the bridge (`control_server.py:423
     do_POST → _send`).
  3. **Stale `restart_loop.json`** — the gateway restart-loop breaker was
     tripped, so auto-resume of interrupted sessions was suppressed.

## Root cause chain
- Local 27B model ⇒ a single turn takes 300–350 s.
- The gateway loop liveness watchdog (default probe 30 s / timeout 10 s /
  3 strikes ≈ 90–120 s tolerance) tripped during those long turns ⇒ code 75
  self-kill ⇒ crash loop.
- Model calls already run off-loop (`asyncio.to_thread`) and the Telegram
  poller is async, so the long model wait is *not* the loop blocker itself —
  but the watchdog tolerance was too tight for this deployment either way.
  The faulthandler dump the watchdog wrote was empty (0 bytes), so the exact
  loop-blocker is still unconfirmed; raising the watchdog is mitigation, not a
  proven root-cause fix.

## Fixes applied (this host)
1. **Bridge** — wrapped `ControlHTTPRequestHandler._send()` in
   `core/control_server.py` in `try/except (BrokenPipeError,
   ConnectionResetError)`, logging a one-line `logger.debug` instead of a full
   traceback, and setting `self.close_connection = True`. A gateway restart
   dropping the socket is now expected and silent.
2. **Gateway config** (via `hermes config set`, not a direct file edit):
   - `gateway.loop_watchdog_probe_interval_s` 30 → **60**
   - `gateway.loop_watchdog_probe_timeout_s`  10 → **30**
   - `gateway.loop_watchdog_max_strikes`       3 → **6**
   Effective worst-case tolerance ≈ 60 + 6×(60+30) ≈ 600 s, comfortably past a
   single long turn. (Takes effect on next gateway start — gateway was restarted
   separately by the user.)
3. **Cleared** `~/.hermes/gateway/restart_loop.json` so the gateway resumes
   interrupted sessions normally instead of sitting in "resume-pending."
4. **Restarted the Reticulum bridge** via `./start.sh` (the pidfile had gone
   stale — it pointed at an old PID while the live proc was 533484; killed the
   real PID, removed the stale pidfile, started fresh as PID 1418695). Verified:
   `Timeout: 600s`, `Liveness guard: 600s`, `Control endpoint listening on
   127.0.0.1:8471`, `Bridge ready`.

## Still open / worth watching
- **Why does the loop block for 300 s+?** The watchdog faulthandler dump was
  empty so the blocker is unproven. If code-75 restarts resume even with the
  relaxed watchdog, capture a non-empty dump (`logs/gateway_faulthandler.log`)
  to see the actual stack.
- **Flaky Telegram API path** — recurring `Bad Gateway` / CLOSE-WAIT socket
  rebuilds against a fallback Telegram API IP on this box. Network-side,
  not a hermes bug, but it adds latency/reconnect churn on top of the above.
- **Disk at 80% (2.8 G free)** — adds memory/IO pressure to the same box.
- The `.lxmf/reticulum.log` file in the repo is a *stale* log (last write
  Aug 4); live bridge logs go to journald. Don't trust that file for "is it
  alive."

## How to reproduce the diagnosis
- `journalctl --user -t hermes-reticulum --no-pager -n 50`
- `tail ~/.hermes/logs/gateway.log` (grep `watchdog|code 75|inbound|outbound`)
- `curl -X POST -H "Authorization: Bearer $(cat ~/.lxmf/storage/control_token)" http://127.0.0.1:8471/health`
- `hermes config get gateway.loop_watchdog_max_strikes`
