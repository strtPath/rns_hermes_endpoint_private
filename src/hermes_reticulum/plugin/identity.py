"""
Identity & name mapping for the Reticulum platform adapter (spec section 4).

The gateway keeps no hash-to-name mapping and RNS has no contact directory,
so the adapter owns one: a plain hash → display-name map. ``get_chat_info``
is not a hot path (spec section 4), so staleness is tolerable — no caching
or invalidation machinery here.

Also carries the two validation predicates the adapter needs:

- ``is_valid_destination`` — a valid LXMF destination is exactly 32 hex
  chars (either case). Written here rather than imported from
  ``core.acl`` because the ACL is a live-bridge module (env loading at
  import time, blocklist semantics); the logic is equivalent to the
  ACL's 32-hex check (``core.acl``:``_parse_hash_set`` / ``_normalize_hash``)
  but self-contained: never raises, always returns a bool.
- ``is_parseable_inbound`` — spec section 14: a message whose source hash
  is not 32 hex, whose payload is not a decodable text, or whose peer is
  not in the allowlist must be DROPPED (log line, no reply, no crash of
  the drain loop). The adapter applies this in ``_normalize_inbound`` /
  ``_dispatch_inbound`` by returning ``(None, None)``.
"""

import logging
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger("hermes_reticulum.identity")

# RNS truncated hash: 16 bytes = 32 hex chars (same constant as core.acl).
DESTINATION_HEX_LEN = 32
_HEX_CHARS = frozenset("0123456789abcdef")


def is_valid_destination(destination_hash: Any) -> bool:
    """True iff ``destination_hash`` is exactly 32 hex characters.

    Accepts either case. Rejects: non-strings, empty strings, wrong
    length, any non-hex character. NEVER raises — always returns a bool.
    Matches the behaviour of the mesh ACL's hash validation
    (``core.acl.AccessControl._parse_hash_set``): 32 hex chars, lowercased,
    nothing else.
    """
    if not isinstance(destination_hash, str):
        return False
    if len(destination_hash) != DESTINATION_HEX_LEN:
        return False
    return all(c in _HEX_CHARS for c in destination_hash.lower())


def _normalize_for_acl(destination_hash: Any) -> str:
    """Normalise a hash the way the ACL does (lowercase, strip colons/spaces).

    A hash that is valid after colon/space stripping is still valid for the
    adapter's map lookups, which key on the raw (unstripped) value — so this
    helper is only used for the *predicate* check, not for map keys.
    """
    if not isinstance(destination_hash, str):
        return ""
    return destination_hash.strip().lower().replace(" ", "").replace(":", "")


def is_allowed_destination(destination_hash: Any, allowlist: Iterable[str]) -> bool:
    """True iff the destination is a valid 32-hex hash AND in ``allowlist``.

    ``allowlist`` entries are compared after ACL-style normalisation
    (lowercase, colon/space stripped) so an operator-pasted
    ``"AA:BB:..."`` form still matches. An empty allowlist means
    deny-by-default (matching ``core.acl`` allowlist mode when allow-all
    is off).
    """
    if not is_valid_destination(destination_hash):
        return False
    normalized = _normalize_for_acl(destination_hash)
    return any(_normalize_for_acl(h) == normalized for h in allowlist)


def is_parseable_inbound(
    event: Any,
    allowlist: Optional[Iterable[str]] = None,
) -> bool:
    """Spec section 14 malformed-inbound predicate.

    Returns False (drop) when ANY of the following holds:
      - the source/destination hash in ``event`` is not 32 hex chars;
      - the payload field is missing, empty, or not a str;
      - ``allowlist`` is provided and the source hash is not in it.

    Returns True when the event carries a well-formed (hash, text) pair
    and either no allowlist was given or the source is in it.
    NEVER raises — safe to call on hostile input.
    """
    # Extract (source_hash, text) the same way the adapter does.
    if isinstance(event, dict):
        source_hash = event.get("source") or event.get("destination")
        text = event.get("text") or event.get("payload")
    elif isinstance(event, (tuple, list)) and len(event) >= 2:
        source_hash, text = event[0], event[1]
    else:
        return False

    if not is_valid_destination(source_hash):
        return False
    if not isinstance(text, str) or not text:
        return False
    if allowlist is not None and not is_allowed_destination(source_hash, allowlist):
        return False
    return True


# ── Hash → display-name map ────────────────────────────────────────────────


class IdentityMap:
    """Adapter-owned hash → display-name map (spec section 4).

    Not a hot path; no invalidation or caching beyond the plain dict.
    Thread-safety is not a concern: all mutations happen on the asyncio
    loop thread (announce callbacks are queued, not fired in-place).
    """

    def __init__(self, seed: Optional[Dict[str, str]] = None):
        self._names: Dict[str, str] = dict(seed or {})

    # ── Write ────────────────────────────────────────────────────────────

    def set_name(self, destination_hash: str, display_name: str) -> None:
        """Store ``display_name`` for ``destination_hash``.

        Does not validate the hash — the adapter gates sends separately;
        this is a pure map. Overwrites any existing entry.
        """
        if isinstance(destination_hash, str) and isinstance(display_name, str):
            self._names[destination_hash] = display_name

    def seed(self, entries: Dict[str, str]) -> None:
        """Merge a config-provided dict into the map (existing keys win
        if a later ``set_name`` has already populated them — this is a
        merge, not a replacement)."""
        for k, v in entries.items():
            if isinstance(k, str) and isinstance(v, str):
                self._names.setdefault(k, v)

    # ── Read ─────────────────────────────────────────────────────────────

    def get_name(self, destination_hash: Any, fallback: Optional[str] = None) -> str:
        """Look up the display name for ``destination_hash``.

        Fallback (documented behaviour for unknown/invalid hashes):
        if ``fallback`` is given, return it; otherwise return the raw
        hash string itself (so ``get_chat_info`` can always produce a
        non-empty name without raising).
        """
        if not isinstance(destination_hash, str):
            return fallback if fallback is not None else ""
        return self._names.get(destination_hash, fallback if fallback is not None else destination_hash)


# ── Display-name decode from LXMF announce app_data (spec section 12) ─────


def decode_display_name(app_data: Any) -> Optional[str]:
    """Decode a peer's display name from an LXMF announce ``app_data`` blob.

    Delegates to ``LXMF.display_name_from_app_data`` when the LXMF package
    is importable; otherwise falls back to a minimal direct decode
    (raw UTF-8 bytes → string, NUL-stripped) so the adapter can still
    populate the name map in environments where LXMF is not installed.
    Returns ``None`` when no name can be extracted. NEVER raises.
    """
    try:
        import LXMF
        return LXMF.display_name_from_app_data(app_data)
    except ImportError:
        # LXMF not available: try a plain decode of raw bytes.
        try:
            if app_data is None or len(app_data) == 0:
                return None
            return app_data.decode("utf-8").replace("\x00", "").strip() or None
        except Exception:
            return None
    except Exception:
        return None


def record_announce(
    identity: IdentityMap,
    destination_hash: Any,
    app_data: Any,
) -> Optional[str]:
    """Decode ``app_data`` and, if a name is found, store it in ``identity``
    under ``destination_hash``. Returns the decoded name (or None).

    This is the entry point an incoming announce should call to keep the
    name map current. It does NOT implement announcing itself — that
    belongs to ``transport.py``.
    """
    name = decode_display_name(app_data)
    if name and is_valid_destination(destination_hash):
        identity.set_name(destination_hash, name)
    return name
