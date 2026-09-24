# Reticulum/LXMF fit against Hermes gateway platform-adapter model

Repo: a local Hermes agent checkout (read-only; do not edit)

## 1. Gateway assumptions from base.py

### Abstract methods (base.py:2595-2607)
- connect(is_reconnect=False) -> bool (line 2595)
- disconnect() (line 2600-2602)
- send(chat_id, content, reply_to=None, metadata=None) -> SendResult (line 2604-2607)
- get_chat_info(chat_id) -> Dict with at least name and type ('dm'|'group'|'channel') (line 4602-4605)

### connect() contract (base.py:2595-2598)
"Connect and start receiving; True on success. is_reconnect: the reconnect watcher is
re-establishing a dropped platform - adapters with a server-side update queue (Telegram)
must preserve it so outage-time messages are not discarded."

Assumption: connect() returns a boolean quickly; the gateway treats True as "platform is live
and will deliver inbound via handle_message". The docstring explicitly names Telegram's
server-side update queue as the motivating case - the gateway expects a persistent,
server-pushed connection. Reticulum has no such thing: it is a local RNS instance whose
delivery depends on radio paths that may not exist for hours. connect() returning True
would mean "RNS instance is up", not "any peer is reachable". The gateway has no concept of
"connected but no reachable peers" (see run_adapters.py reconnect watcher, line 648).

### send() contract (base.py:2604-2607, SendResult at 1608-1623)
send() returns SendResult(success, message_id, error, retryable, retry_after,
continuation_message_ids, error_kind). The gateway treats success=True as "delivered to
platform"; it does not wait for end-to-end delivery.

SendResult fields that matter:
- success: bool (line 1610)
- retryable: bool - "transient connection error - base retries automatically" (line 1616)
- retry_after: Optional[float] - "server-requested delay (Telegram FloodWait)" (line 1617)
- error_kind: one of SEND_ERROR_KINDS = too_long, bad_format, forbidden, not_found,
  rate_limited, transient, unknown (lines 1635-1636)

classify_send_error (line 1674-1678) maps exception text to those kinds via substring
matching (lines 1660-1671). The taxonomy assumes API-server-style failures: rate limits,
forbidden, not_found, too_long. There is no "queued" or "pending" state. A Reticulum send
that is accepted by the local RNS queue but not yet delivered to the peer has no
corresponding SendResult value. The closest fit is success=True (accepted by RNS), but the
gateway will treat that as delivered and not track further state.

### _send_with_retry (base.py:3501-3580)
Retries up to max_retries=2 on transient network errors (retryable=True, rate_limited,
retry_after set, or _is_retryable_error). Timeouts are NOT retried (line 3534-3536:
"timeouts: not safe to retry (may have delivered)"). For Reticulum, a send that sits in
the RNS queue for minutes before delivery is neither a timeout nor a transient error - it
is a normal store-and-forward delay. The retry logic would either (a) not fire at all if
send() returns success=True immediately on queue-accept, or (b) fire incorrectly if send()
blocks until actual delivery and times out.

### edit_message / delete_message (base.py:2618-2635)
Both have default no-op implementations (return success=False / False). The gateway
assumes platforms MAY support these but does not require them. Reticulum/LXMF has no
message-edit or delete API. The gateway handles this gracefully: "success=False makes
callers send anew" (line 2623-2624), and delete_message returning False means "callers
leave it" (line 2627-2628). No break here.

### handle_message (base.py:3890-3960)
The adapter calls self.handle_message(event) when a message arrives. The gateway then:
1. Checks for a message handler (line 3894)
2. Coerces plaintext gateway commands (line 3907-3908)
3. Drops unresolved events (line 3910)
4. Resolves session_key (line 3918)
5. Checks if session is already active - if so, routes to _handle_message_while_active
   which bypasses certain commands inline and queues the rest (line 3932-3960)
6. Otherwise starts session processing (line 3930)

Assumption: messages arrive as a stream with well-defined session keys derived from
chat_id + user_id. The session key is what the gateway uses for concurrency control
(one active session per key), pending-message queuing, and clarify round-trips. For
Reticulum, the chat_id would be an LXMF destination hash. Stability across reconnects
depends on whether the peer's identity (and thus its destination hash) is invariant.

### get_chat_info (base.py:4602-4605)
Must return a dict with at least name and type ('dm', 'group', 'channel'). For Reticulum
a 1:1 peer would be type='dm'. The name would have to come from somewhere - RNS/LXMF has
no built-in contact directory; the adapter would need to maintain a local mapping of
destination hash to display name. The gateway does not call get_chat_info on a hot path
during normal message flow (it is used for display and some routing), so staleness is
tolerable.

### build_source (base.py:4558-4600)
Builds a SessionSource with platform, chat_id, chat_name, chat_type, user_id, user_name,
thread_id, chat_topic, user_id_alt, chat_id_alt, is_bot, scope_id, guild_id, parent_chat_id,
message_id, role_authorized, auto_thread_created, auto_thread_initial_name. Many of these
fields are platform-specific (guild_id for Discord, scope_id for Slack). For Reticulum
most would be None. The key fields are platform, chat_id, chat_name, chat_type, user_id,
user_name. The adapter must supply sensible values. user_id_alt exists as a fallback for
platforms where the primary user_id is not a stable string - this is a natural fit for
LXMF destination hashes if the adapter treats them as user_id.

### Typing indicators / status text
No abstract method for typing indicators in BasePlatformAdapter. The capability flag
supports_status_text exists (found by earlier search) but is a class-level flag, not a
required method. Reticulum has no typing indicator concept. The gateway will simply not
show typing for this platform. No break.

### Threading model in base.py
Only two threading uses in base.py (found by search):
- Line 93: threading.BoundedSemaphore for media history lookup
- Line 2578: threading.Thread for media history lookup worker
These are internal to the base class, not a pattern the adapter is expected to follow.
The gateway is asyncio-based throughout. Adapters are expected to be async.

## 2. Chat-ID mapping

### Signal (signal.py:974)
get_chat_info returns name and type from the Signal account store. Chat IDs are Signal
group IDs or user IDs - stable, server-assigned, human-readable.

### Reticulum chat id
An LXMF destination is a hash derived from the sender's public key. It is:
- Stable as long as the peer keeps the same keypair (RNS identity)
- Changes if the peer rotates keys
- Not human-readable (a hash, not a name or email)

The gateway's chat_id is used as:
- Session key component (for concurrency: one active session per chat_id+user_id)
- Routing target for send()
- Key for cron delivery, user authorization, send_message routing
- System prompt hint (chat_name, chat_type)

For Reticulum, using the LXMF destination hash as chat_id is workable but:
- get_chat_info must resolve it to a name (adapter-side mapping required)
- If the peer rotates keys, the chat_id changes and the session key changes, breaking
  session continuity. The gateway has no concept of "chat_id migrated to a new value"
  for the same logical peer.
- user_id in build_source: for a 1:1 DM, user_id and chat_id may be the same hash.
  This is acceptable (Signal DMs work this way: user_id is the Signal user ID which
  equals the chat_id for DMs).

### ADDING_A_PLATFORM.md (read in full)
The document describes the plugin-system expectations:
- User authorization: keyed on chat_id (and optionally user_id)
- Cron delivery: targets a chat_id
- send_message routing: targets a chat_id
- System prompt hints: use chat_name and chat_type from get_chat_info

All of these work with a stable hash as chat_id, as long as the adapter can resolve it
to a name. The instability-on-key-rotation is the real risk.

## 3. Delivery semantics

### SendResult model (base.py:1608-1623)
The model is binary: success=True (delivered to platform API) or success=False (failed).
There is no intermediate "queued / pending delivery" state. The retryable flag covers
transient network errors that the base class will retry automatically (up to 2 retries
with exponential backoff, base.py:3501-3580).

### Reticulum store-and-forward
A send to an unreachable peer is queued by RNS and delivered when a path opens. This can
take minutes to hours, or never. The adapter has two choices:
(a) Return success=True immediately on queue-accept: the gateway considers it delivered
    and does not track further. If the message is never delivered, no one knows.
(b) Block in send() until RNS confirms delivery: this violates the asyncio model (send()
    is async and must not block for minutes), and the gateway's timeout handling
    (base.py:3534-3536) would treat a long wait as a timeout and return failure.

Neither option is clean. The model has no "accepted but not yet delivered" state. The
gateway's delivery ledger (referenced in comments at base.py:1626-1628) handles long
cooldowns by returning a typed failure and letting the ledger retry, but that mechanism
is designed for rate-limit backoffs (minutes), not multi-hour radio delivery delays.

### _send_with_retry behavior
- Retries on: retryable=True, rate_limited, retry_after set, _is_retryable_error
- Does NOT retry on: timeout (base.py:3534-3536, "may have delivered")
- Max 2 retries, base_delay=2.0s, exponential backoff (base.py:3501-3543)
- Server-requested retry_after overrides backoff, capped at 60s inline
  (base.py:3544-3552, _SEND_RETRY_INLINE_WAIT_CAP_SECS=60.0)

For Reticulum, the "transient" classification does not fit: the send is not transiently
failing, it is structurally delayed. The retry logic would either not fire (if
success=True on queue-accept) or misfire (if the adapter maps a radio-delay to a
transient error).

## 4. Connectivity and lifecycle

### connect() and the reconnect watcher (run_adapters.py:648)
_platform_reconnect_watcher is an async task that monitors adapter health and calls
connect(is_reconnect=True) when the adapter drops. The gateway expects:
- connect() returns True when the platform is reachable and will push inbound
- If connect() returns False, the watcher retries on a backoff schedule
- is_reconnect=True signals the adapter that this is a re-establish, not a first connect

For Reticulum:
- connect() would return True once the local RNS instance is initialized (regardless of
  whether any peer is reachable). This is a semantic mismatch: the gateway treats
  connect=True as "the platform connection is live", but for Reticulum it only means
  "the local RNS daemon is running".
- If the RNS process dies, the adapter's send() calls would fail. The reconnect watcher
  would call connect() again. This works if the adapter can re-initialize RNS.
- The gateway has no concept of "connected but no reachable peers". It does not poll
  for peer reachability. If a peer is unreachable for hours, the gateway does not know
  and does not do anything different.

### Can an adapter own a background thread?
The gateway is asyncio-based. Adapters are expected to be async. However, base.py does
use threading internally (line 93: BoundedSemaphore, line 2578: Thread for media history
lookup), so the codebase is not purely single-threaded. An adapter could run a background
thread that feeds an asyncio queue (the standard pattern for bridging callback-based
libraries into asyncio). No existing adapter in the repo appears to do this for a
non-asyncio transport (Telegram and Discord use their respective asyncio SDKs; Signal
uses a websocket). But the pattern is not forbidden by the gateway.

### is_reconnect semantics
The docstring for connect() (base.py:2596-2598) says: "adapters with a server-side update
queue (Telegram) must preserve it so outage-time messages are not discarded." For
Reticulum, RNS maintains its own queue during peer outages, so the is_reconnect flag is
less critical - the RNS queue preserves messages. But the gateway does not know about
this queue; it only knows whether connect() returned True.

## 5. Async fit

### Reticulum Python API
The Reticulum Python library (rns) uses a Reticulum instance with an event loop thread.
Callbacks (on_message, on_path, etc.) are invoked from the RNS event loop thread, not
from the asyncio event loop. The standard bridge pattern is:
1. Run the RNS event loop in a background thread (or subprocess)
2. RNS callbacks push events onto a thread-safe queue (queue.Queue or asyncio.Queue
   via loop.call_soon_threadsafe)
3. An asyncio task in the adapter drains the queue and calls handle_message

This pattern is well-established and does not conflict with the gateway's asyncio model.
The adapter's connect() would start the RNS thread and the asyncio drain task.
disconnect() would stop the drain task and the RNS thread.

### send() bridging
send() would call RNS's send API (which is synchronous/blocking or callback-based).
The adapter would use asyncio.to_thread() or a background thread to avoid blocking the
event loop. The challenge is that RNS send() for an unreachable peer may block until
delivery or until a timeout. The adapter would need to:
- Either return success=True immediately on queue-accept (losing delivery tracking)
- Or use asyncio.wait_for with a timeout and return a failure if delivery is not
  confirmed within the timeout (but this breaks for legitimately slow deliveries)

No existing adapter in the repo handles a transport where send() can take minutes.
The closest analog is Telegram's FloodWait handling (retry_after, base.py:1617),
but that is a server-imposed delay, not a transport-level store-and-forward delay.

### No break in asyncio model per se
The gateway does not require send() to complete in a bounded time. It is async and can
await indefinitely. The problem is not the asyncio model but the semantic model:
the gateway assumes send() returns when the platform API has accepted the message,
not when the end user has received it. Reticulum's queue-accept and end-user
receipt are separated by an unbounded time gap.

## 6. Verdict

### Assumptions that HOLD
- Discrete chat IDs: Reticulum can use LXMF destination hashes as chat_ids. Stable
  as long as peer keys do not rotate.
- Chat types (dm/group/channel): Reticulum 1:1 peers map to type='dm'. RNS has no
  group concept in LXMF (groups are possible in RNS but not standard). type='dm'
  is sufficient for the primary use case.
- Messages arrive in a stream: RNS delivers messages via callbacks; the adapter can
  bridge to asyncio and call handle_message. This works.
- Reply can be sent at any time: RNS is always "connected" once the local instance
  is up. send() can be called at any time. This holds.
- edit_message / delete_message not required: gateway handles absence gracefully.
- Typing indicators not required: no abstract method; capability flag defaults to False.
- Adapter can own a background thread: not forbidden; the standard bridge pattern
  works.

### Assumptions that are STRAINED
- connect() semantics: the gateway treats connect=True as "platform is live and will
  push inbound". For Reticulum, connect=True only means "local RNS instance is running".
  The gateway cannot distinguish "RNS is up but no peers reachable" from "RNS is up
  and peers are reachable". This is a semantic gap but not a hard break: the gateway
  will simply not send messages it thinks will be delivered, and inbound will flow
  when peers become reachable. The practical effect is that the gateway has no way
  to report "mesh peer offline" to the user.
- Chat ID stability: if a peer rotates keys, the LXMF destination hash changes and
  the session key changes, breaking session continuity. The gateway has no mechanism
  for chat_id migration. The adapter would need to maintain a mapping from old hash
  to new hash, but the gateway does not support this.
- get_chat_info must resolve name: the adapter must maintain a local mapping of
  destination hash to display name. This is workable but is adapter-side state that
  the gateway does not manage.

### Assumptions that GENUINELY BREAK
- Delivery semantics: SendResult has no "queued / pending delivery" state. Reticulum's
  store-and-forward model means send() success=True does not mean delivered. The
  gateway treats success=True as delivered and does not track further. There is no
  mechanism for the gateway to know a message was queued but never delivered. This is
  a structural gap: the gateway's entire delivery model (send, retry, delivery ledger)
  is built on the assumption that "accepted by platform API" is a reasonable proxy
  for "delivered to user". Reticulum breaks this assumption.
- send() timeout behavior: if the adapter blocks in send() until RNS confirms delivery,
  a legitimately slow delivery (minutes to hours) would either time out (the gateway
  would treat it as a failure) or block the asyncio task for an unbounded time. If the
  adapter returns success=True on queue-accept, the gateway loses all delivery tracking.
  There is no clean solution within the existing adapter contract.
- Reconnect / lifecycle: the gateway's reconnect watcher (run_adapters.py:648) expects
  connect() to reflect actual platform reachability. For Reticulum, connect() would
  return True even when no peers are reachable, which is a false positive in the
  gateway's model. The gateway would not trigger reconnect logic for "no peers
  reachable" because from its perspective the platform is still "connected".

### Workability
The delivery-semantics break is the hardest. It is structural: the gateway's SendResult
model, _send_with_retry logic, and delivery ledger are all built around the assumption
that platform-API acceptance approximates user delivery. An adapter-side shim could
partially mitigate this by:
- Returning success=True on queue-accept (losing delivery tracking, but keeping the
  gateway from blocking)
- Maintaining its own delivery ledger inside the adapter (independent of the gateway's)
- Periodically reporting undelivered messages back to the gateway via a custom mechanism

But none of these are clean: they require the adapter to maintain state the gateway does
not know about, and the gateway will not act on that state. The gateway's clarify
round-trip, approvals, and session state machinery all assume that outbound messages
are reliably delivered, which Reticulum cannot guarantee on a timely basis.

The chat-ID stability issue is workable with an adapter-side mapping table, but it
breaks session continuity on key rotation in a way the gateway cannot recover from.

The connect() semantic gap is the most benign: it is a false positive in the gateway's
reachability model, but the practical effect is limited to the gateway not knowing
when peers are offline.

Overall: Reticulum/LXMF can be made to work as a platform adapter for basic 1:1 text
messaging, but the delivery-semantics gap is structural and cannot be fully closed
without changes to the gateway's SendResult model or the delivery ledger. The adapter
would be "connected" in the gateway's sense but unreliable in the user's sense, and
the gateway has no mechanism to surface that unreliability to the user.

## Appendix: capability flags (base.py:1826-1865)

- supports_code_blocks: bool = False (line 1826)
- supports_status_text: bool = False (line 1828)
- supports_async_delivery: bool = True (line 1843) - "can wake a fresh turn AFTER a turn ends
  (detached-subagent completions); False for stateless adapters (API server). Propagated to
  HERMES_SESSION_ASYNC_DELIVERY so tools never promise a delivery they can't keep."
  For Reticulum: True is correct - the mesh can deliver a message that starts a new turn
  even after the previous turn ended. The constraint is the same as for all platforms:
  the message must actually arrive.
- splits_long_messages: bool = False (line 1845) - the gateway's router truncates to
  MAX_MESSAGE_LENGTH (default 4096, line 1942-1946). For Reticulum, the adapter should
  set this to True and implement truncation in send() via truncate_message(), because
  LXMF messages over lossy radio benefit from being split. Alternatively the adapter
  can leave it False and accept that the gateway truncates to 4096 chars.
- typed_command_prefix: str = "/" (line 1847)
- supports_inchannel_continuable: bool = False (line 1851)
- interactive_resume: bool = True (line 1860)
- serves_profile_prefix: bool = False (line 1865)
- REQUIRES_EDIT_FINALIZE: bool = False (line 2610)

## Appendix: max_message_length (base.py:1942-1946)
Default MAX_MESSAGE_LENGTH is 4096 chars. The relay adapter overrides it. For Reticulum,
4096 is a reasonable upper bound per LXMF message over radio; the adapter can leave it
at the default or lower it to match the radio link's practical MTU.

## Appendix: reconnect watcher details (run_adapters.py:648-673)
_platform_reconnect_watcher is an async task that:
1. Waits 10s initial delay (line 661)
2. Loops: if no failed platforms, idle 30s (line 664)
3. If failed platforms exist, iterates over _failed_platforms dict and calls
   _reconnect_failed_platform for each (line 668-671)
4. Re-checks every 10s (line 672)
Backoff is 30s to 300s cap (line 649 docstring). Retryable failures retry forever
(self-heal); non-retryable drop out. Pausing is manual only (/platform pause).

The watcher tracks failed platforms in _failed_platforms dict. A platform enters this
dict when connect() returns False. For Reticulum, connect() would return True even when
no peers are reachable (the local RNS instance is up), so the platform would never enter
_failed_platforms, and the watcher would never retry it. This is the false-positive gap:
the gateway has no mechanism to detect "connected but no peers reachable" and trigger
reconnect or alert the user.

## Appendix: is_reconnect in connect() docstring (base.py:2596-2598)
"is_reconnect: the reconnect watcher is re-establishing a dropped platform - adapters with
a server-side update queue (Telegram) must preserve it so outage-time messages are not
discarded."

For Reticulum, RNS maintains its own delivery queue during peer outages. The is_reconnect
flag is less critical because RNS preserves messages locally. But the gateway does not
know about this queue; it only knows whether connect() returned True. If the adapter
returns True on initial connect and the RNS process later dies, the reconnect watcher
would call connect(is_reconnect=True). The adapter would re-initialize RNS, and RNS would
resume its queue. This works, but only if the adapter's connect() is idempotent and can
re-initialize RNS from scratch.

## Appendix: delivery ledger (gateway/delivery_ledger.py)

The gateway has a SQLite-backed delivery ledger (delivery_ledger.py, 565 lines) that tracks
outbound messages that have been sent but not yet confirmed delivered. Key functions:

- compute_obligation_id (line 260): derives a unique ID from session_key + message_ref + content
- record_obligation (line 266): records a new delivery obligation in the ledger
- mark_attempting (line 284): marks the obligation as being attempted
- mark_delivered (line 288): marks the obligation as delivered
- mark_failed (line 292): marks the obligation as failed with an error string
- sweep_recoverable (line 342): periodic sweep that retries failed obligations
- sweep_failed_for_runtime (line 420): runtime-specific failure sweep
- pending_retries (line 479): lists obligations pending retry
- ledger_enabled (line 526): checks if the ledger is active (config-gated)

The ledger is invoked from base.py at lines 4070-4093 (record + mark_attempting) and
4095-4120 (finalize: mark_delivered or mark_failed). The flow:

1. _record_delivery_obligation: called before send() completes. Records the obligation
   with platform, chat_id, thread_id, content. (base.py:4070-4093)
2. _finalize_delivery_obligation: called after send() returns. If success=True, calls
   mark_delivered. If success=False, calls mark_failed with the error string.
   (base.py:4095-4120)

This is significant for Reticulum: the ledger tracks delivery at the platform-API level,
not the end-user level. For Telegram, mark_delivered means "Telegram API accepted the
message" - which is a good proxy for "user will see it." For Reticulum, if the adapter
returns success=True on queue-accept, mark_delivered is called, and the ledger considers
the obligation complete. If the message is never delivered by RNS, the ledger does not
know. There is no callback from RNS to the gateway when a queued message is finally
delivered (or dropped).

The ledger does have a retry mechanism (sweep_recoverable, line 342) that can re-attempt
failed obligations. But this is triggered by mark_failed, not by "no delivery confirmation
received." For Reticulum, the adapter would need to:
(a) Return success=False with a transient error when the peer is unreachable, triggering
    the ledger's retry sweep. But this means the gateway will retry the send, which may
    cause duplicate messages when the peer becomes reachable and both the queued and
    retried copies are delivered.
(b) Return success=True on queue-accept and accept that the ledger will never know if
    the message was actually delivered.

Neither is clean. The ledger model is designed for API-based platforms where acceptance
by the server is a reliable signal of eventual delivery. Reticulum's store-and-forward
model breaks this assumption.

## Appendix: threading in existing adapters

- api_server.py: uses threading.Thread (line 605) for a turn-process reaper, and
  threading.Lock (line 614) for epoch counters. Also uses asyncio.to_thread (line 186)
  for credential verification.
- signal.py: uses asyncio.to_thread (line 591) for audio remuxing. No background threads.
- webhook.py: 7 references (likely asyncio.to_thread for callback processing).
- bluebubbles.py: 2 references (likely asyncio.to_thread).

No existing adapter runs a persistent background thread for its transport. The closest
pattern is api_server.py's use of threading.Thread for a one-shot background task
(turn reaper), not a long-lived transport thread. A Reticulum adapter would need to run
a persistent background thread for the RNS event loop, which is a new pattern for this
codebase but not forbidden by the gateway.

## Appendix: wake mechanism (gateway/wake.py)

The gateway has a wake mechanism (wake.py, 256 lines) for delivering background completion
events to existing sessions. The key function is deliver_wake (line 91):

- If supports_async_delivery=True (the default): creates a synthetic MessageEvent
  (internal=True) and calls handle_message on the adapter. The adapter's handle_message
  processes it like any inbound message, and the gateway's normal outbound path sends
  the reply to the user via send().
- If supports_async_delivery=False (API server): self-POSTs to the API server's
  /v1/chat/completions endpoint with the raw session id.

For Reticulum, supports_async_delivery=True is the correct setting: the adapter can
receive a synthetic MessageEvent and send the reply via RNS. The mechanism works
identically to how it works for Telegram or Signal. The constraint is the same as for
all platforms: the message must actually be delivered via send(). If the peer is
unreachable, the send is queued by RNS and may be delivered later. The gateway does not
track this.

The wake mechanism is relevant because:
1. Cron jobs, detached subagent completions, and background process completions all use
   deliver_wake to notify the user.
2. For Reticulum, these notifications would be queued by RNS if the peer is offline,
   and delivered when the peer comes back. This is actually a feature: the user gets
   the notification when they next connect, rather than missing it entirely.
3. But the gateway does not know the notification was delayed. The delivery ledger
   marks it as delivered (success=True on queue-accept), and the user may not see it
   for hours. There is no mechanism to tell the user "this notification was delayed
   because your mesh peer was offline."

## Final summary of key file:line citations

- base.py:2595 - connect() abstract method
- base.py:2596-2598 - connect() docstring (is_reconnect, server-side update queue)
- base.py:2604-2607 - send() abstract method
- base.py:4602-4605 - get_chat_info abstract method
- base.py:1608-1623 - SendResult dataclass
- base.py:1635-1636 - SEND_ERROR_KINDS tuple
- base.py:1674-1678 - classify_send_error function
- base.py:1826-1865 - capability flags
- base.py:1843 - supports_async_delivery flag
- base.py:1942-1946 - max_message_length_for_chat (default 4096)
- base.py:2618-2624 - edit_message default (returns success=False)
- base.py:2626-2635 - delete_message default (returns False)
- base.py:3501-3580 - _send_with_retry
- base.py:3534-3536 - timeout not retried
- base.py:3890-3960 - handle_message
- base.py:4070-4093 - delivery ledger record
- base.py:4095-4120 - delivery ledger finalize
- base.py:4558-4600 - build_source
- run_adapters.py:648-673 - _platform_reconnect_watcher
- wake.py:91-116 - deliver_wake
- delivery_ledger.py:260-296 - ledger functions
- signal.py:974 - Signal get_chat_info
