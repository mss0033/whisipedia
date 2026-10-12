# Source Research: High-Value Stores of Human Knowledge

| | |
|---|---|
| Research date | 2026-10-09 |
| Method | Desk research, plus live checks against upstream endpoints (Kiwix OPDS catalog, Wikimedia dump status and `other/incr`, HTTP HEAD on bulk files, S3 listings, the archive.org metadata API) |
| Legend | **✓** measured live on the research date · **≈** estimate, to be measured in Phase 1 before committing |
| Scoring | See PLAN §5.2. Scores are the initial assessment, 0–5 weighted. License is a hard gate. |
| Machine-readable form | [`../../registry/sources.yaml`](../../registry/sources.yaml) |

---

## 1. Selection philosophy

The aim is the **most condensed, highest-quality, most authoritative** knowledge available to copy legally in bulk, rather than the most bytes. In practice that means:

1. **Prefer curated summaries** (encyclopedias, textbooks, reviews, standards) over raw volume.
2. **Prefer canonical primary sources** where they are the authority: law, RFCs, scholarly metadata, public-domain literature.
3. **Prefer sources with real editorial process**: peer review, community review with revision history, or official publication.
4. **Prefer open, documented, long-lived formats** with upstream checksums.
5. **Count on the connections:** Wikidata QIDs link Wikipedia, OSM, OpenAlex, Open Library and Gutenberg into one graph, which makes the whole library worth more than its parts.

---

## 2. Encyclopedic and lexical (Wikimedia family)

| Source | What | License | Access | Size | Tier | Score |
|---|---|---|---|---|---|---|
| **Wikipedia, top 10 languages** (en, ceb, de, fr, sv, nl, es, ru, it, pl) | General encyclopedia; 7.25M en articles ✓ | CC BY-SA 4.0 | Dumps (`pages-articles-multistream`, `meta-history`), daily `other/incr`, Kiwix ZIM | Current ✓ **68 GB** · full history ✓ **457 GB** (.7z) · en ZIM maxi ✓ **127 GB** · weekly incr ✓ **11 GB** raw / ≈0.3–0.6 GB as diffs | T1 | 4.6 |
| **Wikidata** | Structured knowledge graph, links every source together | CC0 | JSON / RDF dumps (weekly) | ✓ **103 GB** `all.json.bz2`, ✓ **44 GB** `truthy.nt.bz2` | T1 | 4.7 |
| **Wiktionary (en)** | Multilingual dictionary, etymology, translations | CC BY-SA | Dump, ZIM | ✓ 1.6 GB dump, ✓ 9.2 GB ZIM (nopic) | T1 | 4.3 |
| **Wikisource (en)** | Transcribed public-domain texts, including the 1911 Britannica | CC BY-SA / PD | Dump, ZIM | ✓ 3.4 GB dump, ✓ 8.6 GB ZIM | T1 | 4.1 |
| Wikibooks / Wikiversity / Wikiquote / Wikivoyage | Textbooks, courses, quotations, travel | CC BY-SA | ZIM, dump | ✓ 6.2 / 2.5 / 1.0 / 1.5 GB ZIM | T2 | 3.6 |
| Wikinews | Openly licensed news reports | CC BY 2.5 | Dump | ≈ <1 GB | T2 | 3.0 |
| Wikimedia Commons | Media (> 100M files) | Mixed free licenses | API, dumps (metadata) | ≈ hundreds of TB. Full copy not feasible. | T3 (subsets: Featured/Quality images, Wikipedia-referenced media) | 3.2 |

**Notes**
- Cebuano (ceb) and Swedish (sv) are mostly **bot-generated** stubs. They are in the top 10 by count only. Edit rate is very low for ceb (~0.1 MB/day ✓). Kept because the owner set the "top 10 by article count" rule; open to revisiting.
- Keep **both** forms of Wikipedia. Raw dumps give exact text, history and diffs. ZIM gives instant offline rendering with images.

---

## 3. Books and literature

| Source | What | License | Access | Size | Tier | Score |
|---|---|---|---|---|---|---|
| **Project Gutenberg** | 75k+ public-domain books | PD (US) + PG trademark terms | rsync mirror, RDF/CSV catalog (daily), Kiwix ZIM | ✓ 11 GB plain-text zip · ✓ 221 GB ZIM (en) / 253 GB (all) · ≈ 1 TB full mirror | T1 | 4.4 |
| **Standard Ebooks** | Carefully typeset and proofread PD editions | CC0 (production work) + PD | git repo per book, OPDS | ≈ 10–20 GB | T1 | 4.5 |
| **Open Library** | Catalog of every book (editions, works, authors) | CC0 | Monthly dumps | ✓ 12.6 GB editions + ✓ 4.1 GB works (+ authors) | T1 | 4.0 |
| **OpenStax** | Peer-reviewed college textbooks | CC BY 4.0 | PDF/web, GitHub (CNXML) | ≈ 5–10 GB | T1 | 4.6 |
| LibreTexts | Large open textbook library | CC BY-NC-SA mostly | Bulk export / Kiwix | ≈ 50–200 GB | T2 | 3.9 |
| Survivor Library | Pre-industrial and early-industrial practical know-how | PD scans | Kiwix ZIM | ✓ 220 GB | T2 | 3.7 |
| Internet Archive, PD texts subsets | Curated PD collections (americana, biodiversity, etc.) | PD / per item | `ia` CLI, metadata API | ≈ select by collection, 1–5 TB budget | T2 | 3.5 |
| HathiTrust public domain | Scanned PD volumes | PD | Bulk access only by agreement | n/a | T3 (needs agreement) | — |

---

## 4. Science, scholarship and medicine

| Source | What | License | Access | Size | Tier | Score |
|---|---|---|---|---|---|---|
| **arXiv** | 2.5M+ preprints (physics, math, CS, …) | Per-paper (arXiv license / CC) | Metadata via OAI-PMH (free). PDFs and source via S3 **requester-pays**. | ≈ metadata a few GB · ≈ PDFs several TB (budget 10 TB) | T1 | 4.4 |
| **PubMed** | 37M+ biomedical citations and abstracts | NLM terms (free bulk) | FTP baseline (✓ 1,334 files, 2026 baseline) + daily update files | ≈ 40–60 GB | T1 | 4.5 |
| **PMC Open Access subset** | Full-text OA articles | CC BY / CC0 / NC variants (per article) | FTP / S3 (`pmc-oa-opendata`) | ≈ several TB (budget 5 TB) | T1 | 4.4 |
| **OpenAlex** | Scholarly graph: works, authors, institutions, topics, citations | CC0 | S3 snapshot | ≈ hundreds of GB. Snapshot path moved; **re-measure**. | T1 | 4.3 |
| **MDWiki** (WikiProjectMed) | Medical encyclopedia | CC BY-SA | Kiwix ZIM | ✓ 10.8 GB | T1 | 4.2 |
| **MedlinePlus** | Consumer health topics (NLM) | US government PD | XML health topics | ≈ <1 GB | T1 | 4.3 |
| NCBI Bookshelf OA (incl. StatPearls, GeneReviews) | Medical reference texts | Per-book licenses | FTP OA subset | ≈ 10–50 GB | T2 | 4.0 |
| NASA NTRS, APOD | Technical reports; astronomy pictures with explanations | US government PD | API / ZIM | APOD ✓ 16.7 GB ZIM · NTRS ≈ TB-scale (select) | T2 | 3.6 |
| OEIS | Integer sequences | CC BY-SA 4.0 | GitHub `oeis/oeisdata` | ≈ few GB | T2 | 4.0 |
| PlanetMath / ProofWiki / nLab | Mathematics references | CC BY-SA (varies) | git / dumps | ≈ <5 GB | T2 | 3.6 |
| Stanford Encyclopedia of Philosophy | Peer-reviewed philosophy reference | Copyright, no bulk license | Web only | ≈ 1–2 GB | T3, **needs permission** | 4.6 if permitted |
| Internet Encyclopedia of Philosophy | Peer-reviewed philosophy reference | Copyright | Web only | ≈ <1 GB | T3, **needs permission** | 4.0 |
| GBIF / iNaturalist open data | Biodiversity occurrences and taxonomy | CC0 / CC BY | Bulk exports | ≈ TB-scale (select taxonomy backbone ≈ GB) | T2 (backbone) / T3 (occurrences) | 3.5 |

---

## 5. Q&A, how-to and education

| Source | What | License | Access | Size | Tier | Score |
|---|---|---|---|---|---|---|
| **Stack Exchange network** | Expert Q&A, 180+ sites | CC BY-SA | Kiwix ZIMs (current), archive.org data dump (**last public: ✓ 2024-04-07**), new dumps require per-account terms | ✓ 191 GB (181 ZIMs) · ✓ 99 GB raw 2024 dump | T1 | 4.3 |
| **Khan Academy** | Video lessons and exercises | CC BY-NC-SA | Kiwix ZIM (✓ 2023-03, **stale**) | ✓ 180 GB | T2 | 3.9 |
| MIT OpenCourseWare | Course materials | CC BY-NC-SA | Per-course zip downloads | ≈ 1–5 TB with video, ≈ 100 GB without | T2 | 4.0 |
| iFixit | Repair guides | CC BY-NC-SA | Kiwix ZIM | ✓ 3.6 GB | T2 | 3.8 |
| Appropedia | Appropriate technology, sustainability | CC BY-SA | Dump / ZIM | ≈ <5 GB | T2 | 3.4 |
| PhET simulations | Interactive science simulations | CC BY 4.0 | Kiwix ZIM | ✓ 0.3 GB | T2 | 3.8 |
| CrashCourse, TED | Educational video | Various (NC/ND) | Kiwix ZIM | ✓ 23 GB / ✓ 91 GB | T3 | 3.0 |

---

## 6. Technology and standards

| Source | What | License | Access | Size | Tier | Score |
|---|---|---|---|---|---|---|
| **IETF RFCs** | Internet standards | IETF Trust (free reproduction) | rsync | ≈ 1–3 GB | T1 | 4.6 |
| Arch Wiki | Linux system reference | GFDL | Kiwix ZIM | ≈ <1 GB | T2 | 4.0 |
| DevDocs bundles | Programming-language and library documentation | Per project | Kiwix ZIMs | ≈ few GB | T2 | 3.8 |
| man-pages / POSIX | Unix reference | Various / Open Group terms | git / tarball | ≈ <1 GB | T2 | 3.8 |
| W3C / WHATWG specifications | Web standards | W3C / CC BY | git / HTML | ≈ <1 GB | T2 | 3.9 |
| Python / Rust / kernel documentation | Language and system docs | PSF / MIT/Apache / GPL | Release tarballs | ≈ <5 GB | T2 | 3.8 |

---

## 7. Geography and data

| Source | What | License | Access | Size | Tier | Score |
|---|---|---|---|---|---|---|
| **OpenStreetMap** | Global map database | ODbL | Planet PBF (weekly) + replication diffs | ✓ **95 GB** planet · ≈ 100 MB/day diffs | T1 | 4.6 |
| OSM vector tiles (PMTiles) / Kiwix maps | Renderable offline maps | ODbL | Build ourselves, or Kiwix `maps_*` | ✓ 77.7 GB (Kiwix maps_en_all) · ≈ 120 GB planet PMTiles | T1 (for the Navigator) | 4.2 |
| Our World in Data | Curated statistics and explainers | CC BY | GitHub / bulk CSV | ≈ few GB | T2 | 4.2 |
| World Bank WDI, UN data | Development indicators | CC BY 4.0 | Bulk CSV / API | ≈ <5 GB | T2 | 4.0 |
| Natural Earth | Cartographic base data | PD | Zip | ≈ <1 GB | T2 | 3.9 |

---

## 8. Law and government

| Source | What | License | Access | Size | Tier | Score |
|---|---|---|---|---|---|---|
| **US Code** | Federal statutes | PD | uscode.house.gov XML (USLM) | ≈ <1 GB per release point | T1 | 4.5 |
| **eCFR / Federal Register** | Regulations and daily register | PD | govinfo bulk data | ≈ 10–100 GB | T1 | 4.3 |
| **CourtListener** (Free Law Project) | US case law opinions, dockets | Free bulk data (PD opinions) | Quarterly bulk CSV on S3 | ✓ **55 GB** opinions (2026-09-30) + other tables | T1 | 4.3 |
| Constitution Annotated (CONAN) | Constitutional analysis | PD | congress.gov downloads | ≈ <1 GB | T2 | 4.2 |
| UK legislation.gov.uk / EUR-Lex | Other jurisdictions | OGL / EU reuse policy | APIs / bulk | ≈ 10–100 GB | T3 | 3.8 |

---

## 9. News and the historical record

News is the **weakest category for open bulk copying**. Most current journalism is under copyright and paywalled. The plan relies on sources that are legally clean:

| Source | What | License | Access | Size | Tier | Score |
|---|---|---|---|---|---|---|
| Wikipedia "Current events" portal + Wikinews | Curated daily news summaries with citations | CC BY-SA / CC BY | Part of the dumps | small | T1 (comes with Wikipedia) | 3.8 |
| **Chronicling America** (Library of Congress) | Historical US newspapers 1770–1963 | PD | Bulk OCR text batches; page images | ≈ OCR text 1–2 TB · images ≈ hundreds of TB (exclude) | T2 | 4.0 |
| GDELT | Global event metadata (not article text) | Open | Bulk CSV | ≈ TB-scale (select event tables) | T3 | 3.0 |
| CC BY newsrooms (e.g., Global Voices, The Conversation (CC BY-ND), ProPublica (CC BY-NC-ND)) | Selected reporting | Per outlet | RSS capture | small | T3 | 3.2 |
| Government press releases (PD) | Official statements | PD | RSS / API | small | T3 | 3.0 |
| Common Crawl CC-NEWS | Crawled news articles | **Copyrighted content** | S3 | TB-scale | **X** | — |

---

## 10. Cultural heritage

| Source | What | License | Access | Size | Tier | Score |
|---|---|---|---|---|---|---|
| Smithsonian Open Access | 4M+ items of metadata and images | CC0 | S3 / GitHub | ≈ metadata GB · images TB (select) | T2 | 3.8 |
| The Met Open Access | Artworks: metadata and images | CC0 | GitHub CSV + images | ≈ metadata <1 GB · images ≈ TB | T2 | 3.7 |
| Library of Congress catalog records | Bibliographic MARC | PD | Bulk MARC | ≈ 10–50 GB | T2 | 3.6 |
| Europeana | European cultural heritage metadata | CC0 metadata | API / dumps | ≈ 100 GB metadata | T3 | 3.4 |

---

## 11. Explicitly excluded

| Source | Reason |
|---|---|
| Library Genesis, Sci-Hub, Anna's Archive, Z-Library | Copyright infringement. Firm exclusion. |
| Full Common Crawl | Uncurated, petabyte scale, copyrighted content |
| Paywalled news archives | No license for bulk copying |
| Full Internet Archive / Wayback mirror | Petabyte scale, mixed rights. Only curated PD/CC subsets. |
| Sites whose terms forbid bulk copying | Unless written permission is obtained (tracked as T3 "needs permission") |
| Classified, leaked, export-controlled (ITAR/EAR), FOUO/CUI or Distribution B–F material; login-gated security products (OSAC, TRIPwire) | Not lawfully public. Firm exclusion, whatever mirror holds a copy ([PREPAREDNESS_AND_DEFENSE.md §3](PREPAREDNESS_AND_DEFENSE.md#3-boundaries-policy)). |
| Material whose primary content is CBRN agent production or weaponisation, explosives and demolitions, weapons and munitions technical data (manufacture, guidance, firing tables), or offensive tooling. Public employment doctrine (gunnery, fires, targeting) is in scope (owner decision 2026-10-12) | Safety exclusion, even when publicly released ([§3.2](PREPAREDNESS_AND_DEFENSE.md#32-safety-exclusions-regardless-of-availability-or-license)). |

---

## 12. The Kiwix catalog as an accelerator

The Kiwix OPDS catalog (✓ **3,631** ZIM files, ✓ **5.5 TB** total; English-containing ✓ **1,301** files, ✓ **3.0 TB**) packages many of the sources above in one consistent, self-contained, checksummed format that opens directly in free readers. That makes it the **fastest path to broad coverage** in Phase 1. Raw-dump adapters are added for the T1 sources where we need history, diffs or structured data.

English ZIM volume by category (✓, maxi/unflavoured):

| Category | Files | Size |
|---|---|---|
| Gutenberg | 41 | 698 GB |
| TED | 357 | 680 GB |
| Other (Khan Academy, Survivor Library, maps, …) | 73 | 577 GB |
| Uncategorized (misc sites) | 546 | 453 GB |
| Wikipedia variants | 29 | 214 GB |
| Stack Exchange | 181 | 191 GB |
| Wikisource / Wikibooks / iFixit / Wikiversity / Wikivoyage / Wikiquote | 7 | 23 GB |

Recommendation: start with a curated list of about 1 TB (Wikipedia en maxi, Wiktionary, Wikisource, Wikibooks, Wikivoyage, Stack Exchange top sites, MDWiki, iFixit, PhET, Arch Wiki, DevDocs, Khan Academy, APOD, Survivor Library). Grow toward the full English catalog as budget allows.

---

## 13. Preparedness and defensive collections

Two collections are researched in their own document, [`PREPAREDNESS_AND_DEFENSE.md`](PREPAREDNESS_AND_DEFENSE.md) (2026-10-10, 104 sources across 11 domains, 81 of them T1/T2 candidates):

- **Preparedness & resilience** (vault shelf `preparedness`): agriculture and food preservation, water and sanitation, austere medicine, shelter/energy/engineering, navigation and communications, emergency management and continuity of government. Issuers such as USDA, FAO, WHO, JTS, FEMA, CISA, NOAA, NGA, FAA, USGS and the UK Cabinet Office.
- **Defensive & security literature** (vault shelf `defense`): electronics and radar fundamentals (NEETS, DOE handbooks), publicly released doctrine (Army, Joint, USMC), intelligence analysis and counterintelligence history (CIA CSI, FRUS, NSA history), protective security (CISA, ISC, USSS NTAC) and cybersecurity standards (NIST, DISA STIGs, MITRE).

That document adds three things to this one: **issuer authority as a hard gate** next to the license gate, **currency labels** so superseded and historical guidance is kept but never outranks the current edition, and **binding boundaries** (lawfully public material only; safety exclusions for weapons and offensive content). Mixed collections are filtered per document by the new `doc-collection` adapter, with owner review of borderline items through `wsp proposals`. 30 candidate and 2 excluded entries are in the registry.

| Collection | Top recommended first ingestions | ≈ Size |
|---|---|---|
| Preparedness | Bowditch + NGA pubs, NOAA ENC + Coast Pilot ✓ 0.8 GB, FAA handbooks, JTS CPGs, FEMA Independent Study, UK resilience guidance (OGL), USDA canning guide + Farmers' Bulletins, WHO IRIS WASH/clinical, NCVEC pools + SAFECOM guides | ≈ 0.15 TB |
| Defense | NIST CSRC + OSCAL, FRUS ✓ 14.8 GB, CIA CSI, Army public doctrine (filtered), CISA protective security + ISC + NTAC, DOE handbooks + NEETS | ≈ 0.1 TB |

---

## 14. Re-research cadence

- The **Curator** (PLAN §5) rescores every active source each quarter and runs the discovery bots weekly.
- This document is regenerated from `registry/sources.yaml` plus the narrative notes each quarter, and the diffs are reviewed in a PR.
