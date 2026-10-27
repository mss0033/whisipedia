#!/bin/sh
# ERA5 backfill lane (docs/components/weather.md): each year from the newest back to 1940, made by
# `wsp weather era5 --year Y` (a rerun continues a half-made year) and sealed by `wsp ingest`.
#
#   tools/wsp-bg --no-heavy-lock era5 2G -- tools/lanes/era5-lane.sh [FIRST_YEAR [LAST_YEAR]]
#
# FIRST_YEAR (default: this year) down to LAST_YEAR (default 1940). Exit codes of `wsp weather era5`:
# 0 staged (then ingested here, queueing for the ingest lock: the lane has nothing else to do), 7
# already sealed to its last published day, 3 another run is making it, anything else a failure: the
# year is retried after LANE_RETRY_WAIT seconds, at most LANE_TRIES times, then left for the next run
# of the lane. The log is $WSP_LANE_STATE/era5.log.
set -u
here=$(cd "$(dirname "$0")/../.." && pwd)
WSP="${WSP:-$here/.venv/bin/wsp --config-dir $here/config}"
state="${WSP_LANE_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/whisipedia/collections}"
mkdir -p "$state"
log="$state/era5.log"
first="${1:-$(date -u +%Y)}"
last="${2:-1940}"
retry_wait="${LANE_RETRY_WAIT:-600}"
tries="${LANE_TRIES:-3}"
stop=0
trap 'stop=1' TERM INT
say() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$log"; }

left=0
year=$first
while [ "$year" -ge "$last" ] && [ "$stop" -eq 0 ]; do
  n=0
  while :; do
    n=$((n + 1))
    say "=== era5 $year (try $n)"
    out=$($WSP weather era5 --year "$year" 2>&1)
    code=$?
    printf '%s\n' "$out" >>"$log"
    case $code in
      0)
        version=$(printf '%s\n' "$out" | sed -n 's/^next: wsp ingest --source era5-daily --version //p' | tail -1)
        if [ -z "$version" ]; then  # nothing staged: no ingest of an empty version (2026-10-26 review)
          say "era5 $year: nothing staged"
          break
        fi
        say "era5 $year: staged $version; ingesting"
        # Wait our turn in the ingest lock's queue (first come, first served): retrying every few
        # minutes lost to the download lanes' ingests for hours (2026-10-20), and a raw flock(1) wrapper
        # jumped the queue (2026-10-26 review).
        # shellcheck disable=SC2086  # $WSP is several words
        $WSP ingest --wait 86400 --source era5-daily --version "$version" >>"$log" 2>&1
        icode=$?
        say "era5 $year: ingest exit $icode"
        [ "$icode" -eq 0 ] || left=1
        break
        ;;
      7) say "era5 $year: already sealed"; break ;;
      *)
        say "era5 $year: exit $code"
        if [ "$n" -ge "$tries" ] || [ "$stop" -ne 0 ]; then
          say "era5 $year: left for a later run"
          left=1
          break
        fi
        sleep "$retry_wait"
        ;;
    esac
  done
  year=$((year - 1))
done
say "=== era5 lane done (left: $left)"
exit "$left"
