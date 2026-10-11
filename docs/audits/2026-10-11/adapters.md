# Audit: wsp-adapters (refine at 100+ GB inputs; doc-collection policy)

Tools on the machine:
- Installed: `pigz`, `indexed_bzip2` 1.7.0, `libzim` 3.13.1.
- Not installed: `lbzip2`, `pbzip2`, `orjson`, `isal`, `zlib-ng`, `rapidgzip`.

Experiments are in `/tmp/claude-1000/-home-elizabeth-dev-whisipedia/audit-adapters/`.

## CRITICAL

**C1. A generation holds only the files that were fetched, but `documents()` treats it as complete.**

- `_common.py:108-133` (`plan_missing`) skips files the previous generation already holds and says they are "carried over by the vault". Nothing carries them over (`pipeline.py:625-644` `_seal` ingests only `plan.files`).
- Every adapter's `documents()` reads a single generation dir.

Evidence:
- `medicine/pubmed/pubmed26-u1671` holds only 337 update files. The 1,334 baseline files are in `pubmed26`. The refine of u1671 never sees the baseline.
- `knowledge-graph/wikidata/CURRENT.txt` is `2026-10-07`, which contains only `wikidata-20261007-truthy-BETA.nt.bz2`.
  - `WikidataAdapter.documents` filters for `-all.json.bz2`, yields 0 docs, and still commits an empty entity lookup.
  - `index_generation` would then diff the old generation against an empty one and delete every Wikidata doc.
- Kiwix bundles drop unchanged books.
- Gutenberg and Open Library have no checksums, so everything is refetched. `books/gutenberg/2026-10` and `2026-10-10` hold byte-identical 11.3 GB `txt-files.tar.zip`.
- arXiv unions earlier harvests correctly but does not implement `depends_on`, so retention can prune what it reads.

Fix, either:
- (a) seal complete generations by reference: manifest entries carry `in_generation`, and `depends_on` is derived from them; or
- (b) make each adapter's content set explicit, as arXiv does, and have the adapter raise when an expected file is missing.

Immediate guards:
- Wikidata raises if there is no `-all.json.bz2`.
- PubMed raises if there are no baseline files.

**C2. The Kiwix refine reads ZIMs in path order, so about one cluster decompression per article (~7 articles/s per worker on English Wikipedia).**

- `kiwix.py:438-455` builds 2,000-entry path-ordered tasks. The default cluster cache is 16 MiB.
- A 2,000-entry chunk of English Wikipedia touches 898 distinct clusters for 914 HTML entries.
- Measured cold: 1,166 articles in 161.9 s, decompressing 1,123 MiB.
- Redirects cost 3.39 ms each through `get_redirect_entry()`, about 10 h serial for 10M+ redirects.
- Reading clusters sequentially decodes 13.7k blobs/s per core, including SMB I/O.

Fix:
- Parse the dirent table once in Python, sort articles by `(cluster, blob)`, and build tasks from contiguous cluster ranges of about 64 MB.
- Resolve redirects from the dirent table.

**C3. The doc-collection deny-first gate fails open (`pol.py`, `pol2.py`).**

- `policy.py:523-531`: `require: [public-release]`, `[PD]` and `[license:*]` return ALLOW without checking the marking. A memo with no marking is allowed under `cia-reading-room` rules. Fix: `if req == "public-release" and marking.public_release:`.
- `policy.py:482-484`: only the first `DISTRIBUTION STATEMENT` is read, so "A … D" is allowed. Fix: `findall` and take the most restrictive.
- `adapter.py:414-427, 435-449`: a missing text layer, an extraction error, or an unsupported format (`.zip/.tif/.jpg/.docx/.epub`) gives text `""`, which is allowed. `Screen.text_found` is computed but never used. Fix: no text means HELD.
- Normalization bypasses (`policy.py:215-220`) all come back `allow:content=clean`:
  - the `ﬁ` ligature;
  - U+2011 non-breaking hyphen;
  - the soft hyphen;
  - `weapon-\nization` (hyphenated across a line break);
  - fullwidth characters;
  - spaced letters (`F I R E  C O N T R O L`).

  Fix:
  - NFKC;
  - strip U+00AD, U+200B–U+200D and U+2060;
  - map `\p{Pd}` to `-`;
  - join `-\n`;
  - also test a de-spaced copy.
- `normalize_pub_number` doesn't canonicalize dashes, so `FM-3-22.9` slips past the deny-series rule for `FM 3-22.9`.
- Origins are checked only on the first URL, while the fetcher follows redirects (`http.py:183`) to any host, plain http included.
- Git bundles are never screened.

## HIGH

- **H1. PubMed takes the DOI of a cited reference.** `pubmed.py:302` walks every `ArticleId`, including those under ReferenceList, and the last one wins. 6.9% of articles get a wrong DOI (2,075 of 30,000). Fix: `elem.findall("PubmedData/ArticleIdList/ArticleId")`.
- **H2. Wikidata parses whole entities to keep 0.76% of the bytes.**
  - `json.loads` takes 1.9–2.2 ms per line. Skipping `claims` is 9.2× faster.
  - Batches are by line count; a 1,000-line batch pickles to 79 MB.
  - The parent pickles about 1.5 TB in total.
  - Fix: batch by bytes, parse selectively, and let workers seek into the bz2.
- **H3. Release identity is the version string.**
  - Kiwix uses `max(zim_date)` with the re-release letter stripped, so re-releases and updates to non-newest books are never fetched.
  - Gutenberg and Open Library fall back to today's date.
  - Fix: the Kiwix version is `<max date>+<sha of sorted names>[:8]`; checksum-less sources plan by `(name, size, Last-Modified/ETag)`.
- **H4. Kiwix native ids depend on how many books the selector names.** Adding a second book prefixes every id, and the resulting 7M+7M diff runs through `fetchall` plus sets. Fix: always prefix, or pin `prefix_books`.
- **H5. The parent process caps throughput at about 8k docs/s.** Measured 120 µs/doc to unpickle plus write; the frozen slotted dataclass makes unpickling 4.2× slower than tuples. Fix: workers write Parquet shards; the parent only orders and commits them.

## MEDIUM

1. `html_to_text` keeps boilerplate: sidebar and navbox, `#catlinks` plus hidden categories, `.zim-footer`. The title is duplicated, and MathML is flattened (use `alttext`). Boilerplate is 1.5–8.4% of text.
2. `wikitext.py`:
   - A stray `{{` drops the rest of the article.
   - `<nowiki>` is mishandled.
   - `wikt:` and `:fr:` links leak into text.
   - `_DROP_ELEMENTS_RE` is quadratic on unclosed `<ref>`.
   - `<math>` and table captions are lost.
3. `wikimedia.py:480-498` duplicates `open_bz2` without the `indexed_bzip2` path.
4. No fault isolation: one bad record aborts the whole refine. Unsealed output is rmtree'd, so the refine restarts from zero.
5. `indexer.py:375-382` refines every sealed generation, not only the current one.
6. Kiwix keeps every `text/html` entry, including Stack Exchange `users_page=N` pages.
7. `archive_org` partitions are defined by scrape-API page number, which shifts as upstream changes.
8. PubMed:
   - a `FileResult` pickles to 77 MB;
   - workers are hardcoded to 4;
   - discovery re-fetches about 1,700 `.md5` files every run (about 10 min).
9. `diff.py:404-411` creates a new spawn pool for every flush.

## LOW

- `default_workers` ignores the cgroup CPU quota, and `access.workers` bypasses `WSP_MAX_WORKERS`.
- `stream_offsets` takes about 40 s for the English index.
- Open Library picks arbitrarily between `latest` and dated files, and leaks `names_db` on error.
- The Kiwix default "mirror" is the redirector itself.
- `parse_checksum_file` ignores bare-digest files.
- Wiki links to non-namespace-0 pages produce dangling ids.

## Design target

- Workers own I/O, decoding and writing. Each writes `part-<taskid>.parquet` plus `_DONE`. Resume skips finished tasks, and a final step composes the parts.
- Everything is bounded by bytes, with one persistent pool per refine.
- Per format:
  - ZIM: cluster-ordered tasks.
  - bz2: cached block indexes, with workers seeking directly.
  - PubMed: per-file shards with newest-wins and tombstones applied at compose.
  - Open Library: authors in a local SQLite first.
- Generation content is an explicit set of `(generation, file)`. Adapters fail loudly when a member is missing.
- Per-record errors go to `_rejects.jsonl`; the refine fails only above a threshold.
