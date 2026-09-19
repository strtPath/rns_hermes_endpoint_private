"""Jev triage decision logic for mesh-tool-gate — pure, no Hermes deps.

Kept importable in isolation so the bridge's own test suite can unit-test it
without pulling in the Hermes install (which owns ``tools.approval``).

The decision is deterministic routing over Jev's answers, per TypeSafe's
"keep code in control" guidance: Jev supplies *classified signals*
(risk noul, stakes score, handling choice, confidence); this module applies
the operator's thresholds and returns a routing verdict. It never sees raw
state, never makes a network call, and never decides on uncertainty.
"""

from __future__ import annotations

# Jev question-space constants (must match what the caller sends / parses).
HANDLING_ALLOW = "allow"
HANDLING_ESCALATE = "escalate"
HANDLING_DENY = "deny"

NONE_CODE = "__none__"  # Jev may answer "nothing applies" — not a real verdict


def classify_stakes(stakes) -> str:
    """Normalize Jev's stakes score object to 'routine' | 'sensitive' | 'destructive'.

    Jev is a decisions model; its score answer shape can vary (a name, a
    map, a bare string). Tolerate the common shapes and fail to 'destructive'
    (the safest bucket) on anything unrecognized so an odd score never
    *lowers* a call's stakes.
    """
    if isinstance(stakes, dict):
        # Two known shapes: {"score": <0..1>} or {"choice"/"name"/...} on old
        # payloads. Prefer the literal bucket when present.
        for k in ("bucket", "label", "name", "choice"):
            v = stakes.get(k)
            if v in ("routine", "sensitive", "destructive"):
                return v
        val = stakes.get("score")
        if val is None:
            return "destructive"  # no score → assume worst, never routine
        try:
            val = float(val)
        except (TypeError, ValueError):
            return "destructive"
        if val < 0.33:
            return "routine"
        if val < 0.66:
            return "sensitive"
        return "destructive"
    if isinstance(stakes, str):
        s = stakes.strip().lower()
        if s in ("routine", "sensitive", "destructive"):
            return s
    return "destructive"


def verdict(
    *,
    handling: str,
    risk_noul: float,
    stakes,
    confidence: float,
    conf_floor: float = 0.6,
    allow_execute_code: bool = True,
    tool_name: str = "",
) -> str:
    """Route a gated tool call to allow | escalate | deny.

    Rules (all must hold to auto-ALLOW; anything uncertain escalates):
      1. handling must be explicitly 'allow' (Jev was asked "should the
         human review this?"; only a confident 'allow' auto-routes past).
      2. confidence >= conf_floor (universal floor; 'destructive' uses a
         higher floor via the confidence gate below).
      3. risk_noul < 0.2 (the call must not plausibly harm mesh/host/node).
      4. stake level is at most 'sensitive' — never auto-allow destructive.
      5. if tool_name == 'execute_code', allow_execute_code must be True
         (the Python payload is opaque to the gate's regex detection).

    DENY only when Jev itself confidently says deny (handling == 'deny' and
    confidence >= conf_floor) OR the call is destructive at high risk
    (risk_noul > 0.8) AND Jev is confident (confidence >= 0.9) — Jev's own
    "must be blocked". An uncertain denial — including one deduced from a
    malformed risk/stakes payload that only normalizes to max-risk + destructive
    on missing data — still escalates so a human reviews it. Never start
    blocking on a verdict Jev isn't sure of.

    Everything else — Jev error, low confidence, escalate, unknown — is
    'escalate' (the human gate stays the backstop; never auto-allow on doubt).
    """
    # Jev answered "nothing fits" or a missing/unknown handling → escalate.
    if not handling or handling == NONE_CODE:
        return "escalate"
    handling = handling.strip().lower()
    risk = _noul(risk_noul)
    stakes_bucket = classify_stakes(stakes)

    # DENY is a blocking verdict that bypasses the human gate, so we only
    # honor it when Jev is CONFIDENT — otherwise an explicitly uncertain or
    # low-confidence model answer could make a benign call unrecoverable.
    # An uncertain denial escalates so a human still reviews it. The
    # destructive+high-risk deny below is the deterministic rule Jev reads
    # of the call itself and does not depend on Jev's confidence.
    if handling == HANDLING_DENY:
        if confidence >= conf_floor:
            return "deny"
        return "escalate"

    # Highest stakes command with high asserted risk → deny (Jev's "must
    # block" reads on the call, not the human's choice). This deny must NOT
    # fire on an uncertain verdict: if Jev's risk/stakes fields are missing
    # or malformed, _noul/classify_stakes normalize to max risk + destructive
    # but aggregate confidence is 0 — an unconditional deny there would
    # permanently block the call with no human review. Escalate instead so a
    # human still sees it (same rule as the confident-DENY branch above).
    if stakes_bucket == "destructive" and risk > 0.8:
        if confidence >= max(conf_floor, 0.9):
            return "deny"
        return "escalate"

    if handling != HANDLING_ALLOW:
        # explicit escalate (or non-token value) → human decides
        return "escalate"

    # Auto-allow path — every gate must pass.
    eff_floor = max(conf_floor, 0.9 if stakes_bucket == "destructive" else 0.0)
    if confidence < eff_floor:
        return "escalate"
    if risk >= 0.2:
        return "escalate"
    # Only ROUTINE calls may auto-allow. 'sensitive' is still consequential —
    # the user explicitly wants the critical ones in front of a human.
    if stakes_bucket != "routine":
        return "escalate"
    if tool_name == "execute_code" and not allow_execute_code:
        return "escalate"
    return "allow"


def _noul(value) -> float:
    """Coerce a Jev noul answer to a 0..1 risk float.

    Verified Jev shape (probe_jev.py): ``{"noul": <float 0..1>}``.
    Accept a bare float too, and be conservative (1.0 = maximal risk) on any
    unrecognized shape so an odd answer never *lowers* asserted risk.
    """
    if isinstance(value, dict):
        v = value.get("noul")
        if isinstance(v, bool):
            # older negation-style answer: False = no risk, True = risk
            return 1.0 if v else 0.0
        if v is not None:
            return _noul(v)
        v = value.get("probability")
        if v is not None:
            return _noul(v)
        return 1.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 1.0
    return min(1.0, max(0.0, f))