# RNS Bridge — Research & Plan (2026-08-31)

**Status:** research complete, plan pending user sign-off.
**Branch:** `feat/slash-commands-and-model-pin` @ `1d71cf3`
**Method:** 3 parallel subagents — fork inventory, RNS/LXMF protocol research (reticulum.network docs + source), hermes-agent plugin/hook/approval internals (installed v0.20.6 source).

---

## 1. Current state (verified)

Working: inbound LXMF → ACL → slash dispatch → `hermes chat -q` child, liveness guard
(600s, gen-scoped heartbeat marker), deny-loop fixed (`_deny_veto` + idempotent
`on_deny`), step-watcher recap replay fixed (seed at session MAX), pre-exec gate via
mesh-tool-gate plugin, step-through mode, 20+ slash commands.

Broken / regressed:
- **SIGTERM hang** — `systemctl stop/restart` hangs; needs `kill -9`. Handler only sets
  `_running=False`; RNS C-level event-loop threads keep the process alive.
- **Fire-and-forget downlink** — no delivery receipt, silent tail loss on weak LoRa.
- **Blind profiler** — `rssi=None, snr=None` → every channel classifies as
  `tcp_default` (1 Gbit assumption).
- **No per-recipient burst pacing** — flat `time.sleep(0.5)` only; the 2026-08-29
  downlink-burst-loss incident (17–24 chunks in one second) can recur.
- **Approval timeout drift** — source default back to 120s after revert; live env has
  `HERMES_MESH_APPROVAL_TIMEOUT=900`. If the env var is ever lost, gate drops to 120s.
- **Test failure:** `test_slow_turn_with_fresh_marker_completes` — gen field mismatch
  (test helpers don't stamp `gen`; `_marker_alive()` requires it since `a154d7c`).

### The revert situation (`1322e89` → reverted by `1d71cf3`)

Commit `1322e89` landed: DownlinkTracker (chunk seq + ack logging + per-recipient
pacing), real RSSI/SNR from `RNS.Transport.local_client_rssi_cache/snr_cache`,
SIGTERM clean exit via `RNS.exit(0)`, 900s approval timeout. Reverted same day at
15:40 EDT — **no findings doc records why**. The audit doc (`2026-08-30-audit.md`)
treats the revert as given and notes the commit is intact for restore via
`git revert 1d71cf3`.

**Decision needed:** reapply wholesale, or cherry-pick pieces. My read: the SIGTERM
fix and 900s timeout are unambiguous wins (revert them back first); downlink acks +
RSSI/SNR should be reapplied *with* the fixes below rather than as-is, since we now
know exactly what was missing from that design.

---

## 2. Protocol facts (RNS/LXMF — verified against docs/source)

- **LXMF has no built-in chunking.** One LXMessage = one msgpack structure. Single-packet
  opportunistic delivery caps at ~389 bytes payload (500 MTU − 111 overhead). Large
  payloads ride RNS Link/Resource layer (segmentation, sequencing, checksumming,
  retransmission) — that's why our app-level chunking exists.
- **Delivery receipts exist at two levels:**
  - Packet: `RNS.PacketReceipt` (`create_receipt=True`) with statuses SENT / DELIVERED /
    FAILED / CULLED + `get_rtt()`. Requires destination proof strategy PROVE_ALL or
    PROVE_APP (PROVE_NONE = fire-and-forget). Implicit proofs piggy-back on return traffic.
  - LXMF: LXMRouter tracks delivery, retries (`MAX_DELIVERY_ATTEMPTS=5`,
    `DELIVERY_RETRY_WAIT=10s`), propagation nodes do store-and-forward for offline peers.
- **Known caveat (from our own findings):** first-hop DELIVERED ≠ end-to-end. It means the
  first hop accepted with a signed proof; tail can still drop between rnode and phone.
  True E2E ack needs an app-level protocol decision on the Columba side (Tier 5).
- **RSSI/SNR access paths:** `packet.rssi`/`.snr` attrs (master instance),
  `packet.get_rssi()/get_snr()`, shared-instance `reticulum.get_packet_rssi(packet_hash)`,
  and `link.track_phy_stats(True)` → `link.get_rssi()/get_snr()` on established Links.
  RNode LoRa interfaces support all of them. The reverted commit used the Transport
  caches (`local_client_rssi_cache`) — that path works; the Link-level path is a
  stronger option for downlink (it measures *our* link to the peer, not just inbound).
- **QoS knobs:** no user-settable per-packet TTL (implicit `PATHFINDER_M=128` hop cap).
  Interface modes matter: `access_point` for LoRa APs (quieter announces), `roaming`
  for mobile nodes, `boundary` where a LoRa node meets the Internet. `announce_cap`,
  `bitrate`, `gravity` tune routing/timeout math.

**Implication:** RNS handles routing/retransmission — we do NOT need to build transport.
What's missing is *visibility* (receipts, signal quality) and *pacing discipline*, both
application-level. Streaming over the mesh stays deferred per user decision; if it ever
happens it rides Link/Resource delivery, not app-level chunking.

---

## 3. Hermes-agent side (v0.20.6 — verified against installed source)

- **Plugin API:** `pre_tool_call` callbacks may return None (proceed),
  `{action: block}`, `{action: approve}` → escalates to Hermes' built-in
  `request_tool_approval()`, or `{action: modify, args}`. First directive wins.
- **Why we bypassed the builtin flow:** in `-q` single-query mode,
  `request_tool_approval()` calls `input()` and hangs. That's why mesh-tool-gate POSTs
  to `/gate/notify` directly instead of returning `approve`.
- **The security gap (2026-08-30 doc) is now fixable properly:** the plugin *can* return
  `{action: approve}` and let Hermes own the approval UX — but only if we wire a mesh
  gateway notify callback so the prompt reaches the operator over LXMF instead of
  stdin. Until then, extending `_COMMAND_TOOLS` coverage (write_file/patch/execute_code)
  in our plugin is the pragmatic fix; the `approve`-directive path is the clean one.
- **Hooks:** mesh-tool-events is a gateway hook (`agent:step`, `agent:start`) — pure
  stream now, refreshes liveness marker, POSTs `/step` + `/step/full`. Token auth via
  `~/.lxmf/storage/control_token`.

---

## 4. Prioritized plan

**Decision (2026-08-31):** NO wholesale `git revert 1d71cf3` — the original commit had
regressions. Cherry-pick pieces only, rebuilt where we now know better.

### P0 — Restore what the revert took (cherry-picked from `1322e89`)
1. **SIGTERM clean exit** — cherry-pick the `RNS.exit(0)` path from `1322e89`. Verify with a
   real `systemctl restart`, not just unit tests.
2. **Approval timeout 900s + env override in source** — cherry-pick; removes the drift risk
   (source 120s vs live-env 900s).
3. **Fix the failing test** (`test_slow_turn_with_fresh_marker_completes`) — stamp `gen`
   in test helpers to match `_marker_alive()` contract from `a154d7c`.

### P1 — Downlink reliability, redesigned (the meat)
Rebuild DownlinkTracker fresh (do NOT cherry-pick the reverted version as-is) with:
- **Receipts:** keep `lxm.register_delivery_callback` first-hop ack logging
  (DELIVERED/SENT/FAILED + elapsed). Document explicitly that this is *first-hop*, not E2E.
- **Sequence tagging:** `[pN i/N]` prefixes stay — lets the recipient detect dropped
  tails with zero protocol change.
- **Pacing:** per-recipient adaptive interval keyed on `note_outcome` counters (loss →
  widen interval), replacing flat 500ms. Start at 500ms, back off to ~1–2s on FAILED/timeout.
- **RSSI/SNR into profiler:** reapply Transport-cache read; *add* the Link-level path
  (`track_phy_stats` + `link.get_rssi()/get_snr()`) so downlink pacing is keyed on our
  actual link to the peer, not just inbound signal. This was OP-015 in the old list and
  the protocol research confirms it's supported on RNode LoRa.

### P2 — Gate security (tool coverage ONLY, per user decision)
- Extend mesh-tool-gate coverage: write_file, patch, execute_code (currently terminal-only).
- **Deferred (not now):** session/permanent allowlist, YOLO mode, and the `{action: approve}`
  directive path that would let Hermes own the approval UX. Revisit after tool coverage lands.

### P3 — Hygiene / visibility
- Log deny *reason* (timeout vs explicit) on every deny event.
- Reconcile announce-log hash vs CLI delivery hash (OP-012).
- Message labeling Part 2 (`⚙︎ agent:<kind>` markers) — still unimplemented, low value now.
- Unknown-toolsets warning prefix cleanup.

### Deferred / dropped
- **Streaming over mesh** — deferred per user decision; if revived, it's Link/Resource
  delivery, not app chunking.
- **E2E app-level ack (bridge → Columba "received")** — Tier 5, needs phone-side protocol
  decision. Parked with a pointer to the first-hop caveat above. User can send test
  messages from Columba but is away from home / off LoRa as of 2026-08-31 — revisit when
  back on the mesh.

---

## 5. Decisions locked (2026-08-31)

1. **Cherry-pick, no wholesale revert** — `1322e89` had regressions; restore SIGTERM +
   approval timeout by cherry-pick, rebuild downlink/RSSI fresh with Link-level signal path.
2. **Columba E2E ack stays Tier 5** — user can message from Columba but is off-LoRa right
   now; no protocol work until back home on the mesh.
3. **P2 = tool coverage only** — extend gate to write_file/patch/execute_code. Allowlist,
   YOLO, and the approve-directive path are deferred.

Ready to execute P0 → P1 in that order when you give the word.
