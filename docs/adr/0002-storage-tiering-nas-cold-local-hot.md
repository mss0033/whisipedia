# ADR-0002: Cold write-once data on the NAS, hot indexes on local storage

- Status: Proposed
- Date: 2026-10-09

## Context
The NAS (`/mnt/z`, 123 TB free) is mounted over SMB3 (`cifs`) with the `soft` option. SMB byte-range locking is unreliable for embedded databases such as SQLite and LMDB, and a `soft` mount can surface timeouts as I/O errors partway through a write.

## Decision
- The NAS holds only write-once, sealed files: upstream originals, manifests, parity, ledger segments, and derived Parquet.
- All databases and search indexes (job queue, Tantivy, ANN, FST) live on local storage and can be rebuilt from the NAS.
- Every NAS write is atomic: write `*.partial`, flush, read back, verify the hash, rename.
- No reliance on symlinks or hard links; pointers are text files (`CURRENT.txt`).

## Consequences
Indexes need local capacity (see PLAN §9.4, decision D5). Losing the local disk costs only rebuild time, never data.
