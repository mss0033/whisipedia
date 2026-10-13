# Move the bulk index files to the NAS

Applies [ADR-0011](../adr/0011-bulk-index-files-on-the-nas.md). Databases stay on local disk. Leave the
NAS mount configuration alone.

The paths below are the owner's machine. `INDEX` is `[index] root` (local disk) and `NAS` is
`/mnt/z/whisipedia/index`.

## 1. The Refinery

1. **Stop the writers.** Stop the orchestrator and the nightly index job, so nothing refines or indexes
   during the copy:
   `systemctl --user stop whisipedia-orchestrator whisipedia-index.timer`.
   Check that no `whisipedia-step-*` or `whisipedia-job-*` unit is running. The API keeps serving
   from the local copy.
2. **Copy.** Keep hard links: set generations link their members' part files. Leave out the id maps,
   which stay local:

   ```
   rsync -aH --info=progress2 --no-perms --no-owner --no-group \
     --exclude '_ids.h.npy' --exclude '_ids.loc.npy' --exclude '*.partial' \
     INDEX/refinery/ NAS/refinery/
   ```
3. **Verify.** Every copied file must have the same SHA-256 on both sides:

   ```
   (cd INDEX/refinery && find . -type f ! -name '_ids.*.npy' ! -name '*.partial' -print0 | sort -z \
     | xargs -0 sha256sum) > /tmp/refinery.local.sha256
   (cd NAS/refinery && sha256sum --quiet -c /tmp/refinery.local.sha256)
   ```

   No output means every file matches. Do not continue until it does.
4. **Place the id maps** in the local map root, so the API does not rebuild them over the network:

   ```
   uv run python tools/place_refinery_maps.py INDEX/refinery NAS/refinery INDEX/refinery-maps
   ```
5. **Switch.** In `config/local.toml`, under `[index]`, set
   `refinery_root = "/mnt/z/whisipedia/index/refinery"`. Restart the API and start the orchestrator and
   the timer again. Open a few documents from different sites and check them; check
   `wsp doctor` too.
6. **Keep the local copy** until the owner approves deleting it, after about a week of normal
   operation. Then run `rm -r INDEX/refinery`.

Roll back by removing `refinery_root` and restarting. The local copy is unchanged.

## 2. The change-feed archive

1. In `config/local.toml`, under `[index]`, set
   `changes_archive = "/mnt/z/whisipedia/index/changes-archive"`. Optionally also set
   `changes_keep_rows` (default 2,000,000) and `changes_keep_per_source` (default 1,000).
2. Restart the API. Readers must know the archive before any rows leave the database.
3. Run `wsp index archive-changes`. It seals 10M-change ranges, read back and checked, and removes
   their rows locally. It is safe to stop and run again.
4. Run `wsp index archive-changes --compact` once, at a quiet time. It rewrites `changes.sqlite` to
   give the space back.

The archive is write-once. Never edit or delete a range while the feed is in use: every cursor
before the archive mark is read from it.
