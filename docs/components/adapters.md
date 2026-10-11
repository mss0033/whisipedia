# Adapters (`packages/wsp-adapters`)

Source adapters for the Acquirer (PLAN §6.1–6.2) and the snapshot diff that turns two generations
of one source into a change set and a net-diff segment (PLAN §6.4, §6.7).

| Entry point (`whisipedia.adapters`) | Class | Registry sources |
|---|---|---|
| `kiwix-opds` | `wsp_adapters.kiwix:KiwixOpdsAdapter` | `wikipedia-en-zim`, `stackexchange`, `mdwiki`, `kiwix-curated-t2` |
| `wikimedia-dumps` | `wsp_adapters.wikimedia:WikimediaDumpsAdapter` | `wikipedia-top10`, `wiktionary-en`, `wikisource-en` |
| `wikidata` | `wsp_adapters.wikidata:WikidataAdapter` | `wikidata` |
| `arxiv` | `wsp_adapters.arxiv:ArxivAdapter` | `arxiv` |
| `pubmed` | `wsp_adapters.pubmed:PubmedAdapter` | `pubmed` |
| `courtlistener` | `wsp_adapters.courtlistener:CourtListenerAdapter` | `courtlistener` |
| `open-library` | `wsp_adapters.openlibrary:OpenLibraryAdapter` | `open-library` |
| `gutenberg` | `wsp_adapters.gutenberg:GutenbergAdapter` | `gutenberg` |
| `osm-planet` | `wsp_adapters.osm:OsmPlanetAdapter` | `osm-planet` |

`wsp_adapters.load_adapter(name)` instantiates an adapter by its registry name. Every adapter passes
the shared contract suite `wsp_core.testing.adapter_contract.SourceAdapterContract`, which also
provides `FakeFetcher`, an in-memory `Fetcher` for tests.

Besides `documents()` (articles only), both adapters implement `EntrySource.entries()`, which yields
articles **and** redirects (`wsp_adapters.entries.Redirect`) in one decoding pass; the diff uses the
redirects to detect page moves.

## `kiwix-opds`

- **Discover**: queries `https://library.kiwix.org/catalog/v2/entries` (it 301-redirects to
  `opds.library.kiwix.org`; the fetcher must follow redirects) with the selector's `name`/`category`/`lang`,
  paginated, then re-filters client side. `access.selector` accepts `name`, `names`, `flavour`, `category`
  and `lang` (ISO 639-3, as in the catalog). Without `flavour`, one file per book is kept, preferring
  `maxi`, then the unflavoured build, then `nopic`, `mini`, `nodet`. The catalog only lists the newest
  file per book and flavour, so discovery returns one release; its `version` is the newest ZIM date
  (`YYYY-MM`) in the selection.
- **Checksums**: the SHA-256 from `<file>.zim.sha256`. The catalog's `length` is rounded up to KiB, so
  `RemoteFile.size` stays `None` at discovery (the rounded value is in `ReleaseRef.meta["approx_size:<file>"]`).
- **Plan**: skips files the previous generation already holds byte-identically (unchanged books in a
  bundle), then reads the `.meta4` metalink of each remaining file for the exact size, MD5/SHA-1 and the
  mirror list. A metalink hash that disagrees with `.sha256` aborts the plan.
- **Mirrors**: `RemoteFile.mirrors` lists byte-identical mirrors only; the `.meta4` URL is
  `RemoteFile.metalink` (per-piece SHA-1 for multi-mirror piece verification). `ReleaseRef.format` is `zim`.
  `KiwixOpdsAdapter.upstream_checksums(ctx, file_url)` reads the published `<file>.sha256` (used by `wsp ingest`).
- **Documents**: read with `libzim`. Every user entry with an HTML mimetype becomes a `DocRecord`;
  redirects become `Redirect`s; assets are skipped. `native_id` is the ZIM path; sources that select more
  than one book prefix it with the book name (`ifixit_en_all/<path>`). `html_ref = "zim:" + native_id`.
  `lang` comes from the ZIM `Language` metadata, `revision` is the ZIM file stem, `license` from the registry.
- **HTML to text**: `selectolax` (Lexbor, C). Sub-millisecond per article, several times faster than
  `lxml.html` and an order of magnitude faster than BeautifulSoup, one wheel with no dependencies.
  A newline is injected before block tags with one regex before parsing, which keeps paragraph structure
  without walking the tree in Python. Scripts, styles, citation markers, reference lists and navboxes are dropped.
- **Parallelism**: entry-id ranges of 2,000 are decoded in worker processes (`access.workers`, default
  `cpu_count - 1`), each opening the ZIM once.

## `wikimedia-dumps`

- **Wikis**: `access.wikis` if given; else `wikipedia-*` + `languages` → `<lang>wiki`; `<project>-<lang>`
  ids → `<lang><project>` (`wiktionary-en` → `enwiktionary`); `wikidata` (or an endpoint under
  `/wikidatawiki/entities/`) → entity dumps.
- **Discover**: lists `https://dumps.wikimedia.org/<wiki>/`, takes the newest `access.max_runs` (3) run
  dates common to all wikis and reads each `<date>/dumpstatus.json`. A run is a release when
  `articlesmultistreamdump` is `done` on **every** wiki. Files come from `articlesmultistreamdumprecombine`
  when it is done (one `.xml.bz2` plus index per wiki), else from the per-part job; `access.layout: parts`
  forces parts. SHA-1 and MD5 come from `dumpstatus.json`, falling back to `<wiki>-<date>-sha1sums.txt`.
  `version` is the run date (`YYYY-MM-DD`).
- **Wikidata**: lists `wikidatawiki/entities/`; a dated run is ready when `wikidata-<date>-sha1sums.txt`
  lists every wanted file (`access.entity_files`, default `all.json.bz2`). Sizes come from the directory listing.
- **Mirrors**: `access.mirrors`, default `ftp.acc.umu.se/mirror/wikimedia.org/dumps/` and
  `dumps.wikimedia.your.org/`, same layout as the primary. Mirrors can lag; checksums always come from the primary.
- **Documents**: namespace 0 by default (`access.namespaces`). `native_id` is the title in URL form
  (`Alan_Turing`); multi-wiki sources prefix the language (`en/Alan_Turing`). Redirect pages become
  `Redirect`s. `revision` is the revision id, `url_native` the article URL, `links` the outbound wiki links
  as doc ids. Wikidata entities become short documents (label, description, aliases) with `native_id = Q42`
  and `entities = ("Q42",)`.
- **Parallel bz2 decompression** (the big speed win): `pages-articles-multistream` is a concatenation of
  independent bz2 streams of 100 pages, and the `-index` file gives every stream's byte offset. The adapter
  groups whole streams into ~8 MiB byte ranges and decodes them in worker processes; each worker
  `seek`s, reads its range, `bz2.decompress`es it and parses its pages. bz2 runs at roughly 30–60 MB/s per
  core, so this scales close to linearly with cores; order is preserved and at most `2 × workers` ranges
  are in flight, so memory stays flat. Without an index (or for the non-multistream Wikidata JSON) the file
  is streamed sequentially, through `lbzip2`/`pbzip2` when installed.
- **Wikitext to text**: a dependency-free scanner (`wsp_adapters.wikitext`), not `mwparserfromhell`.
  `mwparserfromhell` builds a full Python node tree, about 5–20 ms per average English article, which is
  10–40 CPU-hours per monthly enwiki dump. The scanner is a few linear passes and precompiled regexes,
  around 10× faster. Templates are dropped (as `strip_code` does); prose, headings, lists and table cells
  are kept, and links are collected in the same pass.

## Bulk-file adapters (Phase 4)

Each module's docstring is the detailed reference; this is the summary. Throughput was measured on
samples of the real upstream files (one process unless noted; NAS reads under load at the time).

| Adapter | Upstream and checksums | Documents | Measured |
|---|---|---|---|
| `pubmed` | NCBI `baseline/` + `updatefiles/` listings, MD5 sidecars fetched at ≤ 3 req/s; version `pubmed26` or `pubmed26-u<NNNN>` | `iterparse` with element clearing; files newest first, a PMID's newest version wins, `DeleteCitation` hides older copies (PMID bitset, ~5 MB); MeSH/keywords as text lines, MeSH descriptor URIs, DOI and PMC as `links` (not aliases: `Humans` on 20M records would swamp autocomplete) | 3,470 docs/s per worker on a real 30k-record file (default 4 workers) |
| `courtlistener` | public S3 bucket `ListObjectsV2`; ETag = MD5 for single-part uploads | opinions CSV in PostgreSQL `ESCAPE '\'` dialect, unlimited field size; best of `html_with_citations`/… /`plain_text`; case name, court, date, docket, judges joined from clusters/dockets/courts via an SQLite lookup in `ctx.workdir` (dockets filtered by a bitset of referenced ids) | 1,075 docs/s (1 worker), 2,200 docs/s (8) on real opinions (avg 7 KB of text) |
| `open-library` | `ol_dump_*_latest.txt.gz` (dated by the archive.org redirect); **no checksums** (unverifiable; the vault's own hashes take over) | explicit **catalog records** ("Open Library catalog record: work."); author keys resolved to names via local SQLite; Wikidata ids → `entities` | 62k docs/s (1), 98k docs/s (8) on 105k real records |
| `gutenberg` | `pg_catalog.csv.gz`, `rdf-files.tar.bz2`, `txt-files.tar.zip`; **no checksums** | one per book; texts streamed ZIP → tar (nothing extracted), PG header/licence cut at the `*** START/END` markers, RDF summary; catalog-only records for books without text; `links` to `wsp:gutenberg-zim/<title>.<id>` (ZIM path map built once from the reading-copy ZIM and cached) | ~52 books/s (≈ 20 MB/s of text) from the NAS; the one-time ZIM path scan (5.2M entries) took ≈ 4 min |
| `wikidata` | as `wikimedia-dumps` entity dumps (SHA-1 + MD5) | labels/descriptions/aliases in `access.languages`, sitelinks of those wikis as `links`; **entity lookup** `<workdir>/wikidata-entities.sqlite` (`EntityLookup.get/label/by_label`), published only after a full pass | 460 docs/s (1), 680 docs/s (12) on the first entities of the real dump (the heaviest, ~90 KB each); the average entity is ~7× smaller |
| `osm-planet` | `planet.openstreetmap.org/pbf/` listing + `.md5` | pyosmium `FileProcessor` + `KeyFilter("name")` in C++; settlements (`place=*`) and anything with a `wikidata` tag; node coordinates; QID in `entities` | fixture only (see below) |
| `arxiv` | OAI-PMH `oaipmh.arxiv.org` (moved in 2025), raw pages stored as `oai-arXiv-<from>-<until>.jsonl.zst`; newest `pdf_months` from the public GCS mirror (MD5) | union of all harvests up to the generation, newest record first, deletions honoured; `html_ref = arxiv-pdf:<part generation>/<path>` of the newest PDF version (local SQLite index of chunk manifests); optional `pdf_text` with `pypdfium2` (first `pdf_max_pages` pages, one page in memory) | 6,200 docs/s parsing a real 1,300-record OAI page |

**bz2.** The Wikidata JSON dump is a *single* bz2 stream (no stream boundary in its first 200 MB),
so the multistream trick does not apply. `wsp_adapters._streams.open_bz2` uses `lbzip2`/`pbzip2` when
installed, else `indexed_bzip2` (a wheel; it finds block boundaries at bit level): measured 70 MB/s
sequential, 250 MB/s on 4 threads, 410 MB/s on 8, 580 MB/s on 15. At ~580 MB/s the 1.4 TB of Wikidata
JSON decodes in ~45 min; JSON parsing in worker processes is then the limit (≈ 1–2 h with 12 workers).

**OSM runtime.** A planet scan reads 95 GB; pyosmium decodes PBF blocks on C++ threads and only
named objects reach Python. During this work the NAS delivered 27 MB/s (≈ 1 h of pure reading) and
no useful figure could be taken from the shared share, so copy the PBF to local disk for the first
run. Ways and relations get no coordinates: that needs a node-location index (tens of GB, `flex_mem`
or a dense file on local NVMe); a later step can add it.

### arXiv: generation model and ingest

* **Metadata generations** `YYYY-MM-DD` (one per cycle) hold the OAI harvest and the newest PDF
  months; `CURRENT.txt` points at the newest, and the indexer refines that one.
* **PDF part generations** `<snapshot>.pdf.<archive>-<yymm>` hold one chunk of the bulk pull
  (`tools/arxiv_pull.py`): `upstream/pdf/<archive>/<yymm>/*.pdf` plus the chunk manifest
  `upstream/manifests/<chunk>.jsonl`. The snapshot's content is the union of its parts.
* `wsp ingest` plans `_incoming/arxiv/<snapshot>/` directly (it is not in `targets.json`). A chunk is
  ready only if the pull listed it in `done-<snapshot>.txt` (state dir `$WSP_ARXIV_STATE_DIR`, else
  `$XDG_STATE_HOME/whisipedia/arxiv`, else `~/.local/state/whisipedia/arxiv`); without that file, when
  every manifest entry exists with its size and no `.aria2` file. In-progress chunks are never touched.
* Each ready chunk: `FileVault.adopt_many` checks every PDF's MD5, computes SHA-256/BLAKE3 on worker
  threads and renames it into staging (staging record flushed per 1,000 files, not per file, which
  would be quadratic); the manifest is adopted; the part is sealed and never promoted. A crash at any
  point resumes: sealed parts are skipped, staged files are re-hashed and recorded.

## Snapshot diff (`wsp_adapters.diff`)

1. `snapshot_generation(adapter, ctx, generation_dir, out)` / `write_snapshot(entries, out)` writes a
   **snapshot file** to local disk: zstd JSON lines `"<native_id>"\t[kind, hash, title, target, text]`,
   sorted by native id, with normalized text (`keep_text=False` keeps hashes only). Sorting is an external
   merge sort: runs of `run_bytes` (256 MiB) are sorted in memory and spilled, then merged with at most
   `fan_in` (64) open at once. Keep the previous generation's snapshot on local disk (never the NAS) until the next cycle.
2. `SnapshotDiff(old, new, source_id=..., generation_id=..., segment=...)` merge-joins two snapshots in
   one pass and yields `ChangeRecord`s:
   - same id, different hash → `modified` (streamed out immediately);
   - added and removed candidates go to a temporary **on-disk SQLite** database, then are paired:
     a removed id that is now a redirect to an added id is `renamed` (MediaWiki page move or ZIM redirect);
     otherwise an added and a removed doc with the same, unique, non-empty content hash are `renamed`;
   - the rest are `removed` (tombstones) and `added`.
3. With `segment=increment_path(source_dir, generation_id)` it also writes the **net-diff segment**
   `increments/<YYYY-MM>.jsonl.zst`: a header line (`wsp-increment/1`, from/to generation), then one line
   per change: a zero-context unified diff of the normalized text for `modified` (and changed `renamed`),
   the full text for `added`, a tombstone for `removed`. `apply_unified_diff` replays it; the tests rebuild
   generation N+1 from generation N plus the segment and check every hash.

**Memory bound**: about `run_bytes` while snapshotting, then one buffered row per open run during merges;
the diff holds one row per side, a batch of 256 modified pairs per worker (`workers` parallelizes the text
diffs), and SQLite's 64 MiB page cache. Nothing scales with the number of documents, so 7M-article wikis fit.

## Testing

- Fixtures are built at test time (all well under 1 MB): a small ZIM via `libzim.writer`, a multistream
  dump and index via `bz2`, a Wikidata JSON dump, PubMed XML, CourtListener CSVs in the PostgreSQL
  dialect, Open Library TSV, Gutenberg feeds (catalog, RDF tar, ZIP of a tar), an OSM XML extract, and
  arXiv OAI pages with part-generation manifests. Recorded upstream metadata lives in `tests/fixtures/`
  (an OPDS feed excerpt and a trimmed `dumpstatus.json`, both < 15 KB).
- `uv run pytest packages/wsp-adapters -m network -s` runs the optional live discovery smoke tests
  (metadata only, no corpus downloads).
