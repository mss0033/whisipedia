# Runbooks

Step-by-step procedures for the owner (PLAN §12.6). Start with `wsp doctor`.

| Runbook | When |
|---|---|
| [Install services](install-services.md) | First setup, moving the checkout, after reinstalling WSL |
| [Add a source](add-a-source.md) | Accepting Curator proposals, auto-promotions, adding by hand |
| [Recover a DEGRADED generation](recover-degraded.md) | `wsp status` or `wsp://system` lists a degraded generation |
| [Investigating a held release / possible compromise](held-release.md) | `wsp doctor` "Source integrity" WARN/FAIL, alerts `held:`, `integrity:`, `origin-drift:` |
| [Rotate the signing key](rotate-signing-key.md) | Suspected key leak, scheduled rotation |
| [Rebuild indexes](rebuild-indexes.md) | Format bump, local disk failure, broken search |
| [Move the bulk index files to the NAS](move-to-nas.md) | Freeing the PC disk (ADR-0011) |

Restoring from a second copy is not written yet: there is no second copy (decision D2). When one
exists, set `vault.mirror_root` and the repair ladder uses it automatically.
