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
nice -n 19 wsp collections fetch --source nga-nautical-pubs      # writes <vault>/_incoming/<id>/<YYYY-MM-DD>/
wsp ingest --source nga-nautical-pubs --dry-run                  # check
wsp ingest --source nga-nautical-pubs                            # adopt + seal + promote
wsp index build --source nga-nautical-pubs
```

`fetch` is resumable per cycle (`--version YYYY-MM-DD` to continue an earlier cycle); documents
already held byte-identically are skipped. `--limit N` fetches a first batch. It exits 1 and prints
`ALERT` when an issuer listed a restricted document: the file was deleted, nothing was kept.

Run sources one after another (or in separate shells per host); long ones belong in `tmux`/`nohup`:

```sh
nohup nice -n 19 wsp collections fetch --source cia-csi > ~/.local/state/whisipedia/collections/cia-csi.log 2>&1 &
```

## 3. Owner review

Documents the rules do not clearly allow are never fetched into the library. Each fetch files one
proposal per batch (`wsp proposals list`), listing every held document with its rule; files held after
the front-matter screen wait in `<vault>/_incoming/_quarantine/<source>/`.

```sh
wsp proposals show 41
wsp proposals accept 41 --note "CBRN medical management: in scope"   # adds them to allow_documents
wsp proposals reject 42 --note "weapons employment: out of scope"    # adds them to deny_documents
git diff registry/filters/ && git commit -am "chore(registry): owner review of <source>"
```

Accepting writes the publication numbers into `registry/filters/<source>.yaml`; the next fetch takes
them. Delete quarantined files of rejected documents (`_incoming/_quarantine/<source>/`).

## 4. The large D14 sets

Both are fetched as part generations, one partition at a time, so a cycle never holds the whole set
in one plan. See research doc §8 for sizes and timing.

```sh
# USGS Historical Topographic Map Collection: one state per run (GeoPDF and GeoTIFF of that state)
nice -n 19 wsp collections fetch --source usgs-historical-topo --partition hi
wsp ingest --source usgs-historical-topo
# CIA reading room (archive.org mirror of foia.cia.gov): batches of 10,000 items, b0001 … b0090
nice -n 19 wsp collections fetch --source cia-reading-room --partition b0001
wsp ingest --source cia-reading-room
```

## When an issuer is blocked (403)

Never change the User-Agent or retry around a WAF. Record the official alternative (GovInfo, an
agency API or bucket, an agency's own archive.org collection) in the source's `access.mirrors`, or
"no official mirror found"; the research doc §5.6 table is the current state.
