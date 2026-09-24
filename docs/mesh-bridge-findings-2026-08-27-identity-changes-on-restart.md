# Findings: "Keeps changing identities after restarts" — root cause is a CLI hash-convention bug, NOT key rotation (2026-08-27)

## Symptom
User reports the Hermes-for-Reticulum bridge "keeps changing identities after
restarts" and that two destination hashes have each, at some point, been
messageable. (The two hashes pasted in the report were byte-identical —
one delivery hash, twice — so there is really one live address.)

## Root cause — a CLI display bug, not identity rotation
The on-disk identity `~/.lxmf/storage/hermes_identity` has **never** rotated:
- journald, every bridge start Aug 1 → Aug 27 (~20 restarts): Aug 1 15:45 logged
  `Created new identity`; **every** start since logged `Loaded existing identity`
  and announced the same destination.
- The identity file mtime is Jul 18 (untouched).

The confusion comes from **three different 32-byte hashes for the same key**,
produced by mixing up what a "destination" is:

| Hash | What it actually is |
|---|---|
| `<redacted-1>` | **identity** hash (`identity.hash`) — what `status`/`address` *used to* print. **Not** a valid LXMF delivery address. |
| `<redacted-2>` | **delivery destination** = `RNS.Destination(identity, IN, SINGLE, "lxmf", "delivery").hash`. **This is the real, current, live address the bridge answers at.** |
| `<redacted-3>` | The value the *running* bridge logs as "Announced destination". A different rendering the live process emits. |

Live recompute from the on-disk identity (2026-08-27, from the project venv):
```
RNS.Destination(identity, IN, SINGLE, "lxmf", "delivery").hash
  -> <redacted-2>
RNS.Destination(identity, IN, SINGLE, "lxmf").hash
  -> <redacted-4>
RNS.Destination(identity, IN, SINGLE, "lxmf", "propagation").hash
  -> <redacted-5>
identity.hash -> <redacted-1>
```
So the delivery hash (redacted-2) is **not** stale — it is the live delivery
address. The bridge has
been answering there the whole time; the *CLI* was the thing that was wrong.

## What was wrong
`cli.py` `cmd_address` / `cmd_status` printed `RNS.prettyhexrep(identity.hash)`
— the **identity** hash — and labeled it "LXMF Address". A client who dialed the
identity hash (redacted-1) got nothing, while the real delivery hash (redacted-2)
worked. Combined with the running bridge's announce log showing a *third*
rendering (redacted-3), it read as "the identity changes on every restart."

## Fix (applied 2026-08-27)
`cli.py`: added `_delivery_address(storage)` which computes the delivery
destination exactly as `LXMRouter.register_delivery_identity` does
(`RNS.Destination(identity, IN, SINGLE, "lxmf", "delivery")`) and prints
`RNS.prettyhexrep(destination.hash)`. Both `cmd_address` and `cmd_status` now
use it. Verified:
- `hermes-reticulum address` → `LXMF Address: <redacted-2>`
- `hermes-reticulum status`  → `Address:   <redacted-2>`

**Do NOT print `identity.hash` as the LXMF address** — it is not the address a
client should message.

## Open item worth an upstream PR
The **running** bridge's `announce()` logs `self.address`, and the observed
live value (redacted-3) differs from the offline-computed delivery hash
(redacted-2). That is the one remaining discrepancy to reconcile: confirm which
app/aspect the live `register_delivery_identity` destination actually uses, and
make `LXMFBridge.address` (bridge.py) and the CLI agree on a single canonical
delivery hash so the announce log, the CLI, and the on-mesh contact all show the
same value. Until then, the on-mesh contact that actually receives replies is
the delivery hash (redacted-2).

## Files
- `src/hermes_reticulum/cli.py` — `_delivery_address()` (new); `cmd_address`,
  `cmd_status` now print the delivery hash.
- `src/hermes_reticulum/core/bridge.py` — `address` property returns
  `RNS.prettyhexrep(self.destination.hash)`; `announce()` logs it. (The live
  value to reconcile per the open item.)
- Prior: `docs/mesh-bridge-findings-2026-08-20.md` §2 (same "two renderings of
  one identity" trap, first noticed).
