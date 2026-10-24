# Runbook: document collections (preparedness, defense, D14 sets)

Document collections (`access.adapter: doc-collection`, research doc
[§5](../research/PREPAREDNESS_AND_DEFENSE.md#5-tracking-design)) are fetched by `wsp collections fetch`,
not by aria2 lanes: every document is screened (distribution marking, safety lists) as it lands, and
requests are spaced to at most one per second per host (ten seconds for `www.cia.gov`, its
`robots.txt` crawl delay). Git-hosted sources in the same shelves (`frus`, `nist-oscal-content`) are
fetched the same way as one `git bundle`.

## 0. Once: API keys (GovInfo, Congress.gov)

Get a free key at <https://api.data.gov/signup/> and put it in `config/local.toml` (never in git):

```toml
[curator.api_keys]
govinfo_key = "<key>"
congress_key = "<key>"
```

## 1. Dry run (metadata only)

```sh
nice -n 19 wsp collections dry-run --top                      # research doc §6 top picks, sampled
nice -n 19 wsp collections dry-run --source army-pubs-public --max-pages 0   # whole listing
```

Per source it prints what is listed, the upstream total, allow/deny/review counts, known and
estimated sizes (`*` = extrapolated from a sample) and the requests it made, then the most frequent
rules and examples. The JSON report goes to `~/.local/state/whisipedia/collections/`.

## 2. Fetch, then ingest

```sh
nice -n 19 wsp collections fetch --source nga-nautical-pubs --version 2026-10-12   # <vault>/_incoming/<id>/<cycle>/
wsp ingest --source nga-nautical-pubs --version 2026-10-12 --dry-run              # check
wsp ingest --source nga-nautical-pubs --version 2026-10-12                        # adopt + seal + promote
wsp index build --source nga-nautical-pubs
```

`fetch` is resumable per cycle (`--version` names the cycle, default today in UTC; give the same one
to continue an earlier cycle); documents already held byte-identically are skipped. `--limit N`
fetches a first batch. It prints `ALERT` when an issuer listed a restricted document: the file was
deleted, nothing was kept. `wsp ingest --version` takes the same cycle label and works on that
version alone; without it, `wsp ingest --source` works on (and its exit code answers for) every
planned version of the source.

### Exit codes

Scripts and lanes decide by these codes, never by reading the output (audit 2026-10-12, C1).

`wsp collections fetch`:

| code | outcome | ingest? | item done? |
|---|---|---|---|
| 0 | complete: every listed document fetched, unchanged, rejected or held; plan written | yes | after the ingest succeeds |
| 1 | crashed (a traceback) | no | no |
| 2 | refused (excluded source, no legal basis, wrong or missing `--partition`) | no | no: fix the command |
| 4 | a listing was incomplete: nothing fetched | no | no: retry the **same** cycle later |
| 6 | partial: some or all downloads failed (or another fetch held the file); the rest is planned | yes, what came | no: fetch again later, in a **new** cycle |
| 7 | complete, with alerts (restricted documents listed and deleted) | yes | after the ingest succeeds |
| 8 | complete, nothing new: no plan | (nothing planned) | yes |
| 137, 143 | killed (OOM, SIGTERM) | no | no |

`wsp ingest` (for every planned version; with `--source S --version V [--partition P]`, for that
version only):

| code | meaning |
|---|---|
| 0 | ingested, or already sealed |
| 1 | a version failed (verification, or the vault refused it) |
| 2 | usage (`--version` without `--source`, `--partition` on an unpartitioned source, ...) |
| 3 | another ingest runs (the `ingest` lock), or this one stopped after `--max-seconds` (lanes pass an hour, `LANE_INGEST_SLICE`) so others could run; retry, it goes on where it stopped |
| 4 | files still missing or downloading |
| 8 | `--version` only: no download plan names that version (nothing was fetched for it) |

A version sealed after a partial fetch (6) cannot take more files: the next fetch uses a new cycle
label (`2026-10-12-r2`, ...; ordered naturally, so `-r10` is newer than `-r4`). A re-fetch of a cycle
whose ingest was interrupted before the seal is fine: a staged file is replaced only by bytes that
verify against the newer plan's checksum (ledger event `vault.restaged`).

### Lanes: `tools/lanes/lane-run.sh`

Long runs of many sources or partitions go through the lane runner, under `tools/wsp-bg`:

```sh
tools/wsp-bg --no-heavy-lock collections-small 2G -- tools/lanes/lane-run.sh lane-small - neets cia-csi
tools/wsp-bg --no-heavy-lock collections-cia 2G -- tools/lanes/lane-run.sh lane-cia cia-reading-room b0001 b0002
```

It marks an item done (`<state>/collections/<lane>.done`, skipped by every later run) only when the
fetch was complete (0, 7, 8) **and** `wsp ingest --version <cycle>` ingested that cycle (0, or 8 after
a fetch that found nothing new). An incomplete listing (4) is retried in the same cycle; a partial
fetch (6) is ingested, and the item is fetched again in a new cycle. An item whose fetch completed but
whose ingest was interrupted resumes at the ingest. A lane takes a lock per source, so two lanes never
fetch the same source at once; the second one leaves the item for its next run.

Run sources one after another (or in separate shells per host); long ones belong in `tools/wsp-bg`:

```sh
tools/wsp-bg --no-heavy-lock fetch-cia-csi 2G -- .venv/bin/wsp collections fetch --source cia-csi
```

## 3. Owner review

Documents the rules do not clearly allow are never fetched into the library. Each fetch files one
proposal per batch (`wsp proposals list`), listing every held document with its rule; files held after
the front-matter screen wait in `<vault>/_incoming/_quarantine/<source>/`.

```sh
wsp proposals show 41
wsp proposals accept 41 --note "CBRN medical management: in scope"   # adds them to allow_documents
wsp proposals reject 42 --note "demolitions manual: out of scope"    # adds them to deny_documents
git diff registry/filters/ && git commit -am "chore(registry): owner review of <source>"
```

Accepting writes the publication numbers into `registry/filters/<source>.yaml`; the next fetch takes
them. Delete quarantined files of rejected documents (`_incoming/_quarantine/<source>/`).

## 4. The large D14 sets (partitioned sources)

Both are *partitioned* (`access.partitioned: true`, [ADR-0010](../adr/0010-partitioned-sources.md)):
fetched one partition at a time, so a cycle never holds the whole set in one plan. See research doc
§8 for sizes and timing.

```sh
# USGS Historical Topographic Map Collection: one state per run (GeoPDF and GeoTIFF of that state)
nice -n 19 wsp collections fetch --source usgs-historical-topo --partition hi --version 2026-10-12
wsp ingest --source usgs-historical-topo --partition hi --version 2026-10-12
wsp index build --source usgs-historical-topo
# CIA reading room (archive.org mirror of foia.cia.gov): batches of 10,000 items, b0001 … b0090
nice -n 19 wsp collections fetch --source cia-reading-room --partition b0001 --version 2026-10-12
wsp ingest --source cia-reading-room --partition b0001 --version 2026-10-12
wsp index build --source cia-reading-room
```

How partitions behave:

* Each run is the cycle `<YYYY-MM-DD>.<partition>` (`--version 2026-10-11` resumes that day's cycle
  of the partition; `--partition` is required, partition names are lowercase). The fetch carries over
  only that partition's catalog, so documents of other partitions are never relabelled.
* `wsp ingest` seals and promotes each partition into `library/<shelf>/<source>/<cycle>.<partition>/`
  and writes `CURRENT.d/<partition>.txt`; `CURRENT.txt` names the part promoted last. Partitions can
  be ingested in any order; only a newer generation *of the same partition* blocks a promotion.
* The source's content is the **current set** (the current generation of every partition).
  `wsp index build` refines each partition once and indexes their union as one set generation
  (`index/refinery/<source>/<newest>.setNNNNN/`, hard links to `_parts/<generation>/`), so search
  and documents cover every partition fetched so far.
* Retention runs per partition and keeps every generation a current catalog still points at; it
  never prunes one partition because another one is newer. Re-fetching a partition later (a new
  cycle) supersedes only that partition's earlier generation.
* The orchestrator does not schedule discovery for partitioned sources: acquisition stays with
  `wsp collections fetch`, one partition per run.

Check the current set by hand: `ls library/<shelf>/<source>/CURRENT.d/` and
`cat library/<shelf>/<source>/CURRENT.d/*.txt`.

## 5. Document policy in the orchestrator path

When a scheduled release of a `doc-collection` source contains a document the policy rejects
(restricted marking, safety list) or holds (needs review), the orchestrator drops that document and
promotes the rest. The rejected file is deleted at once, a held one waits in
`<orchestrator workdir>/_quarantine/<source>/`; the alert `withheld:<source>/<version>` lists both, the
ledger has a `release.withheld` event, and held documents get one review proposal per release
(`wsp proposals list`, decided as in §3). This never pauses the source. A release in which every
document is withheld ends quarantined (`wsp status`), again without pausing the source.

## FTP channels (owner decision D16)

The fetcher speaks anonymous FTP (`wsp_fetch.ftp`) for issuers whose bulk channel is an FTP service.
It logs in as `anonymous` with the password `anonymous@`, never an email address. It lists a
directory with MLSD and returns it in the HTML index form adapters read. Downloads resume with REST
and are checked against the issuer's MD5 sidecars. A doc-collection admits an FTP host only when its
`origins` name it as `ftp://HOST`; a bare host means HTTPS only.

The rules for a service are its `ftp://HOST/robots.txt` (RFC 9309 covers FTP), honoured like a web
host's. **NCBI's (`ftp.ncbi.nlm.nih.gov`) disallows every agent over FTP as over HTTPS.** D16 was
decided on the belief that FTP had no robots.txt, so `pubmed` and `pubchem` are paused until the
owner rules again (2026-10-24).

Limits:
* NCBI's server refuses `AUTH TLS`. The transfer is unencrypted, and the MD5 sidecars come over the same
  channel: they catch corruption, but not a tampering man in the middle.

## DSpace 7 repositories through their sitemaps (`dspace-sitemap`)

WHO IRIS, NPS Calhoun and the FAO Knowledge Repository run DSpace 7. Its stock robots.txt (read
2026-10-24 on all three) disallows the REST API (`/server/api/*`, search included), OAI-PMH
(`/server/oai/*`), `/search` and `/browse`, with `Crawl-delay: 10`; it allows the sitemaps, the item
pages (`/items/<uuid>`, `/entities/publication/<uuid>`, `/handle/...`) and the bitstream content
endpoint (`Allow: /server/api/core/bitstreams`). The `dspace-sitemap` discovery reads only those:

1. `<base>/sitemap_index.xml` and every sitemap it names: the item pages with their `<lastmod>`
   (`match` narrows them first: FAO's handle ends in the language, `ca8818en`).
2. Each item page. It is rendered on the server: the Highwire `citation_*` tags (title, date,
   language, keywords, one `citation_pdf_url`; no rights, no Dublin Core tags) and the REST answers
   the page was built from (`<script id="dspace-angular-state">`): the item's whole metadata record
   and its ORIGINAL bundle's files in order with sizes. Keys and URLs are those the old `dspace7`
   search made (`who:10665-102539`, `-1` for the second file; the content endpoint), so documents
   already held are recognised and not fetched again.
3. `topics` (the old search queries) select items on their title, subjects, abstract, series and
   descriptions; the search also matched full texts, so a topic now selects fewer items.

**Request budget.** Every item page costs a request at 10 s, and which items are on topic is only on
the page. A run reads at most `max_items` item pages (20,000 in the registry: 2.3 days), newest
`<lastmod>` first, and keeps every page it read in `<workdir>/<source>/dspace-items-*.sqlite`
(committed every 20 pages, so a killed run keeps its work). Later runs read only pages not read yet
and pages whose sitemap date moved; a page is read again after a year regardless. First passes:

| source | items in the sitemaps | pages read | first pass | runs of 20,000 |
|---|---|---|---|---|
| who-iris | ~277,000 (6 sitemaps) | all | ~32 days | 14 |
| fao-knowledge-repository | ~190,000 (4 sitemaps) | English only, ~95,000 | ~11 days | 5 |
| nps-calhoun | ~72,000 publications (2 sitemaps) | all | ~8.4 days | 4 |

A run that leaves pages unread is a *sample*: what it found is fetched, and nothing is inferred
withdrawn. A topical source's listing is always a sample (an item left out by `topics` is still
listed upstream), so held documents are never labelled withdrawn because a topic stopped matching.
A dry run (`--max-pages N`) reads N sitemaps and 5×N item pages, and keeps nothing.

Answers that are not failures: a page whose item request was refused (WHO lists items edited the day
before whose request answers 401) and a page that is gone (404) are remembered until their sitemap date
moves. Failures: a page without its item record. After ten failures with fewer successes the run
stops reading and the listing is incomplete, so nothing is fetched: **NPS Calhoun answers every item
page with an empty client-side shell (2026-10-24)**, so `nps-calhoun` stays paused until its pages
render on the server again (`wsp collections dry-run --source nps-calhoun --max-pages 1` shows it).

## When an issuer is blocked (403)

Never change the User-Agent or retry around a WAF. Record the official alternative (GovInfo, an
agency API or bucket, an agency's own archive.org collection) in the source's `access.mirrors`, or
"no official mirror found"; the research doc §5.6 table is the current state.
