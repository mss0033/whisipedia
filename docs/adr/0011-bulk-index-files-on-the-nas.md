# ADR-0011: Bulk index files on the NAS, databases on local disk

- Status: Accepted (owner, 2026-10-12); amended by [ADR-0012](0012-search-index-stays-local.md): the Tantivy index stays local
- Date: 2026-10-12
- Amends: [ADR-0002](0002-storage-tiering-nas-cold-local-hot.md)

## Context
By October 2026 the local index root had grown to about 470 GB on the PC's system disk:
- the Tantivy index, 166 GB;
- the Refinery, 170 GB;
- `meta.sqlite`, 73 GB;
- `changes.sqlite`, 42 GB;
- scratch.

The owner wants as much of it as possible on the NAS. The NAS has 120 TB free and is mounted over SMB3 with `soft`.

Measured on 2026-10-12 against `/mnt/z`:
- **SQLite fails.** Its first write fails with "database is locked", in both WAL and rollback mode.
- **Tantivy works.** A commit and a phrase query both succeeded.
- **The file operations the Refinery relies on work:** `os.replace` over an existing file, directory `fsync` and hard links.
- **No block storage.** The NAS (UniFi Drive) offers SMB and NFS; no iSCSI is listed.

## Decision
- **Write-once files may live on the NAS:**
  - Refinery generations, via `[index] refinery_root`;
  - sealed Tantivy index generations, selected by a local pointer (withdrawn by ADR-0012: served from the NAS, searches did not finish);
  - sealed change-feed archive ranges.
- **Databases stay on local disk,** as before: SQLite (the change-feed window, settings, orchestrator state) and anything opened for writing in place. `ensure_local` still guards them.
- **Nothing hot is memory-mapped over the NAS** where a local copy is cheap. The Refinery's id-map sidecars, about 20 bytes a document, live under `<index root>/refinery-maps`. Without a map root, a reader on a network mount uses row-group lookups and maps nothing. On a `soft` SMB mount, a page fault on a failed read raises SIGBUS rather than an `OSError`.
- **Tantivy is the one exception,** because it memory-maps by design. A NAS outage can therefore crash a reader (the API). The health check restarts the API; the live index is never written in place, so it cannot be damaged.
- **The NAS mount configuration is not changed** (owner, 2026-10-12).

## Consequences
- The PC keeps about 10–20 GB: SQLite files, id maps, rank, spelling, suggest tries, vectors and build scratch.
- Fresh searches read word postings over the network, so they are slower until the page cache warms. Repeated searches are unaffected.
- Refine steps write Parquet over the network and run slower.
- A new index is built on local disk and then copied to the NAS in one sequential pass. The pointer switch makes it live, and the previous generation is kept for rollback.
