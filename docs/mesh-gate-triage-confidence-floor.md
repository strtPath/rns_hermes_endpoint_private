# Jev triage: confidence floor 0.60, and why

Short public writeup for GitHub users: why `MESH_GATE_TRIAGE_CONF`
defaults to 0.6 and how to change it.

## What the floor does

With `MESH_GATE_TRIAGE=allow_benign`, Jev may auto-allow a call only if it
rates it routine, benign, AND at least 0.60 confident. Anything below the
floor, anything sensitive or destructive, and any Jev error still
escalates to the human approve/deny gate. The floor is the single dial
between "Jev clears more" and "Jev clears less."

## Why 0.60

Calibrated 2026-09-18 by rating 13 representative calls with
`typesafe/jev-1.13` through the exact question text and state template
the live gate sends, then sweeping the floor from 0.2 to 0.8:

- 0.4 through 0.7 is a flat plateau: the same five read-only calls
  (git status, git diff --stat, ls, systemctl status, a benign
  execute_code) auto-clear at every one of them.
- 0.2 to 0.3 buys one extra auto-allow: `pip install <pkg>` at 0.33
  confidence. Not worth auto-clearing package installs on a coin-flip.
- 0.8 trims two calls the operator would not mind auto-clearing
  (`ls` at 0.74, a fixture write at 0.70) — the floor only gets
  stricter without adding safety.
- Nothing sensitive or destructive auto-clears at any tested floor.

0.60 sits mid-plateau: conservative, and exactly on the boundary where
"Jev clears the calls you never want to watch for" stops being true.

## Defaulting it

`MESH_GATE_TRIAGE_CONF` defaults to `0.6` in the plugin. Public users do
not need to set it; set it higher (0.7-0.8) if you want fewer
auto-clears, lower only after watching your own
`mesh-tool-events.log` distribution. The stage itself stays `off` by
default — `allow_benign` is opt-in and requires `OPENROUTER_API_KEY`
(or `TYPESAFE_API_KEY`) in the gateway env.

## Caveat

The calibration set is 13 synthetic calls, not weeks of live traffic.
The recommendation for operators who auto-clear anything: run
`hint_only` for a few days, read the logged confidence distribution,
and confirm 0.60 still separates your benign core from your risky
tail before flipping to `allow_benign`.
