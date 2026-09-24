---
title: Reticulum as a Gateway Platform, Feasibility
level: sub-spec
parent: spec-rns-hermes-endpoint.md
subsystem: feasibility
status: DRAFT
---

# Reticulum as a Gateway Platform: Feasibility

## Scope

Assesses whether Reticulum/LXMF can become a Hermes gateway platform adapter, replacing the
current spawn-a-child bridge. States what fits, what strains, and what genuinely breaks.
Sub-specs describe today's system; the intended-interaction spec states the target; this
document judges whether the target is reachable and at what cost.

Status: DRAFT, revised 2026-09-24. Established by reading both codebases against the installed
sources (rns 1.5.4, lxmf 1.1.1), not by prototyping. No adapter has been written or run. The
delivery-confirmation question raised in the first draft is now answered from source; see
section 4. Evidence notes: `_notes-reticulum-fit.md`, `_notes-delivery-confirmation.md`.

## 1. The question

Hermes' gateway supports platforms through an adapter interface. Telegram, Matrix and
WhatsApp are plugins implementing `BasePlatformAdapter`. If Reticulum can implement the same
four abstract methods, the bridge's hand-built machinery becomes unnecessary, because the
gateway already provides it.

The interesting question is not whether it can be made to compile. It is whether Reticulum's
transport model fits assumptions the gateway makes about platforms.

## 2. What fits

Established from source, with the reasoning:

**Chat identity.** An LXMF destination hash serves as a chat id. Stable for as long as the
peer does not rotate keys.

**Chat type.** A one-to-one peer maps to type `dm`, which is a supported value.

**Inbound delivery.** The adapter's listener pushes a `MessageEvent` into the gateway by
calling `self.handle_message(event)`. Reticulum's delivery callback can do this.

**Outbound at any time.** There is no abstract typing-indicator or edit method to satisfy.
`supports_status_text` defaults to False and the gateway handles the absence.

**Adapter-owned background thread.** Not forbidden. `api_server.py` runs a
`threading.Thread`, so the pattern exists in-tree.

**Async bridging.** Reticulum's callback and thread API can feed an asyncio queue from a
background thread. This is a conventional pattern, though no existing adapter runs a
persistent transport thread, so it is new ground for this codebase rather than a copied shape.

## 3. What strains

**`connect()` cannot express reachability.** The gateway reads a True return as "the platform
is live and will push inbound." For Reticulum, True can only mean "the local RNS instance is
running." Whether any given peer is reachable is a separate and continuously changing fact.
The reconnect watcher keys off connect failure, so a peer going silent for hours never
triggers it. The gateway has no concept of connected-but-unreachable-peers.

**Chat ids are stable only until key rotation.** If a peer rotates its keys the destination
hash changes, which changes the session key, which breaks conversation continuity. The gateway
has no chat-id migration mechanism. In practice key rotation is rare, but it is the kind of
rare that silently loses a conversation.

**Names must be resolved adapter-side.** `get_chat_info` returns a name and type. The gateway
does not manage a hash-to-name mapping, so the adapter must maintain one.

## 4. Delivery semantics: what the source actually reports

This section replaces the first draft's claim that there is no callback from RNS to the
gateway when a queued message is finally delivered or dropped. That claim was wrong as stated.
Verified from installed source; citations are file:line in the notes.

Reticulum and LXMF do report per-message delivery, and the signal is bounded in the direct
case. The distinction that matters is delivery *method*, which the first draft did not draw.

**Direct delivery: a true, bounded delivery confirmation.**

`RNS.PacketReceipt` carries status `FAILED = 0x00`, `SENT = 0x01`, `DELIVERED = 0x02`,
`CULLED = 0xFF` (Packet.py:406-410). `DELIVERED` is set only when a cryptographic proof
returned by the destination validates, in `validate_proof()` (Packet.py:495-538, set at 503
and 527). That is not acceptance by a local router. It is proof from the peer.

On the LXMF layer, `LXMessage` has a full state machine, `GENERATING = 0x00`, `OUTBOUND = 0x01`,
`SENDING = 0x02`, `SENT = 0x04`, `DELIVERED = 0x08`, `REJECTED = 0xFD`, `CANCELLED = 0xFE`,
`FAILED = 0xFF` (LXMessage.py:15-23). A validated receipt invokes `__mark_delivered()`, which
sets `state = DELIVERED` and fires the registered delivery callback (LXMessage.py:561-571).
Callbacks are registered with `register_delivery_callback` / `register_failed_callback`
(LXMessage.py:267-271).

The window is bounded. A receipt times out after `get_first_hop_timeout(hash) +
TIMEOUT_PER_HOP * hops_to(hash)` for non-link sends (Packet.py:432-433), with
`DEFAULT_PER_HOP_TIMEOUT = 6` seconds (Reticulum.py:142). When it times out,
`__link_packet_timed_out` returns the message to `OUTBOUND` (LXMessage.py:616-621) and
`LXMRouter.process_outbound` retries: `MAX_DELIVERY_ATTEMPTS = 5` (LXMRouter.py:30) with
`DELIVERY_RETRY_WAIT = 10` seconds between attempts (LXMRouter.py:32) and `PATH_REQUEST_WAIT
= 7` seconds when a path must be found (LXMRouter.py:33).

Failure is reported, too. After the attempts are exhausted `fail_message()` sets `state =
FAILED` and calls `failed_callback` (LXMRouter.py:2564-2571). The first draft's implication
that failure never surfaces was also wrong.

So for direct delivery the sender learns yes or no, per message, within a horizon of roughly
five attempts times the receipt timeout plus retry waits. That is a `SendResult`-shaped fact:
`success=True` on `DELIVERED`, `success=False` on `FAILED`, with an error string.

**Propagated delivery: a half-truth, and it is unbounded.**

This is where the structural gap actually lives, narrower than the first draft said but
sharper.

When a message is handed to a propagation node instead of the destination directly,
`__mark_propagated()` fires on the sender (LXMessage.py:573-583). It sets `state = SENT`, not
`DELIVERED`, and progress to 1.0. The state constant names the semantics honestly: the node
accepted the message. The node then holds it in `propagation_entries` (LXMRouter.py:222,
written at 581) until some peer syncs and pulls it. No per-message signal reaches the original
sender when that finally happens.

The peer-level sync bounds that exist, `MAX_UNREACHABLE = 14 days` and `SYNC_BACKOFF_STEP =
12 minutes` (LXMPeer.py:39, 45), describe whether a *peer* is considered alive. They are not
per-message delivery confirmation and cannot be used as one.

So propagated delivery has no bounded answer to "did the peer receive this." The sender gets a
success-shaped callback at the moment of node acceptance, which the gateway would record as
delivery. If the peer never returns, that record is false and nothing corrects it.

**There is no silent queue at the packet layer.** `Packet.send()` returns `False` immediately
when no interface can carry the packet (Packet.py:307-313), and `PacketReceipt.status` starts
at `SENT` (Packet.py:423). Queueing and retry are LXMF router behaviours, which is why the
five attempt window is the real bounding mechanism rather than any lower-layer buffer.

**The gateway's model, restated against this.**

`SendResult` is binary: `success`, `message_id`, `error`, `retryable`, `retry_after`,
`error_kind`, with no queued or pending state. The gateway treats acceptance as delivery and
maintains a SQLite delivery ledger (`delivery_ledger.py`: `record_obligation` at 266,
`mark_delivered` at 288, `mark_failed` at 292) that is finalised on `send()` return
(base.py:4095-4120).

For direct delivery, an adapter can map `DELIVERED` and `FAILED` onto that binary honestly.
The match is real, not a workaround.

For propagated delivery, the adapter cannot. It would return `success=True` on node
acceptance and the ledger would record an obligation as delivered that may never be. That is
exactly the failure mode the first draft described, and it survives this revision, scoped to
propagated messages.

## 5. What this means

The adapter approach is right, and it does not come free. Three of the bridge's hand-built
components map cleanly onto gateway machinery and can be deleted:

Session handling, because the gateway owns it.
Tool-call streaming, because the gateway emits tool events directly rather than the adapter
polling a database.
Clarify, because the gateway sets `agent.clarify_callback` in-process where a real user exists.

One does not map: the downlink queue. It is not redundant with gateway machinery, because the
gateway's machinery assumes a transport that tells you when things arrive. Directly, Reticulum
does tell you, and the adapter can be thin. Propagated, it does not, and the adapter owns
tracking the gateway has no vocabulary for.

The honest shape of the pivot is therefore conditional rather than absolute. If the deployment
delivers directly, most of the complexity is deleted and the delivery problem is a translation.
If it relies on a propagation node, the part that stays is the part solving a real problem
peculiar to radio, and it is a design task rather than a mapping.

## 6. Recommendation

Worth pursuing. The gating question from the first draft is answered, and it is narrower: not
"does Reticulum report delivery" but "does this deployment deliver directly or through a
propagation node."

Do this before writing adapter code:

Establish which delivery method the intended use carries. If peers are usually reachable,
direct delivery gives a bounded, honest confirmation and the adapter is a translation of
`PacketReceipt.DELIVERED` / `LXMessage.FAILED` onto `SendResult`. If the design leans on a
propagation node for store-and-forward, then the adapter must own a delivery ledger for the
node-to-peer leg, and that ledger is the design work.

Two closing checks on the direct path, both cheap: read `RNS.Link.TRAFFIC_TIMEOUT_MIN_MS` to
bound the link-transport receipt window, and trace `RNS.Transport.outbound()` to confirm the
no-path return path end to end. Neither changes the shape of the finding.

## 7. Open questions and unknowns

Whether the deployment's traffic is direct or propagated in practice. This now gates the design.
How the adapter should surface a propagated message whose peer never returns: a distinct state
the gateway lacks, or a failure after a chosen horizon.
Whether `send()` can return a value the gateway treats as "not yet resolved" without breaking
the ledger. Source says no such state exists in `SendResult`; confirm no other hook exists.
How the gateway behaves when an adapter never calls back about a message it accepted.
Whether the asyncio-to-callback bridge should be a thread, an executor, or a
`loop.add_reader` on Reticulum's file descriptor.

## 8. Invariants of the current adapter contract

A send result is binary: done or failed.
The gateway treats platform-API acceptance as delivery.
`connect()` returning True asserts that inbound will be pushed.

## 9. Corrections to the first draft

Kept deliberately, since the first draft circulated and its errors are instructive.

The claim that the gateway never learns whether a queued message was delivered or dropped was
wrong. Direct delivery yields a cryptographic receipt and a bounded callback; failure yields
`failed_callback` after five attempts. The claim held only for propagated delivery.

The claim of no callback from RNS to the gateway was wrong as an absolute.
`PacketReceipt.set_delivery_callback` and `LXMessage.register_delivery_callback` both exist
and fire.

What survives: the gateway has no queued-or-pending state, so *unbounded* delivery has no
honest representation. The gap is real, and it is about propagated delivery specifically.
