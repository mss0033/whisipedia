# Adapters (`packages/wsp-adapters`)

Source adapters for the Acquirer (PLAN §6.1–6.2) and the snapshot diff that turns two generations
of one source into a change set and a net-diff segment (PLAN §6.4, §6.7).

| Entry point (`whisipedia.adapters`) | Class | Registry sources |
|---|---|---|
| `kiwix-opds` | `wsp_adapters.kiwix:KiwixOpdsAdapter` | `wikipedia-en-zim`, `stackexchange`, `mdwiki`, `kiwix-curated-t2` |
| `wikimedia-dumps` | `wsp_adapters.wikimedia:WikimediaDumpsAdapter` | `wikipedia-top10`, `wikidata`, `wiktionary-en`, `wikisource-en` |

`wsp_adapters.load_adapter(name)` instantiates an adapter by its registry name. Both adapters pass
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
- **Mirrors**: `RemoteFile.mirrors` lists byte-identical mirrors and, **as the last entry, the `.meta4`
  URL**. A fetcher should treat a mirror ending in `.meta4` as a metalink (per-piece SHA-1 for multi-mirror
  piece verification), not as a copy of the file. It is also in `ReleaseRef.meta["meta4:<file>"]`.
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

- Fixtures are built at test time: a small ZIM via `libzim.writer`, a multistream dump and index via
  `bz2`, a Wikidata JSON dump. Recorded upstream metadata lives in `tests/fixtures/`
  (an OPDS feed excerpt and a trimmed `dumpstatus.json`, both < 15 KB).
- `uv run pytest packages/wsp-adapters -m network -s` runs the optional live discovery smoke tests
  (metadata only, no corpus downloads).
