# Runbook: install and run the services

Whisipedia runs as three systemd **user** units (PLAN §6.5, §12.6), rendered from the templates in
`deploy/systemd/`:

| Unit | What | Network |
|---|---|---|
| `whisipedia-orchestrator.service` | `wsp-orchestrator`: discovery, downloads, scrub, parity, drills, curator bots | yes (the only one) |
| `whisipedia-api.service` | `wsp serve` on `127.0.0.1:8737`, serving `navigator/dist` | loopback only |
| `whisipedia-index.timer` → `whisipedia-index.service` | nightly `wsp index build` (refine, index, catalog) | none |

## Before you start

1. `uv sync --all-packages` in the checkout (the units call `.venv/bin/wsp` and `.venv/bin/wsp-orchestrator`).
2. `config/local.toml` sets `vault.root`, `index.root` and the LOCAL `orchestrator.*` paths
   (see `config/local.toml.example`).
3. `cd navigator && npm ci && npm run build` so the API has a Navigator to serve.
4. `wsp doctor` shows no `FAIL` (install `par2` if it complains: parity is the bit-rot defence).

## Install

```sh
wsp service install --dry-run   # shows where the four unit files would go
wsp service install             # writes ~/.config/systemd/user/whisipedia-*, daemon-reload, enable
systemctl --user start whisipedia-orchestrator whisipedia-api
loginctl enable-linger "$USER"  # keep running without a login session
wsp service status
```

`install --now` enables and starts in one step; `--no-enable` only writes the files. Re-running
`install` after moving the checkout rewrites the units (only files it wrote itself are replaced).
Optional environment overrides (`WSP__SECTION__KEY=value`) go in `~/.config/whisipedia/whisipedia.env`.

Both the orchestrator and the index refresh run `wsp doctor --quiet --require vault` first: if the
NAS share is not mounted yet (WSL just started), the unit fails and systemd retries a minute later.

## Operate

* Logs: `journalctl --user -u whisipedia-orchestrator -o cat` (JSON lines), `-u whisipedia-api`.
* State: `wsp status`, `wsp doctor`, `wsp parity status`, `wsp proposals list`, or `wsp://system` in
  the Navigator.
* Stop: `systemctl --user stop whisipedia-orchestrator` (graceful: in-flight steps finish, the rest
  resumes on the next start).

## Uninstall

```sh
wsp service uninstall           # disable --now, remove the files it wrote, daemon-reload
```
