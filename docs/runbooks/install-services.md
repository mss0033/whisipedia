# Runbook: install and run the services

Whisipedia runs as three systemd **user** units (PLAN §6.5, §12.6), rendered from the templates in
`deploy/systemd/`:

| Unit | What | Network |
|---|---|---|
| `whisipedia-orchestrator.service` | `wsp-orchestrator`: discovery, downloads, scrub, parity, drills, curator bots | yes (the only one) |
| `whisipedia-api.service` | `wsp serve` on `127.0.0.1:8737`, serving `navigator/dist` | loopback only |
| `whisipedia-index.timer` → `whisipedia-index.service` | nightly `wsp index build` (refine, index, catalog) | none |
| `whisipedia-trawler.service` | `tools/wsp-trawl`: opens unvisited pages in a headless Chromium, one a minute, pausing while the owner reads; `wsp trawl report` shows what it found | loopback only |
| `whisipedia-yield.service` | `tools/wsp-yield`: when the whole machine runs short of memory, freezes `whisipedia-background.slice`; still short after 30 s, stops its resumable jobs (`wsp-bg` jobs, the trawler, the nightly index) and starts the trawler and `whisipedia-resume.service` after 10 calm minutes. While the owner reads (the API's `last-interactive` marker is younger than `WSP_YIELD_READER_SECONDS`, 300 by default, 0 for never), it also freezes the slice, and thaws it once the library has gone unused that long: downloads, ingests and the index build stop competing with the reader for the NAS. Probes (`X-Whisipedia-Probe`: audit, trawler, health check) and polling (health, system, events, the change feed) do not count as reading. The API is left alone, and so is the weather pull (`app.slice`: the weather page's refresh is the owner asking). A unit started during a pause waits for the thaw: the trawler is `Type=simple` so its start cannot time out, and the quality run's start timeout is 12 h | none |
| `whisipedia-digest.timer` → `whisipedia-digest.service` | 09:00 daily: `wsp alerts digest --notify`, the last day's alerts and trawler findings as one Windows notification (critical alerts also show at once, from the orchestrator) | none |
| `whisipedia-quality.timer` → `whisipedia-quality.service` | Sundays 03:17: `wsp quality run --alerts`, the gold queries through the API; a drop of nDCG@10 opens a warning | loopback only |

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
* A stalled API's Python stacks: `systemctl --user kill --kill-whom=main -s USR1 whisipedia-api`
  (stacks go to the journal). Without `--kill-whom=main` the signal also reaches the ZIM worker
  processes, which have no handler for it and die.

## Memory budget and background jobs

Background work shares `whisipedia-background.slice`; the slice file explains the budget for a 16 GiB
WSL (API 5 GiB, slice 8 GiB, 3 GiB for the kernel and the desktop). Inside it, the orchestrator,
the download lanes (`whisipedia-background-fetch.slice`) and one heavy job at a time each have their
share, and the shares fit under the slice's MemoryHigh, so one job cannot throttle the others.

* Ad-hoc jobs: `tools/wsp-bg NAME [MEMORY_MAX] -- COMMAND...` (heavy lock, default 4G) or
  `tools/wsp-bg --fetch NAME -- COMMAND...` (downloads, default 1G). It refuses a MEMORY_MAX at or
  above the slice's MemoryHigh. SIGTERM (`systemctl --user stop whisipedia-job-NAME`) reaches the job.
* Collection lanes: `tools/lanes/lane-run.sh` (see the document-collections runbook, "Exit codes").
* A rebuild that needs more than 4G: stop the lanes, then
  `tools/wsp-bg --wait index-rebuild 6G -- tools/lanes/rebuild-resume.sh <done file> SOURCE...`;
  it holds the heavy lock for the whole rebuild.
* Resuming after a reboot: `tools/lanes/whisipedia-resume.service` (install by hand, see the file)
  runs `tools/lanes/resume-after-reboot.sh`, which waits up to 15 minutes for the NAS and restarts
  the lanes in `~/.local/state/whisipedia/lanes.conf`, the jobs named in `jobs.conf` (from the command
  `tools/wsp-bg` saved for each, `jobs/NAME.cmd`), a rebuild in `index/rebuild.conf`, and the jobs
  `tools/wsp-yield` stopped for memory (`jobs/yield-stopped`). Render it with the checkout's path
  (`sed "s|@REPO@|$PWD|g" tools/lanes/whisipedia-resume.service > ~/.config/systemd/user/...`): until
  2026-10-22 the installed unit ran a copy of an old script that read none of these, so the lanes the
  memory guard stopped were never started again. A restart of `tools/wsp-yield` keeps what it owes
  (`jobs/yield-stopped`).

## Uninstall

```sh
wsp service uninstall           # disable --now, remove the files it wrote, daemon-reload
```

## Disk read-ahead (root, once)

WSL's virtual disks default to an 8 MB read-ahead; a memory-mapped search index pays it on every
random page fault (a cold search read 8-11 GB, 2026-10-13). Install the udev rule, and set it for
the running session:

```
sudo cp deploy/udev/60-whisipedia-readahead.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=block
sudo sh -c 'echo 256 > /sys/block/sdd/queue/read_ahead_kb'   # the root disk, this session
cat /sys/block/sdd/queue/read_ahead_kb                        # 256
```

`wsp doctor` does not check it; `cat /sys/block/*/queue/read_ahead_kb` does.
