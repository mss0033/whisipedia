# Vault (`packages/wsp-vault`)

Write-once golden storage and its Warden: PLAN §7 (layout), §8 (integrity, scrub, repair, ledger, drills), ADR-0002 (NAS over `soft` SMB).

## Layout

```
<root>/
├── README.txt  CATALOG.html  CATALOG.csv      wsp_vault.docs.write_library_docs
├── library/<domain>/<source_id>/
│   ├── README.txt  CURRENT.txt                 CURRENT.txt = generation id + LF
│   └── <generation_id>/                        sealed, files chmod 0444
│       ├── upstream/…  parity/…
│       ├── MANIFEST.json  MANIFEST.json.sig
│       └── DEGRADED.txt                        only if repair failed
├── staging/<domain>/<source_id>/<generation_id>/   being assembled; sealing = one dir rename
├── quarantine/…                                damaged files moved aside, never deleted
├── ledger/YYYY-MM-DD.jsonl                     hash-chained events, signed daily checkpoint
└── keys/<key_id>-<fingerprint>.pub             public keys only
```

`<domain>` comes from the registry (`domains_from_specs`); a source that already has a directory keeps it.

## Guarantees

- **Atomic, verified writes.** `*.partial` → fsync → drop page cache → read back and re-hash → rename. `EIO`/timeouts are retried with backoff. A rename never replaces an existing file, except pointer files (`CURRENT.txt`) and docs.
- **One read pass** computes size, SHA-256, BLAKE3 and any upstream algorithm hashlib supports (md5, sha1, …). Upstream checksums are checked during ingest.
- **Adoption.** `FileVault.adopt()` hashes a file that is already on the vault filesystem (e.g. `<root>/_incoming/`), checks it, then `rename`s it into staging. Nothing is copied.
- **Deterministic, signed manifests.** Every file has its path, size, sha256, blake3, role and upstream checksums. Signed with `SshSigner` (`ssh-keygen -Y`, namespace `whisipedia-manifest`) or `Ed25519Signer`.
- **Promotion** requires a valid signature and no `DEGRADED.txt`.
- **Ledger.** Supports a single writer only, with no locking. Appends are fsynced and read back. `verify()` replays the whole chain and checks every checkpoint signature.
- **Nothing is deleted.** The only exceptions are our own temporaries and the staging record.

## Scrub, repair, drill

```python
from wsp_vault.scrub import Scrubber
from wsp_vault.repair import RepairLadder

scrub = Scrubber(vault, Path("/local/state/scrub.sqlite"), bytes_per_sec=200e6, ledger=ledger)
report = scrub.run(max_bytes=500 * 2**30)  # resumable; call repeatedly (e.g. nightly)
RepairLadder(
    vault, ledger, parity=Par2Parity(), mirror_root=second_copy, refetch=adapter_refetch
).repair_findings(report.findings)
```

- **Scrub state** is kept in SQLite on local disk; the constructor refuses a path inside the vault. Files are due when never checked, when they failed last time, or after 30 days.
- **Repair ladder:** par2 → mirror (same layout under `mirror_root`) → re-fetch (must match the manifest) → `DEGRADED.txt`. If the degraded generation was current, `CURRENT.txt` is rolled back to the newest good generation. Every rung writes a ledger event. A bad manifest signature can only be restored from the mirror.
- **Drill:**
  ```python
  run_drill(vault, source_id, generation_id, workdir, DrillKind.BIT_FLIP, ledger=ledger)
  ```
  It copies the generation into `workdir`, injects the fault (bit flip, truncation, deletion, bad signature or interrupted promotion), and asserts that scrub detects it and the ladder repairs it. The real vault serves as the second copy. A `drill.result` event is recorded.
