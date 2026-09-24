# Drafts — superseded document versions

Working drafts kept for the reasoning trail, not for reading. **Nothing in this directory is
authoritative.** For any design question, read the current spec; these are here only so the
progression of a decision is not lost when scratch space is pruned.

## spec-reticulum-platform-adapter

The design for shipping Reticulum as a Hermes gateway platform adapter. The authoritative
version is `../spec-reticulum-platform-adapter.md`. These are the drafts that preceded it:

| File | Lines | What changed |
|---|---|---|
| `spec-reticulum-platform-adapter.md` | 308 | First draft. |
| `spec-reticulum-platform-adapter-v2.md` | 332 | |
| `spec-reticulum-platform-adapter-v3.md` | 382 | |
| `spec-reticulum-platform-adapter-v4.md` | 426 | |
| `spec-reticulum-platform-adapter-v5.md` | 487 | Last draft before review. |
| `../spec-reticulum-platform-adapter.md` | 544 | **Current.** v5 plus the review amendments (see below). |

The final revision added six things found in a source review of the gateway contract:

- Section 6 — the delivery ledger is wrong in both directions on a mesh. The boot sweep claims
  only rows for platforms connected at boot, so a disconnected adapter's rows are skipped and
  abandoned at 24h. The adapter's pending set is therefore the authoritative delivery record.
- Section 7 — the downlink row split. Chunking carries over, the queue does not.
- Section 10 — a test for the disconnected-at-boot redelivery case.
- Section 11 — the shared-instance version coupling, and why pins must be exact.
- Section 15 — the version divergence question, settled.
- Section 16 — two invariants: the pending set's authority, and `approvals.timeout` at 900.

Diff any draft against the current spec to see the edits that superseded it.

## Why these exist

Drafts of a spec that is still being negotiated are useful: they show which claims were
corrected and why, and a claim that was wrong once tends to be wrong again. They were rescued
from scratch space before pruning; the content is byte-identical to the drafts as written.
