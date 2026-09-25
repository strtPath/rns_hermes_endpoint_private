"""
Tests for hermes_reticulum.plugin.identity (spec sections 4, 7, 12, 14).

Run with: venv/bin/python -m pytest tests/test_reticulum_identity.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))



# Use a placeholder 32-hex hash — never a real identity.
HASH_A = "0" * 32
HASH_B = "a" * 32
HASH_C = "b" * 32
UPPER_HASH = "ABCDEF0123456789ABCDEF0123456789"


# ── is_valid_destination (spec section 14) ──────────────────────────────


def test_valid_32_hex_lowercase():
    from hermes_reticulum.plugin.identity import is_valid_destination
    assert is_valid_destination(HASH_A) is True
    assert is_valid_destination(HASH_B) is True


def test_valid_32_hex_uppercase():
    from hermes_reticulum.plugin.identity import is_valid_destination
    assert is_valid_destination(UPPER_HASH) is True


def test_valid_mixed_case():
    from hermes_reticulum.plugin.identity import is_valid_destination
    assert is_valid_destination("A" * 16 + "b" * 16) is True


def test_rejects_short_hash():
    from hermes_reticulum.plugin.identity import is_valid_destination
    assert is_valid_destination("0" * 31) is False
    assert is_valid_destination("0" * 10) is False


def test_rejects_long_hash():
    from hermes_reticulum.plugin.identity import is_valid_destination
    assert is_valid_destination("0" * 33) is False
    assert is_valid_destination("0" * 100) is False


def test_rejects_non_hex():
    from hermes_reticulum.plugin.identity import is_valid_destination
    assert is_valid_destination("g" * 32) is False
    assert is_valid_destination("0" * 16 + "z" * 16) is False
    assert is_valid_destination("not-a-real-hash") is False


def test_rejects_non_string():
    from hermes_reticulum.plugin.identity import is_valid_destination
    assert is_valid_destination(None) is False
    assert is_valid_destination(12345) is False
    assert is_valid_destination(b"0" * 32) is False  # bytes, not str
    assert is_valid_destination(["0" * 32]) is False
    assert is_valid_destination({"source": "0" * 32}) is False


def test_rejects_empty():
    from hermes_reticulum.plugin.identity import is_valid_destination
    assert is_valid_destination("") is False
    assert is_valid_destination("   ") is False


def test_never_raises_on_hostile_input():
    from hermes_reticulum.plugin.identity import is_valid_destination
    # None, bytes, int, list, dict, 10k-char string — must all return False, not raise.
    hostile = [None, b"0" * 32, 42, ["x"], {"a": 1}, "x" * 10_000, "0123456789abcdef"[:15]]
    for item in hostile:
        result = is_valid_destination(item)
        assert result is False


# ── IdentityMap (spec section 4) ─────────────────────────────────────────


def test_name_set_and_lookup():
    from hermes_reticulum.plugin.identity import IdentityMap
    m = IdentityMap()
    m.set_name(HASH_A, "peer-a")
    assert m.get_name(HASH_A) == "peer-a"


def test_name_overwrite():
    from hermes_reticulum.plugin.identity import IdentityMap
    m = IdentityMap()
    m.set_name(HASH_A, "peer-a")
    m.set_name(HASH_A, "peer-a-renamed")
    assert m.get_name(HASH_A) == "peer-a-renamed"


def test_unknown_hash_fallback_to_hash_itself():
    from hermes_reticulum.plugin.identity import IdentityMap
    m = IdentityMap()
    assert m.get_name(HASH_B) == HASH_B  # default fallback is the hash itself


def test_unknown_hash_fallback_custom():
    from hermes_reticulum.plugin.identity import IdentityMap
    m = IdentityMap()
    assert m.get_name(HASH_B, fallback="?") == "?"


def test_non_string_hash_returns_empty_or_fallback():
    from hermes_reticulum.plugin.identity import IdentityMap
    m = IdentityMap()
    assert m.get_name(None) == ""
    assert m.get_name(None, fallback="?") == "?"
    assert m.get_name(42) == ""
    assert m.get_name(42, fallback="?") == "?"


def test_seed_from_config_dict():
    from hermes_reticulum.plugin.identity import IdentityMap
    seed = {HASH_A: "peer-a", HASH_B: "peer-b"}
    m = IdentityMap(seed=seed)
    assert m.get_name(HASH_A) == "peer-a"
    assert m.get_name(HASH_B) == "peer-b"
    # Unknown hash still falls back.
    assert m.get_name(HASH_C) == HASH_C


def test_seed_merge_preserves_existing():
    """seed() is a merge: an existing set_name entry is not overwritten."""
    from hermes_reticulum.plugin.identity import IdentityMap
    m = IdentityMap()
    m.set_name(HASH_A, "original")
    m.seed({HASH_A: "should-not-override", HASH_B: "peer-b"})
    assert m.get_name(HASH_A) == "original"
    assert m.get_name(HASH_B) == "peer-b"


def test_set_name_ignores_non_string():
    from hermes_reticulum.plugin.identity import IdentityMap
    m = IdentityMap()
    m.set_name(None, "x")
    m.set_name(HASH_A, None)
    assert m.get_name(HASH_A) == HASH_A  # unchanged


# ── is_allowed_destination (spec section 7) ──────────────────────────────


def test_allowed_in_allowlist():
    from hermes_reticulum.plugin.identity import is_allowed_destination
    assert is_allowed_destination(HASH_A, [HASH_A, HASH_B]) is True
    assert is_allowed_destination(HASH_B, [HASH_A, HASH_B]) is True


def test_not_in_allowlist():
    from hermes_reticulum.plugin.identity import is_allowed_destination
    assert is_allowed_destination(HASH_C, [HASH_A, HASH_B]) is False


def test_empty_allowlist_denies_all():
    from hermes_reticulum.plugin.identity import is_allowed_destination
    assert is_allowed_destination(HASH_A, []) is False


def test_invalid_hash_rejected_even_if_in_allowlist():
    from hermes_reticulum.plugin.identity import is_allowed_destination
    assert is_allowed_destination("not-a-hash", ["not-a-hash"]) is False
    assert is_allowed_destination(None, [None]) is False


def test_allowlist_normalization_matches_acl():
    """Colon-separated and uppercase entries in the allowlist still match."""
    from hermes_reticulum.plugin.identity import is_allowed_destination
    # HASH_A is all-zeros; spell it with colons and uppercase.
    colons = ":".join("00" * 16)  # "00:00:..."
    assert is_allowed_destination(HASH_A, [colons]) is True
    upper = "A" * 32
    assert is_allowed_destination("a" * 32, [upper]) is True


# ── is_parseable_inbound (spec section 14) ──────────────────────────────


def test_valid_tuple_event_passes():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    assert is_parseable_inbound((HASH_A, "hello")) is True


def test_valid_dict_event_passes():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    assert is_parseable_inbound({"source": HASH_A, "text": "hello"}) is True


def test_short_hash_rejected():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    assert is_parseable_inbound(("0" * 31, "hello")) is False


def test_long_hash_rejected():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    assert is_parseable_inbound(("0" * 33, "hello")) is False


def test_non_hex_rejected():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    assert is_parseable_inbound(("g" * 32, "hello")) is False


def test_non_string_hash_rejected():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    assert is_parseable_inbound((None, "hello")) is False
    assert is_parseable_inbound((42, "hello")) is False


def test_empty_payload_rejected():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    assert is_parseable_inbound((HASH_A, "")) is False
    assert is_parseable_inbound((HASH_A, None)) is False


def test_non_string_payload_rejected():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    assert is_parseable_inbound((HASH_A, b"bytes")) is False
    assert is_parseable_inbound((HASH_A, 123)) is False


def test_malformed_event_shape_rejected():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    assert is_parseable_inbound(None) is False
    assert is_parseable_inbound("just a string") is False
    assert is_parseable_inbound(42) is False
    assert is_parseable_inbound((HASH_A,)) is False  # only 1 element
    assert is_parseable_inbound({}) is False  # empty dict


def test_allowlist_filters_inbound():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    allow = [HASH_A]
    assert is_parseable_inbound((HASH_A, "hi"), allowlist=allow) is True
    assert is_parseable_inbound((HASH_B, "hi"), allowlist=allow) is False
    # None allowlist = no filtering (gateway authz handles it).
    assert is_parseable_inbound((HASH_B, "hi"), allowlist=None) is True


def test_never_raises_on_hostile_inbound():
    from hermes_reticulum.plugin.identity import is_parseable_inbound
    hostile = [None, b"raw", 42, [None], {"source": None}, ("x" * 10_000, None)]
    for item in hostile:
        result = is_parseable_inbound(item)
        assert result is False


# ── decode_display_name / record_announce (spec section 12) ──────────────


def _synthetic_app_data(name: str) -> bytes:
    """Build a synthetic LXMF v0.5.0+ announce app_data blob.

    Uses the same msgpack vendored into LXMF (``RNS.vendor.umsgpack``) so
    the test does not require the top-level ``msgpack`` package. A
    1-element list packs as a fixarray with first byte 0x91, which the
    v0.5.0+ branch of ``LXMF.display_name_from_app_data`` recognises.
    """
    from RNS.vendor.umsgpack import packb
    return packb([name.encode("utf-8")])


def test_decode_display_name_msgpack_v050():
    """Synthetic LXMF v0.5.0+ announce: msgpack-packed list [name_bytes]."""
    from hermes_reticulum.plugin.identity import decode_display_name
    app_data = _synthetic_app_data("mesh-peer")
    assert app_data[0] >= 0x90
    result = decode_display_name(app_data)
    assert result == "mesh-peer"


def test_decode_display_name_returns_none_for_none():
    from hermes_reticulum.plugin.identity import decode_display_name
    assert decode_display_name(None) is None


def test_decode_display_name_returns_none_for_empty():
    from hermes_reticulum.plugin.identity import decode_display_name
    assert decode_display_name(b"") is None


def test_decode_display_name_never_raises():
    from hermes_reticulum.plugin.identity import decode_display_name
    hostile = [None, b"", 42, b"\xff\xfe\x00garbage", {"not": "bytes"}]
    for item in hostile:
        result = decode_display_name(item)
        assert result is None  # or a string — must not raise


def test_record_announce_populates_map():
    from hermes_reticulum.plugin.identity import IdentityMap, record_announce
    m = IdentityMap()
    app_data = _synthetic_app_data("announce-peer")
    name = record_announce(m, HASH_A, app_data)
    assert name == "announce-peer"
    assert m.get_name(HASH_A) == "announce-peer"


def test_record_announce_invalid_hash_not_stored():
    from hermes_reticulum.plugin.identity import IdentityMap, record_announce
    m = IdentityMap()
    app_data = _synthetic_app_data("some-name")
    result = record_announce(m, "too-short", app_data)
    # Name decoded but hash invalid → not stored.
    assert result is not None, "the call should return the decoded name"
    assert m.get_name("too-short") == "too-short"  # fallback, not stored


def test_record_announce_no_name_not_stored():
    from hermes_reticulum.plugin.identity import IdentityMap, record_announce
    m = IdentityMap()
    name = record_announce(m, HASH_A, None)
    assert name is None
    assert m.get_name(HASH_A) == HASH_A  # unchanged
