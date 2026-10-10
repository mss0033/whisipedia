# Runbook: recover a DEGRADED generation

A generation is DEGRADED when the scrub found files that no longer match the signed manifest and the
repair ladder (par2 → second copy → re-fetch the same version) could not fix them (PLAN §8.4). The
generation keeps a `DEGRADED.txt` listing the files; if it was current, `CURRENT.txt` was rolled back
to the newest good generation, so search keeps working. Nothing was deleted: damaged files are in
`<vault>/quarantine/`.

## 1. See what happened

```sh
wsp status                                     # "degraded: <source>/<generation>"
cat <vault>/library/<domain>/<source>/<gen>/DEGRADED.txt
grep -h '"repair\.' <vault>/ledger/*.jsonl | tail   # which rungs ran and why they failed
wsp parity status                              # did this generation have parity?
```

## 2. Try again (the cause may have been transient)

A soft SMB mount can return I/O errors that look like damage. Re-check first:

```sh
wsp scrub --budget-gb 0      # inventories manifests, re-hashes what is due
wsp scrub                    # full pass of what is due; repairs run automatically
```

## 3. Repair from somewhere else

* **Parity missing or damaged:** parity is created by the orchestrator's backfill; once the data is
  good again it is protected automatically.
* **Upstream still serves the version:** the ladder's re-fetch rung needs the adapter to offer it;
  check the release with the adapter's discover output (`wsp ingest --dry-run --source <id>`).
* **Another copy exists** (a second NAS, an external disk): set `vault.mirror_root` to its root (same
  layout) and run `wsp scrub` again.
* **A good copy of a single file:** verify its SHA-256 against `MANIFEST.json`, then restore it with
  `FileVault.restore_file` (it verifies before replacing and quarantines the damaged file).

## 4. When it cannot be recovered

The DEGRADED generation stays as evidence; the previous generation serves. Re-acquire the newest
upstream release (`wsp resume <source>` if the source was paused, or wait for the next cycle). Retention
never prunes a DEGRADED generation; move it out by hand only after the newer generation is sealed,
indexed and verified.
