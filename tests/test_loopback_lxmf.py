#!/usr/bin/env python3
"""
Loopback test client — verifies the full LXMF message loop to the running
hermes-reticulum bridge WITHOUT a second shared RNS instance.

What it proves
--------------
- We can reach the bridge destination from this box.
- The bridge RECEIVES our message and produces a reply.
- The reply comes BACK to us over the mesh (delivery callback fires).

This is the local oracle for the "reply stuck in propagation" symptom.

Usage
-----
    venv/bin/python tests/test_loopback_lxmf.py [--dest <hex>] [--timeout S]

Discovers the bridge destination from the running bridge's log if you don't
pass --dest.  Reuses the SAME shared RNS instance as rnsd/nomadnet
(abstract socket @rns/default) — no new identity conflict, no second rnsd.

ACL note
--------
The bridge runs in allowlist mode.  The test identity here is NOT on the
allowlist, so the bridge will take the ACL-rejection path and reply with a
rejection message.  That is still a valid end-to-end proof: it confirms the
bridge receives us and the reply traverses the mesh back to us.  To get a
real Hermes-generated answer, temporarily run the bridge with
HERMES_RETICULUM_ALLOW_ALL=true or add the test identity to the allowlist.
"""

import argparse
import os
import re
import sys
import time

import LXMF
import RNS

TEST_IDENTITY_FILE = os.path.expanduser("~/.lxmf/testloopback_identity")
TEST_STORAGE_DIR = os.path.expanduser("~/.lxmf/testloopback_storage")
BRIDGE_LOG = os.path.expanduser(
    # Default assumes a standard clone at ~/rns_hermes_endpoint; override with
    # BRIDGE_LOG when your checkout lives elsewhere.
    os.environ.get("BRIDGE_LOG", "~/rns_hermes_endpoint/.lxmf/reticulum.log")
)
FALLBACK_DEST = "00000000000000000000000000000000"  # placeholder; pass --dest for real discovery


def discover_bridge_destination() -> str:
    """Pull the most recent announced bridge destination from the log."""
    if os.path.exists(BRIDGE_LOG):
        try:
            with open(BRIDGE_LOG, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            pass
        else:
            matches = re.findall(
                r"(?:Announced destination|LXMF address:)\s*<([0-9a-f]{32})>",
                text,
            )
            if matches:
                return matches[-1]
    return FALLBACK_DEST


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dest", help="Bridge LXMF destination hash (hex, no <>)")
    ap.add_argument("--timeout", type=int, default=120,
                    help="Seconds to wait for a reply (default 120)")
    ap.add_argument("--text", default=None,
                    help="Message body to send (default: loopback-test-<ts>)")
    args = ap.parse_args()

    # Throwaway test identity (separate from the bridge's identity).
    if os.path.exists(TEST_IDENTITY_FILE):
        identity = RNS.Identity.from_file(TEST_IDENTITY_FILE)
        if identity is None:
            identity = RNS.Identity()
            identity.to_file(TEST_IDENTITY_FILE)
    else:
        identity = RNS.Identity()
        identity.to_file(TEST_IDENTITY_FILE)

    # Attach to the shared RNS instance (abstract @rns/default).
    RNS.Reticulum()
    print("[*] Attached to shared RNS instance.")

    router = LXMF.LXMRouter(storagepath=TEST_STORAGE_DIR)
    my_dest = router.register_delivery_identity(
        identity, display_name="loopback-test"
    )
    my_hash = RNS.prettyhexrep(my_dest.hash)
    print(f"[*] Test identity:     {my_hash}")

    # Resolve the bridge destination to an RNS.Destination object.
    # RNS loaded the on-disk known_destinations store (msgpack) into
    # Identity.known_destinations during Reticulum() init, so recall()
    # finds the bridge here.  recall() returns an Identity; we wrap it in
    # an OUT/SINGLE "LXMF" Destination to match how the bridge addressed.
    dest_hex = (args.dest or discover_bridge_destination()).strip("<>").lower()
    target_hash = bytes.fromhex(dest_hex)
    bridge_identity = RNS.Identity.recall(target_hash)
    if bridge_identity is None:
        print(f"[-] Could not resolve bridge destination <{dest_hex}> — "
              f"is the bridge announced? (rnpath <{dest_hex}>)")
        return 1
    # The LXMF delivery destination is (identity, IN, SINGLE, "lxmf", "delivery").
    # To SEND to the bridge's delivery destination we build the OUT equivalent:
    # (identity, OUT, SINGLE, "lxmf", "delivery").  APP_NAME is lowercase "lxmf"
    # in this LXMF build — must match or the destination hash won't line up.
    bridge_dest = RNS.Destination(
        bridge_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        "lxmf",
        "delivery",
    )
    print(f"[*] Bridge destination: {RNS.prettyhexrep(bridge_dest.hash)}")

    router.announce(my_dest.hash)

    sink = {"reply": None, "t": None}

    def on_reply(message):
        sink["reply"] = message
        sink["t"] = time.time()
        print(f"[*] reply arrived from "
              f"{RNS.prettyhexrep(message.source_hash) if message.source_hash else '?'}")

    router.register_delivery_callback(on_reply)
    print("[*] Test client ready; sending and waiting for reply...")

    t0 = time.time()
    body = args.text or f"loopback-test-{int(t0)}"
    msg = LXMF.LXMessage(
        bridge_dest,
        my_dest,
        body,
        title="loopback",
        desired_method=LXMF.LXMessage.DIRECT,
        include_ticket=True,
    )
    router.handle_outbound(msg)
    print(f"[+] Sent '{body}' at {time.strftime('%H:%M:%S')}")

    deadline = time.time() + args.timeout
    while sink["reply"] is None and time.time() < deadline:
        time.sleep(2)

    if sink["reply"] is None:
        print(f"[-] No reply within {args.timeout}s — loop NOT confirmed.")
        return 1

    dt = (sink["t"] or time.time()) - t0
    reply = sink["reply"]
    src = RNS.prettyhexrep(reply.source_hash) if reply.source_hash else "?"
    content = reply.content_as_string()
    print(f"[+] REPLY in {dt:.1f}s from {src}")
    print(f"    Content: {content[:500]}")
    print("[+] Loopback OK — the bridge received our message and replied over the mesh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
