# Findings: Local LXMF Loopback Test — Mesh Loop Verified (2026-08-26)

## Question
User reported: after restarting the hermes gateway and sending `/new` to the
reticulum bridge, the message "just goes to propagation" and never resolves.
Columba (the phone peer) shows "last seen hermes 2 hours ago, network
distance 16 hops." User asked whether installing nomadnet *on the bridge's
own machine* (this box) would help test changes against itself.

## Answer
**No.** This box already runs the full stack — rnsd (config
the user's `.reticulum` directory), nomadnet (`-d`, same shared instance), and the
hermes-reticulum bridge — all sharing the `@rns/default` local instance
socket. The bridge's own LXMF destination (redacted) resolves at
**0 hops via LocalInterface** on this box. Installing nomadnet a second time
(or pointed at a different config) would create a *separate* shared instance
and a *second identity* — the exact "identity path mismatch / two hashes"
trap the 2026-08-26 findings doc warns about. That would break, not help.

The correct local test harness is a thin LXMF **client** that attaches to the
*same* shared instance and talks to the bridge's destination. That is
`tests/test_loopback_lxmf.py` (added this session).

## What the loopback test proves
`venv/bin/python tests/test_loopback_lxmf.py`

1. Attaches to the shared RNS instance (`RNS.Reticulum()`, abstract
   `@rns/default`) — no second rnsd, no port conflict, no second identity.
2. Generates a throwaway test identity (`~/.lxmf/testloopback_identity`).
3. Resolves the bridge destination from the on-disk `known_destinations`
   store (msgpack, loaded into `Identity.known_destinations` at init), then
   builds the OUT delivery destination as
   `Destination(bridge_identity, OUT, SINGLE, "lxmf", "delivery")`.
   **APP_NAME is lowercase `"lxmf"`** in this build (LXMF/LXMF.py line 1) —
   the bridge's own reply path (bridge.py:468) uses the same lowercase
   app/aspect, so the destination hashes line up. (My first draft used
   uppercase `"LXMF"` + identity-hash aspect, which produced a *different*
   hash and never matched.)
4. Sends via `router.handle_outbound(lxm)` with `desired_method=DIRECT`,
   `include_ticket=True` — identical to the bridge's `send_reply` path.
5. Waits for the delivery callback.

### Result (ran twice)
```
[*] Bridge destination: <redacted>
[+] Sent 'loopback-test-...'
[*] reply arrived from <redacted>
[+] REPLY in 0.6s   /   7.4s
    Content: ⛔ Access not authorized.
[+] Loopback OK
```

Bridge-side log (`.lxmf/reticulum.log`) for the same run:
```
Received LXMF from <identity-A> [link, sig=valid]: loopback-test-...
Sender not in allowlist: <identity-A>
Message from <identity-A> rejected by ACL
Reply dispatched to <identity-A> (26 bytes)
```

**The bridge received the message, ran the ACL, and dispatched a reply over
the mesh — and the client got it back.** The round-trip works on this box.
The reply was an ACL rejection *because the test identity is not on the
allowlist* — that is still a valid end-to-end proof of the mesh loop. To get
a real Hermes-generated answer, run the bridge with
`HERMES_RETICULUM_ALLOW_ALL=true` for the test window, or add the test
identity to the allowlist.

## What this means for the "stuck in propagation" report
- The bridge's **receive + reply** path is healthy on this box (0.6–7.4 s
  loop).
- Columba's "16 hops / last seen 2 h" is the *phone → internet bootstrap*
  path to this box (the propagation server TCP endpoint, redacted), not a local
  fault. 16 hops is
  expected for bootstrap-only routing from the phone.
- A reply that "just propagates" on the *phone* side is therefore a
  **path/peering/propagation-node** problem between the phone and this box,
  **not** a bridge bug — and it is **not** fixed by installing nomadnet here.

## Remaining real risk (unrelated to nomadnet)
The hermes **gateway** (separate from the bridge) is still emitting
**code-75 loop watchdog kills** (see `~/.hermes/logs/gateway.log`,
`restart_loop.json`). The relaxed watchdog
(probe 60 / timeout 30 / strikes 6 ≈ 600 s tolerance) was saved to config,
but code-75 kills continued after the 12:59 restart. Each code-75 kill drops
in-flight mesh replies and severs the control socket — the *same class* of
failure the user is seeing. Next step is to capture a non-empty faulthandler
dump to see where the loop blocks, and/or confirm whether the 27B model's
single-turn latency (100–950 s) is exceeding even the 600 s tolerance.

Also: disk at 80 % / 2.8 G free on a 14 G SD-card FS — real IO pressure on
the model + gateway + mesh, which feeds back into slow turns and the watchdog.

## Files
- `tests/test_loopback_lxmf.py` — the loopback test client (this session).
- `docs/mesh-bridge-findings-2026-08-26-code-75-gateway-loop.md` — code-75
  findings (prior session).
- `docs/mesh-bridge-findings-2026-08-26-bridge-pipe-guard-and-gateway-watchdog.md`
  — pipe guard + watchdog (prior session).