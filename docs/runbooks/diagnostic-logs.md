# Diagnostic run logs

A run log is a timed, structured record of one `wsp` command: what ran, which code ran it, how long
each step took and how much memory it used, and what the library's loggers said on the way. Use it to
find out why a job was slow or what it did before it failed.

## Where and when

`<state>/logs/<command>/<UTC time>-<pid>.jsonl` (state is `~/.local/share/whisipedia/state` on this
machine), for example `logs/index-build/20261022T030000Z-12345.jsonl`. The newest 30 of each command are
kept. A log whose process is still running is never removed, and a run that found its lock held and
left at once (exit 3 within a minute) keeps none: lanes retry a held lock every minute.

* Always kept: `wsp index build`, `index rank`, `index spell`, `ingest`, `weather era5` (long jobs:
  the index build of 2026-10-21 ran 28 hours and left no record of which step took the time).
* Any other command: `wsp --diag <command> ...`, or set `WSP_DIAG=1` in the environment (for a service,
  in `~/.config/whisipedia/whisipedia.env`). `WSP_DIAG=debug` also keeps debug lines; `WSP_DIAG=1` set
  for an always-kept command keeps the same info lines it keeps anyway.

A log that cannot be opened (no state directory, a full disk) prints one warning and the command runs
on without it.

## What is in it

One JSON object per line, each with `ts` (UTC), `level`, `logger` and `msg`:

| `event` | Fields |
|---|---|
| `run-start` | `command`, `argv`, `pid`, `revision` (the checked-out commit), `python` |
| `phase-start` | `phase`, plus the phase's own fields (`source`, `generation`) |
| `phase-end` | the same, `seconds`, `peak_rss_mb`, and the phase's results |
| `phase-error` | the same, `seconds`, `error` (the exception) |
| `run-end` | `exit` (the exit code, or the exception that ended the run), `seconds`, `peak_rss_mb` |

Lines without an `event` are the library's own log lines (the index build's progress lines, warnings).
`peak_rss_mb` is the process's peak so far, so a phase whose peak is higher than the one before it is
the one that grew.

The index build's phases: `index-finish`, `refine`, `index-generation`, `index-partitioned`, `rank`,
`spell`, `reindex`, `refine-again`.

The ingest's phases: `ingest-version` (per source and generation, with its status, file count and
bytes) and, inside it, `seal`.

## Reading one

```sh
# which phases took the time
jq -r 'select(.event == "phase-end") | [.seconds, .phase, .source // ""] | @tsv' FILE | sort -rn | head
# what failed
jq 'select(.level == "error")' FILE
```

Code: `wsp_core.diag` (`phase()` times a step in any module; outside a run log it writes nothing).
