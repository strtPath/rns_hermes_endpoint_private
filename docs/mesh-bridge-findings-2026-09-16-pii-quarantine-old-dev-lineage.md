# Findings: PII quarantine of the old `dev` lineage — what to PR, what to scrub

Date: 2026-09-16
Status: analyzed — no forward code to PR; PII fully contained off the active branch.

## Context

`origin/dev` is canonical. The local `dev` branch held a *separate, older*
lineage that shared the same branch name but had **no common ancestor** with
`origin/dev`. That older lineage contained PII (a real relay IP and the
deployment host's absolute path layout), which is why the remote was
re-baselined and scrubbed instead of amended. The old local `dev` tip was
preserved as the tag `local-dev-pii-lineage-b4377fd` (points at `b4377fd`)
before `dev` was re-pointed to `origin/dev`.

## Question 1 — which commits need to be PR'd into origin/dev?

**Answer: none.** There is no forward work in the old tag.

Evidence (tip-to-tip tree comparison, `b4377fd` vs `origin/dev`):

- Files present in the old tip but absent on origin/dev: **zero**.
  `comm -23` of the two trees is empty.
- Files present on origin/dev but absent in the old tip: six
  (`tool_emoji.py`, `plugins/reticulum/*`, the 2026-09-15 and 2026-09-16
  findings). Origin is a strict *superset*.
- Every substantive file the old lineage introduced is **identical** at both
  tips: `downlink.py`, `profiler.py`, `preflight.py`, `bridge_liveness.py`,
  `test_downlink.py`, `test_downlink_integration.py`,
  `docs/feature-parity-roadmap.md`,
  `docs/pre-tool-callback-timeout-issue.md`.

Conclusion: origin/dev already contains all of the old lineage's code
(re-landed cleanly). The 28 files that *differ* between the tips are files
origin is simply newer on (`control_server.py`, `commands.py`, `cli.py`,
`hermes_client.py`, `bridge.py`, config, `install.sh`, `plugin.yaml`,
`README`, `.gitignore`, `pyproject.toml`). There is nothing to cherry-pick.

Do **not** cherry-pick or merge the old tag into origin — that would drag the
PII history back in.

## Question 2 — PII scan of the old lineage

### Real PII found (two categories)

1. Real relay IP `<relay-ip>` in `config/reticulum.conf`
   (`target_host = ...`). Introduced in `e393932`, removed in the scrub
   commit `ad4bf27`.
2. Deployment host absolute path layout in the systemd units and
   `start.sh`: `WorkingDirectory=/opt/data/rns_hermes_endpoint`,
   `Environment=HOME=/opt/data`, `EnvironmentFile=/opt/data/.../.env`,
   `ReadWritePaths=/opt/data/.lxmf /opt/data/.reticulum /opt/data/.hermes`.
   These reveal the real host layout and where secrets live.

### Not PII (false positives from the scan)

- `src/.../bridge.py` — `self.identity = RNS.Identity(...)` and similar are
  code identifiers, not key material.
- `acl.py` — stores LXMF sender **hashes** (`hashes.add(h)`), not raw client
  identifiers.
- `README`/`QUICKSTART`/`.gitignore` — the word "secrets", the port `:37428`,
  "IP:37428" placeholders — documentation, not live PII.
- High-entropy base64 scan: only dashes/underscores in comments and the
  `ExecStart` path. No real secrets.

### No key/credential material anywhere

No `.env`, `*.pem`, `*.key`, `id_rsa`, `*.p12/.pfx/.jks`, or credential files
were ever committed in the old lineage. No `sk-`, `Bearer`, or
`PRIVATE KEY` material in any blob.

### Where the PII actually lives now

- Exactly **6 commits** carry the real IP in their tree:
  `e393932` (added), then `25d4e3b`, `659043f`, `5292bff`, `b83259d`,
  `58d4db7` (descendants). The scrub `ad4bf27` (parent `58d4db7`) is clean;
  everything from `ad4bf27` forward is clean.
- No local ref has the PII at its **tip** — every tip is already scrubbed.
- The PII is reachable only through the *history* of 9 local refs, all of
  which descend from the pre-scrub commits:
  `deep-hermes-integration`, `deep-hermes-integration-work`,
  `feat/slash-commands-and-model-pin`, `feature/t4-3-status-health-endpoint`,
  `fix/downlink-acks-v2`, `fix/mesh-gate-session-mismatch`,
  `fix/p0-sigterm-timeout-cleanup`, the tag
  `local-dev-pii-lineage-b4377fd`, and the tag `pre-agent-reset-20260915`.
  Each reaches all 6 PII commits.

## Origin/dev is clean — verified

- Real relay IP `<relay-ip>`: **absent** on origin/dev. Replaced by the
  placeholder `target_host = YOUR_MESH_PEER_HOST` with a comment explaining
  why a placeholder is used.
- Real `/opt/data/...` path layout: **absent** on origin/dev. Service units
  now use a generic `/opt/rns_hermes_endpoint/...` base (neutralized, not
  the real user home).
- Working tree (now `dev` = origin/dev): no real relay IP, no `/opt/data/`.

## Recommendation

- **PR: nothing.** origin/dev is complete and already the clean, publishable
  superset.
- **Quarantine / delete the PII-bearing refs.** The 9 refs above still expose
  the real IP and real host paths through their history. None of their *tips*
  carry forward work that origin lacks (confirmed: the old tip is a subset of
  origin), so they can be safely deleted locally. If you want to keep them for
  archaeology, at minimum confirm they are **never pushed** to any remote and
  never fast-forwarded into origin.
- **Do not merge or cherry-pick** the old tag into origin.

## Suggested cleanup (run only after you confirm)

```
git tag  -d local-dev-pii-lineage-b4377fd pre-agent-reset-20260915
git branch -D deep-hermes-integration deep-hermes-integration-work \
    feat/slash-commands-and-model-pin feature/t4-3-status-health-endpoint \
    fix/downlink-acks-v2 fix/mesh-gate-session-mismatch \
    fix/p0-sigterm-timeout-cleanup
git reflog expire --expire=now --all
git gc --prune=now --aggressive
```

The `reflog expire` + `gc` step is what actually purges the PII blobs from
local object storage; deleting the refs alone only makes them unreachable.
Run this only after you are certain no unmerged, non-PII work lives on any
of those branches.