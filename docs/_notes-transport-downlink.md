# Transport (Reticulum/LXMF) + downlink queue notes for rns_hermes_endpoint

Scope: (1) the Reticulum/LXMF transport layer, (2) the downlink queue
internals (`src/hermes_reticulum/core/downlink.py`). Companion to
`_notes-turn-lifecycle.md` (turn flow, inbound ACL, outbound retry/pacing
overview) and `_notes-approval-steppush.md` (approval gate, step watcher).
Complementing those notes, this one goes deeper on what the bridge does
with the RNS/LXMF stack itself. All claims cite file:line.

Secret-handling rule for this notes file: this repo is public. Do NOT
write any RNS/LXMF identity hash (no full hash, no 8+ hex-char prefix),
no hostnames, no host:port endpoints. Peers are referred to as "peer A",
"the operator identity", etc. Values that are secrets live in gitignored
files; name the file, never the value.

## 1. Transport (Reticulum/LXMF)

### 1.1 How the bridge initialises Reticulum

`LXMFBridge.__init__` (bridge.py:145-223) and `LXMFBridge.start`
(bridge.py:277-333). Key pieces:

- `self.reticulum = RNS.Reticulum(self.rns_config_path)` (bridge.py:283).
  `rns_config_path` is passed as a constructor arg (bridge.py:151); when
  it is None (the default for this deployment), RNS uses its own default
  config directory. `expand_path` is imported at bridge.py:24 but is not
  applied to the RNS config path in the constructor (bridge.py:169); it
  is applied only to the LXMF storage path (bridge.py:164-166).
- The LXMF storage directory defaults to `~/.lxmf/storage` in the
  constructor (bridge.py:164-166), but the value that is actually used is
  the `RETICULUM_STORAGE` env var as set in `.env` (cli.py:394-411
  passes it as the `storage_path` kwarg). `expand_path` (utils/
  `__init__.py`:17-31) expands `~` and `$VARS` because `.env` values are
  read verbatim; without it the bridge would create a directory literally
  named `~` relative to the CWD and the identity + control token would
  end up in different places (utils docstring, lines 17-31). The storage
  dir is created on `start()` (bridge.py:281).
- LXMF router: `LXMF.LXMRouter(storagepath=..., enforce_stamps=...)`
  (bridge.py:287-290). `enforce_stamps` defaults to False in the
  constructor signature (bridge.py:150) and cli.py does not override it,
  so in this deployment the LXMF router does NOT drop invalid-stamp
  inbound messages (see section 1.2).
- Identity: stored at `self.storage_path / "hermes_identity"` (i.e.
  `~/.lxmf/storage/hermes_identity` in this deployment). If the file
  exists, `RNS.Identity.from_file` loads it; on failure (corrupt) a new
  identity is created and written back; otherwise a new identity is
  created and written (bridge.py:292-304). The loaded/new identity is
  used as the delivery identity.
- Delivery destination: `self.destination = self.router.
  register_delivery_identity(self.identity, display_name=...,
  stamp_cost=...)` (bridge.py:306-310). `stamp_cost` defaults to 8
  (bridge.py:149) and is a bandwidth throttle on the mesh.
- The bridge then registers its own inbound callback:
  `self.router.register_delivery_callback(self._on_lxmf_message)`
  (bridge.py:312). It also freezes the delivery hash at this point
  (bridge.py:320, comment at 314-319) so `/announce` and the periodic
  re-announce reference the exact destination that was registered
  (this was the fix for a 2026-09-15 identity/announce hash-drift bug).
- Stamp enforcement: `enforce_stamps` is passed to the LXMRouter but the
  inbound callback does NOT re-check it; the LXM router handles stamp
  validation internally (see the inbound section below).

Files on disk that hold identities and config, and gitignore status:
- `~/.lxmf/storage/hermes_identity` -- the RNS identity (gitignored:
  `.gitignore` has a `# Reticulum / LXMF runtime data` section listing
  `.lxmf/`).
- `~/.lxmf/storage/` -- the LXMF storage directory (gitignored via the
  same `.lxmf/` entry).
- `~/.reticular/` -- RNS's default config directory (gitignored via the
  `.reticular/` entry in `.gitignore`).
- The repo-local `.env` file (gitignored via the `.env` entry at
  .gitignore:24, under the `# Environment / secrets` section) carries
  operator-set overrides such as `RETICULUM_STORAGE`,
  `RETICULUM_STAMP_COST`, `HERMES_CHUNK_INTERVAL_MS`,
  `HERMES_DOWNLINK_ACK_TIMEOUT_S`, `RETICULUM_ANNOUNCE_INTERVAL`, and
  the ACL allowlist. Note: in this checkout there is no committed
  `.env` in the working tree (only `.env.bak.*` backup files, which are
  gitignored via the `*.bak` entry at .gitignore:52); the live `.env`
  lives on the operator's machine.
- The control-server token file (`control_token`, written by
  `ControlServer._persist_token` at control_server.py:145-153, mode
  0o600) lives inside the LXMF storage dir and is therefore also
  gitignored via the `.lxmf/` entry.
- No hostnames or host:port endpoints are stored in these files in a way
  that would be written into these notes; peers are discovered at runtime
  via RNS path/announce (see section 4).

### 1.2 Inbound: how a message arrives and is validated

Callback: `LXMFBridge._on_lxmf_message` (bridge.py:427-465), registered
at bridge.py:312. It runs on the RNS event-loop thread.

- Content extraction: `message.content_as_string()` if present, else
  `str(message.content)` (bridge.py:430-433).
- Sender: `source_hash = RNS.prettyhexrep(message.source_hash)`
  (bridge.py:435); a raw hex form is also derived (bridge.py:436-439)
  and handed to the worker.
- Signature: `message.signature_validated` is read and logged as
  "valid" or "invalid/unknown" (bridge.py:441) but is NOT enforced in the
  callback. Enforcement, if any, is delegated to the LXMF router via the
  `enforce_stamps` constructor flag (bridge.py:150, 287-290). A message
  with an invalid signature still reaches the handler; the handler chain
  (ACL) is the next gate.
- Where `signature_validated` actually comes from: it is set during
  `LXMessage.unpack_from_bytes` in LXMF/LXMessage.py:809-820. The
  message's Ed25519 signature (64 bytes, LXMessage.py:58) is validated
  against the source identity; `signature_validated` is True only if
  `source.identity.validate(signature, signed_part)` succeeds. If the
  source identity is unknown, it stays False with a debug log
  (LXMessage.py:815-817). So the bridge is seeing the result of a real
  cryptographic check, it is just choosing not to drop on failure.
- Where the stamp check happens: in `LXMF.LXMRouter.lxmf_delivery`
  (LXMF/LXMRouter.py:1837-1919). The router looks up the required stamp
  cost for the destination (LXMRouter.py:1861), validates the message's
  stamp against inbound tickets (LXMRouter.py:1863-1866), and, if the
  stamp is invalid: drops the message only if `self._enforce_stamps` is
  True (LXMRouter.py:1875-1877), otherwise logs "allowing anyway, since
  stamp enforcement is disabled" and lets it through (LXMRouter.py:1878-1879).
  In this deployment `enforce_stamps` is False (bridge.py:150 default,
  cli.py does not override it), so invalid-stamp messages are accepted
  and logged, not dropped. Blackholed sources are dropped earlier at
  LXMRouter.py:1841-1843, and already-received duplicate message hashes
  are dropped at LXMRouter.py:1906-1908 (dedup via
  `locally_delivered_transient_ids`).
- Delivery method: `message.method` is mapped to a name
  (opportunistic / link / propagated / unknown) and logged
  (bridge.py:442-451). This tells us whether the message came over a
  direct link, was propagated, or was opportunistic, but the callback
  does not branch on it.
- Profiling: `ChannelMetrics.from_lxmessage(message)` then
  `self.profiler.classify(metrics)` (bridge.py:453-454). The profile
  drives truncation/splitting and pacing downstream (see the outbound
  section of the turn-lifecycle notes).
- Fan-out: the raw sender `RNS.Destination` is captured as
  `source_identity = getattr(message, "source", None)` (bridge.py:457)
  and the message is submitted to the handler pool:
  `self._pool.submit(self._process_and_reply, source_hash_raw, content,
  profile, source_identity)` (bridge.py:458-460). If no handler or pool
  is set, the message is dropped with a warning (bridge.py:461-462).
- Exceptions inside the callback are caught and logged; the message is
  dropped (bridge.py:464-465).

### 1.3 Outbound: how a message is sent

`LXMFBridge.send_reply(recipient_hex, text, source_identity)`
(bridge.py:485-571). This is the atomic single-message send path used by
`push_reply` (bridge.py:239-271) for each chunk.

- Identity resolution: if `source_identity` is an RNS.Destination, its
  `.identity` is extracted (bridge.py:497-501). If it is None, the
  recipient hash is parsed and `RNS.Identity.recall` is tried
  (bridge.py:503-509). If recall returns None, the bridge calls
  `RNS.Transport.request_path(recipient_hash)` and polls `recall` up to
  8 times at 1s intervals (bridge.py:516-522). If the identity is still
  None after that, the send is refused and False is returned
  (bridge.py:524-529).
- Destination built for the send: `RNS.Destination(recipient_identity,
  RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")`
  (bridge.py:536-542).
- LXMF message built: `LXMF.LXMessage(dest, self.destination, text,
  desired_method=LXMF.LXMessage.DIRECT, include_ticket=True)`
  (bridge.py:544-550). `desired_method=DIRECT` means the router is asked
  to try a direct link first; if that fails, RNS/LXMF will fall back to
  propagation (or opportunistic, depending on router internals).
  `include_ticket=True` is what enables the per-message delivery
  callback to fire (see below).
- Per-message callback: `lxm.register_delivery_callback(lambda msg,
  _s=seq, _r=recipient_hex: self._on_outbound(_s, _r, msg))`
  (bridge.py:551-553). This is what gives us the "first-hop ack" signal:
  the callback fires when the LXMF router transitions the message to a
  terminal-ish state (SENT / DELIVERED / FAILED) and we record it in the
  downlink tracker.
- Dispatch: `self.router.handle_outbound(lxm)` (bridge.py:554). On
  success (no exception), the pacing clock is recorded and the seq is
  registered: `self.downlink.record_send(recipient_hex)` and
  `self.downlink.register_dispatch(seq, recipient_hex)` (bridge.py:557-558).
  On exception, the send is logged as failed and False is returned
  (bridge.py:566-571); the pacing clock is NOT advanced.
- What happens when a peer is unreachable: the bridge has no explicit
  "peer is down" path. Two distinct failure points:
  (a) Unknown identity: if `RNS.Identity.recall` returns None even after
      8 path-request polls, the send is refused before any LXMF message
      is built and False is returned (bridge.py:516-529).
  (b) Known identity, no usable path: the LXMF router's
      `handle_outbound` (LXMRouter.py:1746-1793) first checks the local
      path table; if the destination is unknown it requests a path
      (unknown_path_requested) and appends the message to
      `self.pending_outbound`, then spawns a thread that runs
      `process_outbound` (LXMRouter.py:1792-1793). So the message is NOT
      rejected immediately: it sits in the router's pending-outbound
      queue until a path appears (from a path response) or the router's
      own path-processing logic fails it. `process_outbound` then picks
      the best available path: with `desired_method=DIRECT` it prefers a
      direct link; if none exists the message falls back to propagation
      or opportunistic delivery per the router's internal selection
      (LXMRouter.py:1749-1793 plus the path-selection logic in
      `process_outbound`). A PROPAGATED-desired message with no outbound
      propagation node configured is failed and raises IOError
      (LXMRouter.py:1749-1751) -- the bridge never sends PROPAGATED-desired
      messages (it always uses DIRECT, bridge.py:544-550), so this path
      is not reachable from the bridge.
  - The per-message delivery callback then fires when the router reaches
    a terminal-ish state (SENT / DELIVERED / FAILED) and is recorded in
    the downlink tracker. If it never fires within `ACK_TIMEOUT_S`
    (default 300s, downlink.py:16), the lazy timeout sweep in
    `next_seq()` counts the seq as `timeout` (downlink.py:215-229).
    There is NO auto-retry at the bridge level: a timed-out or failed
    seq is counted and forgotten, not resent. (Note: the LXMF router
    itself may internally re-attempt delivery of a pending message while
    it is still in `pending_outbound` -- that is router-internal and not
    a bridge-level retry of a timed-out seq.)
- Verification of the full loop is done by tests/test_loopback_lxmf.py:
  a separate LXMF client attaches to the same shared RNS instance
  (abstract socket, test_loopback_lxmf.py:91-93), resolves the bridge
  destination via `RNS.Identity.recall` (the on-disk known_destinations
  store is loaded during `Reticulum()` init, test_loopback_lxmf.py:103-109),
  builds the OUT/SINGLE "lxmf" "delivery" destination
  (test_loopback_lxmf.py:118-124, APP_NAME must be lowercase "lxmf"),
  sends a DIRECT message with `include_ticket=True`
  (test_loopback_lxmf.py:142-149) and waits for the reply callback.
  This is the local oracle for the "reply stuck in propagation"
  symptom (docstring, test_loopback_lxmf.py:12).

### 1.4 Path / announce handling

Peer discovery and reachability are handled by RNS itself; the bridge's
role is to (a) make itself discoverable and (b) ask RNS for paths when
it needs to send.

- Self-announce: `LXMFBridge.announce()` (bridge.py:335-387) calls
  `self._do_announce()` (bridge.py:389-393) which does
  `self.destination.announce()`. This is the RNS-level "here is my
  destination" broadcast. `run_forever()` calls `announce()` once at
  startup (bridge.py:621).
- Periodic re-announce: `RETICULUM_ANNOUNCE_INTERVAL` (env, default
  30 minutes, bridge.py:197) drives a daemon timer thread
  (`_start_announce_timer`, bridge.py:395-406; loop at 408-417). The
  minimum allowed positive interval is `MIN_ANNOUNCE_INTERVAL_MIN`
  (1.0 min, bridge.py:51); values below that are rejected at startup
  (bridge.py:210-216) and at `announce()` time (bridge.py:360-364) to
  avoid flooding the mesh. 0 disables periodic re-announce (announce
  only at startup, bridge.py:384-387).
- Why periodic re-announce is needed (comment at bridge.py:42-46):
  "RNS drops destinations from other nodes' path tables over time, so a
  long-lived bridge must re-announce to stay discoverable." So peer
  discovery is not a one-shot: other nodes forget this bridge's
  destination after some time and the periodic re-announce keeps it
  fresh.
- How long discovery takes: there is no fixed "discovery time" in the
  bridge. `RNS.Transport.request_path` (RNS/Transport.py:3279-3322) is a
  BROADCAST: it builds a plain OUT destination for APP_NAME "path"
  "request" and sends a RNS.Packet with `transport_type=BROADCAST`
  (RNS/Transport.py:3298-3300). Any reachable peer that knows a path to
  the destination will announce it (docstring, RNS/Transport.py:3281-3283).
  The request tag and destination hash are recorded in
  `Transport.path_requests` (RNS/Transport.py:3321) so responses can be
  matched. The bridge's worst-case wait for a peer's identity to become
  known is the 8 x 1s path-request poll in `send_reply`
  (bridge.py:518-522): up to 8 seconds. If the peer is already in the
  RNS path table (or in the on-disk `known_destinations` msgpack store,
  loaded during `Reticulum()` init -- see test_loopback_lxmf.py:103-106),
  `RNS.Identity.recall` returns immediately with no broadcast at all.
- `RNS.Identity.recall` (the "recall" the bridge uses) resolves a
  destination hash to an Identity from the local path table / known
  destinations; it does not itself broadcast. The broadcast is the
  separate `request_path` call the bridge makes only when recall
  returns None (bridge.py:516).
- The `/announce` command (handled in cli.py) can override the live
  cadence for the current run; `announce(interval_min=...)` keeps the
  current live cadence when called with no argument
  (bridge.py:347-350, comment at 348-349).

## 2. Downlink queue (src/hermes_reticulum/core/downlink.py)

### 2.1 What the queue holds per recipient

`DownlinkTracker` (downlink.py:87-229). State (all under `self._lock`):

- `self._outbound: dict[int, tuple[str, float]]` (downlink.py:101) --
  maps seq -> (recipient_hex, dispatch_time_monotonic). This is the
  "pending" set: seqs that have been dispatched but have not yet
  received a first-hop ack callback. The recipient hex is "" until
  `register_dispatch` fills it in (see 2.2).
- `self._next_seq: int` (downlink.py:102) -- monotonically increasing
  outbound sequence counter.
- `self._last_send: dict[str, float]` (downlink.py:103) -- per-recipient
  clock of the last SUCCESSFUL send (used for pacing).
- `self._counters: dict[str, int]` (downlink.py:104-110) -- delivered,
  propagated, failed, timeout, unknown. Operator-visible via `stats()`.
- `self._push_counter: int` (downlink.py:111) -- monotonic per-push tag
  counter for multi-part pushes.
- `MAX_OUTSTANDING = 256` (downlink.py:97) -- hard cap on the number of
  seqs held in `_outbound`; when exceeded, the oldest seq is dropped
  (downlink.py:130-132).
- `self._ack_timeout_s` (downlink.py:112) -- from `ACK_TIMEOUT_S`
  (downlink.py:16, env `HERMES_DOWNLINK_ACK_TIMEOUT_S`, default 300s).

There is no separate "junk" bucket: junk (a seq that never got a
callback) is just a seq that stays in `_outbound` until either
`note_outcome` pops it or the lazy sweep counts it as `timeout` and
pops it (downlink.py:215-229). "Pending" and "junk-before-sweep" are the
same set, distinguished only by elapsed time.

### 2.2 How acknowledgements are matched

Matching is by seq number, not by recipient. The flow:

- On send, `next_seq()` allocates a seq and stores `("", now)` in
  `_outbound` (downlink.py:118-133). The recipient is not known yet
  because the seq is allocated BEFORE the send (so the dispatch time is
  recorded even if the callback never fires, bridge.py:531-533).
- After a successful `handle_outbound`, `register_dispatch(seq,
  recipient_hex)` fills in the recipient (downlink.py:135-141).
- The LXMF per-message callback `_on_outbound` (bridge.py:573-592)
  fires on the RNS event-loop thread. It maps the LXMF state to an
  outcome via `_state_name` / `_state_outcome` (downlink.py:23-38,
  46-51) and calls `downlink.note_outcome(seq, outcome)`
  (bridge.py:586).
- `note_outcome` (downlink.py:178-187) pops the seq from `_outbound`
  (idempotent: a second call for the same seq is a no-op because the
  seq is already gone) and increments the matching counter. So the
  first callback to arrive for a seq wins; later callbacks for the same
  seq (e.g. a delivery receipt plus a propagation receipt) are ignored.

### 2.3 What "lazily detected timeout" means (and the no-retry claim)

The lazy timeout is in `next_seq()` (downlink.py:118-133), which calls
`_sweep_timeouts_locked()` (downlink.py:215-229) on every allocation.
The sweep iterates `_outbound` and, for any seq whose dispatch time is
older than `self._ack_timeout_s`, pops it and increments the `timeout`
counter and logs an INFO line (downlink.py:220-228).

This means:
- A seq is NOT actively watched by a timer. It is only checked when the
  next `next_seq()` call happens (i.e. when the next outbound message is
  being prepared). If no outbound messages are sent for a long time, a
  stale seq can sit in `_outbound` well past `ACK_TIMEOUT_S` without
  being counted as a timeout, until the next send triggers a sweep.
- There is NO auto-retry. Confirmed: the sweep only pops and counts.
  There is no code path in `downlink.py` or `bridge.py` that resends a
  timed-out or failed seq. The `_on_outbound` callback maps to a terminal
  outcome and `note_outcome` pops; nothing schedules a resend. The
  turn-lifecycle notes say the same (downlink retry is "no automatic
  resend"); this is consistent with the code.

### 2.4 Pacing: per-recipient interval, where enforced, multi-part tagging

- Per-recipient interval: `MIN_CHUNK_INTERVAL_MS` (downlink.py:15, env
  `HERMES_CHUNK_INTERVAL_MS`, default 500ms).
- Enforcement: `pace_wait(recipient_hex, interval_ms)`
  (downlink.py:147-166). It reads `self._last_send[recipient_hex]` under
  the lock, computes how long to sleep, and sleeps on the CALLING thread
  (the pool worker, the step-watcher thread, or the ThreadingHTTPServer
  handler thread -- see the class docstring at downlink.py:88-95). The
  lock is held only for the state read, never across the sleep
  (downlink.py:157-165).
- The clock is updated by `record_send(recipient_hex)`
  (downlink.py:168-172), which `send_reply` calls only AFTER a
  successful `handle_outbound` (bridge.py:557). So a failed send does
  not consume pacing budget (class docstring, downlink.py:90-95;
  comment in `pace_wait`, downlink.py:151-153).
- Multi-part tagging: `push_reply` (bridge.py:239-271) splits the text
  with `split_message` (adapter.py:42-70), which already numbers parts
  `[i/total]` when there is more than one (adapter.py:65-68). If there
  is more than one part, `push_reply` then allocates a per-push tag via
  `downlink.next_push_tag()` (downlink.py:193-197, returns `p<N>`) and
  wraps each part with `[pN i/N]` via `sequence_chunks`
  (downlink.py:54-84). The prefix is never allowed to push a part over a
  single LXMF block boundary (368-byte budget, downlink.py:40-43,
  65-83); if it would, the PART is truncated on a UTF-8 codepoint
  boundary, not the prefix (downlink.py:66-83).
- Ordering: `push_reply` sends parts in order, pacing BEFORE each
  non-first send (bridge.py:264-270). So part 1 goes out immediately,
  then the worker sleeps until at least `MIN_CHUNK_INTERVAL_MS` has
  passed since the last successful send, then sends part 2, and so on.
  The `[pN i/N]` tags let a recipient spot a dropped tail without any
  protocol change (docstring, downlink.py:54-57).

### 2.5 What happens to a message that is never acknowledged

- The seq stays in `_outbound` until either (a) the lazy sweep pops it as
  `timeout` on the next `next_seq()` call (downlink.py:215-229), or
  (b) the `MAX_OUTSTANDING` cap (256) evicts the oldest seq
  (downlink.py:130-132) if a new seq is allocated while 256+ are
  outstanding.
- Operator-visible state: `stats()` (downlink.py:203-209) returns the
  counters (delivered, propagated, failed, timeout, unknown) plus
  `outstanding` (current size of `_outbound`) and `next_seq`. This is
  the only place the operator can see the downlink state; there is no
  per-message list of "what was never acked" beyond the counters and the
  INFO log line from the sweep (downlink.py:226-228).
- There is no cleanup thread, no retry, and no per-recipient backoff. A
  never-acked message is simply counted as `timeout` (eventually) and
  dropped from `_outbound`. The operator can see the aggregate `timeout`
  counter in `stats()` but not which specific messages were lost.

## 3. Files on disk / gitignore summary

- `~/.lxmf/storage/hermes_identity` -- RNS identity (gitignored, `.lxmf/`).
- `~/.lxmf/storage/` -- LXMF storage (gitignored, `.lxmf/`).
- `~/.reticular/` -- RNS default config dir (gitignored, `.reticular/`).
- `.env` -- operator overrides for announce interval, chunk interval,
  ack timeout, etc. (gitignored, `.env`).
- `~/.hermes/.reticulum-step-mode` and `~/.hermes/.reticulum-hold-state`
  -- step/hold mode state (written by the bridge, not gitignored because
  they live under `~/.hermes/`, not the repo).
- No identity hashes, hostnames, or host:port endpoints are written in
  these notes.

## 4. Open questions / things I did not verify

Resolved during this pass (no longer open):

- Where `enforce_stamps` is enforced and what it does: resolved by
  reading the LXMF router source. The stamp check happens in
  `LXMRouter.lxmf_delivery` (LXMF/LXMRouter.py:1837-1919); with
  `enforce_stamps=False` (this deployment) invalid-stamp inbound
  messages are logged and accepted, not dropped.
- The exact RNS/LXMF fallback behaviour when `desired_method=DIRECT`
  fails: resolved. `handle_outbound` queues the message in
  `pending_outbound` and spawns `process_outbound`, which selects a path
  (direct first, then propagation/opportunistic); only a PROPAGATED-
  desired message with no propagation node is failed immediately
  (LXMRouter.py:1746-1793).
- Whether `RNS.Transport.request_path` triggers a path broadcast:
  resolved. It is a BROADCAST packet to APP_NAME "path" "request";
  any peer that knows a path announces it (RNS/Transport.py:3279-3322).

Remaining (lower value, not needed for the bridge's behaviour):

- The exact internals of `process_outbound`'s path selection (hop count,
  egress-limit interaction) -- not needed to describe the bridge's
  contract, which is: send DIRECT, wait for the per-message callback,
  count outcomes lazily.
- The exact RNS announce interval / path-table expiry constants that
  determine "how long other nodes keep this bridge's destination" -- the
  bridge only documents that periodic re-announce is needed
  (bridge.py:42-46) and exposes `RETICULUM_ANNOUNCE_INTERVAL` to tune it.
