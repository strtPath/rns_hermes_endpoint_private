# Tier 4.5 — Watchdog / auto-restart: Option 1 (systemd native watchdog)

_Archived 2026-09-09. Superseded by Option 3 (idle heartbeat). Keep as
reference if we ever revisit._

## What Option 1 was

Use systemd's built-in watchdog instead of a separate process or an external
probe. Modify the **existing** `hermes-reticulum.service` in place (no second
service, no user-visible service-count change):

- `Type=simple` → `Type=notify`
- add `NotifyAccess=all`
- add `WatchdogSec=90` (tunable per-deployment, like `HERMES_LIVENESS_TIMEOUT`)
- bridge calls `sd_notify("WATCHDOG=1")` from a timer thread every N seconds
- systemd kills + `Restart=on-failure` brings it back if pings stop

`sd_notify` needs `NOTIFY_SOCKET` (set automatically by systemd for
`Type=notify` units).

## Why it was archived (in favor of Option 3)

Option 1's watchdog is a **coarse** signal: "the process is still alive."
It cannot distinguish:

- bridge wedged (event loop hung) but the `sd_notify` timer thread (on a
  separate OS thread) is still pinging → systemd never restarts. **This is
  the exact failure we're trying to catch, and Option 1 misses it.**
- bridge healthy → pings continue → no restart (correct).

Option 1 only catches a *total* process freeze. Option 3's idle heartbeat
ties the liveness signal to the *actual event loop*: the main loop touches
a marker file every iteration; if the loop hangs, the marker goes stale,
and we kill. That matches the real failure mode, and it reuses the existing
marker-file pattern (turn-level liveness guard) for consistency.

## To revisit

If we ever need a process-level (not event-loop-level) watchdog, or if the
idle-heartbeat marker approach proves insufficient, Option 1 is the fallback.
The service-file diff and the `sd_notify` timer thread are both small.
