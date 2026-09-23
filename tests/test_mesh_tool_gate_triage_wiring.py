"""Integration-style tests for the mesh-tool-gate Jev triage *wiring*.

These load the real plugin __init__.py the same way Hermes does
(hermes_plugins.mesh-tool-gate) and exercise the gate-level helper, NOT the
pure triage module (that's test_mesh_tool_gate_triage.py). The point here:

- triage OFF (default) → escalate, and we assert NOTHING hit the network
  (fail-closed shortcut: no Jev call is made at all when disabled).
- hint_only → escalate (dry-run), Jev still logged.
- allow_benign + benign Jev answer → allow (auto-route past the human gate).
- Jev error / missing key → escalate (fail-open: human gate stays backstop).

The triage helper's Jev HTTP call is monkeypatched so these are hermetic.
"""

import importlib.util
import os
import sys
import types

import pytest


def _load_plugin():
    here = os.path.dirname(os.path.abspath(__file__))
    plugin_dir = os.path.normpath(
        os.path.join(here, "..", "src", "hermes_reticulum", "mesh-tool-gate")
    )
    init_file = os.path.join(plugin_dir, "__init__.py")
    ns = types.ModuleType("hermes_plugins")
    ns.__path__ = ["hermes_plugins"]
    ns.__package__ = "hermes_plugins"
    sys.modules["hermes_plugins"] = ns
    name = "hermes_plugins.mesh-tool-gate"
    spec = importlib.util.spec_from_file_location(
        name, init_file, submodule_search_locations=[plugin_dir]
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = name
    mod.__path__ = [plugin_dir]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_plugin()


@pytest.fixture(autouse=True)
def fresh_mode(monkeypatch):
    """Isolate the module-level TRIAGE_MODE per test; restore after."""
    yield
    monkeypatch.undo()
    mod.TRIAGE_MODE = "off"


def _benign_jev(*_a, **_k):
    return {
        "risk": 0.05,
        "stakes": "routine",
        "handling": "allow",
        "confidence": 0.95,
    }


# --- triage OFF: gate behaves exactly as before, and we PROVE no Jev call ---


def test_off_short_circuits_to_escalate_without_network(monkeypatch):
    mod.TRIAGE_MODE = "off"
    calls = []

    def _spy(*a, **k):
        calls.append(a)
        return _benign_jev()

    monkeypatch.setattr(mod, "_ask_jev", _spy)
    v = mod._triage_tool_call("terminal", "git status", "dangerous")
    assert v == "escalate"
    assert calls == [], "triage OFF must not make any Jev call"


# --- hint_only: dry-run, Jev runs but verdict is ignored → escalate ---


def test_hint_only_runs_jev_but_escalates(monkeypatch):
    mod.TRIAGE_MODE = "hint_only"
    calls = []

    def _spy(*a, **k):
        calls.append(a)
        return _benign_jev()

    monkeypatch.setattr(mod, "_ask_jev", _spy)
    v = mod._triage_tool_call("terminal", "git status", "dangerous")
    assert calls, "hint_only must still consult Jev (for calibration)"
    assert v == "escalate", "hint_only must never change the gate outcome"


# --- allow_benign: benign call auto-allows ---


def test_allow_benign_routes_benign_to_allow(monkeypatch):
    mod.TRIAGE_MODE = "allow_benign"
    monkeypatch.setattr(mod, "_ask_jev", lambda *_a, **_k: _benign_jev())
    v = mod._triage_tool_call("terminal", "git status", "dangerous")
    assert v == "allow"


# --- fail-open: any Jev error escalates, never auto-allows ---


def test_allow_benign_jev_error_escalates(monkeypatch):
    mod.TRIAGE_MODE = "allow_benign"
    monkeypatch.setattr(mod, "_ask_jev", lambda *_a, **_k: None)  # network/parse fail
    v = mod._triage_tool_call("terminal", "git status", "dangerous")
    assert v == "escalate"


def test_allow_benign_missing_key_escalates(monkeypatch):
    mod.TRIAGE_MODE = "allow_benign"
    monkeypatch.setattr(mod, "_triage_key", lambda: "")
    # _ask_jev would return None (no key) — prove the helper escalates
    v = mod._triage_tool_call("terminal", "git status", "dangerous")
    assert v == "escalate"


# --- allow_benign: Jev says deny → deny (block), never auto-allow ---


def test_allow_benign_jev_deny_blocks(monkeypatch):
    mod.TRIAGE_MODE = "allow_benign"
    monkeypatch.setattr(
        mod, "_ask_jev",
        lambda *_a, **_k: _benign_jev() | {"handling": "deny"},
    )
    v = mod._triage_tool_call("terminal", "rm -rf /", "dangerous")
    assert v == "deny"


# --- allow_benign: sensitive stakes → escalate (not auto-allowed) ---


def test_allow_benign_sensitive_escalates(monkeypatch):
    mod.TRIAGE_MODE = "allow_benign"
    monkeypatch.setattr(
        mod, "_ask_jev",
        lambda *_a, **_k: _benign_jev() | {"stakes": "sensitive"},
    )
    v = mod._triage_tool_call("terminal", "sudo pacman -Syu", "dangerous")
    assert v == "escalate"


# ---------------------------------------------------------------------------
# Security hardening (review findings):
#   F1 opaque execute_code never auto-allows
#   F2 zero/missing confidence forces escalation (never discarded)
#   F3 credential-like description material is scrubbed before OpenRouter
#   F5 malformed Jev response fails open (returns None, never raises)
# ---------------------------------------------------------------------------

class _FakeResp:
    """Minimal response shim with a `.read()` returning a JSON string."""

    def __init__(self, body: dict):
        import json
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _set_triage_key(monkeypatch):
    monkeypatch.setattr(mod, "_triage_key", lambda: "test-key")


def test_ask_jev_malformed_response_fails_open(monkeypatch):
    # Non-numeric probability + dict/list legend combo → would previously
    # raise ValueError/TypeError out of _ask_jev. It must return None so the
    # caller escalates to the human gate (F5).
    _set_triage_key(monkeypatch)

    def _urlopen(_req, timeout=0):
        return _FakeResp({
            "answers": {
                "risk": {"noul": 0.05},
                "stakes": {
                    "probabilities": {"not-a-number": "oops"},
                    "legend": [0, 1, 2],
                },
                "handling": {"choice": "allow", "confidence": 0.9},
            }
        })

    monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)
    j = mod._ask_jev("something")
    assert j is None, "malformed response must fail open (None), not raise"


def test_ask_jev_zero_confidence_forces_escalation(monkeypatch):
    # One axis reports 0/missing confidence: the min() must keep that 0 so the
    # combined confidence is 0 → any caller escalates. It must NOT be dropped
    # in favor of the other axis' high confidence (F2).
    _set_triage_key(monkeypatch)

    def _urlopen(_req, timeout=0):
        return _FakeResp({
            "answers": {
                "risk": {"noul": 0.05},
                "stakes": {"choice": "routine", "confidence": 0.95},
                "handling": {"choice": "allow", "confidence": 0},  # missing/0
            }
        })

    monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)
    j = mod._ask_jev("something")
    assert j is not None
    assert j["handling"] == "allow"
    assert j["confidence"] == 0.0, "0 confidence must not be discarded"


def test_scrub_triage_desc_redacts_credentials():
    # A terminal command with inline credentials that would otherwise ship to
    # OpenRouter verbatim — the scrubber must redact the secret material.
    d = (
        "curl -H 'Authorization: Bearer ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345' "
        "https://api.example.com --data 'password=hunter2&token=sk_live_x'"
    )
    out = mod._scrub_triage_desc(d)
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz012345" not in out, "bearer token must be redacted"
    assert "hunter2" not in out, "password value must be redacted"
    assert "sk_live_x" not in out, "token value must be redacted"
    assert "<redacted>" in out, "redaction placeholder should remain"


def test_triage_state_scrubbed_description():
    # The state actually shipped to Jev must not contain credential material.
    state = mod._triage_state(
            "terminal",
            "curl -H 'Authorization: Bearer PPQqRrSsTtUuVvWwXxYyZzAaBbCcDdEeFfGg' x",
            "dangerous",
        )
    assert "PPQqRrSsTtUuVvWwXxYyZzAaBbCcDdEeFfGg" not in state


def test_execute_code_never_auto_allowed(monkeypatch):
    # F1: opaque Python payload — even a benign Jev verdict must NOT let the
    # tool bypass the human gate. execute_code is ALWAYS human-gated: the
    # triage stage may DENY it, but an `allow` verdict is collapsed to
    # `escalate` (the human gate decides) inside _triage_tool_call, so
    # the handler's execute_code branch never sees an auto-allow to honor.
    mod.TRIAGE_MODE = "allow_benign"
    monkeypatch.setattr(mod, "_ask_jev", lambda *_a, **_k: _benign_jev())
    v = mod._triage_tool_call("execute_code", "import os; os.system('rm -rf /')", "execute_code")
    assert v == "escalate", "execute_code must never auto-allow — integral escalates"


def test_execute_code_triage_may_deny(monkeypatch):
    # Safety net kept: Jev CONFIDENTLY calling a destructive execute_code
    # "must be blocked" still yields a block verdict (the handler blocks on
    # `deny`), but a LOW-confidence deny must still escalate (not hard-block).
    mod.TRIAGE_MODE = "allow_benign"
    monkeypatch.setattr(
        mod, "_ask_jev",
        lambda *_a, **_k: _benign_jev() | {"handling": "deny", "confidence": 0.95},
    )
    v = mod._triage_tool_call("execute_code", "import os; os.system('rm -rf /')", "execute_code")
    assert v == "deny"
