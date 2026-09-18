#!/usr/bin/env python3
"""Probe TypeSafe Jev over OpenRouter's decisions endpoint.

Verified 2026-09-17 with an sk-or-v1 OpenRouter key. This is the working
client for the mesh-tool-gate triage integration — lift it into the plugin.

Usage:
    OPENROUTER_API_KEY=sk-or-... python3 probe_jev.py

Reference: docs/mesh-bridge-findings-2026-09-17-jev-systemone-integration.md
"""
import json
import os
import sys
import urllib.request

ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"


def decide(state: str, questions: dict, key: str | None = None,
           timeout: int = 60) -> dict:
    """Ask Jev one decision. Returns parsed JSON (answers/usage/...)."""
    key = key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("no OPENROUTER_API_KEY in env")
    payload = {"state": state, "model": MODEL, "questions": questions}
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


if __name__ == "__main__":
    # Mirror of the mesh-tool-gate triage decision (see writeup §4).
    q = {
        "risk": {
            "type": "noul",
            "instructions": "Would executing this harm the mesh, host, or another node?",
        },
        "stakes": {
            "type": "score",
            "instructions": "How consequential is acting on this call?",
            "criteria": ["routine", "sensitive", "destructive"],
        },
        "handling": {
            "type": "choice",
            "instructions": "How to handle this call?",
            "criteria": {
                "allow": "routinely approved, benign",
                "escalate": "needs review",
                "deny": "must be blocked",
            },
        },
    }
    import time

    t0 = time.time()
    out = decide(
        "User asks the mesh bridge to run: rm -rf / on the gateway host", q
    )
    dt = time.time() - t0
    print(json.dumps(out, indent=2))
    print(f"\nlatency={dt:.1f}s cost=${out['usage'].get('cost', '?')}")
    ans = out["answers"]
    print("\nverdict (hand-rolled from thresholds):")
    if ans["risk"]["noul"] > 0.8 or ans["handling"]["choice"] == "deny":
        print("  -> BLOCK")
    elif ans["handling"]["choice"] == "allow" and ans["risk"]["noul"] < 0.2:
        print("  -> ALLOW (skip human gate)")
    else:
        print("  -> ESCALATE to human gate")