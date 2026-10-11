# Whisipedia — Project Plan

> A local, verified, self-maintaining library of the best openly available
> stores of human knowledge, surfed through a late-80s/90s-style offline web
> browser with a fast natural-language search engine built in (an "offline Google"). It is also the seed for **Whisipedia
> Living**, a second project where models keep improving a separate,
> writable copy of the data.

| | |
|---|---|
| Status | Draft v1 for review |
| Date | 2026-10-09 |
| Owner | Repository owner |
| Companion docs | [`research/SOURCES.md`](research/SOURCES.md), [`../registry/sources.yaml`](../registry/sources.yaml), [`adr/`](adr/) |

---

## 1. Goals and non-goals

### 1.1 Goals

| # | Goal | Measured by |
|---|------|-------------|
| G1 | **Preserve.** Keep a known-good, verifiable copy of high-value knowledge sources. | Every stored byte is covered by a signed manifest. A full scrub passes every 30 days. |
| G2 | **Stay current.** Find and apply upstream updates without manual work. | Each source's lag behind upstream stays within its SLO (see §6.6). |
| G3 | **Survive damage.** Detect and repair corruption, failed downloads and bad upstream releases. | Corruption drills (§8.5) end in automatic repair or a clear alert. Silent loss never happens. |
| G4 | **Improve the selection.** Find new sources and retire weak ones, within set policy. | Discovery files proposals every update cycle. Promotions follow the rules in §5. |
| G5 | **Surf and search.** Browse an offline web in 90s style, with an offline natural-language search engine. | Search results p95 < 100 ms. Every page and link resolves locally. Zero network calls. |
| G6 | **Work without our software.** A person can browse the raw data with ordinary tools. | Every directory has a README. Formats are open and documented (§7). |
| G7 | **Be built well.** Clean public APIs and swappable parts, with repo hygiene fit for a public project. | Contract tests for each port. Versioned OpenAPI spec. CI gates (§12). |
| G8 | **Seed Living.** Give the second project stable IDs, a change feed and a safe overlay model. | The Living interfaces in §11 are frozen at v1 before Phase 6. |

### 1.2 Non-goals

- **No copying the whole Internet Archive or Common Crawl.** These run to petabytes and are mostly uncurated. We take curated subsets only.
- **No pirated or "shadow library" content** (LibGen, Sci-Hub, Anna's Archive, Z-Library). This is a firm legal and ethics line. See §13.
- **No redistribution.** This is a private, personal archive. Licenses are tracked so that redistribution stays possible later, but nothing is published.
- **No model ever writes to the golden library.** Only Living, working on its own copy, may change content (§11).

---

## 2. Environment and the constraints it imposes

Measured on 2026-10-09:

| Resource | Observed | Consequence |
|---|---|---|
| NAS `/mnt/z` | 153 TB total, **123 TB free**. SMB3 (`cifs`) from `192.168.1.169`, mounted **`soft`**. | A `soft` mount can return `EIO` on timeouts. Every write must be atomic and checked by reading it back (§8.2). Never put SQLite, LMDB or any lock-dependent database on SMB. |
| NAS filesystem on the server side | **Unknown** (Open decision D1) | If it is ZFS or Btrfs, we turn on read-only snapshots and scheduled scrubs as a second integrity layer. |
| Workstation | WSL2, 16 cores, **23 GiB RAM**, RTX 4090 (24 GB), 619 GB free on the local disk | Hot indexes go on local storage. RAM is the tight spot for vector search, so we use quantized or disk-backed ANN (§9.4). The 4090 runs embeddings and reranking. |
| Runtime host | WSL2 only runs while Windows is up | The scheduler must catch up on missed runs (§6.5). Moving the updater onto the NAS is Open decision D6. |
| Network | Unknown ISP cap | The first download is about 20–35 TB (§4). Open decision D7. |
| Tooling present | Python 3.12, uv, Rust/cargo, Node 24, Docker, git + LFS | Matches the chosen stack (ADR-0004). |
| Tooling missing | `gh`, `par2`, `b3sum`, `aria2c`, `rclone`, `zimdump`, `kiwix-serve` | Install them in Phase 0. The service has Python equivalents, so the CLIs are optional. |

---

## 3. System overview

```
                       ┌────────────────────────── upstream ──────────────────────────┐
                       │ Kiwix OPDS · Wikimedia dumps/incr · OSM · Gutenberg · arXiv  │
                       │ OpenAlex · PubMed/PMC · CourtListener · IETF · Open Library… │
                       └───────────────▲──────────────────────────────────────────────┘
                                       │ (only component allowed network)
┌──────────────────────────────────────┴────────────────────────────────────────────────┐
│ CURATOR       registry · discovery · scoring · health monitors · policy gate          │
│ ACQUIRER      adapters → fetch → verify → seal → promote   (state machine, idempotent)│
│ VAULT         golden storage on NAS: generations, manifests, ledger, parity           │
│ WARDEN        scrub · repair ladder · drills · alerts                                 │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ REFINERY      normalize → Parquet · entity-link (Wikidata) · chunk · embed            │
│ INDEX         lexical (Tantivy) · title FST · vectors (ANN) · graph   (local NVMe)    │
│ ORACLE        query router · hybrid retrieval · rerank · PageRank · did you mean      │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ API           HTTP /v1 (OpenAPI 3.1) · Python SDK · `wsp` CLI · change feed (SSE)     │
│ NAVIGATOR     retro offline browser (web UI + TUI "BBS mode")                         │
└───────────────────────────────────────────────────────────────────────────────────────┘
                     ▲ read-only API + change feed + CoW snapshot
          ┌──────────┴──────────┐
          │  WHISIPEDIA LIVING  │  overlay store · patch log · model workers · eval gates
          └─────────────────────┘
```

**Hard rule:** only the Curator and the Acquirer may use the network. Everything below the Vault runs network-isolated, and CI tests this with `unshare -n` (§12.4).

---

## 4. Source selection (summary)

The full research, scoring rubric and per-source notes are in [`research/SOURCES.md`](research/SOURCES.md). The machine-readable seed list is [`registry/sources.yaml`](../registry/sources.yaml).

**Approach.** Every candidate is scored on authority, editorial process, density, uniqueness, license, bulk access, update health, format longevity and storage cost (§5.2). Sources fall into tiers:

| Tier | Meaning | Examples |
|---|---|---|
| **T1 Core** | Ingest in Phases 1–4. Keep long history. | Wikipedia (top 10 languages), Wikidata, Wiktionary, Wikisource, Project Gutenberg, Standard Ebooks, OpenStreetMap, arXiv, PubMed and PMC OA, OpenAlex, Stack Exchange, OpenStax, IETF RFCs, US Code / eCFR / Federal Register, CourtListener, Open Library, MDWiki, MedlinePlus |
| **T2 High value** | Ingest in Phase 4 and later, within budget. | Kiwix catalog (Khan Academy, iFixit, Wikibooks, Wikivoyage, DevDocs, Arch Wiki, Survivor Library), MIT OCW, LibreTexts, Chronicling America OCR, OEIS, PlanetMath, Our World in Data, World Bank WDI, NASA NTRS / APOD, Smithsonian and Met open access, LoC catalog records |
| **T3 Candidate** | Tracked. Needs a policy or budget decision. | Wikimedia Commons subsets, TED / CrashCourse video, arXiv source tarballs, Chronicling America page images, legislation from other countries |
| **X Excluded** | Never ingested. | Shadow libraries, full Common Crawl / CC-NEWS (copyrighted, uncurated), paywalled news, content whose terms forbid bulk copying (unless permission is obtained) |

**Preparedness and defensive collections.** Two curated collections are tracked separately in [`research/PREPAREDNESS_AND_DEFENSE.md`](research/PREPAREDNESS_AND_DEFENSE.md):

- **Preparedness & resilience** (shelf `preparedness`): food, water, austere medicine, shelter and engineering, navigation and communications, emergency management and continuity of government, from authoritative issuers (USDA, FAO, WHO, FEMA, CISA, NOAA, NGA, FAA, USGS, UK Cabinet Office).
- **Defensive & security literature** (shelf `defense`): electronics and radar fundamentals, publicly released doctrine, intelligence analysis and counterintelligence history, protective security, cybersecurity standards.

Issuer authority is a hard gate next to the license gate. Only lawfully public material is admitted (public domain, open license or Distribution Statement A); weapons, CBRN-production, targeting and offensive-tooling content is excluded whatever its release status. Mixed collections are filtered per document by the `doc-collection` adapter (§6.2), with owner review through `wsp proposals`. Year-one cost ≈ 0.25–0.4 TB.

**Storage budget for year one** (measured where marked ✓, otherwise estimated):

| Block | Size |
|---|---|
| Whole Kiwix catalog, 3,631 ZIMs ✓ | 5.5 TB (English subset 3.0 TB ✓) |
| Wikimedia raw: top-10 Wikipedia current ✓ 68 GB, full history ✓ 457 GB (.7z), Wikidata ✓ 103 GB + 44 GB, sister projects ≈ 50 GB | ≈ 0.75 TB |
| OpenStreetMap: planet ✓ 95 GB + vector tiles ≈ 120 GB + diffs | ≈ 0.3 TB |
| Gutenberg full mirror (all formats) | ≈ 1 TB |
| arXiv PDFs (S3, requester pays) | budget 10 TB |
| PMC Open Access | budget 5 TB |
| OpenAlex, PubMed, CourtListener (✓ 55 GB/snapshot), Open Library (✓ 17 GB), law, RFCs | ≈ 1 TB |
| Chronicling America OCR text | budget 2 TB |
| Retained older generations (×≈1.5 on rolling sources) and par2 parity (10% of irreplaceable data) | ≈ 3–8 TB |
| **Total, year one** | **≈ 20–35 TB of the 123 TB free** |

**Steady-state growth (≈, at the default monthly cadence, §6.7): about 150–400 GB per month, or about 300–800 GB per quarter on the quarterly setting.** This counts net new data plus generation churn after retention has pruned old copies. The Wikimedia part for the top 10 Wikipedias is about 1–3 GB per cycle as compressed net diffs of article namespaces. For reference, keeping every individual edit would need weekly incrementals at 11 GB per week raw (✓) or 0.3–0.6 GB per week as diffs.

---

## 5. Curator: registry, discovery and how the selection improves

### 5.1 Registry

- `registry/sources.yaml` is the single source of truth. It lives in git, is checked against `registry/schema/source.schema.json`, and changes only through pull requests, including those opened by bots.
- Each entry records: `id`, `name`, `publisher`, `domain[]`, `tier`, `license` (SPDX where one exists), `access` (method, endpoints, mirrors, auth), `formats`, `update` (cadence, strategy, checksum scheme), `size` (measured or estimated, with a date), `retention` policy, `adapter`, `scores`, `status` (`candidate | active | paused | retired | excluded`) and `notes`.

### 5.2 Scoring rubric (0–5 per axis, weighted)

| Axis | Weight | Question |
|---|---|---|
| Authority and editorial process | 0.20 | Is there peer review, a strong editorial process, or a formal or official status? |
| Density and quality | 0.15 | How much reliable knowledge is there per byte? Little noise, little duplication? |
| Uniqueness | 0.15 | Can this knowledge be found elsewhere in the library? |
| License and legal clarity | 0.15 | Is bulk copying for personal use clearly allowed, ideally under an open license? |
| Bulk accessibility | 0.10 | Are there dumps, an API or rsync, with checksums? |
| Update health | 0.10 | Is it maintained, with a predictable cadence? |
| Format longevity | 0.10 | Is the format open, documented and readable without special software? |
| Cost efficiency | 0.05 | How does the value compare to the TB it takes and the egress cost? |

`score = Σ weight × axis`. **License is a hard gate:** a score below 3 means the source cannot become `active`, whatever its total.

### 5.3 Discovery bots (once per update cycle, network-enabled)

| Watcher | Finds |
|---|---|
| Kiwix OPDS (`library.kiwix.org/catalog/v2`) | New and updated ZIMs and new titles |
| Wikimedia dump index | New wikis that cross the article-count thresholds. Changes to the top-10 ranking. |
| Internet Archive collections (curated allow-list of collections) | New public-domain or CC items |
| Hugging Face and other dataset hubs (license-filtered) | Curated datasets with clear licenses |
| Gap miner (local only) | Queries in the local search log that returned poor results, grouped by topic and turned into "seek a source for X" tasks |

Each finding becomes a **candidate record**. Rubric fields that can be computed (size, license, cadence, checksums) are filled automatically. The rest are drafted for human review.

### 5.4 Policy gate (automatic or human)

- **Auto-promote** a candidate only when *all* of these hold: the license is on the allow-list; it is a new version or variant of an existing active source, or a Kiwix title in an allowed category; it fits the domain's storage budget; and its score is ≥ 3.5.
- **Everything else** opens a PR with the score breakdown for the owner to review.
- **Auto-pause** a source when its license changes, it fails its checksum twice in a row, it stays stale past 3× its expected cadence, or its format changes. One alert is raised and the last good generation stays the current one.

Example of why this matters: in 2024 Stack Exchange moved its data dump behind per-account downloads with new terms. The latest public archive.org dump is dated **2024-04-07** ✓. The gate should catch this kind of change and route it to a human.

### 5.5 Health monitors

- A daily lightweight probe per source: endpoint reachable, latest release, checksum file present.
- **Adapter contract tests** against live upstream, a week before each scheduled cycle,, using a small sample, to catch format drift before a full fetch runs.

---

## 6. Acquirer: fetching and updating

### 6.1 Adapter port

```python
class SourceAdapter(Protocol):
    id: str

    def discover(self, ctx: Ctx) -> Iterable[ReleaseRef]: ...  # what upstream has now
    def plan(self, ctx: Ctx, have: Generation | None, want: ReleaseRef) -> FetchPlan: ...
    def fetch(self, ctx: Ctx, plan: FetchPlan) -> Iterable[Artifact]: ...  # resumable
    def verify(self, ctx: Ctx, art: Artifact) -> Verdict: ...  # upstream checksums
    def documents(self, ctx: Ctx, gen: Generation) -> Iterable[DocRecord]: ...  # for Refinery
```

Adapters are Python packages registered through entry points (`whisipedia.adapters`). Each one ships a **fixture corpus**: a few MB of real-shaped sample data checked into the repo, cut down so it carries no licensing burden. It must also pass the shared contract suite.

### 6.2 Generic adapters (cover most sources)

| Adapter | Covers |
|---|---|
| `kiwix-opds` | Every ZIM in the Kiwix catalog, chosen by name, flavour and language filters |
| `wikimedia-dumps` | `pages-articles-multistream`, `meta-history`, Wikidata entities. `other/incr` daily incrementals are used only if a source is set to a weekly cadence. |
| `http-files` | Versioned file sets with checksum files (PubMed baseline and updates, CourtListener, Open Library, World Bank) |
| `rsync-mirror` | Gutenberg, IETF RFCs |
| `s3-bucket` | arXiv (requester pays), OpenAlex |
| `git-mirror` | Standard Ebooks, OEIS data, Our World in Data, PlanetMath |
| `oai-pmh` | arXiv metadata, institutional repositories |
| `doc-collection` | Curated document sets published as index pages or APIs (FEMA, NIST CSRC, Army/Joint doctrine, CIA CSI…): per-document issuer, publication number, edition, distribution statement, supersession and checksum; document-level allow/deny filtering with owner review; superseded editions kept as history. Spec: [`research/PREPAREDNESS_AND_DEFENSE.md` §5](research/PREPAREDNESS_AND_DEFENSE.md#5-tracking-design) |
| `osm-replication` | Planet PBF plus replication diffs |

### 6.3 Release state machine

```
DISCOVERED → PLANNED → FETCHING → FETCHED → VERIFIED → SEALED → REFINED → INDEXED → PROMOTED
                 ╰───────────── FAILED (retry with backoff; quarantine after N) ─────────────╯
```

- Every transition is a **ledger event** (§8.3). Each step is idempotent, so a crash at any point resumes cleanly.
- **Promotion is atomic.** The `CURRENT` pointer for a source switches only after its indexes are built, so search never sees a half-ingested release.

### 6.4 Update strategies and how edits are handled

| Strategy | Used for | What happens on change |
|---|---|---|
| **Snapshot + retention** | ZIMs, OSM monthly, CourtListener, Open Library | A new generation goes into a new directory. Document-level diff against the previous generation yields a change set (add, modify, delete, rename). Indexes are updated with upserts and tombstones, not rebuilt. Older generations are pruned by GFS retention: keep the last 3, then monthly for 12 months, then yearly forever. |
| **Base + net diffs** | Wikimedia (snapshot-to-snapshot), OSM replication diffs, PubMed update files, arXiv OAI | One diff segment per cycle is appended to the generation. For Wikimedia it is the net change between consecutive full dumps; for the others, the upstream changes accumulated since the last cycle. Content can be reconstructed at any cycle boundary as base + segments. A new base is taken yearly, and segments older than the window are compacted into it. |
| **Mirror (rsync/git)** | Gutenberg, RFCs, git sources | Mirror into a staging copy, compute the change set, then seal it as a new generation that records file-level deltas. Git sources keep their full history for free. |

**Deletions upstream.** The library keeps the content. In the current view the document gets a `removed_upstream` tombstone, but its old text stays reachable through history. Retention policy is the only thing that ever deletes data, and it never deletes the last copy of anything that is gone from upstream.

**Vandalism and bad edits ("known good").** With monthly or quarterly snapshots, most vandalism has already been reverted upstream before the dump is taken. If a source is switched to a weekly cadence with incrementals, a revision becomes *known good* only once it has survived a **settling window** (default 72 h) without being reverted. Reverted revisions are kept in history but never shown as current. Snapshot sources are checked for sanity before promotion: the article count must not drop by more than 2%, and size must stay within ±10% of the trend. Anything outside that is held for review.

### 6.5 Scheduling

- A single orchestrator daemon. It runs as a systemd user service in WSL, or as Docker Compose, depending on D6. Jobs sit in a persistent queue in local SQLite.
- **Catch-up semantics:** on startup it runs every job whose last success is older than its cadence. Upstream retention sets how long a cycle can slip: Wikimedia keeps only the last few full dump runs (≈ 2–3 months), OSM replication diffs are kept indefinitely, and PubMed update files stay until the next annual baseline. A cycle that has not run by halfway through its retention window raises an alert.
- **Politeness:** a bandwidth cap per host, at most 2–3 connections per host, mirrors and torrents preferred (Kiwix publishes metalinks), resumable range requests, and a `User-Agent` that names the project. The UA carries a project contact address, not a personal email, unless the owner chooses otherwise.

### 6.6 Freshness SLOs (starting values)

| Source class | Target lag |
|---|---|
| Wikimedia | ≤ 1 cycle + 14 days (the dump run takes up to ~2 weeks to finish) |
| ZIM catalog items | ≤ 45 days after publication |
| Scholarly feeds (arXiv, PubMed) | ≤ 1 cycle + 7 days |
| Law and regulations | ≤ 1 cycle + 7 days |
| Everything else | ≤ 1 cadence + 7 days |

### 6.7 Update cadence (default: monthly; quarterly is one setting)

One global setting, `acquirer.cycle = "monthly" | "quarterly"`, sets the library-wide rhythm. Individual sources can override it with `update.cadence` in the registry; a source without one follows the cycle.

| | Monthly (default) | Quarterly |
|---|---|---|
| When | Shortly after the 1st-of-month Wikimedia dumps finish (≈ mid-month) | Same, in Jan / Apr / Jul / Oct |
| What gets captured | The net state of every document once a month. Edits made and changed again within the month are not kept individually. | The same, at 3-month resolution |
| Wikimedia download per cycle (top 10) | ✓ 68 GB current dumps, then local diff against the previous snapshot | Same 68 GB |
| Wikimedia stored per cycle | ≈ 1–3 GB net diffs | ≈ 2–5 GB net diffs |
| Library-wide growth (≈) | 150–400 GB per month | 300–800 GB per quarter |
| Margin for missed cycles | Wikimedia keeps ≈ 2–3 months of runs, so a skipped month can be recovered | Tight. The cycle must run within its window, or use a mirror with longer retention. |
| Search index refresh | Monthly incremental upsert | Quarterly |

**What isn't affected by the cycle:**
- **Integrity scrubbing stays on its 30-day rotation.** It protects data already stored, not freshness.
- **Lightweight health probes stay frequent (weekly).** They are a few HTTP requests, and they catch broken upstreams before a cycle depends on them.
- **Full edit history** can still be refreshed separately: a yearly pull of the ✓ 457 GB `meta-history` dump (.7z) fills in every individual edit between snapshots, for anyone who wants it.

---

## 7. Vault: storage layout people can read directly

Everything sits under `/mnt/z/whisipedia/`. Directory names are readable and the layout is stable; generation directories are sealed and read-only.

```
/mnt/z/whisipedia/
├── README.txt                 ← start here: what this is, how to read it without software
├── CATALOG.html / .csv        ← every source, license, date, size; opens in any browser
├── library/
│   ├── encyclopedias/
│   │   └── wikipedia-en/
│   │       ├── README.txt     ← source description, license, formats, tools to open
│   │       ├── CURRENT.txt    ← name of current generation (no symlinks; SMB-safe)
│   │       ├── 2026-10-01/                     ← sealed generation (read-only)
│   │       │   ├── upstream/                   ← byte-for-byte upstream files
│   │       │   │   └── wikipedia_en_all_maxi_2026-08.zim
│   │       │   ├── MANIFEST.json               ← every file: size, sha256, blake3, upstream hash
│   │       │   ├── MANIFEST.json.sig           ← signature (SSH/minisign key)
│   │       │   └── parity/                     ← par2 (if source is marked irreplaceable)
│   │       └── increments/2026-10.jsonl.zst     ← net diff segment per cycle
│   ├── books/  gutenberg/  standard-ebooks/ …
│   ├── science/  arxiv/  pubmed/  pmc-oa/  openalex/ …
│   ├── maps/  osm-planet/ …
│   ├── law/  us-code/  ecfr/  courtlistener/ …
│   └── reference/  wikidata/  wiktionary/  rfc/ …
├── normalized/                ← derived Parquet (rebuildable; not sealed)
├── ledger/                    ← append-only hash-chained event log (JSONL, daily files)
└── keys/                      ← public verification keys only
```

**Rules:**

1. **Upstream bytes are never changed.** Normalized and derived data live in a separate tree and can always be rebuilt.
2. **No symlinks or hard links are relied on.** SMB support for them is inconsistent, so pointers are small text files.
3. **No databases on the NAS.** The NAS holds only files that are written once and then read (ADR-0002).
4. **Every directory has a README.txt** naming the format and the free tools that open it: Kiwix for ZIM, any EPUB reader, `bzcat`, `zstdcat`, `osmium`, DuckDB for Parquet.
5. Long-lived formats: original upstream formats, plus Parquet, JSONL+zstd and UTF-8 text for derived data.

---

## 8. Warden: integrity, self-healing and known-good guarantees

### 8.1 Layers of defence

| Layer | Mechanism |
|---|---|
| Ingest | Check the upstream checksum (md5, sha1 or sha256, whatever upstream publishes). Compute our own **SHA-256 and BLAKE3**. Read the file back from the NAS and re-hash it before sealing. |
| At rest | Signed `MANIFEST.json` per generation. If D1 permits, NAS snapshots (ZFS/Btrfs) and server-side scrubs. |
| Irreplaceable data | **par2 at 10%** for anything upstream no longer serves: superseded versions, the 2024 Stack Exchange dump, old ZIMs. |
| Second copy | **Strongly recommended (D2).** A single NAS is a single copy. Fire, theft, ransomware or a controller failure would defeat any amount of self-healing. Use a second NAS, rotated external drives, or cold cloud storage for the T1 originals (≈ 10–20 TB). |
| Write path | Atomic: write `*.partial`, flush, read back, verify the hash, then rename. This is mandatory because of the `soft` SMB mount. |

### 8.2 Scrub

A rolling scrub re-hashes **every byte every 30 days**, rate-limited and paused while the browser is in active use. At about 200 MB/s, 35 TB takes about 49 hours of scrub time per month.

### 8.3 Ledger

- An append-only, hash-chained JSONL event log: every ingest, verification, repair, promotion, prune and policy decision.
- Each daily file ends with a signed checkpoint, so tampering or accidental edits can be detected.
- `wsp ledger verify` replays the chain.

### 8.4 Repair ladder (automatic)

1. Rebuild from **par2**.
2. Restore from the **second copy**.
3. **Re-fetch the same version** from upstream or a mirror. Accept it only if the hash matches the manifest.
4. Otherwise mark the generation **DEGRADED**, keep serving the previous good generation, and raise an alert.

Every rung writes a ledger event.

### 8.5 Drills (chaos tests)

- **Monthly automatic drill:** flip bytes in a random sealed file copied into a sandbox; truncate a download; corrupt a manifest signature; kill the daemon mid-promotion. Expect detection and repair with no human help.
- Drill results feed into the health page.

---

## 9. Refinery, Index and Oracle: natural language at speed

### 9.1 Normalization (Refinery)

- Each adapter emits `DocRecord`: `{doc_id, source_id, native_id, title, lang, url_native, license, revision, timestamp, text, html_ref, entities[], links[]}`.
- Records are stored as Parquet per source and generation. Text extraction keeps section structure.
- **Entity linking through Wikidata QIDs** connects the sources. The same entity can appear in Wikipedia, Wikidata, OSM, OpenAlex, Open Library and Gutenberg, which makes "everything we have about X" possible.

### 9.2 Stable identifiers

- `doc_id = wsp:<source_id>/<native_id>`. Examples: `wsp:wikipedia-en/Q42`, `wsp:rfc/9110`, `wsp:gutenberg/1342`.
- Content-version IDs are the BLAKE3 hash of the normalized text.
- These IDs are part of the public API contract and are what Living relies on.

### 9.3 Query pipeline and latency budgets

| Stage | Tech (initial choice, swappable) | Budget |
|---|---|---|
| Autocomplete (titles, entities) | FST over titles and aliases | < 10 ms |
| Router | Rules plus a small classifier. Intents: *lookup, define, how-to, navigate (`rfc 9110`), locate (map), compare, ask* | < 5 ms |
| Lexical retrieval | Tantivy BM25 (multi-field, per-language analyzers) | < 30 ms |
| Dense retrieval | Open embedding model, picked by benchmark, int8 or binary-quantized ANN | < 30 ms |
| Fusion | Reciprocal rank fusion plus source-quality priors | < 2 ms |
| Rerank top 50 | Cross-encoder on the GPU | < 50 ms |
| **Results on screen** | | **p95 < 100 ms** |
| Static rank (offline) | PageRank over the inter-document link graph, per source and across sources, computed at index-build time and blended into lexical scores and fusion | 0 ms at query time |
| "Did you mean" (only for sparse or low-score results) | Noisy-channel spelling correction over the index's own vocabulary (symmetric-delete candidates) | < 5 ms |

**Results rules:**
- The search engine is results-first, like a classic web search engine (§10.3). Everything on the results page is extractive: links, snippets, facets, "did you mean" and instant-answer boxes built from structured data.
- **Summaries removed by owner decision (2026-10-10, ADR-0008):** there is no generative AI in the user experience. The "Summarize these results" feature, the `/v1/summarize` endpoint and the local LLM runtime are gone. Backend ranking may still use any modern technique (embeddings, cross-encoders, learned ranking).
- If retrieval confidence is low, the results page says so instead of guessing.

### 9.4 Index sizing (where the bytes go)

- English Wikipedia is about 25M chunks of 200 words.
- At 384 dimensions int8 that is about 10 GB, plus graph overhead.
- Indexing *all* T1 text would be about 300–500M chunks, which is too much RAM for 23 GiB. So the plan is:
  - Lexical index for **everything**. Size is about 15–25% of the text, held on local NVMe.
  - Dense index for **priority corpora**: encyclopedias, textbooks, medical, RFCs, abstracts. Binary-quantized vectors stay in RAM; int8 vectors are mmapped for rescoring.
  - Hot local storage budget: about 400 GB of the 619 GB free. **Recommendation:** a dedicated 2–4 TB NVMe for indexes in Phase 3 (Open decision D5).
- Embedding throughput on the 4090 is in the low thousands of chunks per second, so the English Wikipedia vectors take a few hours.

### 9.5 Evaluation harness (needed before calling it "fast and good")

- A gold query set of 500 or more questions across the domains, with expected documents.
- Metrics: nDCG@10, Recall@50, answer faithfulness (claim-level, checked against the cited passages), p50/p95/p99 latency.
- Runs in CI on the fixture corpus and nightly on the real index. Regressions block promotion of new models or indexes.

---

## 10. Navigator: a retro web browser for an offline web

**Design intent (owner clarification, 2026-10-09):** this is a *web-surfing experience first*. You browse the library the way people surfed the web in 1994–1999: a home page, directories, hyperlinks, back and forward, bookmarks, wandering from page to page. Natural-language search lives *inside* that web as an offline search engine, the equivalent of visiting a search site. It is not a chatbot front end.

### 10.1 Form factor

- **Primary:** a browser-shaped local web app served by the Whisipedia daemon on `127.0.0.1`. It has period chrome: location bar, Back, Forward, Reload, Home, Bookmarks and Go menus, a status bar showing link targets, and a throbber.
  - All assets, fonts and map libraries are bundled.
  - A strict Content Security Policy (`default-src 'self'`) makes any outbound request fail visibly.
- **Secondary:** a terminal UI ("BBS mode", built with Textual), Lynx-style, for keyboard-only use over SSH.

### 10.2 An offline web to surf

Every source is a "site" with its own address under a local pseudo-domain:

| Address | Site |
|---|---|
| `wsp://home` | **Start page**, a late-90s portal: today's featured article, "On this day", newest additions, random-page buttons, a link to every site |
| `wsp://directory` | **Hand-curated directory** (Yahoo! '97 style): Arts › Literature › Poetry …, built from Wikidata classes, Library of Congress subjects and Gutenberg LCC categories |
| `wsp://wikipedia.en/…`, `wsp://gutenberg/…`, `wsp://rfc/…`, `wsp://maps/…` … | One "site" per source, rendered in its native look (ZIM HTML through libzim, EPUB/text reader, PDF viewer, MapLibre with local tiles) |
| `wsp://search` | **The offline search engine** (§10.3) |
| `wsp://whatsnew` | "What's New!" page fed by the change feed |
| `wsp://system` | System Monitor: freshness, scrub progress, degraded items, disk budget, ledger tail |

**Links work across the whole offline web.** Links between sources are rewritten to local addresses, so a Wikipedia citation to an RFC, a Gutenberg book, an arXiv paper or a map coordinate opens the local copy. Links to anything not held locally appear as "not in library" (period-style broken-link styling) and are logged for the gap miner (§5.3).

Other period features: a Netscape-style history and **Hotlist**, a webring-style "Next / Random site" bar linking related sources, "View Source Data" (jump to the file on the NAS and its manifest entry, for the direct route), and a **time machine** that shows a page as of a date, with a diff, for sources that keep history.

### 10.3 The offline search engine (`wsp://search`)

A search site styled like a 1998–2000 search engine: sparse home page, one box, "I'm Feeling Lucky".

- **Type anything, in plain language:** "how do vaccines train the immune system", "rfc about http caching", "map of Lisbon", "books by Mary Shelley", "define sesquipedalian".
- **Results page:** a classic ten blue links list with title, snippet with highlighted terms, local address and source badge (license, "as of" date). Links are grouped or filterable by kind (Encyclopedia, Books, Papers, Law, Maps, Q&A), with "more from this site" and paging.
- **Instant answers, kept small and optional:** a definition box (Wiktionary), a fact box (Wikidata), a map box. All of them are extracted from structured sources, never generated. **Summaries removed by owner decision (ADR-0008):** there is no "Summarize these results" button; the results are the answer.
- **"Did you mean: …?"** appears when the results are sparse or a query word is not in the library's vocabulary, with a corrected query taken from the index's own words.
- **Site scoping:** a query that names a site (`rfc 9110`) searches that site; totals and facets count the same scope. If the site has nothing for it, the whole library is searched and the page says so in one quiet line.
- **The location bar is also a search box.** Typing something that isn't an address runs a search, like late-90s "keyword" browsing. Known navigational queries ("rfc 9110", "wikipedia Alan Turing") jump straight to the page, "I'm Feeling Lucky" style.
- Speed targets are in §9.3: results on screen p95 < 100 ms, search-as-you-type suggestions < 10 ms.

### 10.4 Look and feel (selectable themes)

| Theme | Inspiration |
|---|---|
| *Navigator '96* (default) | Netscape 3 / Win95 chrome, grey bevels, animated throbber, default-blue/purple links, `<hr>` and `<table>` page layouts |
| *Mosaic '93* | Earliest web: grey background, serif text, minimal chrome |
| *Encarta '95* | Multimedia-encyclopedia skin for the start page and directory |
| *Phosphor* | Amber or green CRT, scanlines (can be turned off), for the browser and BBS mode |
| *System 7* | Classic Mac chrome, 1-bit icons |

Fonts are bundled under open licenses. The retro look never costs accessibility: full keyboard navigation (period-accurate shortcuts plus modern ones), focus rings, font scaling, a high-contrast variant, an option to reduce motion, and screen-reader landmarks.

### 10.5 Offline guarantee

- The Navigator, API, Oracle and Index run with **no network capability**: a separate process group, and in the container profile `network_mode: none`.
- The Curator and Acquirer run as a separate service that is allowed network access.

---

## 11. Seed contract for Whisipedia Living

Living is a **separate repository and service** that depends on Whisipedia's public SDK and API. It does not depend on its internals.

### 11.1 What Whisipedia guarantees to Living (frozen v1 before Phase 6)

| Contract | Description |
|---|---|
| Stable `doc_id` and content-version hash | §9.2 |
| Read API | `/v1/documents/{id}`, `/v1/documents/{id}/history`, `/v1/search`, `/v1/sources` |
| Change feed | `/v1/changes?since=<cursor>` plus server-sent events. Every add, modify or tombstone, with version hashes. |
| Snapshot handle | A `generation_id` that names an immutable, reproducible view of the whole library |
| Provenance schema | License, source and timestamp on every document, carried through into Living output |

### 11.2 Living's data model: an overlay, never a mutation

```
materialized_doc(doc_id, at) = base(doc_id, golden generation) ⊕ accepted_patches(doc_id)
```

- **Patch record:** `{patch_id, doc_id, base_version, op (replace|insert|annotate|link|summary|translation|flag), diff, rationale, evidence[doc_id#span], model{id, version, params}, prompt_hash, scores{faithfulness, quality}, status, created_at}`.
- Patches go into an **append-only patch log** in their own storage area, separate from the golden library. The golden library is mounted **read-only** for Living, enforced with separate OS credentials.
- **When upstream changes:** a change event triggers a three-way rebase of the open patches. If a patch conflicts, it is re-run or discarded, never silently kept.
- Everything can be reversed. "Revert all patches by model X" or "everything after date D" is a single command.

### 11.3 Work types (prioritized queue)

- Repair broken markup and extraction.
- Add cross-source links using Wikidata entities.
- Write condensed summaries at several levels: one line, one paragraph, one page.
- Check consistency against Wikidata.
- Fill gaps by translation where an article exists in de/fr/es and not in en.
- Generate Q&A pairs for the evaluation set.
- Flag suspected vandalism or contradictions back to the Warden as **signals only**.

### 11.4 Guardrails

- **Eval gates:** a patch is accepted only if its citations resolve, an independent model verifies its claims against the evidence, and it passes the regression suite.
- **Human review:** a sampled review queue, with acceptance rates tracked per model and task type.
- **Resource governor:** interactive Navigator queries pre-empt Living's GPU work. There are budgets for GPU-hours, tokens and storage, and a kill switch.
- **Model registry:** pinned model weights with hashes. Upgrades go through A/B evaluation before they take over the queue.

---

## 12. Engineering standards

### 12.1 Stack (ADR-0004)

- **Python 3.12+** for orchestration, adapters, API and SDK, managed as **uv** workspaces.
- **Rust** crates where profiling shows a need. Tantivy is used through its Python bindings at first.
- **TypeScript** for the Navigator front end, with minimal dependencies and a vendored build output.

### 12.2 Architecture rules

- **Hexagonal (ports and adapters).** Ports: `SourceAdapter`, `Fetcher`, `BlobStore`, `Signer`, `ParityProvider`, `LexicalIndex`, `VectorIndex`, `Embedder`, `Reranker`, `Renderer`, `Scheduler`, `EventBus`, `Ledger`.
- Each port comes with a **reference implementation** and a **contract test suite**. Swapping a component means passing the same suite and changing one line of config.
- **Public API surfaces:**
  - HTTP `/v1`, specified in OpenAPI 3.1.
  - A typed Python SDK generated from the spec and checked by hand.
  - The `wsp` CLI.
  - The change feed.
- **API versioning:** semver, with a deprecation window of at least one minor release. Breaking changes require an ADR.
- **Configuration:** layered TOML, `config/default.toml` < `config/local.toml` (gitignored) < environment variables. **No host names, IPs, paths or credentials are committed.**

### 12.3 Repository layout

```
whisipedia/
├── packages/
│   ├── wsp-core/        domain model, ports, ledger, manifests
│   ├── wsp-adapters/    one module per adapter + fixtures
│   ├── wsp-vault/       storage, sealing, scrub, repair
│   ├── wsp-index/       refinery, lexical, vector, fusion
│   ├── wsp-oracle/      router, rerank, answer generation
│   ├── wsp-api/         HTTP server (OpenAPI), SSE change feed
│   ├── wsp-sdk/         public Python client
│   └── wsp-cli/         `wsp` command
├── navigator/           front-end (TS) + TUI
├── registry/            sources.yaml + JSON Schema
├── docs/                PLAN, research, ADRs, runbooks, API docs
├── deploy/              compose files, systemd units
└── tests/               cross-package integration + offline tests
```

### 12.4 Quality gates (CI)

- `ruff` (lint and format), `pyright --strict`, and `pytest` with coverage of at least 85% on the core packages.
- Contract suites, OpenAPI diff checks for breaking changes, and front-end lint and tests.
- An **offline test**: the full stack runs on fixtures inside `unshare -n` or `network_mode: none` and must pass.
- Secret scanning (gitleaks), license scanning of dependencies, an SBOM (CycloneDX), and pinned lockfiles.

### 12.5 Repo hygiene (written for a "public on Times Square" standard)

- **Private GitHub repo.** The default branch is protected: PRs only, CI required, linear history. Branch protection on private repos needs a paid GitHub plan; on the free plan it is enforced by local hooks and by discipline.
- **Conventional Commits, signed commits** (SSH signing with the existing ed25519 key), and a CHANGELOG generated from commits.
- **Pre-commit:** gitleaks, large-file block (> 1 MB), EOF and whitespace fixes, ruff, conventional-commit message check.
- **No data in git, ever.** Fixtures are small, synthetic or license-clean. LFS is not used for corpus data.
- **Commit identity:** use a GitHub `noreply` address so no personal or institutional email appears in history (D8).
- **Repo files:** README, SECURITY.md, CONTRIBUTING.md, CODEOWNERS, ADRs, issue and PR templates, Renovate or Dependabot.

### 12.6 Operations

- Structured JSON logs and Prometheus-format metrics, available locally.
- Health endpoint and the System Monitor page.
- Alerts through desktop notification; optionally email or ntfy, if the owner turns them on.
- **Runbooks:** add a source, recover a DEGRADED generation, rotate signing keys, restore from the second copy, rebuild indexes from scratch.

---

## 13. Legal, licensing and ethics

- **Allow-list only:** public domain, CC0, CC BY, CC BY-SA, government open licenses (OGL, US federal works), and permissive open data licenses (ODbL for OSM). Sources under NC/ND licenses or bespoke terms (MIT OCW, some PMC articles, TED) are allowed **for personal use only** and flagged.
- License metadata is kept **per document** and shown in the Navigator.
- Exports from Living carry attribution and share-alike obligations along with them.
- **Upstream terms are respected:** rate limits, robots rules, requester-pays costs. Nothing is copied when its terms forbid bulk copying, unless written permission is obtained (for example, Stanford Encyclopedia of Philosophy).
- **Excluded categories are enforced in code.** The registry schema rejects `status: active` for any source with `legal_basis: none`.

---

## 14. Roadmap

Estimates assume one developer with heavy agent assistance. Each phase ends with a demo and a tagged release.

| Phase | Weeks | Deliverables | Exit criteria |
|---|---|---|---|
| **0 Foundations** | 1–2 | Repo, CI, pre-commit, ADRs 1–6, config system, `wsp-core` domain and ports, ledger, manifest and signing, NAS layout and README generator, tooling installed. Decisions D1–D8 made. | CI green. `wsp ledger verify` works. A `wsp vault init` dry run works on the NAS. |
| **1 Vault MVP** | 3–6 | Adapters `kiwix-opds` and `wikimedia-dumps` (current + snapshot diffs). Fetch, verify, seal, promote. Scrub, repair ladder, par2. CATALOG.html. First real data: Wikipedia en ZIM, top-10 current dumps, Wikidata. | One full corruption drill is repaired automatically. A full monthly update cycle runs end to end on its own. |
| **2 Search core** | 6–10 | Refinery to Parquet, Tantivy index, title FST, `/v1` read API and SDK, Navigator v0: browser chrome, start page, ZIM site reader, `wsp://search` with lexical results, cross-source link rewriting. Offline CI test. | The offline test passes. Results p95 < 100 ms on English Wikipedia. |
| **3 Natural language** | 10–14 | Embedder and ANN, hybrid fusion, reranker, router, instant-answer boxes, link-analysis static rank, "did you mean", directory, eval harness and gold set, all themes, TUI. | nDCG@10 target set and met. Results on screen p95 < 100 ms. |
| **4 Breadth** | 14–22 | Adapters for OSM (plus map reader), Gutenberg, Standard Ebooks, arXiv, PubMed/PMC, OpenAlex, Stack Exchange, OpenStax, RFCs, US law, CourtListener, Open Library, MDWiki/MedlinePlus. Entity linking across sources. | All T1 sources are active and within their freshness SLOs. |
| **5 Autonomy** | 18–26 | Discovery bots, scoring, policy gate with automatic PRs, health monitors, gap miner, Renovate auto-updates (merged when tests pass), monthly drills, time-machine UI. | 30 days unattended with all SLOs met and no manual repairs. |
| **6 Living seed** | 24+ | Freeze the v1 Living contracts. Create the `whisipedia-living` repo. Overlay store, patch log, first worker (summaries and cross-links), eval gates, review queue. | Patches are accepted, reverted and rebased correctly in an end-to-end test. Golden storage is unchanged, verified against manifests. |

---

## 15. Risks

| Risk | L | I | Mitigation |
|---|---|---|---|
| Single NAS lost (hardware, fire, ransomware) | M | **Critical** | Second copy (D2), NAS snapshots, write-once generations |
| `soft` SMB mount corrupts data silently | M | H | Read back and verify before sealing. Recommend switching to `hard`, or running the writer on the NAS (D6). |
| ISP data cap or bandwidth limit | M | M | Spread the initial load over time, prefer torrents and mirrors, use bandwidth windows |
| Upstream changes license or access | M | M | Policy gate auto-pauses. Last good generation is kept. Human review. |
| Index doesn't fit hot storage or RAM | H | M | Quantization, tiered dense index, dedicated NVMe (D5) |
| WSL host offline → missed cycle | L | M | Catch-up logic, alerts at half the upstream retention window, run on the NAS (D6) |
| Living models hallucinate or degrade content | H | M | Overlay-only design, eval gates, sampled review, one-command revert |
| Scope creep (it's a library of everything) | H | M | Tier system, storage budgets per domain, phase exit criteria |
| Requester-pays egress costs (arXiv) | M | L–M | Explicit budget approval (D4), metadata first, then PDFs |

---

## 16. Open decisions (owner input needed)

| ID | Decision | Recommendation |
|---|---|---|
| D1 | NAS OS and filesystem: ZFS, Btrfs, ext4? Does it support snapshots? | **Decided 2026-10-10: no snapshots for now.** Integrity relies on the application layer only: signed manifests, scrub, par2, write-once generations. |
| D2 | Second-copy location for T1 originals (≈ 10–20 TB) | **Deferred 2026-10-10 (no resources yet). Known open risk:** losing the NAS loses the library. Revisit when budget allows; `vault.mirror_root` is ready to use. |
| D3 | Languages beyond English | Top-10 Wikipedias (already planned); English for everything else at first |
| D4 | arXiv PDFs | **Resolved 2026-10-10 at no cost:** pulled from the public `arxiv-dataset` GCS bucket (not requester-pays, MD5 per file, current through 2026-09) with `tools/arxiv_pull.py`. The S3 route would have cost ≈ $800 one-time plus ≈ $13/month. |
| D5 | Dedicated NVMe for hot indexes | **Deferred 2026-10-10 (cost).** Indexes use the workstation disk (≈ 600 GB free); dense vectors only for priority corpora (§9.4). |
| D6 | Where the daemons run: WSL workstation or NAS (Docker) | Acquirer and Warden on the NAS if it can run containers; Oracle and Navigator on the workstation (GPU) |
| D7 | ISP data cap | **Decided 2026-10-09: no cap.** |
| D8 | Commit identity and project license | **Decided 2026-10-10:** keep the existing commit identity (private, single-user repo). License: none / all rights reserved while private. |
| D9 | Include video (TED, Khan Academy, CrashCourse ≈ 0.3–1 TB)? | Yes for Khan Academy; TED as T3 |
| D10 | Update cycle: monthly or quarterly (§6.7) | Monthly. It gives a margin to recover a missed cycle before upstream retention expires. Quarterly is a single config change. |
| D11 | Sites that refuse automated downloads (most `.mil`, fema.gov, cdc.gov, congress.gov) | **Decided 2026-10-10:** find official mirrors (GovInfo, DTIC, NTRS, agency S3 buckets, official Internet Archive uploads by the issuing agency); never spoof a browser identity. Implemented 2026-10-11: channels recorded per source in `access.mirrors` (research doc §5.6). |
| D12 | Sources that need written permission (Hesperian, Sphere, MSB, MSF, UK MOD) | **Decided 2026-10-10:** deferred; no permission requests for now. |
| D13 | Scope of the defensive collection | **Decided 2026-10-10:** as broad as possible within the law, Anthropic's usage policies and the §13 safety boundaries (no weapon/explosive/CBRN production, targeting or offensive tooling). Public-release engineering references such as NAWCWD TP 8347 are included. Outdated historical guidance is shown with a "historical, may be unsafe" banner rather than hidden. Implemented 2026-10-11: `doc-collection` document filter and `Document.banner` (adapters doc). |
| D14 | Very large public collections | **Decided 2026-10-10:** include the CIA FOIA reading room (≈5-10 TB) and USGS historical topographic maps (≈10-20 TB). Acquisition plans: research doc §8. |
| D15 | Non-English material | **Decided 2026-10-10:** configurable budget; initially the best 300 GB in each of the top 10 non-English languages (3 TB total), chosen by source score per GB. Implemented 2026-10-11: `wsp plan multilingual` → `registry/plans/multilingual.yaml`. |
