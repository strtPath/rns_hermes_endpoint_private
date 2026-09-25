# Findings: a literal `~` directory inside the repo — unexpanded tilde in the storage path

2026-09-25

## Symptom

A 415 MB LXMF router storage tree exists at the repo-relative path
`~/​.lxmf/storage/` (a directory literally named `~` in the working tree),
containing live state written between 2026-08-01 and 2026-09-15:
`outbound_stamp_costs`, `available_tickets`, `local_deliveries`, `node_stats`,
`ratchets/`, and an identity file.

That identity is a DIFFERENT key from the bridge's real one
(`~/.lxmf/storage/hermes_identity`, mtime 2026-07-18, never rotated). So the
tree is not a stale copy: it is a second identity that was actually in
operation for six weeks.

## Root cause

`src/hermes_reticulum/core/bridge.py:165` calls

```python
expand_path(storage_path or "~/.lxmf/storage")
```

When the tilde is not expanded before this point — `HOME` unset, or the string
reaching the filesystem unresolved — `~/.lxmf/storage` is a RELATIVE path. It
resolves against the process working directory, which here was the repo root.
Hence `<repo>/~/.lxmf/storage`.

The same shape exists in `core/preflight.py:84` (`os.path.expanduser` is used
there, so it is only exposed when `HOME` is unset entirely) and in
`core/bridge_liveness.py:39` / `core/tool_emoji.py:142` as string defaults
passed through `expand_path`.

`expand_path` must expand `~` itself and treat an unexpanded `~` as a hard
error rather than a relative directory name. A path beginning with `~` that
reaches `os.makedirs` is always a bug, never a legitimate location.

## Why it matters beyond the stray directory

A bridge running with an unresolved tilde writes to a fresh, empty storage
tree, so it:

- creates a NEW identity instead of loading the existing one (new address,
  unreachable at the old hash);
- announces from that new identity, putting a second address on the mesh;
- accumulates propagation-node state (stamps, tickets) in the wrong place,
  which is invisible to an operator looking at `~/.lxmf/storage`.

This is the same class of failure as the 2026-08-27 "keeps changing identities
after restarts" report, which turned out to be a display bug rather than
rotation. This one is real rotation: different key, different address.

## Not deleted

The tree is 415 MB and is the only record of which identity was live during
that window. It is covered by `.gitignore:41` (`.lxmf/`) and was never
committed, so there is no PII exposure. Left in place deliberately; the
owner should decide whether the period's traffic needs reconciling before
removal.

## Related

- `docs/mesh-bridge-findings-2026-08-27-identity-changes-on-restart.md` — the
  three-hashes-one-identity confusion (display bug, not this).
- The platform-adapter work adds a THIRD identity location
  (`~/.hermes/.reticulum-gateway/storage/gateway_identity`, the transport's
  default). Three distinct identity roots on one machine is two too many;
  the adapter is intended to converge on the bridge's file at release.
