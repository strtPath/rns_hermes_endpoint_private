"""Unit tests for mesh-tool-gate Jev triage decision logic.

The triage module is importable in isolation (no Hermes install needed), so
these run under the bridge's own venv. They pin the ROUTING TABLE — the
deterministic mapping from Jev signals to allow/escalate/deny — so a change
to thresholds or ordering fails here loudly instead of live on the mesh.
"""

import importlib.util
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_DIR = os.path.normpath(
    os.path.join(_HERE, "..", "src", "hermes_reticulum", "mesh-tool-gate")
)


def _load(module_name: str, path: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None  # pragma: no cover
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


triage = _load("mesh_tool_gate_triage", os.path.join(_PLUGIN_DIR, "triage.py"))


# --- routing table: an "allow" must satisfy EVERY gate --------------------


def test_allow_routine():
    r = triage.verdict(
        handling="allow", risk_noul=0.05, stakes="routine",
        confidence=0.9, tool_name="terminal",
    )
    assert r == "allow"


def test_allow_sensitive_escalates_to_human():
    # 'sensitive' is still consequential — only ROUTINE calls auto-allow.
    r = triage.verdict(
        handling="allow", risk_noul=0.1, stakes="sensitive",
        confidence=0.95, tool_name="terminal",
    )
    assert r == "escalate"


def test_execute_code_requires_flag():
    r = triage.verdict(
        handling="allow", risk_noul=0.1, stakes="routine",
        confidence=0.95, tool_name="execute_code", allow_execute_code=False,
    )
    assert r == "escalate"


def test_execute_code_allowed_with_flag():
    r = triage.verdict(
        handling="allow", risk_noul=0.1, stakes="routine",
        confidence=0.95, tool_name="execute_code", allow_execute_code=True,
    )
    assert r == "allow"


# --- confidence gating ----------------------------------------------------


def test_low_confidence_escalates_even_for_allow():
    r = triage.verdict(
        handling="allow", risk_noul=0.05, stakes="routine",
        confidence=0.4, tool_name="terminal",
    )
    assert r == "escalate"


def test_borderline_confidence_escalates():
    r = triage.verdict(
        handling="allow", risk_noul=0.05, stakes="routine",
        confidence=0.6, tool_name="terminal",  # floor is 0.6, strict >=
    )
    # equals floor passes; just below fails:
    assert triage.verdict(
        handling="allow", risk_noul=0.05, stakes="routine",
        confidence=0.599, tool_name="terminal",
    ) == "escalate" and r == "allow"


# --- risk gating ---------------------------------------------------------


def test_high_risk_escalates_even_for_allow():
    r = triage.verdict(
        handling="allow", risk_noul=0.5, stakes="routine",
        confidence=0.95, tool_name="terminal",
    )
    assert r == "escalate"


def test_destructive_high_risk_denies():
    r = triage.verdict(
        handling="allow", risk_noul=0.9, stakes="destructive",
        confidence=0.95, tool_name="terminal",
    )
    assert r == "deny"


# --- stakes gating --------------------------------------------------------


def test_destructive_never_auto_allow():
    r = triage.verdict(
        handling="allow", risk_noul=0.05, stakes="destructive",
        confidence=0.95, tool_name="terminal",
    )
    assert r == "escalate"


def test_destructive_dict_shape_never_auto_allows():
    r = triage.verdict(
        handling="allow", risk_noul=0.05,
        stakes={"score": 0.9}, confidence=0.95, tool_name="terminal",
    )
    assert r == "escalate"


def test_unknown_stakes_shape_is_safe():
    # unrecognized stakes → escalate (never lower), not auto-allow
    r = triage.verdict(
        handling="allow", risk_noul=0.0, stakes={"weird": "shape"},
        confidence=0.99, tool_name="terminal",
    )
    assert r == "escalate"


# --- handling gating ------------------------------------------------------


def test_explicit_escalate_stays_human():
    r = triage.verdict(
        handling="escalate", risk_noul=0.0, stakes="routine",
        confidence=0.99, tool_name="terminal",
    )
    assert r == "escalate"


def test_explicit_deny_high_confidence():
    # Confident deny does block — Jev explicitly says the call must be
    # denied and is sure about it.
    r = triage.verdict(
        handling="deny", risk_noul=0.0, stakes="routine",
        confidence=0.95, tool_name="terminal",
    )
    assert r == "deny"


def test_explicit_deny_low_confidence_escalates():
    # An UNCERTAIN deny must not bypass the human gate: a low-confidence
    # model answer claiming "deny" could make a benign call unrecoverable,
    # contradicting the "low confidence → escalate" rule. It escalates so a
    # human still reviews it.
    r = triage.verdict(
        handling="deny", risk_noul=0.0, stakes="routine",
        confidence=0.5, tool_name="terminal",
    )
    assert r == "escalate"


def test_none_bucket_escalates():
    r = triage.verdict(
        handling=triage.NONE_CODE, risk_noul=0.0, stakes="routine",
        confidence=0.99, tool_name="terminal",
    )
    assert r == "escalate"


def test_missing_handling_escalates():
    r = triage.verdict(
        handling="", risk_noul=0.0, stakes="routine",
        confidence=0.99, tool_name="terminal",
    )
    assert r == "escalate"


# --- noul coercion (Jev answer shapes) ------------------------------------


def test_noul_dict_float():
    assert triage._noul({"noul": 0.42}) == 0.42


def test_noul_dict_bool_false():
    assert triage._noul({"noul": False}) == 0.0


def test_noul_bare_float():
    assert triage._noul(0.7) == 0.7


def test_noul_unknown_is_max_risk():
    assert triage._noul(None) == 1.0
    assert triage._noul("garbage") == 1.0


# --- classify_stakes shapes -----------------------------------------------


def test_classify_stakes_string():
    assert triage.classify_stakes("routine") == "routine"
    assert triage.classify_stakes("SENSITIVE") == "sensitive"


def test_classify_stakes_score_map():
    assert triage.classify_stakes({"score": 0.1}) == "routine"
    assert triage.classify_stakes({"score": 0.5}) == "sensitive"
    assert triage.classify_stakes({"score": 0.9}) == "destructive"


def test_classify_stakes_unknown_is_destructive():
    assert triage.classify_stakes({"bogus": True}) == "destructive"
    assert triage.classify_stakes(None) == "destructive"