# ADR-0012: The search index stays on local disk

- Status: Accepted (owner, 2026-10-13)
- Date: 2026-10-13
- Amends: [ADR-0011](0011-bulk-index-files-on-the-nas.md)

## Context
ADR-0011 let sealed Tantivy generations live on the NAS behind a local pointer, on the strength of one
commit and one phrase query that succeeded there. The first real switch (2026-10-13, the rebuilt index of
257M documents, 169 GB in 35 segments, copied and sha256-verified) served nothing: every search, the start
page and the random pick hit their deadlines (503) while the API pulled 62 MB/s from the NAS. The site
was rolled back after eight minutes.

Measured directly on that copy (`/mnt/z`, SMB3, `cache=strict`, `rsize=4M`):
- The mount's read-ahead is 8 MB per page fault (`/sys/class/bdi/cifs-*/read_ahead_kb`). One cold
  two-word search pulled 9.9 GB and took 133 s.
- With read-ahead disabled on the mappings (`madvise(MADV_RANDOM)`), the same searches read 300-470 MB
  and took 14-21 s; an id lookup read 30 MB in 1.8 s.
- A query runs every word over 18 fields (9 languages x title and text) in each of 35 segments: term
  dictionaries, postings and field norms are touched at random, tens of thousands of 4 KB faults. Local
  NVMe absorbs that; a network share does not, and the page cache cannot hold a 169 GB working set.
- Changing read-ahead or `rasize` needs root on the mount; the owner keeps the NAS mount as it is.

## Decision
- **The Tantivy index stays on local disk** (`<index root>/lexical/tantivy`). The pointer file
  (`TANTIVY.txt`) and `wsp index point-tantivy` remain for a verified local copy elsewhere on the PC, not
  for the NAS.
- Everything else in ADR-0011 stands: Refinery generations, the change-feed archive and other
  write-once files live on the NAS; databases and anything memory-mapped stay local.
- Revisit only with a query path that reads far less per search (one field per query by language
  routing, or term dictionaries local and postings remote), measured on the real index first.

## Consequences
- The PC keeps about 180 GB for the index: Tantivy, the rank and spelling sidecars and the suggest tries.
  The 73 GB `meta.sqlite` of the previous format is gone (ADR-0011's fast columns replaced it).
- A rebuilt index needs its full size free on the PC beside the live one; the runbook orders the moves
  so that the Refinery goes first.
- The NAS keeps a verified copy of each sealed generation as a backup, not for serving.
