# Runbook: investigating a held release / possible compromise

The integrity guard (ADR-0009) stops a release before it reaches the library in two ways:

| What you see | Meaning | Data at risk |
|---|---|---|
| `wsp doctor` **WARN** "N held", alert `held:<source>/<version>` | **Held**: an unknown origin domain (state FETCHED) or a content check out of range (state INDEXED). Waits for you. | None. The previous generation keeps serving. |
| `wsp doctor` **FAIL** "possible compromise", alert `integrity:<source>/<version>` (critical), source paused | **Quarantined**: an independent digest disagreed with the bytes, or mirrors disagreed. Never promoted. | None. The bytes never entered the vault. |

Alerts `origin-drift:<source>:...` (a new host on a known domain, a changed certificate issuer) are
informational: the release continued. Read them anyway; a series of them is how a slow takeover looks.

## 1. Collect the evidence (read-only)

```sh
wsp doctor                                   # "Source integrity" line: held, quarantined, drift
sqlite3 -readonly <state_db> \
  "SELECT source_id, version, state, hold_reason, last_error FROM releases WHERE hold_reason IS NOT NULL OR state='quarantined'"
sqlite3 -readonly <state_db> \
  "SELECT verdict, provenance_json FROM provenance WHERE source_id='<source>' AND version='<version>'" \
  | python3 -m json.tool | less              # files, mirrors, byte ranges, TLS, channels, findings
sqlite3 -readonly <state_db> "SELECT * FROM origin_pins WHERE source_id='<source>'"
grep -h '"release.hold"\|"source.paused"' <vault>/ledger/*.jsonl | tail
```

For a release held before promotion the signed report is in the vault:
`<vault>/library/<domain>/<source>/<generation>/PROMOTION.json` (checks with value and limit, the
most-changed titles, the baseline generation). `PROVENANCE.json` next to it is the signed record of
where the bytes came from. A quarantined release has no generation; its downloaded files are kept
in `<orchestrator.workdir>/<source>/` for inspection (they are not cleaned until a later release of
the source is promoted).

## 2. Decide what happened

**`checksum-channel-mismatch`** (quarantined). The digest fetched from the primary disagrees with
the bytes or with the digest seen at discovery. Check the upstream yourself from another network if
you can: fetch the checksum file (e.g. `https://dumps.wikimedia.org/<wiki>/<date>/<wiki>-<date>-sha1sums.txt`,
`https://download.kiwix.org/zim/.../<file>.sha256`) and compute `sha1sum`/`sha256sum` of the kept
file. Upstream re-publishing a file under the same name (rare, but it happens after a broken
build) shows as a changed digest on the primary with the old one still on mirrors. Anything else is
a compromise until proven otherwise: keep the files, note the mirror hosts from the provenance.

**`mirror-size-disagreement`, `mirror-content-disagreement`, `mirror-corrupt-pieces`**
(quarantined). One mirror served something else. The provenance names the mirror (`sources[].url`,
`rejected[]`). If the other mirrors and the independent channel agree, the mirror is broken or
tampered with; report it to the upstream project. To accept that a mirror was merely mid-sync,
lower `integrity.mirror_disagreement` to `alert` for one cycle, then restore it.

**`origin-unknown-domain`** (held in FETCHED). The release or its checksum came from a domain this
source never used. Legitimate upstream moves are announced (project blog, mailing list, the
registry's `access.endpoints`); verify the announcement through a channel you already trust before
approving. Approving pins the new hosts.

**Content checks** (held in INDEXED): `deleted-fraction`, `changed-fraction`, `namespace:<ns>`,
`size-histogram`, `new-mimetypes`, `script-density`, `binary-html`, `executables`, `sanity`. Look at
`top_changed` in `PROMOTION.json` and open a few of those titles in both generations (the previous
one is still current). A cleanup upstream (spam purge, a namespace split) explains count changes; new
script tags or executables inside articles almost never have an innocent explanation in an archive.

## 3. Act

```sh
wsp approve <source> [version] --note "why"   # held: continue (origin: trust and pin the new hosts)
wsp reject  <source> [version] --note "why"   # held: quarantine it; it is never promoted
wsp resume  <source>                           # after a quarantine: un-pause the source and clear
                                               # its integrity alerts; the next cycle tries the
                                               # newest upstream release (a new version)
```

Every decision is a ledger event (`release.hold` with your note). An approved content hold writes a
new `PROMOTION.json` recording "approved by the owner". A quarantined version is never retried
automatically; if you are sure it was a false alarm, re-acquire it by hand (`wsp ingest`) after the
upstream issue is fixed.

## 4. Afterwards

- If you lowered a threshold or an action, restore it.
- If a mirror was bad, remove it from `access.mirrors` in the registry (or tell the upstream).
- `wsp doctor` should show the integrity line OK again.
