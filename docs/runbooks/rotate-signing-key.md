# Runbook: rotate the signing key

Manifests (`MANIFEST.json.sig`), parity sidecars (`PARITY.json.sig`) and the ledger's daily
checkpoints are signed with the vault key (`vault.signing_key`, default
`~/.config/whisipedia/vault-signing-ed25519.pem`, mode 0600, never in the repository). Its public
half is published under `<vault>/keys/<key_id>-<fingerprint>.pub`.

Rotate when the private key may have leaked, or on a schedule you choose.

## Why it needs care

Verification uses the configured key. Generations signed with the old key do not verify with the new
one, so the scrub would report every old manifest. A rotation therefore **re-signs** existing
manifests after verifying them with the old key, never blindly.

## Steps

1. Stop writers: `systemctl --user stop whisipedia-orchestrator` (and do not run `wsp ingest`).
2. Verify everything with the OLD key first: `wsp scrub` until "pass complete", and `wsp doctor`
   (ledger fast verify). Do not rotate while anything is DEGRADED.
3. Keep the old key: `mv ~/.config/whisipedia/vault-signing-ed25519.pem ~/.config/whisipedia/vault-signing-ed25519.old.pem`.
4. Generate the new key: any `wsp` command that writes (e.g. `wsp drill`) creates it on first use, or
   set `vault.signing_key` to a key you made (`signer = "ssh"` uses `ssh-keygen -Y`).
5. Re-sign: for each sealed generation, verify `MANIFEST.json` (and `parity/PARITY.json`) with the
   OLD key, then write a new `.sig` with the NEW key (atomic write, read back). Record one
   `key.rotated` ledger event with both fingerprints. (There is no `wsp` command for this yet; do it
   with `wsp_vault.signing` in a short script and keep the script with the old key.)
6. The ledger keeps old checkpoints signed by the old key: keep the old public key in `<vault>/keys/`
   and verify the history with it; new days are checkpointed with the new key.
7. `wsp doctor`, `wsp scrub --budget-gb 1`, then start the orchestrator again.
8. Store the old private key offline (or destroy it if it leaked, after step 5).
