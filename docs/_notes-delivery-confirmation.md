# Delivery-confirmation check: RNS 1.5.4 / LXMF 1.1.1 (local source)

Question: does RNS/LXMF expose any bounded per-message delivery-confirmation signal to the sender?
Source: site-packages RNS (1.5.4) and LXMF (1.1.1) under the bridge venv.

## Q1 — LXMessage delivery status

Constants (LXMF/LXMessage.py:15-23):
- `GENERATING = 0x00`, `OUTBOUND = 0x01`, `SENDING = 0x02`, `SENT = 0x04`, `DELIVERED = 0x08`, `REJECTED = 0xFD`, `CANCELLED = 0xFE`, `FAILED = 0xFF`
- `states = [GENERATING, OUTBOUND, SENDING, SENT, DELIVERED, REJECTED, CANCELLED, FAILED]`

State is set in `LXMessage.__init__`: `self.state = LXMessage.GENERATING` (LXMessage.py:147).

Callbacks:
- `register_delivery_callback(callback)` → `self.__delivery_callback = callback` (LXMessage.py:267-268)
- `register_failed_callback(callback)` → `self.failed_callback = callback` (LXMessage.py:270-271)

State transitions in LXMessage.py:
- `__mark_delivered()` (561-571): `self.state = LXMessage.DELIVERED`, `self.progress = 1.0`, fires `self.__delivery_callback(self)`
- `__mark_propagated()` (573-583): `self.state = LXMessage.SENT` (NOT DELIVERED), `self.progress = 1.0`, fires `self.__delivery_callback(self)`
- `__mark_paper_generated()` (585-595): `self.state = LXMessage.PAPER`, fires `self.__delivery_callback(self)`
- `__resource_concluded()` (597-606): if `resource.status == RNS.Resource.COMPLETE` → `__mark_delivered()`; if `REJECTED` → `state = LXMessage.REJECTED`; else → `state = LXMessage.OUTBOUND`
- `__propagation_resource_concluded()` (608-614): if `resource.status == RNS.Resource.COMPLETE` → `__mark_propagated()`; else → `state = LXMessage.OUTBOUND`
- `__link_packet_timed_out()` (616-621): `self.state = LXMessage.OUTBOUND`

State transitions in LXMRouter.py:
- `fail_message(lxmessage)` (2563-2572): `lxmessage.progress = 0.0`, removes from `pending_outbound`, `lxmessage.state = LXMessage.FAILED` (unless REJECTED), fires `lxmessage.failed_callback(lxmessage)`
- `process_outbound()`: CANCELLED → `failed_callback(lxmessage)` (2720-2721); REJECTED → `failed_callback(lxmessage)` (2726-2727)
- `process_deferred_stamps()`: cancelled during stamp gen → `failed_callback(lxmessage)` (2594-2595, 2624-2625, 2655-2656)

**Key correction**: `failed_callback` IS called — from `LXMRouter.fail_message()` and from `process_outbound()` on CANCELLED/REJECTED. The earlier finding of "never called" was wrong.

## Q2 — LXMRouter attempt tracking

Constants (LXMF/LXMRouter.py:30-34):
- `MAX_DELIVERY_ATTEMPTS = 5`
- `DELIVERY_RETRY_WAIT = 10` (seconds)
- `PATH_REQUEST_WAIT = 7` (seconds)
- `MAX_PATHLESS_TRIES = 1`

`LXMessage.delivery_attempts` field: initialized to 0 (LXMessage.py:179), incremented in `process_outbound()` for each delivery attempt (LXMRouter.py:2740, 2744, 2758, 2815, 2853, 2868).

`process_outbound()` (LXMRouter.py:2682-2920) tracks per-method retry logic:
- OPPORTUNISTIC: retry with `DELIVERY_RETRY_WAIT` between attempts; after `MAX_PATHLESS_TRIES` tries with no path, request path and wait `PATH_REQUEST_WAIT`; after `MAX_DELIVERY_ATTEMPTS` → `fail_message()` (2735-2759)
- DIRECT: uses established direct/backchannel links if available; if no link, establishes one; if no path, requests path; after `MAX_DELIVERY_ATTEMPTS` → `fail_message()` (2765-2849)
- PROPAGATED: sends to `outbound_propagation_node` via link; if no path to PN, requests path; after `MAX_DELIVERY_ATTEMPTS` → `fail_message()` (2850-2920)

`propagation_entries` structure (LXMRouter.py:581-589):
```
[destination_hash, filepath, received_timestamp, msg_size, [handled_peers], [unhandled_peers], stamp_value]
```
No retry-count or attempt-counter in propagation_entries. Attempt tracking is on the LXMessage object itself (`delivery_attempts`, `next_delivery_attempt`).

## Q3 — Sender-side learning path

`LXMessage.send()` (LXMessage.py:463-505):
- OPPORTUNISTIC: `lxm_packet.send().set_delivery_callback(self.__mark_delivered)`; `state = SENT` immediately after send() returns (line 472)
- DIRECT packet: `receipt = lxm_packet.send()`; if receipt: `receipt.set_delivery_callback(self.__mark_delivered)`, `receipt.set_timeout_callback(self.__link_packet_timed_out)`; if no receipt: `__delivery_destination.teardown()`, no callbacks
- DIRECT resource: `RNS.Resource(...)` with callback `self.__resource_concluded`; `state = SENDING`
- PROPAGATED packet: `receipt.set_delivery_callback(self.__mark_propagated)` — sets state=SENT, NOT DELIVERED
- PROPAGATED resource: callback `self.__propagation_resource_concluded`

**What the sender learns:**
1. **Immediately** (synchronous): `send()` returns a receipt (truthy) or not. `state` set to SENT/SENDING. This is router-acceptance, not delivery.
2. **Bounded wait** (RNS receipt): delivery callback fires when cryptographic proof from destination validated. Timeout callback fires if proof not received within receipt timeout.
3. **Bounded wait** (LXMRouter retry): after up to `MAX_DELIVERY_ATTEMPTS=5` attempts with `DELIVERY_RETRY_WAIT=10s` or `PATH_REQUEST_WAIT=7s` between attempts, `fail_message()` sets `state=FAILED` and fires `failed_callback`.
4. **Unbounded** (propagation to PN): `__mark_propagated()` fires delivery callback with `state=SENT` when the PN accepts the message. This does NOT confirm destination delivery. The message then sits in the PN's store until a peer syncs. No further per-message signal at the LXMF layer.

**Does delivery confirmation depend on the recipient?**
- DIRECT packets: YES — delivery callback requires cryptographic proof from destination.
- DIRECT resources: YES — `RNS.Resource.COMPLETE` requires the destination to accept the resource.
- PROPAGATED: NO — `__mark_propagated` fires when the propagation node accepts, not when the destination receives.

## Q4 — RNS packet/link delivery receipt

`RNS.Packet.send()` (RNS/Packet.py:285-316):
- Returns `self.receipt` (a `PacketReceipt`) on success
- Returns `False` if no interface could process the packet (no silent queue)

`PacketReceipt` class (RNS/Packet.py:399-592):
- Status constants: `FAILED = 0x00`, `SENT = 0x01`, `DELIVERED = 0x02`, `CULLED = 0xFF` (406-410)
- Initial status: `SENT` (423)
- `set_delivery_callback(callback)` (571-577)
- `set_timeout_callback(callback)` (581-587)
- `check_timeout()` (550-560): if `status == SENT and is_timed_out()` → status = `CULLED` (if timeout==-1) or `FAILED`, fires `callbacks.timeout` in thread
- `validate_proof(proof, proof_packet)` (495-538): if explicit proof valid → `status = DELIVERED`, `proved = True`, fires `callbacks.delivery`
- `validate_link_proof()` (449-492): same for link proofs
- `PacketReceiptCallbacks`: `delivery = None`, `timeout = None` (589-592)

**DELIVERED status is set only when a cryptographic proof from the destination is received and validated.** This is a true delivery confirmation.

`RNS.Transport.receipts` (RNS/Transport.py:174): list of outgoing packet receipts for proof processing.
`RNS.Transport.MAX_RECEIPTS = 1024` (159).
`RNS.Transport.receipts_check_interval = 1.0` seconds (252).
Receipts checked in `process_inbound()` at line 740-745: `if time.time() > Transport.receipts_last_checked + Transport.receipts_check_interval:` then iterate receipts calling `check_timeout()`, pruning to `MAX_RECEIPTS`.

## Q5 — RNS path state

- `RNS.Transport.has_path(destination_hash)` (RNS/Transport.py:3126)
- `RNS.Transport.hops_to(destination_hash)` (3135) — returns `PATHFINDER_M` if unknown (3142)
- `RNS.Transport.request_path(destination_hash, on_interface=None, tag=None, recursive=False)` (3279)
- `RNS.Transport.path_table` dict (180)
- `RNS.Transport.path_states` dict (189)
- `RNS.Transport.STATE_UNKNOWN = 0x00`, `STATE_UNRESPONSIVE = 0x01`, `STATE_RESPONSIVE = 0x02` (148-150)
- `RNS.Transport.REACHABILITY_UNREACHABLE = 0x00`, `REACHABILITY_DIRECT = 0x01`, `REACHABILITY_TRANSPORT = 0x02` (107-109)

No-path behavior in `Packet.send()`: if `RNS.Transport.outbound(self)` returns falsy → `self.sent = False`, `self.receipt = None`, returns `False` (RNS/Packet.py:307-313). **No silent queue at the packet level.**

In `LXMRouter.process_outbound()`: if no path → `RNS.Transport.request_path()`, wait `PATH_REQUEST_WAIT=7s`, retry on next `process_outbound()` cycle.

In `LXMPeer.sync()`: if no path → `RNS.Transport.request_path()`, sleep `PATH_REQUEST_GRACE=7.5s`, if still no path → `sync_backoff += SYNC_BACKOFF_STEP`, `alive=False` (LXMPeer.py:295-304).

## Q6 — Time horizon

RNS layer:
- `RNS.Reticulum.DEFAULT_PER_HOP_TIMEOUT = 6` seconds (RNS/Reticulum.py:142)
- `RNS.Packet.TIMEOUT_PER_HOP = RNS.Reticulum.DEFAULT_PER_HOP_TIMEOUT` (RNS/Packet.py:115)
- Packet receipt timeout (non-link): `get_first_hop_timeout(hash) + TIMEOUT_PER_HOP * hops_to(hash)` (RNS/Packet.py:432-433)
- Packet receipt timeout (link): `max(link.rtt * link.traffic_timeout_factor, Link.TRAFFIC_TIMEOUT_MIN_MS/1000)` (RNS/Packet.py:430)
- `RNS.Transport.PATH_REQUEST_TIMEOUT = 15` seconds (RNS/Transport.py:134)
- `RNS.Transport.PATH_REQUEST_GRACE = 0.4` seconds (136)
- `RNS.Transport.PATHFINDER_M = 128` max hops (118)
- `RNS.Transport.PATHFINDER_E = 60*60*24*7` = 7 days path expiration (126)
- `RNS.Transport.MAX_RECEIPTS = 1024` (159)
- `RNS.Transport.receipts_check_interval = 1.0` seconds (252)

LXMF layer:
- `LXMRouter.MAX_DELIVERY_ATTEMPTS = 5` (LXMRouter.py:30)
- `LXMRouter.DELIVERY_RETRY_WAIT = 10` seconds (32)
- `LXMRouter.PATH_REQUEST_WAIT = 7` seconds (33)
- `LXMRouter.MAX_PATHLESS_TRIES = 1` (34)
- `LXMPeer.MAX_UNREACHABLE = 14*24*60*60` = 14 days (LXMPeer.py:39)
- `LXMPeer.SYNC_BACKOFF_STEP = 12*60` = 12 minutes (45)
- `LXMPeer.PATH_REQUEST_GRACE = 7.5` seconds (50)
- `LXMessage.TICKET_EXPIRY = 21*24*60*60` = 21 days (LXMessage.py:49)

## Q7 — Bottom line

**PARTIAL.** A bounded delivery-confirmation signal exists, but its scope depends on the delivery method:

**DIRECT (link to destination): YES — bounded, true delivery confirmation.**
- `RNS.PacketReceipt` validates cryptographic proof from destination → `status = DELIVERED` → `__mark_delivered()` → `state = DELIVERED`, `progress = 1.0`, delivery callback fires.
- Bounded by: `first_hop_timeout + per_hop_timeout * hops` (typically seconds to minutes for direct links).
- Negative case: receipt timeout → `status = FAILED` or `CULLED` → `__link_packet_timed_out()` → `state = OUTBOUND`. Then LXMRouter retries up to `MAX_DELIVERY_ATTEMPTS=5` with `DELIVERY_RETRY_WAIT=10s` between attempts. After 5 failures → `fail_message()` → `state = FAILED`, `failed_callback` fires.
- Total bounded wait: ~5 * (receipt_timeout + 10s) ≈ minutes to low hours, depending on path length.

**DIRECT (resource to destination): YES — bounded, true delivery confirmation.**
- `RNS.Resource.COMPLETE` → `__resource_concluded()` → `__mark_delivered()`.
- Bounded by resource transfer time (unbounded in principle, but practically bounded by link speed).

**PROPAGATED (via propagation node): PARTIAL — no destination delivery confirmation.**
- `__mark_propagated()` fires when the propagation node accepts the message: `state = SENT`, `progress = 1.0`, delivery callback fires. This is **not** destination delivery.
- After the PN accepts, the message sits in `propagation_entries` until a peer syncs it. No further per-message signal at the LXMF layer.
- Negative case: if the PN link fails 5 times → `fail_message()` → `state = FAILED`, `failed_callback` fires.
- Gap: between "PN accepted" and "peer received," there is no bounded signal. The peer sync is bounded by `MAX_UNREACHABLE=14 days` and `SYNC_BACKOFF_STEP=12 min`, but these are peer-level, not per-message.

**No-path case: bounded, immediate.**
- `Packet.send()` returns `False` immediately. No silent queue.
- `LXMRouter.process_outbound()` requests path, waits `PATH_REQUEST_WAIT=7s`, retries. After `MAX_DELIVERY_ATTEMPTS=5` → `fail_message()`.

**Unverified:**
- `RNS.Link.TRAFFIC_TIMEOUT_MIN_MS` value (not read from RNS/Link.py).
- Exact behavior of `RNS.Transport.outbound()` when no path exists (read only the packet-level consequence).

## Closing the two unverified items (parent, 2026-09-24)

Verified directly from source, both originally flagged by the child as unread.

**`RNS.Link.TRAFFIC_TIMEOUT_MIN_MS` (RNS/Link.py:81-82):**
- `TRAFFIC_TIMEOUT_MIN_MS = 5`
- `TRAFFIC_TIMEOUT_FACTOR = 6`
- Instance copies the factor at Link.py:257: `self.traffic_timeout_factor = Link.TRAFFIC_TIMEOUT_FACTOR`

So the link-transport branch of the receipt timeout (Packet.py:430) is
`max(link.rtt * 6, 5/1000)` seconds. With a healthy RTT this is sub-second to low seconds; the
5ms floor prevents a degenerate zero. This branch is materially shorter than the non-link
branch, which is `get_first_hop_timeout(hash) + TIMEOUT_PER_HOP * hops_to(hash)`. Confirmed at
Packet.py:432-433.

Consequence for the feasibility finding: the direct-delivery bound is dominated by the non-link
path formula and the five-attempt retry window, not by the link constant. Link-transport receipts
resolve fastest.

**`RNS.Transport.outbound()` with no path (RNS/Transport.py:1334-1610):**

`Transport.outbound()` dispatches to `_outbound` when `USE_OUTBOUND_QUEUE` is false
(Transport.py:1335); the queue-based branch raises `NotImplementedError` (1336-1341), so the
queue path is dead code and `_outbound` is the live path.

Two hard returns of False inside `_outbound`:
- `if packet.hops > Transport.PATHFINDER_M-1: return False` (Transport.py:1356) — hop ceiling
- an inner `return False` (Transport.py:1387) — early bailout from the per-packet closure

The function's exit is `return sent` (Transport.py:1610), where `sent` stays False unless a
transmitting interface was found and `packet_sent(packet)` ran (Transport.py:1606-1609). `sent`
is set True only at the transmit sites that actually hand the packet to an interface
(Transport.py:1407, 1427, 1437 region).

So with no path and no interface able to carry the packet, `_outbound` falls through the
interface loop and returns `sent = False`, which `Packet.send()` converts to `self.sent = False`,
`self.receipt = None`, and a `False` return (Packet.py:307-313). **The child's conclusion holds
and is now traced end to end: no silent queue at the packet layer.** The packet is simply not
sent, and the caller learns immediately.

Queueing therefore lives one layer up, in `LXMRouter.process_outbound()`, which is why the
five-attempt window with `DELIVERY_RETRY_WAIT` and `PATH_REQUEST_WAIT` is the real bounding
mechanism.
