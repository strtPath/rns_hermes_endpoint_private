---
title: Inbound and outbound callbacks shared one slot, so every reply echoed back in
date: 2026-09-25
status: fixed
---

# The echo defect: one callback slot, two incompatible jobs

## Symptom

The mesh carried traffic (announces, path requests, and probes sent straight to
the phone all worked), but a message FROM the peer produced nothing usable, and
replies behaved unpredictably. The user located it first, in words worth keeping:

> "There's something between RNS sending and receiving messages and Hermes's
> receiving and sending messages. It seems there's something broken there. Your
> probes and test messages directly to my phone are working, but it doesn't work
> when I try to message you or when you're not invoking it through software,
> instead of letting our adapter handle it."

The distinction is the whole diagnosis: probing `send_to` directly worked,
letting the adapter own the round trip did not. That points at the JOIN between
the transport and the adapter, not at either side.

## Root cause

`ReticulumTransport` had ONE field, `_delivery_cb`, serving two jobs that carry
different data:

| Job | Fires when | Carries |
|---|---|---|
| Inbound | a peer messages US | source hash + payload |
| Outbound outcome | a message WE sent settles | payload + terminal state |

`register_delivery_callback` set that field to the adapter's INBOUND handler.
Then `send_to` registered the per-message delivery callback for our own outgoing
message into **that same field**:

```python
# the defect
if self._delivery_cb is not None:
    lxm.register_delivery_callback(
        lambda msg, _cb=self._delivery_cb: _cb(self._extract_inbound(msg))
    )
```

So when a reply reached the peer, the router fired the adapter's inbound
handler with OUR OWN message, and `_extract_inbound` parsed it as if a peer had
sent it. Every reply echoed back into the inbound queue, addressed from a
destination that is not the peer.

## Proof, before the fix

One live send, watching both slots:

```
sending one message OUT to the phone...
send_to -> True
INBOUND handler fired 1 time(s) from an OUTBOUND send
  payload: ('<router identity hash>', 'echo-slot test, ignore')
```

The inbound handler received the text we had just sent, under the router's own
identity. Not a theory about a mechanism: the payload is right there.

## The fix: two slots, mirroring the bridge

The old bridge had this right all along, which is why it was the reference:

- `router.register_delivery_callback(self._on_lxmf_message)` -> inbound
- `lxm.register_delivery_callback(lambda: self._on_outbound(seq, recipient, msg))` -> outcome

Two different functions. The adapter had collapsed them because nothing in it
corresponded to `_on_outbound`.

The transport interface now declares both explicitly, so the ambiguity cannot
return:

- `register_inbound_callback(cb)` -> `cb(source_hash, payload)`; fed only by
  `_on_router_delivery`.
- `register_outbound_callback(cb)` -> `cb(payload, state)`; fed only by
  `_on_message_outcome`, attached to the per-message callback of a message we
  sent.
- `register_failed_callback` unchanged.

The adapter registers both in `connect()` and routes them apart in the drain
loop: a `("outbound", payload, state)` tuple goes to
`_handle_outbound_outcome`, everything else to `_dispatch_inbound`.
`_last_outbound_chat` records the peer of the most recent send so an
asynchronous receipt can be matched back to it.

## Proof, after the fix

```
sending one message OUT...
send_to -> True
INBOUND slot fired 0 time(s)      <-- was 1
OUTBOUND slot fired 1 time(s)
    payload='post-fix echo check' state=DELIVERED
```

And inbound still works, with the text intact:

```
inbound slot received: [('abab...ab', 'hello from a peer')]
```

`DELIVERED` is worth noting. It is a cryptographic proof from the destination,
not router acceptance, so direct delivery to the peer is now CONFIRMED rather
than inferred. `send_to` returning True remains only acceptance; the outcome
callback is the delivery signal.

## A second invented API, caught in the same pass

The first draft of `_on_message_outcome` called
`LXMF.LXMessage.state_name(message.state)`. **That method does not exist.**
LXMF's `LXMessage` declares state CONSTANTS only (`LXMessage.py:14-23`); there is
no name function anywhere in the package. The bridge maps states with its own
`_state_name` from `core/downlink.py`.

This is the same class of defect as the `message_for_destination` incident: a
plausible-looking library call that nothing tests, because the fake agreed with
it. It survived one edit only because a test compared against the fake's
lowercase name and failed. The fix moves both directions of the mapping into
`plugin/delivery.py` (`state_name`, `state_from_name`) and passes the INTEGER
state across the interface. `state_from_name` returns `STATE_FAILED` for an
unrecognised name, so an unclassifiable state can never read as delivered.

## Why the tests did not catch this

`test_send_to_builds_message_and_hands_to_router` asserted the bug as correct:

```python
msg.delivery_callback(_FakeInbound())
assert received == [(FAKE_HASH2, "ping")]     # 'received' was the INBOUND list
```

It fired an outgoing message's callback and asserted it landed in the inbound
list. The test encoded the defect, so a green suite said nothing. The rewritten
test asserts the opposite and names the reason, plus that a genuine router
callback still reaches inbound.

Non-vacuity, both directions:
- reintroducing the old `_delivery_cb` wiring fails the transport test;
- collapsing the two registrations fails four adapter tests.

## Operational consequence

Inbound traffic was sharing a channel with our own outbound traffic. A peer's
message and our echo occupied the same queue with the same shape, so the
allowlist saw an unknown sender and dropped it, or the session picked up our own
text. Neither produced an error line, because both look like ordinary inbound.

## Test-isolation note

`test_unset_defaults_to_auto` passed before the propagation pin and failed after
it, with no code change to the reader: `.env` now sets
`RETICULUM_PROPAGATION_NODE` on this machine, and the test fell through to the
live environment. It now clears the variable explicitly. A default-value test
that reads the real environment asserts the machine, not the code.
