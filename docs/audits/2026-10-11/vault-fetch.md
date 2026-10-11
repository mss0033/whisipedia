# Audit: wsp-vault, wsp-fetch, wsp-core (storage integrity on a soft CIFS mount, HTTP)

**Mount:** `cifs vers=3.0,cache=strict,soft,nounix,actimeo=1,rsize=wsize=4M,echo_interval=60`.

**What is sound:**
- the HTTP piece engine's resume digests, which are fdatasync'd before the state file;
- the `expected=` chain in ingest;
- the ledger hash chain. The live ledger verifies (8,670 events). It holds one past `ledger.quarantine` from an earlier concurrency bug.

Experiments live in `/tmp/claude-1000/-home-elizabeth-dev-whisipedia/audit-vault-fetch/`. Evidence tags: [demonstrated], [measured], [reasoned].

## CRITICAL / HIGH

**1 CRITICAL: a lost or relocated signing key would mark the whole library DEGRADED, and nothing ever clears it.**
- `bootstrap.py:271-282`: `load_signer(create=True)` silently makes a new key. The key path depends on `$XDG_CONFIG_HOME`/`$HOME`.
- `vault.py:774-784` then publishes the second key.
- The hourly scrub hits `SignatureError` for every generation, and the repair ladder runs `_degrade`. That writes `DEGRADED.txt` into every generation and refuses every promote. Ledger checkpoints then look tampered.
- No code ever removes `DEGRADED.txt`.
- Fix:
  - Pin keys: refuse a signer whose key is not in `keys/` when `keys/` is non-empty, and never auto-create a key when the vault has signed content.
  - Add a circuit breaker: if more than 2 generations or more than 5% fail at manifest level, stop and alert; never degrade.
  - Add a verified un-degrade path (a full re-scrub that passes clears DEGRADED, with a ledger event).

**2 HIGH: a transient soft-mount outage becomes permanent DEGRADED plus a CURRENT rollback.**
- `scrub.py:427-430` turns an `OSError` that survives retries into MISMATCH. The retry budget is only 15 s.
- `_intact` has no retry.
- `_roll_back` repoints `CURRENT.txt` at an older generation that was never re-verified.
- Fix:
  - Add an UNREADABLE status, and never escalate I/O errors to repair.
  - Canary-read the mount before each scrub slice.
  - Retry per chunk.

**3 HIGH: EIO on a ledger append, followed by a failed truncate, permanently breaks the ledger [demonstrated].**
- `ledger.py:261-285` suppresses the truncate error. The retry then appends after the fragment.
- A fresh writer dies in `_load_tail` with `JSONDecodeError`.
- Fix:
  - If the truncate fails, mark the ledger dirty and hold writes until a recovery cuts the file at the last valid line. Better: write-once segment files.
  - Read back with `POSIX_FADV_DONTNEED`.
  - Readers skip a trailing line that has no newline.

**4 HIGH: `atomic_write_bytes` can commit torn or empty content, and the vault has no locking [demonstrated].**
- Every writer of a destination uses the same `<dest>.partial` name.
- 4 writers × 300 writes produced 302 torn or mixed observations.
- Affected writers: `CURRENT.txt`/`CURRENT.d`, `.wsp-staging.json` and `DEGRADED.txt`, written by orchestrator promote, `wsp ingest` promote and repair rollback.
- `wsp scrub` takes no lock.
- Fix:
  - Unique `O_EXCL` temp names.
  - A per-source local flock around every vault mutation, taken by the orchestrator, the CLI and repair.

**5 HIGH: lost updates to `.wsp-staging.json` make seal fail [demonstrated].**
- 4 threads × 40 files: 32 ingests succeeded but only 9 were recorded.
- Fix: the same per-generation lock, or one write-once record file per artifact.

**6 HIGH: the "no-replace" rename is check-then-act** (`atomic.py:114-116`).
- Fix: `link()` then `unlink`, or `renameat2(RENAME_NOREPLACE)`.

**7 HIGH: parity can encode bytes that were never verified.**
- At seal, only the size is checked before `par2 create`.
- `_preverify` skips files the scrub verified within the last 30 days, and par2 re-reads them anyway, a TOCTOU gap.
- Fix: compute MD5 in the verification pass, then require par2's FileDescription MD5 and length to match it.

**8 HIGH: unverified or "Frankenstein" files can reach the vault for sources without checksums.**
- aria2 never fsyncs data before its bitfield. A VM kill can leave holes that get sealed as "unverifiable". With a full checksum, exit 32 rmtrees the whole stage (up to 253 GB). There is no timeout or cancel.
- On HTTP resume, the validator passes when either side is None.
- Same-size mirrors splice pieces when there are no hashes. `_spot_check` is predictable and runs after finalize.
- `_single_stream` accepts a truncated close-delimited body, and has no retry for a lone source.
- Fix:
  - Without upstream digests, require two independent full-file hashes to agree, or don't seal.
  - Make validators mandatory for resume.
  - Use the probed Content-Length.
  - Use the HTTP engine whenever no piece hashes exist.

## MEDIUM

- **9.** Retries restart whole files. Measured NAS reads run at 79–101 MB/s, so one pass over a 253 GB ZIM takes about 50 min. Fix: retry per chunk with an incremental hash, a per-chunk digest map, and resume.
- **10.** The scrub `RateLimiter` banks idle time as unlimited credit. A 25 GB slice after an hour idle slept 0 s instead of about 503 s [demonstrated]. `ParityKeeper.limiter` has the same flaw. `should_pause` is only checked between files. `ionice` has no effect on CIFS. Fix: a token bucket with a burst cap, and check pause inside `consume`.
- **11.** The scrub inventory walks the whole vault every hour: 81.7 s for 60k files. Fix: an incremental local SQLite inventory, verifying signatures only when (mtime, size) changes.
- **12.** Readers get manifests whose signatures are never verified, and `ManifestEntry.from_json` never checks relpaths. A path traversal is possible via `views.py:192`. Fix: validate every path, and verify once per (mtime, size).
- **13.** Rollback is wrong for partitioned sources. Generation ids are ordered lexicographically: `"2026-10-11.ca" < "2026-10.ca"`.
- **14.** A crash during `par2 create` wedges seal: par2 refuses to overwrite and returns rc=6. `-b` breaks above 32,000 files, and argv grows with the file count.
- **15.** Signed redirect URLs expire during long downloads. Re-resolve on 401/403/410.
- **16.** Two processes can fetch the same destination at once. Add a flock per destination.
- **17.** Ledger:
  - `quarantine_day` on a non-newest day breaks the chain.
  - The idempotency index is rebuilt by a full scan.
  - The open day is unsigned.
- **18.** The parity progress file has no fsync, so a corrupt file means the generation is skipped forever.
- **19.** Manifests at scale: a 300k-entry manifest takes 10.4 s to serialize and 4.9 s to parse, about 500 MB. `Manifest.entry()` is a linear scan, about 21 min of CPU across ParityKeeper's lookups. Add a dict index.
- **20.** Drills load whole files into memory (a bit flip on a 253 GB file), copytree, and never test on SMB.
- **21.** Directory renames on SMB need sharing-violation retries: prune, quarantine, `ParityKeeper._finish`.
- **22.** `check_relpath`:
  - `.repair` is not reserved;
  - there is no case-fold collision check;
  - control characters are allowed.
- **23.** Checksum algorithm names are normalized inconsistently, so `SHA-256` is recorded but never verified on the adopt path.
- **24.** TOCTOU in `_adopt_file` (hash then rename). Stale signed manifests are accepted. A crash window can publish `.wsp-staging.json`.
- **25.** A directory fsync on CIFS is a no-op (4 µs) [measured].

## LOW

- Hashing is not the bottleneck: sha256 runs at 2.06 GB/s and blake3 at 7.9–23.6 GB/s, while the NAS delivers 80–100 MB/s.
- Wrong docstrings:
  - "chmod is a no-op" (it maps to the DOS READONLY attribute, which is not immutability);
  - "rollback still verifies";
  - "parity never encodes damaged bytes".
- No jitter on retries.
- HTTPS→HTTP redirects are followed.
- Per-host caps are keyed on the final host.
- `local.toml.example` invites a contact address. The UA gets no validator rejecting `@`.
- Pointers are unsigned and never cross-checked against the ledger.

## Design target

- Treat the NAS as an untrusted store with no ordering guarantees:
  - content-addressed blobs, published via `O_EXCL`/link;
  - generations as signed manifests written once under unique names;
  - pointers as append-only signed records;
  - write-once ledger segments.
- Chunk-level integrity everywhere, plus a delayed re-verification (24 h) before promotion.
- Classify errors (I/O vs content), gate on a mount canary, add a circuit breaker, and a verified DEGRADED clear.
- Key discipline: pinned keys and an off-host backup.
- One local lock namespace used by every mutator.
- Parity bound to verified bytes.
- Defence below SMB:
  - NAS snapshots and ZFS scrub;
  - a read-only SMB account for readers;
  - an off-NAS mirror;
  - ideally a `hard` mount for the writer.
- Fetch policy: no seal without an independent digest or two agreeing full fetches. Refuse HTTPS→HTTP. Re-resolve signed redirects.
