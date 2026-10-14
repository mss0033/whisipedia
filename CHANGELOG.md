# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added: the Exam Practice Hall and the Screening Room (2026-10-13)
* `wsp://practice` drills the library's question pools (the NCVEC amateur radio pools): a syllabus with
  the viewer's progress, study by group with the answer and FCC rule shown, and mock exams drawn one
  question per group and graded at 74%. Keys 1-4/A-D answer, N or Enter moves on. Progress stays in the
  browser. A pool's document page offers the hall.
* `wsp://screening-room` (aliases `cinema`, `theater`): a document whose original file is video or audio
  plays in the room's players instead of the file panel: four skins (Theater 98, Brushed Steel,
  Desktop 95, Tube 2008; original designs), resume positions, keyboard control, an audio deck with a
  live spectrum. The room lists what to continue and which sites hold media, and a projection test
  records a short reel in the browser so the players can be tried before any film is on the shelves.
  No media is fetched: the collections stay a plan until the owner approves them.
* Document kinds `video` and `audio` (registry domains `film`, `lectures`, `video`; `audiobooks`,
  `radio`, `music`, `audio`): search facets, badges and the directory know them.
### Fixed: a cold search reads a gigabyte, not ten (2026-10-13)
* The disk's 8 MB read-ahead made each of a query's ~4,500 random page faults on the memory-mapped index
  pull 8 MB. `deploy/udev/60-whisipedia-readahead.rules` sets 256 KB; `wsp doctor` warns above 1 MB.
  Fresh searches through the API went from 12-20 s to 2-3.5 s.
* The API unit keeps 10 GB (its 4 GB ceiling evicted the index's file pages between queries); a search
  page's passage and content-hash lookups over the NAS run eight at a time; the exact-title pin no longer
  reads ten documents' text; a shared window asks the index once, three times deeper.
* A title whose key contains U+10FFFF no longer crashes the suggest build (the lexical rebuild died at
  its last step on one such title).
* Ingests are paced on the NAS link (`[vault] ingest_bytes_per_sec`, 40 MB/s) and yield to the owner's
  use of the library: a collection lane adopting PDFs at the link's full speed had starved the API.
### Changed: the search index stays on local disk (ADR-0012)
* Served from the NAS, the rebuilt index answered nothing (searches did not finish); the runbook backs
  a sealed index up to the NAS instead of pointing at it.
### Fixed: Refinery readers on the NAS map their id index from local disk everywhere (2026-10-13 review)
* The index job's readers (refresh, catalog build, featured pick, rank, spell) now use the local id-map
  sidecars (`[index] id_map_dir`) like the document store. Without them a reader on the NAS answered
  each point lookup by reading a whole row group of `_ids.parquet` over the link; the featured pick alone
  made thousands of such lookups per source.
* The index job writes a generation's id map as the generation goes live. The API used to build it on the
  first lookup after a nightly refine, streaming the whole `_ids.parquet` from the NAS inside a request
  while holding the reader's lock.
* A reader loads its id index under its own lock, and opens part files outside the lock that guards the
  row-group cache: readers of other row groups no longer wait for a network round trip.
* The document store remembers each source's current generation for five seconds: finding it is three
  metadata round trips on the NAS, and a search page asked once per hit.
* The change feed reads archived ranges (Parquet, maybe on the NAS) outside its lock: a slow read of an
  old cursor no longer holds up the start page's recent changes and every other reader.
* The nightly index unit runs with `MALLOC_ARENA_MAX=2`: Tantivy allocates with glibc malloc, and with
  about fifty threads the default per-thread arenas kept 2.5 GB of freed memory.
* The catalog remembers each source's current generation for its reload period and takes the known
  sources from the registry: a random pick had asked the NAS for every source's pointer and walked the
  Refinery's directory each time.
* A reader without an id map keeps its last two decoded `_ids.parquet` row groups (a history page asks
  the same older generations for one document). `/v1/portal`, `/v1/random` and `/v1/documents/{id}/history`
  run in the NAS pool (deadline and shedding) rather than FastAPI's default thread pool.

### Fixed: content profiles no longer hold the heavy lock; the health check restarts an API at its ceiling (2026-10-13)
* A content profile takes its own `PROFILE` lock instead of `HEAVY`. Paced to a share of the NAS link
  (`[integrity] profile_busy_share`), a profile of a big ZIM runs for most of a day; under `HEAVY` it kept
  every refine waiting and made the nightly `wsp index build` exit busy for that long.
* `tools/wsp-api-health` restarts the API when the API's own cgroup is at its `MemoryHigh`, even while the
  machine is under memory pressure: that pressure was the API's own doing (20 minutes of thrashing on
  2026-10-13 while the check declined to act).

### Changed: faster suggest priors, rank lookups and OSTI downloads (2026-10-12)
* `wsp index rank` writes suggest priors in bulk: the old ones are zeroed, each source's priors are loaded
  into a temporary table and applied with one keyed UPDATE, the prior index is dropped during the load
  and rebuilt once, the suggest trie is rebuilt once, and each source commits. Row-by-row updates had
  run at about 7,000 rows/s (12+ hours for 256M documents) in one transaction whose WAL slowed every
  search to 20+ s. `wsp index rank --reuse` redoes priors and featured articles from the existing rank
  file without recomputing it.
* Static rank lookups read one page of the rank file: a fence of every 512th key (4 MB for 256M
  documents) is written beside the rank file and kept in memory; a binary search over the 2 GB key array
  had faulted in about 28 pages per lookup (1.6 s of a cold search's 4.7 s).
* OSTI downloads run three at a time (one request a second): one 5 MB report takes about 30 s to serve.

### Fixed: link rank runs in bounded memory (2026-10-12)
* `wsp index rank` was killed at a 14.5 GB memory cap on the live library (255.6M documents, 640.7M
  edges): every per-node array (id keys, groups, degrees, ranks) and both key tables were held in
  memory, with sort copies on top. Node keys, redirects and links now go to key partitions in the
  index work directory and are resolved partition by partition (exact id, loose id, then redirect,
  as before); every per-node array is a file there, read and written in slices. Memory peaks at about
  8 bytes a document (the two weight vectors of the power iteration) plus bounded chunks: 1.16 GB
  peak RSS on a synthetic 50M-document, 152M-edge library, an estimated 2.8 GB at the live size.
  Scratch disk peaks at about 20 bytes a library link plus 24 bytes a document. The sidecar is byte
  for byte the one the old build wrote; a new `node_chunk` setting sizes the slices and the
  partitions, and destination buckets crowded by popular pages are split before they are sorted.
### Fixed: court opinions read as opinions (2026-10-12)
* CourtListener pages showed their text in a monospaced block: a one-line Supreme Court order
  ("Certiorari denied") read like a metadata card, and long opinions lost their paragraphs. The
  Navigator now renders court opinions with a case caption (court, date filed, docket, judges,
  author), the opinion in paragraphs rejoined across page breaks, and star pagination (`*80`) as page
  markers. A short order is framed as the court's complete order.

### Changed: content line narrowed (owner decision 2026-10-12)
* Now allowed: officially public military doctrine on weapons employment, marksmanship and weapons
  qualification, gunnery, fire support and fires, targeting, fire control, air and missile defence,
  mortar employment and weapon-system crew drills.
* The deny core keeps five categories: classified or restricted distribution (the marking screen,
  unchanged); explosives and demolitions; weapons and munitions technical data; CBRN weapons
  production; offensive cyber tooling. The categories "weapons employment and gunnery", "targeting and
  fire control" and "weapon-system crew drills" and their patterns are removed. Weapons guidance
  ("missile guidance") moves to weapons and munitions technical data. "Sniper", "gunnery" and
  "marksmanship" no longer count as history-prone nouns next to a core hit.
* Army rules: the FM/ATP/TC 3-09, FM 3-01, ATP 3-01., ATP 3-21.90, TC 3-20., FM 3-22, TC 3-22. and
  ATP/FM 3-60 series denies and the "gunnery" and "sniper" keywords are removed. TM 9-, TM 43- and
  TC 3-23. (demolitions) stay denied. The 19 per-title owner denies of 2026-10-11 are unchanged. Joint
  Publications: the JP 3-60 deny is removed.
* army-pubs-public and joint-publications will fetch the newly allowed series on their next run
  (joint-publications is still a candidate with no official channel, so it lists nothing yet).

### Fixed: legacy Office files linked as PDFs (2026-10-12)
* A `.pdf` link that serves an Excel 97, Word 97 or PowerPoint 97 file (an OLE2 compound file) is renamed
  to `.xls`/`.doc`/`.ppt` like the zip-based Office formats, and held for review (no text extractor reads
  them) instead of failing "not a PDF" on every fetch (five UK resilience attachments from 2011-2013).
  Original files of those and of other office formats are served with their media types.

### Changed: search latency (2026-10-12)
* The source cap re-runs a crowded query at most twice (was three times), and a query reads each stored
  document once (the cap and the hit list shared none of their reads). Measured on the live index: the
  third round cost 0.3-1.5 s per query and brought no title into the window that two did not.

### Fixed: API and Navigator audit (2026-10-12)
* View Source Data names the file that holds the page:
  * the archive a multi-ZIM page is in (it named the source's first archive);
  * the stored PDF of a collection document, with its size and hashes;
  * for a partitioned source, the partition that holds the document (refinery set ids never matched a
    vault generation, so a Connecticut map was reported under New Jersey);
  * for a borrowed page (the Wikipedia top-10 dump shown with the Kiwix page), that page's archive,
    with the new `Document.rendition` naming it.
  * Every member of a partitioned source's current set is `current` in `/generations`.
* A borrowed rendition tries each registry rule until one has the page.
* Cross-source links of text documents resolve to pages of Kiwix sources (up to 32 checks per
  document). The Navigator lists them under a text document ("Also in the library"), so a Gutenberg
  book reaches its illustrated edition.
* A ZIM archive that is still opening is a 503 the client retries, not "not in the library".
* Start Page "What's New!": site entries list only readable sites and link to the site (they opened
  "This site is not in the library"); a site's first generation is "new", later ones "updated".
* Time Machine links work for every generation id (`2026-08`, `pubmed26-u1671`, `2026-10-11.nj`, refinery
  set ids); only dates were accepted, and the others silently showed the current page.
* What's New keeps the newest 500 changes and renders at most twice a second. With the new
  `summarize=true` on `/v1/changes/stream`, a backlog (a whole generation landing) arrives as one
  `skipped` event and the page reloads its list, instead of millions of events.
* A directory category that lists only its best-known pages says "Showing 200 of 7,121" and points to
  the site's search (`DirectoryPage.total`). Site pages link to their shelves ("Browse this site").
* Search: result pages slide a window of ten, so pages 11–20 are reachable. "I'm Feeling Lucky" and
  "Did you mean" keep the kind and site filters. "define <word>" shows a dictionary box from the
  library's Wiktionary entry. The "map of …" promise is gone from Help and the examples: no readable
  map source can answer it.
* Busy is not "missing": Random, the directory and the Time Machine retry a 503 instead of falling back
  to old-daemon behaviour or saying "This source keeps no history".
* Error pages are not recorded in History, and the retry counter no longer keys on a page title. A 404
  for an old version (`?at=`) reports no gap and offers the current page. "Go to the site" appears only
  for a site that exists.
* Shortcuts and autolinks ("rfc 9110", "RFC 2616" in text) go to search, or stay plain text, when the
  library holds no such site.
* A live Kiwix page is dated by its archive's release (`2026-08`), as refined pages of the same archive
  are, not by when the copy reached the vault.
* Original files: at most 4 stream at once (others get 503 busy), so a stalled NAS read cannot hold
  Starlette's shared thread pool. Without a cache directory, previews go to a private temporary
  directory, not `/tmp/previews`.
* History and the Hotlist show the reader's own clock (they showed UTC); library dates stay in UTC.
* `/v1/suggest` describes what it matches; the Navigator drops suggestion fields the API never sends.
* Open Library pages say they are catalog cards, not books, and link to a search of the library's
  books for the title and, for an edition, to its work record.
* A text document with no text of its own (a template-built wiki page) says so and offers a search,
  instead of showing a blank page. A text document whose first line repeats its title no longer
  prints it twice. Start Page site
  groups are alphabetical, with "Other" last. Site entries in What's New read "Name (Oct 12)": the
  badge already says new or updated.
* Not changed: a trailing slash in an address is still dropped (F19). The only native ids that end in
  "/" are bundle book roots, which resolve to the same page without it.

### Added: read a collection document's original file (2026-10-12)
* `GET /v1/files/{doc_id}` serves the PDF, map scan or archive a collection document was refined from,
  read from that document's own generation (paths that leave it are refused). PDFs and web images open
  inline, with `Range` support; anything else downloads. The download name drops the fetcher's prefix
  and hash.
* `GET /v1/files/{doc_id}/preview` is a JPEG at most 2000 px on its long side: page 1 of a PDF
  (pypdfium2) or a downscaled TIFF or image (Pillow). It is cached under the API cache directory and
  rendered one at a time, in its own pool, so a 100-megapixel map does not hold up document reads.
* `Document.original` (API, SDK and generated TypeScript) tells a client where the file is.
* The file routes carry their own CSP: a PDF may be framed by the Navigator (`frame-ancestors 'self'`,
  not sandboxed, because Chrome's viewer refuses to run sandboxed); every other type is sandboxed.
* Navigator: an "Original file" panel above the extracted text. A map shows its rendered sheet, which
  opens the full map. Other PDFs open in the browser's viewer. Every original has Open and Download
  links. The panel is hidden in the Time Machine, which shows text only.
### Fixed: vault, orchestrator and lanes audit (2026-10-12)
* Lanes marked items done that never landed: any fetch outcome (killed, crashed, refused, every
  download failed) was followed by an ingest that exited 0. `wsp collections fetch` now exits with one
  code per outcome (0 complete, 2 refused, 4 listing incomplete, 6 partial, 7 alerts, 8 nothing new),
  `wsp ingest --source S --version V` answers for that one version (8: nothing planned), and
  `tools/lanes/lane-run.sh` marks an item done only after a complete fetch and an ingest of its cycle.
  A stuck version no longer fails the other versions or partitions. Exit codes are in the
  document-collections runbook.
* A re-fetch after an interrupted ingest was refused for good ("already staged with different
  content": new `fetched_at` stamps). A staged, unsealed file is replaced when the new bytes verify
  against the plan's checksum (ledger `vault.restaged`).
* A crash between the link and the unlink of a no-replace publish (no renameat2) blocked adoption and
  sealing; the move is now finished on the next run. The duplicate check of a re-fetched file no
  longer deletes bytes a concurrent fetch renamed onto it.
* Promotion compares generations in natural order (`-r10` after `-r4`) and checks "a newer one is
  current" under the pointer lock, in `wsp ingest` and in the orchestrator.
* Fetch locks live in the machine's lock directory (the orchestrator's `/tmp` is private) and are
  removed after each download. Busy heavy steps take their locks before reading the registry and the
  NAS. A restarted orchestrator no longer deletes the work of a step unit that still runs. Sources
  flipped to `index: false` leave the search index.
* Memory: the background slice is sized for a 16 GiB WSL with shares that fit under its MemoryHigh
  (orchestrator, a new fetch sub-slice for lanes, one heavy job at 4G); `tools/wsp-bg` refuses a job
  as big as the slice, passes SIGTERM to the job, and can hold the heavy lock across a whole rebuild.
* Ops scripts in the repo: `tools/lanes/` (lane runner, resumable rebuild that stops at a failed
  step, resume after reboot that waits for the NAS and fails loudly). The API health check survives
  host sleep and does not restart the API while the whole machine is short of memory. The USR1 stack
  dump is documented with `--kill-whom=main`.
### Fixed: collections and policy audit (2026-10-12)
See `docs/audits/2026-10-12/collections-adapters.md`.
* **Review decisions (CA-1, CA-16).** Rejecting a review proposal now only closes it.
  `wsp proposals reject ID --deny KEY` denies the named rows and nothing else, and
  `wsp proposals accept ID [--only KEY]` allows rows. Decisions are keyed by document key (one edition) and
  go to a machine-owned `registry/filters/decisions/<source>.yaml`. The hand-written rules file is never
  rewritten. A source without `filter.rules` is refused. A content-screen hold needs
  `allow_content_review`, and an owner allow never lifts a deny-core title; `override_safety: {key: reason}`
  does that. Open proposals are not filed again for the same documents.
* **Registry repairs (CA-1).** `cisa-protective-security.yaml` is restored with its comments, including the
  Design-Basis Threat note. That removes the 43 denies a bulk reject wrote. The five unwired files (jts,
  nga, uk, who, fema) are deleted: all 1,090 entries came from closing superseded proposals
  ("superseded: re-fetched under the owner's rules"), and none was a reviewer's decision.
* **Deny core (CA-3, CA-4).** The deny core is grouped by the owner's categories and written as patterns
  that match word forms and hyphen/space variants. It names techniques, not bare nouns. History-prone
  nouns ("sniper", "gunnery", "booby traps", "minelaying") count only next to a core hit. "Kill chain",
  "exploit code" and "weapons effects" no longer deny. The Army rules deny the fires, air and missile
  defence and mortar series (FM/ATP/TC 3-09, FM 3-01, ATP 3-01., ATP 3-21.90).
* **Markings (CA-5, CA-6, CA-18).** The screen now catches "has not been approved for public release",
  FOUO in any case, "Distribution authorized/limited to …", UK OFFICIAL-SENSITIVE and RESTRICTED, a bare
  CONFIDENTIAL or letter-spaced banner, SBU, and "(CUI)". Army titles are screened as printed.
  `declassified_ok` needs a real release stamp on the same page as the banner. A document that only
  mentions the EAR or ITAR is held, and only an export warning denies.
* **Downloads (CA-2, CA-17, CA-21).** An HTML page, a JSON or XML body, or an error message where a PDF
  was expected is a failed download, not a document. Office, zip and text files are still renamed to
  their real format. Zip members are streamed within size and ratio caps, each member's front matter is
  screened, and a restricted member rejects its archive. The throttle charges probes, redirect hops and
  retries.
* **Catalogs and the index (CA-7, CA-8, CA-11, CA-13, CA-14, CA-19).** The orchestrator path builds the
  catalog the way `wsp collections fetch` does, through the new `GenerationFinalizer` port. A document that
  current policy denies, or whose format the source no longer takes (`access.formats`; USGS topo is
  GeoPDF only), is left out at refine time, so the index drops it. Withdrawn documents get a banner,
  historical ones too. Unchanged files are skipped by listing MD5 or size, and carried metadata is
  refreshed. A changed catalog with no new files still becomes a generation. An undated newer edition is
  no longer superseded by an older dated one.
* **Listings and versions (CA-9, CA-10, CA-15, CA-22).** Every paged discoverer flags a short or capped
  listing, and an OAI-PMH error is an incomplete listing. An empty resolve is retried and never cached or
  read as a withdrawal. Only a resolver's explicit "no documents" is cached. A fetch for a sealed version
  takes the next `<cycle>rN` version (the lanes' form: `-rN` sorts before the cycle once a partition
  suffix follows, and would never be promoted). A fetch while *another* version of the source (the same
  partition) awaits ingest exits 5; the same version may be fetched again, which is how an interrupted
  ingest is repaired. The lane runner ingests the waiting versions by name, then fetches again, and
  follows the cycle a fetch moved to. `wsp ingest`
  reports files left in `_incoming` for a sealed version as failed. NGA items without an id are kept,
  and the resolve cache is closed on errors.
* **Readers (CA-12; coordinator findings).** The git-mirror sources are `index: false` until a bundle
  refine exists. Titles have no file extensions or `%20` debris, sentence fragments fall back to the PDF
  title or its first heading, and USGS sheets read "East Greenwich, RI (1942) — 1:24,000 topographic map".
  Document keys keep their spelling; `access.decode_url_keys` decodes escapes for new sources only.
* **Registry (CA-20).** A `doc-collection` sub-schema is added, and unknown filter keys raise.

### Changed: document collections admit all public material outside a narrow content core (owner decision 2026-10-11)
* Still excluded: classified or restricted documents; explosives and demolitions; weapons and munitions
  technical data; weapons employment and gunnery; targeting and fire control; weapon-system crew drills;
  CBRN weapons production; offensive cyber tooling.
* `Rules.builtin_review` now defaults to off, so a document is no longer held just for mentioning
  "chemical" or "combat". A source can opt back in.
* `SAFETY_DENY` is narrowed: "munitions", "small arms", "mine warfare", "fire support", "field
  artillery", "joint fires" and "close air support" are replaced by munitions manufacture and
  effectiveness, mine laying and gunnery.
* Registry: FEMA (x2), OSTI, DOE, CISA, Army, Joint Publications, MCDP and Calhoun now default to allow
  instead of review. WHO IRIS drops its review keywords.
* The Army filter keeps only the weapons, gunnery and targeting series and 19 core titles. The Joint
  Publications filter denies only JP 3-60 (targeting).

### Fixed: search and index audit (2026-10-12)
* Search found no other-language page for a query without function words: the tiers searched only the
  query's language (English when unsure) once it had enough matches, so "Berliner Mauer" listed no German
  page. Every query now searches every language; every-word matches lead (and when there are enough the
  any-word query is skipped, also for the Oracle's 200-hit window); the language is a ranking preference
  only. Pages of any size tile one list (they mixed tiers: repeats and gaps).
* Totals were exact when they were not: a list shortened by collapsing duplicates read as "every match",
  and the Oracle cache then never ran a deeper request. The Oracle now says whether its retrievers ran
  out. Only true duplicates collapse (same source, title and text): separate questions or works that
  share a title stay.
* Title queries found the book they name: "huckleberry finn" across all sites showed no Gutenberg book
  (198 Open Library editions filled the 200-hit window) and within Gutenberg a Hungarian translation led.
  No source takes more than an eighth of the Oracle's lexical window while others match, and for a
  page-name query each source's best near-exact title follows the exact one, the query's language first.
* Fusion no longer penalises the embedded source: a lexical hit's dense rank is the better of its dense
  and lexical ranks, for every source (mdwiki's best lexical match had sunk to rank 183). No full scan
  of the vector store on a request any more.
* Resumed index loads are idempotent: a part committed before its checkpoint is replaced, never added
  twice, and orphan removals resume by doc id (a recount skipped orphans that stayed searchable).
* `wsp index build` refuses a `lexical/` rebuilt without `index-state.json` (it indexed nothing and
  reported success); a source turned `index: false` leaves the search index.
* The result cache is cleared when another process commits to the lexical index. History and dated
  reads write no id-map sidecar for older generations; a truncated sidecar is rewritten. Dense-only hits
  carry their language. Site preferences match project names, not substrings.
* Suggest: the two tries are replaced together (a versioned directory and a pointer), and unranked
  titles are shared by the sources (one source, the last by id, took the whole budget: "dog" completed
  only to Wiktionary phrases).

### Fixed: collections and ranking, second pass (2026-10-12)
* A partial listing (a timed-out page, a failed resolve) never becomes a generation and never marks
  documents withdrawn: the fetch stops with exit 4 and lanes retry. archive.org listings are cached a
  week; XML answers with stray control characters parse (OSTI had stopped at 600 of 51,881).
* Sources that listed nothing now list: Borden (Gatsby page data; ~676 chapter PDFs), USGS
  publications (one query per topic), Army CMH (GovInfo D 114, 236 books), DISA STIGs (the quarterly
  SRG-STIG library zip; zips are screened by README and member names). JP and MCDP have no official
  channel yet (GovInfo D 5 and D 214 hold other titles).
* Screening: firefighting 'fire control' is not weapons fire control; a bare SECRET heading is not a
  banner (only at a page edge); GOV.UK HTML publications are kept as HTML; Word (.docx) is read.
* Fetch prints every rejection with its rule; resumed fetches keep MD5-verified downloads; per-source
  concurrency (starts still 1/s) and archive.org connections.
* Search: tiered lexical search (every word in the query's language first: 17 ms instead of 0.4-3 s on
  130M documents); PubMed has no ranking bonus (rank_prior: 0); same-title duplicates collapse; how-to
  queries prefer Wikibooks inside bundles and MDWiki; ZIM-only sites show page counts and random pages.

### Fixed: browsing and search on the live library (2026-10-12)
* Pages failing at random: a site's recent changes scanned 200,000 feed rows under the GIL, and
  concurrent site pages starved the API's thread pool. The feed has a (source, seq) index now.
* Stack Exchange documents timed out (503): the ZIM pool kept one open archive per Kiwix source, and the
  115 GB Stack Overflow archive takes 99 s to open over SMB. All archives stay open and are opened ahead
  of time; the Navigator retries a 503 by itself ("This page is still loading").
* A restart answered 503 for a minute or more: the last library snapshot is saved and loaded at start.
  The first portal request read every Parquet footer of every source (13.5 s): catalog counts come
  from _SUCCESS.json and random picks from the memory-mapped id index (0.34 s cold).
* Planned sources with nothing downloaded, and raw data, are no longer listed as sites (portal,
  directory, webring, Related). Random page works on ZIM-only sites.
* Wikimedia links (HTML and plain text) open the library's copy. Stack Exchange tag listings are not
  documents and never results.
* Ranking: mdwiki (the only source with passage embeddings) won almost every query; sources without
  embeddings now get their lexical rank on the dense side too. Near-ties prefer the query's language.
  Snippets are cut only for the page shown, and come from the Refinery (Tantivy no longer stores text:
  -105 GB).
* Collections: a re-fetch of an interrupted ingest failed on identical staged files; resumed fetches
  re-downloaded everything; restarts after midnight UTC started a new generation; archive.org round
  trips held the CIA reading room to 0.3 requests/s (now up to 4 in flight, starts still 1/s).
* API memory: the Refinery id index is memory-mapped; model staging memory is trimmed; a health timer
  restarts an API that stops answering.

### Changed: issuer publication counts as release (owner decision 2026-10-11)
* New distribution requirement `published`: a document the issuer posted openly is released even when
  it prints no release line. 17 publisher-hosted sources use it. Restrictive markings still deny
  first: classified without a release stamp, FOUO/CUI, export control, Distribution B-F. DoD
  sources keep requiring Distribution A.
* archive.org batches can be stable hashes of the identifier (`buckets: N`). The CIA reading room uses
  100. Collection fetches report resolve progress every 500 items.

### Fixed
* API hang (2026-10-11): the API sat over its memory ceiling, which throttles rather than kills, and
  one start-page request took 3.8 hours.
  * The Refinery id index (20 B/doc, 0.8 GB for wikipedia-top10) is now a memory-mapped sidecar
    (`_ids.{h,loc}.npy`), written on first use. The document store no longer charges it to its budget.
  * Freed model-staging memory is trimmed after the Oracle loads, and the unit sets
    `MALLOC_ARENA_MAX=2`.
  * Runtime overrides capping the API at 3G were removed.
  * New `whisipedia-api-health.timer` (`tools/wsp-api-health`): restarts the API when the start page
    does not answer in 60 s, twice in a row.
  * Measured after the fix: anon 1.6-1.9 GB, down from 2.5-3.9 GB; start page 0.05 s warm.
* Vector search scans exactly once the candidate set covers the whole store. Before, a filtered search
  over many near-ties could miss matches in the approximate graph (an intermittent test failure).

### Fixed: backend audit 2026-10-11 (docs/audits/2026-10-11/)
- **Orchestrator.**
  - Heavy steps (refine, index, profile) run in capped child units, one at a time machine-wide (`HEAVY`/`INDEX` locks).
  - Crashes are counted per step (two in a state: backoff and a `crashed:` alert; never quarantine).
  - Lock, busy and NAS failures defer without an attempt.
  - The document-policy screen always runs, even when digests verified.
  - The promotion gate runs before the index step, so held content is never searchable.
  - Rows are version-checked; older releases never move CURRENT backwards (`wsp approve --force`).
  - No busy loop; a watchdog; workdir cleanup; crash-loop, stuck and RSS alerts; `wsp retry`. Schema v5 migrates on open.
  - A release with nothing new is superseded quietly.
- **Vault and fetch.**
  - Signing keys are pinned (never auto-created once the vault has signed content).
  - The repair ladder has a circuit breaker; a verified `wsp undegrade` clears DEGRADED.
  - I/O errors are UNREADABLE and never repaired; a mount canary runs before each scrub slice.
  - Ledger torn-append recovery (`wsp ledger recover`).
  - Unique temp files, with no-replace publish via `renameat2` or link (verified on the NAS share).
  - Per-source and per-generation vault locks.
  - Parity is bound to bytes verified in the same pass.
  - Checksum-less files are corroborated by a second full fetch; resume validators are required.
  - Expired signed redirects are re-resolved; HTTPS→HTTP is refused; a token-bucket scrub rate.
  - An incremental scrub inventory; manifest signatures are checked on read and paths validated.
- **Adapters.**
  - The doc-collection gate fails closed: requirements are checked against markings, all distribution statements are read, a document with no text layer is held, and text is normalized against ligature, dash, soft-hyphen and spacing bypasses.
  - Origins are enforced on every redirect hop; git sources are allow-listed and screened.
  - Generations read their full content set across earlier generations (PubMed, Kiwix bundles, Gutenberg, Open Library, arXiv; `depends_on`). Wikidata never publishes an empty lookup.
  - Cluster-ordered ZIM refine (about 10× faster).
  - PubMed DOIs come from the article, not its references.
  - Selective Wikidata parsing; byte-bounded batches; Kiwix re-release versions; boilerplate-free HTML text.
- **API.**
  - ZIM reads happen in worker processes (a SIGBUS kills a worker, not the API).
  - A background library snapshot means requests never touch the NAS (`/v1/system` 5 s → 2 ms).
  - Async health; bounded NAS pools with 503 + `Retry-After`.
  - One-retrieval search with consistent facets (offset ≤ 200).
  - Cached, gzip and ETag documents (text only with `fields=text`).
  - `/go` finds ZIM pages; a read-only change feed; no signing key created. API contract 1.4.0.
- **Rank, spelling, catalog, Oracle.**
  - Explicit site words; language-aware stopwords (MRR@10 0.41 → 0.79 on real titles).
  - Redirect aliases keep their language path.
  - Rank uses 64-bit vectorised keys, a CSR and per-language PageRank (32M nodes in 13 min, 3.7 GB).
  - `WSPSPEL2` per-language spelling with one shared normalizer.
  - Per-language featured articles and directory.
  - Centred binary codes (recall@10 0.81 → 0.96); MADV_RANDOM reads.
  - A crash-safe vector store; the chunker keeps whole books; CJK/Indic tokenization.
- **Index.**
  - Streaming, resumable refresh; thread-safe Refinery reads; capped suggest builds.
  - Refine flushes by size; set ids never repeat.
  - Only current generations are refined.
  - Safe visibility order (documents, then search, then feed).
  - The id index loads lazily.
  - Indexes built with older analyzers are refused: **rebuild the lexical index**, then rerun `wsp index rank` (`WSPRANK2`) and `wsp index spell` (`WSPSPEL2`).

### Fixed (2026-10-11 resource and robustness pass, first round)
- The API could crash (double free, SIGSEGV) when request threads read the same Parquet part at once:
  `RefineryReader` now serializes reads per open file (and bounds open files), with a 16-thread
  regression test that crashed the old reader.
- Index refresh no longer materializes a generation diff: `wsp_index.delta` keeps a 32-byte key per
  old document and reads full rows only for changed row groups; `sync.refresh` commits and publishes
  part by part, with a checkpoint stored atomically with the change-feed records, so a crash resumes
  without losing or repeating a record. First loads add without delete terms (Tantivy held ~1.2 KB
  per pending delete); without the old Refinery generation, stale documents are found from the index.
- Tantivy writer: 4 threads and a 1 GiB heap (measured 4× faster than the default), orphaned segment
  files from crashed runs are collected; meta SQLite gets a busy timeout, a WAL size limit and a
  checkpoint on close.
- Suggest: opening an index never rebuilds a large trie (a refine step that merely opened the index
  took 12.8 GB), commits during bulk loads defer the rebuild, rebuilds are capped at
  `suggest_max_keys` best-ranked keys, and the heavy-prefix fallback scans at most 4,096 keys.
- Refinery writer flushes by text size as well as document count (50,000 Gutenberg books were ~77 GB
  at flush) and sizes row groups by text; set-generation numbers never repeat after a set is deleted,
  and a sealed set is never removed; duplicate resolution reads each row group once; renames are
  followed by a directory fsync. Shadowed rows are kept in numpy.
- `wsp ingest` no longer skips files that `wsp collections fetch` just wrote (the settle wait now only
  applies to downloads that can still be growing) and exits 4 when a version is incomplete, so lanes
  cannot mistake "nothing ingested" for success.
- A partitioned source's Navigator site card now sums every current partition.
### Added
- `deploy/systemd/whisipedia-background.slice`: one memory, swap, CPU and IO budget for the
  orchestrator, index refreshes and ad-hoc jobs (`tools/wsp-bg NAME [MAX] -- CMD`); the API has its
  own memory ceiling.
### Changed
- usda-farmers-bulletins: the Internet Archive microfilm scans are rejected as a channel (owner
  decision 2026-10-11: not an official mirror); the agency's own 31 bulletins remain.
### Added
- Partitioned sources (ADR-0010, port revision v0.4): `access.partitioned: true` sources such as
  `usgs-historical-topo` (one state per run) and `cia-reading-room` (batches `b0001`…`b0090`) keep
  one generation `<cycle>.<partition>` per partition. `FileVault.promote` writes
  `CURRENT.d/<partition>.txt` (plus `CURRENT.txt` as before); `Vault.current_set` is the current
  generation of every partition; the indexer refines each partition once (`refinery/<id>/_parts/`)
  and indexes their union as a hard-linked set generation, so search, documents and history cover
  every partition fetched so far. `wsp collections fetch --partition` carries over only that
  partition's catalog; `wsp ingest` promotes unless a newer generation of the same partition is current.
- Retention plans partitioned sources per partition (`plan_partitioned_retention`) and never prunes a
  generation a current one reads from (new optional adapter capability `GenerationDependencies`,
  implemented by `doc-collection` from its catalog); `FileVault.prune` refuses every generation of the
  current set and the newest of each partition.
- Document policy in the release pipeline: `VerdictStatus.REJECTED` / `HELD`. A rejected document is
  deleted and a held one quarantined in the orchestrator work directory, the release goes on without
  them (ledger `release.withheld`, alert `withheld:<source>/<version>`, one doc-review proposal via
  `Wiring.on_withheld`); withholding never counts as a checksum failure or pauses the source.
- Source integrity (ADR-0009): per-release provenance (URLs, redirect chains, TLS peer certificates,
  HTTP validators, which mirror served which byte ranges, rejected mirrors, spot checks) stored as a
  signed `PROVENANCE.json` next to the manifest; digests re-fetched through an independent channel
  (Wikimedia checksum lists from the primary, Kiwix `.sha256` from `download.kiwix.org`, sidecars
  from a second mirror); any channel or mirror disagreement quarantines the release and pauses the
  source; origin pinning on first use with holds for unknown domains and alerts for new hosts or a
  changed certificate issuer; content profiles compared before promotion (namespaces, size
  distribution, changed/deleted fractions, new MIME types, `<script>` density, binary/executable
  content) with a signed `PROMOTION.json` and owner holds; known-good anchoring in retention;
  `ops.integrity` in `/v1/system` and an "integrity" check in `wsp doctor`; runbook "Investigating a
  held release / possible compromise".
- The API marks interactive use (`<state_dir>/last-interactive`); scrub and parity backfill pause
  while it is recent (`vault.yield_to_interactive_seconds`).
- Link-analysis static rank: per-source and cross-source PageRank over `DocRecord.links`, computed
  offline in bounded memory (`wsp index rank`, also the last step of `wsp index build`), blended into
  BM25 and fusion, and used for suggest priors and the portal's featured article.
- "Did you mean": noisy-channel spelling correction over the index's own vocabulary (symmetric-delete
  candidates), offered only for sparse or low-score results (`wsp index spell`).
- `SearchResults.scope` (API 1.3.0): a query naming a site reports where it was searched; the
  Navigator says quietly when it fell back to the whole library.
- Eval harness: `--rank-weights` and `--typos` ablations.
- Document collections (PLAN D11, D13, D14; research doc §5): the `doc-collection` adapter with
  discoverers for index pages (follow, table rows, detail pages), official APIs (NGA MSI, GovInfo,
  Congress.gov, NTRS, OSTI, USGS Publications Warehouse, GOV.UK, DSpace 7, TNM), OAI-PMH,
  archive.org (provenance recorded), S3 listings and git repositories; a deterministic document
  filter (series allow/deny lists, front-matter marking parse for Distribution B–F, FOUO/CUI,
  classification and export-control warnings, built-in §13 safety lists, keyword screen; unknowns go
  to owner review) with rules files in `registry/filters/`; edition currency (superseded chains,
  historical collections and cut-off dates) in a per-generation `meta/documents.jsonl`.
- `wsp collections dry-run` (metadata-only survey with filter counts and size estimates) and
  `wsp collections fetch` (≤ 1 request/s per host, restricted files deleted with an alert, held files
  quarantined with one review proposal per batch, `_incoming/_plans/targets.d/` ingest plans);
  `wsp ingest` reads `targets.d/*.json` and honours `meta.relpath`; accepting or rejecting a review
  proposal records the decision in the source's rules file.
- Historical banner (D13): `DocRecord.meta` (additive, stored as an optional Refinery column that
  older generations simply lack), `wsp_core.banners`, `Document.currency` and `Document.banner` in the
  API and SDK, and a period "Notice" box on Navigator document pages.
- `wsp plan multilingual` (D15): per-language selection of the best value-per-GB Kiwix ZIMs and
  Wikipedia text dumps within `[curator.multilingual] budget_tb_per_language`, written to
  `registry/plans/multilingual.yaml`; Kiwix selectors accept `name@flavour`.
- Registry: official access channels for the WAF-blocked sources (`access.mirrors`), discovery
  configurations for the top picks, D14 plans for `cia-reading-room` and `usgs-historical-topo`,
  `doe-fundamentals-handbooks`, `crs-reports`, ten `kiwix-multilingual-<lang>` bundles and
  `wikipedia-text-extra`; `[curator.api_keys]` for api.data.gov keys (local config only);
  runbook `docs/runbooks/document-collections.md`.
- `wsp-oracle` (Phase 3, ADR-0007): section-aware passages, a multilingual embedder on the GPU,
  a binary-HNSW + int8 vector store, a deterministic query router, hybrid retrieval with
  cross-encoder reranking, a resumable embedding job
  (`scripts/embed_sources.py`), an evaluation harness with a 125-query gold set, `build_oracle()`
  for the composition root, and `VectorIndex`/`Embedder` contract suites.
- The remaining T1 source adapters (`docs/components/adapters.md`):
  - `arxiv`: OAI-PMH metadata from `oaipmh.arxiv.org` (≤ 1 request / 3 s, resumption tokens,
    incremental `from=`) plus the newest PDF months from the public GCS mirror; `html_ref` points at the
    newest PDF version; optional PDF text with `pypdfium2`.
  - `wsp ingest` seals the arXiv bulk pull chunk by chunk as part generations `<snapshot>.pdf.<chunk>`,
    only for chunks the pull has finished, by rename after the MD5 check (`FileVault.adopt_many`).
  - `pubmed` (baseline + updates, newest version wins, `DeleteCitation` honoured), `courtlistener`
    (opinions joined with clusters, dockets and courts through a local SQLite lookup), `open-library`
    (explicit catalog records), `gutenberg` (catalog, RDF and plain-text feeds streamed from the ZIP,
    linked to the Kiwix ZIM), `wikidata` (multilingual entity documents plus a local QID lookup for
    entity linking), `osm-planet` (named places with Wikidata QIDs via pyosmium).
  - Parallel bz2 decoding for single-stream dumps (`indexed_bzip2`), about 8× faster on 15 threads.
- Phase 5 "Autonomy": a Refinery-backed catalog (featured article, On this day, directory categories,
  uniform random pages; precomputed at index time); par2 parity for all sealed generations
  (`vault.parity_scope`), created and backfilled after sealing with a signed `PARITY.json` sidecar,
  verified by the scrub and used by the repair ladder; real retention pruning guarded by a last-copy
  oracle from cached upstream offers; Curator bots (Kiwix OPDS watcher, Wikimedia top-10 ranking,
  registry health probes, adapter contract smoke, gap miner) feeding the policy gate, proposal files,
  a local registry overlay and `wsp proposals`; System Monitor operations data and `/v1/proposals`
  (API 1.2.0); systemd user units with `wsp service`, `wsp doctor`; runbooks in `docs/runbooks/`.
- Integration of the component packages: port revision v0.2 (ADR-0005) with typed fetch results and
  errors, `Vault.adopt`, a separate `Warden` port, `Ledger.tail` and idempotent appends, document
  aliases, and the browsing ports moved into wsp-core; a real composition root (`wsp_cli.bootstrap`,
  `wsp_cli.wiring:build_wiring`) with an Ed25519 signing key generated outside the repository; operator
  commands `wsp ingest`, `wsp index build`, `wsp scrub`, `wsp drill`, `wsp approve|reject|resume` and
  `wsp status`; an offline end-to-end test from `_incoming` to API search; `config/local.toml.example`.
- Project plan, source research, source registry seed, initial ADRs, repository hygiene configuration.
### Removed
- **Generative summaries, by owner decision (ADR-0008):** the Navigator's "Summarize these results"
  button, `POST /v1/summarize` and its `Summary*` schemas (API 1.3.0), `summarize` in the SDK, the
  `Generator` port (port revision v0.3), the llama.cpp generator and `wsp-oracle-llm`,
  `[oracle.generator]`, `[api] demo_generator` and the summary-faithfulness evaluation. The results
  page is fully extractive; dense retrieval and reranking stay.
### Changed
- par2 runs with `-t2` by default (`vault.parity_threads`, was 4) and `-m256` (`vault.parity_memory_mb`).
- Orchestrator state schema v4 (`origin_pins`, `provenance`); `orchestrator.shutdown_grace_seconds`
  defaults to 10 (was 60).
- Search totals and facets now count the same routed scope as the results (`rfc 9110` showed 0 hits
  with 28 in the facets).
- A registry source without `update.cadence` follows `acquirer.cycle`; explicit cadences are always
  overrides (`acquirer.follow_cycle_cadences` removed). `pytest` is now the optional `wsp-core[testing]` extra.
- Default update cycle is monthly instead of weekly, with quarterly as a single config option (PLAN §6.7).
### Fixed
- Partitioned collections (ADR-0010): only the last ingested partition was current, searchable and
  safe from retention; a partition's fetch carried another partition's catalog over (labelling its
  documents `withdrawn-from-index`); `wsp ingest` refused to promote a partition whose cycle sorted
  before the current one. One restricted or held document no longer fails a whole orchestrator
  release, counts as a checksum failure or auto-pauses the source.
- Browser-test findings (docs/testing/e2e-findings.md): Stack Exchange book front pages linked outside
  their book (E2E-4); re-released ZIMs (`_2026-07a`) were listed under a name that did not open
  (E2E-3); "wikipedia Alan Turing" did not jump to a page served straight from a ZIM (E2E-7); the
  oracle's result cache cut filtered result pages short (E2E-5) and the top hit depended on the page
  size (E2E-6); facet counts now count each kind on its own; live page text no longer includes
  script and style source (E2E-9); the HTML rewrite drops inline styles, which the Navigator's CSP
  reported as violations (E2E-1).
- Slow site list and System Monitor on a busy NAS (E2E-2): the vault caches parsed manifests
  (revalidated by mtime and size), the API reuses site cards for 30 s and refreshes them in the
  background, the ledger replay no longer blocks requests, and the Navigator renders a page after
  waiting at most 3 s for the site list.
- Lexical ranking: documents matching every query word get a coordination bonus
  (`index.coordination_boost`), so one rare word plus a source prior no longer outranks pages about
  the whole query.
- The orchestrator now stops within seconds of SIGTERM instead of being killed after systemd's
  `TimeoutStopSec`: downloads, scrub, parity and par2 are cancelled, job threads are daemon threads
  and a lingering non-daemon thread no longer keeps the process alive.
