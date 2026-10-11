# ADR-0009: Source integrity: provenance, independent verification and anomaly holds

- Status: Accepted
- Date: 2026-10-10

## Context

The owner's requirement for self-maintenance is that the library stays healthy and up to date and
that its data sources "are healthy and are unlikely to have been compromised". The owner does not
want a model-driven "living" library: self-maintenance means health, freshness, integrity and
provenance, never machine-edited content.

Until now a release was trusted when its bytes matched the digest the adapter found at discovery.
For most sources that digest travels next to the bytes (same server, often the same mirror), so a
tampered mirror, a hijacked origin or a poisoned upstream build would pass as long as it published
a matching digest. Nothing recorded where the bytes actually came from, and the only content check
before promotion was "document count did not drop by more than 2 %, size within ±10 % of trend".

## Decision

Add an **integrity guard** (`wsp_orchestrator.integrity`) around the release pipeline. It is
classic engineering only: signatures, digests, pinning and descriptive statistics.

1. **Provenance per release.** `HttpFetcher` records, per request, the final URL, the redirect
   chain, the TLS peer certificate (SHA-256, subject, issuer, validity) and the HTTP validators,
   and per file which mirror served which byte ranges, which mirrors were rejected (and why) and
   spot checks that re-read random ranges from every mirror. The guard adds the checksum channels
   and the origin verdicts. After sealing, the record is written as **`PROVENANCE.json` + `.sig`**
   next to the manifest, signed by the vault signer and bound to the manifest bytes by BLAKE3, the
   same way as the parity sidecar (`wsp_vault.attest`). The scrub verifies the signature.
2. **Independent verification channels.** After the download the digest is fetched again through a
   channel that did not carry the bytes: Wikimedia `sha1sums.txt`/`md5sums.txt` from
   `dumps.wikimedia.org` (bytes from mirrors), Kiwix `<file>.sha256` from `download.kiwix.org`
   (bytes from Metalink mirrors, whose piece hashes and digests the fetcher already cross-checks),
   otherwise `<url>.<algorithm>` from a mirror that served no bytes, then the origin. Status per
   file: `independent`, `same-origin`, `unavailable` or `mismatch`.
3. **Mirror agreement.** A mirror that advertises another size, serves pieces failing their
   Metalink hash, or fails a spot check is a disagreement.
4. **Origin pinning (trust on first use).** The first fetch of a source pins every host and the
   family of the CA that issued its certificate (local state DB, `origin_pins`). Later: an origin
   or checksum host on an unknown registrable domain **blocks** (the release is held in FETCHED);
   a new host on a known domain, a changed issuer family, or a new mirror whose bytes could not be
   verified independently **alerts**; certificate rotation within the same CA is recorded.
5. **Content anomaly detection before promotion.** A content profile per generation (documents per
   namespace and MIME type, a log2 size histogram, a deterministic hash-chosen sample of documents
   with content hash, size and title, and in sampled ZIM HTML the `<script>` density, binary bytes
   and executable signatures) is compared with the last promoted generation: namespace changes,
   histogram total-variation distance, changed and deleted fractions, new MIME types, script
   density jumps, binary/executable content. The PLAN §6.4 count/size checks are part of it. The
   result is a **signed `PROMOTION.json`**; anything out of range holds the release for
   `wsp approve` / `wsp reject`. Thresholds are in `[integrity.anomaly]`.
6. **Actions are configurable** per kind (`[integrity]`: `info | alert | hold | quarantine`). The
   defaults: disagreement between channels or mirrors **quarantines** (never promoted, the source
   is paused, a critical alert is raised, the downloaded files stay in the work directory as
   evidence); an unknown origin domain **holds**; everything else alerts or is recorded.
7. **Known-good anchoring.** Retention keeps the newest generation that passed every check and the
   newest one that has also *settled* (promoted `settle_days` ago and fully verified by the scrub
   since), so the previous good generation stays until its successor has proven itself.
8. **Health reporting** adds provenance coverage, channel status per source, held and quarantined
   releases, pinned hosts and drift alerts to `wsp doctor` and `/v1/system` (`ops.integrity`).

Related operational fixes ship with it: the daemon cancels downloads, scrub, parity and par2 on
SIGTERM and exits within `shutdown_grace_seconds` (10 s) whatever a job does (daemon job threads,
`os._exit` if a library left a non-daemon thread); par2 runs with `-t` (default 2) and `-m`; scrub and
parity pause while the API has seen interactive use recently (a marker file touched by one ASGI
middleware).

## Consequences

- A compromised mirror can no longer slip a matching digest past us for sources with a separate
  digest channel; a wholesale compromise of the primary still can, but then provenance shows the
  pinned hosts and certificates, and the anomaly checks look at the content itself.
- Sources without any published digest (Open Library, Gutenberg) stay `unverifiable`: they raise an
  alert each cycle unless `integrity.unverifiable` is lowered to `info`. Their protection is the
  anomaly check and provenance.
- False positives are possible (a mirror caught mid-sync, a legitimate mass cleanup upstream). They
  cost an owner decision, never data: the previous generation keeps serving.
- Generations sealed before this change have no `PROVENANCE.json`; coverage shows the gap and it
  closes as sources are refreshed. Nothing is re-signed retroactively.
- The orchestrator state schema goes to v4 (two new tables); older code refuses a v4 database.
- See the runbook [Investigating a held release / possible compromise](../runbooks/held-release.md).
