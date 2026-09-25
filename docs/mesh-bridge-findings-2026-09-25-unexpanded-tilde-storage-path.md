# Findings: a literal `~` directory inside the repo — ALREADY FIXED, this is residue

2026-09-25

## Status: resolved. Not a live bug. Do not "fix" this again.

The stray tree is residue from a bug that was fixed on 2026-09-15. Nothing in
the current code can recreate it.

## Symptom (historical)

A 415 MB LXMF router storage tree sits at the repo-relative path
`~/.lxmf/storage/` (a directory literally named `~` in the working tree),
containing state written between 2026-08-01 and 2026-09-15:
`outbound_stamp_costs`, `available_tickets`, `local_deliveries`, `node_stats`,
`ratchets/`, and an identity file.

That identity is a DIFFERENT key from the bridge's real one
(`~/.lxmf/storage/hermes_identity`, mtime 2026-07-18, never rotated), so the
tree is not a stale copy: a second identity was genuinely in operation for
those six weeks.

## Root cause (historical) and the fix

`bridge.py` passed the raw default `"~/.lxmf/storage"` through to `makedirs`
without expanding it, so the tilde was treated as a literal directory name
resolved against the process working directory — the repo root.

Fixed in `5ca0e0d` (2026-09-15, "fix(hermes-compat): support Hermes v0.19.0
flags, session schema, env paths"), which introduced
`hermes_reticulum.utils.expand_path` and routed the storage path through it.
`expand_path` expands both `~` and `$VARS`.

The dates corroborate it: the fix landed 2026-09-15 and the tree's last write
is 2026-09-15 03:37. The fix is what stopped it growing.

## Correction to an earlier draft of this document

An earlier version of this file claimed `os.path.expanduser` silently leaves
`~` unexpanded when `HOME` is unset, and proposed hardening `expand_path` to
treat an unexpanded tilde as a hard error. That claim is WRONG and is
withdrawn after testing:

```
$ env -u HOME python -c "import os; print(repr(os.path.expanduser('~/.lxmf/storage')))"
'/home/amelia/.lxmf/storage'
```

`expanduser` falls back to the `pwd` database when `HOME` is unset, so it
returns a correct absolute path either way. An unset `HOME` was never the
mechanism, and no hardening is needed for it.

The general point survives in weaker form: `expand_path` is the single place
that must expand user-supplied paths, so any NEW call site passing a
`~`-prefixed default straight to the filesystem would recreate the original
bug. The regression guard is the `expand_path` unit tests, which already cover
the tilde case.

## The residue itself

Left in place deliberately: 415 MB, the only record of which identity was live
during that window, and covered by `.gitignore:41` (`.lxmf/`) so it was never
committed and there is no PII exposure. The owner should decide whether that
period's traffic needs reconciling before removing it. Deleting it is safe
from a code standpoint — nothing reads it — but that is a decision, not
cleanup.

## Related

- `docs/mesh-bridge-findings-2026-08-27-identity-changes-on-restart.md` —
  three hashes from one identity (a display bug, unrelated to this).
- The platform-adapter work adds a separate identity location
  (`~/.hermes/.reticulum-gateway/storage/gateway_identity`, the transport's
  default). Deliberately separate for now; the adapter is intended to converge
  on the bridge's file at release.
