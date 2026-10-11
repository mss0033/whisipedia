# ADR-0010: Partitioned sources and per-document withholding in releases

- Status: Accepted
- Date: 2026-10-11

## Context

Two D14 collections are too large to fetch in one cycle: the USGS Historical Topographic Map
Collection (≈ 373k files, 4 TB, fetched one US state per run) and the CIA FOIA reading room
(≈ 900k items, ≈ 6 TB, fetched in 90 batches of 10,000). `wsp collections fetch --partition <p>`
already wrote one cycle `<date>.<p>` per run, but everything downstream treated each run as a new
version of the whole source:

1. `promote` pointed `CURRENT.txt` at the last partition ingested, so `documents()`, the Refinery,
   search and the catalog only saw that partition (and the fetcher carried the *other* partition's
   catalog into the new one, labelling its documents `withdrawn-from-index`). `wsp ingest` also
   refused to promote `2026-10-02.ca` once `2026-10-03.ak` was current ("an older backfill").
2. GFS retention (orchestrator planner and `FileVault.prune`) saw earlier partitions as superseded
   generations and could prune them. Separately, `doc-collection` generations are incremental (a
   catalog points at earlier generations for unchanged documents), so pruning any generation a
   current catalog reads from silently breaks documents, partitioned or not.
3. In the orchestrator pipeline one restricted or held document made `verify` return `mismatch`,
   which failed the whole release, counted as a checksum failure and auto-paused the source after two.

## Decision

**Model.** A source is *partitioned* when its registry entry says `access.partitioned: true`
(`SourceSpec.partitioned`). Its generation ids are `<cycle>.<partition>` (partition: lowercase
letters, digits, `-`; helpers in `wsp_core.partitions`). A partition is never a newer version of
another partition. The source's content is its **current set**: the current generation of every
partition.

**Vault layout (additive).** `promote` of a partitioned source writes
`library/<domain>/<id>/CURRENT.d/<partition>.txt` and then `CURRENT.txt`, which keeps naming the
most recently promoted generation so existing readers keep working. A source directory with
`CURRENT.d/` is read as partitioned whatever the registry says. Generation directories, manifests
and signatures are unchanged; every sealed generation stays readable. A generation promoted before
the source was partitioned (an id without a partition) stays in the current set while `CURRENT.txt`
names it.

**Port revision v0.4.**

* `Vault.current_set(source_id)` (new): the current set, oldest id first; `[current]` for an
  ordinary source. `promote` documents the per-partition behaviour; `Warden.prune` refuses every
  generation of the current set and the newest sealed generation of each partition.
* `GenerationDependencies` (new, optional adapter capability): `depends_on(ctx, generation_dir)`
  returns the other generations a generation reads files from. `doc-collection` implements it from
  its catalog.
* `VerdictStatus.REJECTED` and `VerdictStatus.HELD` (new) with `Verdict.withheld`: document policy
  keeps an intact file out of the vault. `REJECTED` (restricted or out of scope) is deleted at once;
  `HELD` (needs an owner decision) is quarantined outside the vault. Neither is `ok`, neither is an
  integrity failure.

**Retention.** `plan_partitioned_retention` runs the GFS plan per partition (own newest, current,
last *n*, months, years) and keeps the union. For every source, a generation `needed` by a current
one (`GenerationDependencies`) is a hard keep; if the dependencies cannot be read, nothing is pruned.

**Index.** Each partition generation is refined once into `refinery/<id>/_parts/<gid>/`. The indexer
then composes a **set generation** `refinery/<id>/<newest member>.setNNNNN/` that hard-links the
members' Parquet parts and merges their id indexes (O(documents), no text extraction); a `doc_id` in
several members keeps the row of the member with the highest generation id. The set is an ordinary
sealed Refinery generation, so the search index refresh (diff against the previous set), documents,
history, catalog, rank and spelling need no change. Identical member lists reuse the existing set.

**Acquisition.** `wsp collections fetch --partition p` carries over partition *p*'s current catalog
only; `wsp ingest` promotes unless a newer generation *of the same partition* is current. The
orchestrator does not discover partitioned sources (no DISCOVER/HEALTH_PROBE jobs, no
retention-window alerts); it still runs their retention. Promotion sanity checks and content-profile
baselines compare a partition only with that partition.

**Release pipeline.** A withheld file is dropped from the release and the rest proceeds. The narrowed
plan (with the withheld list) is committed first (ledger `release.withheld`), then rejected files are
deleted and held files moved to `<workdir>/_quarantine/<source>/`; the action is repeated idempotently
after a crash. One `withheld:<source>/<version>` alert per release and the `Wiring.on_withheld` hook
(wired to file one doc-review proposal, as `wsp collections fetch` does) report them. A release whose
every file is withheld ends QUARANTINED without pausing the source. A file that is not what it claims
(an HTML page instead of a PDF) is still a `mismatch`.

## Consequences

* Search and documents see every partition; acquiring partition after partition never prunes one.
* Existing vault data needs no migration. `CURRENT.d/` appears only when a partitioned source is
  promoted. Old local Refinery generations of a partitioned source (if any were refined before this
  change) remain on disk as ordinary generations; the next `wsp index build` indexes the set and the
  diff takes care of the switch.
* Set generations accumulate on local disk (hard links plus a small id index per set); they are
  derived data and may be deleted with the rest of the Refinery.
* API generation ids for partitioned sources are set ids (`2026-10-03.ak.set00002`), not vault ids;
  `?at=` still selects by sort order.
* Held files in the orchestrator path wait in the local work directory, not under
  `<vault>/_incoming/_quarantine/` as in the CLI path; the review row's key is read from the file
  name (the release does not carry document keys).
