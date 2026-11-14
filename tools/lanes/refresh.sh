#!/bin/sh
# Scheduled refresh of collection sources that keep publishing (whisipedia-refresh.timer, weekly).
#
#   tools/lanes/refresh.sh [SOURCE...]          start the refresh lane as a background job
#   tools/lanes/refresh.sh --run SOURCE...      (inside the job) the lane itself
#
# A lane marks an item done for good (tools/lanes/lane-run.sh, NAME.done), so a source that keeps
# publishing (CRS reports: several a week) took no new document once its lane was done (2026-11-09).
# This runs the lane "refresh" over SOURCE... (default $WSP_REFRESH_SOURCES, else the service's list:
# crs-reports nist-csrc usmc-mcpel itu-recommendations etsi-deliverables) as the background job whisipedia-job-collections-refresh
# (tools/wsp-bg --fetch: the download lanes' slice, capped at 1G). Each run first opens the items the last refresh finished: out of refresh.done, on a
# new cycle (today's date); an item a refresh left undone keeps its cycle and resumes. It also takes
# every source (partition) with documents given up for good over 30 days ago and not retried since
# (`wsp collections given-up`; a refresh that never fetched them leaves them due next week): a lane marks such an item done and never fetches it again.
# Its items' logs are refresh-<source>.log, apart from the lanes' own.
#
# What a refresh costs: the fetch lists the source and downloads only what the library does not hold
# (a held document the listing calls unchanged is skipped; one already downloaded this cycle is
# reused). Nothing new and an unchanged catalog: exit 8, no plan, nothing sealed, the item done. New
# documents (or changed catalog metadata): one new generation, as any fetch. A source another lane is
# fetching is left for this lane's next round (lane-run.sh's per-source lock; a "-" lane never waits).
# A refresh still running when the timer fires again is left alone.
set -eu
REPO=$(cd "$(dirname "$0")/../.." && pwd)
JOB=collections-refresh
NAME=refresh
if [ "${1:-}" = --run ]; then
  shift
  WSP=${WSP:-$REPO/.venv/bin/wsp}
  STATE=${WSP_LANE_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/whisipedia/collections}
  mkdir -p "$STATE"
  today=$(date -u +%F)
  # reopen LANE ITEM...: items LANE finished last time go out of its .done, on a new cycle (today).
  reopen() {
    lane=$1
    shift
    touch "$STATE/$lane.done" "$STATE/$lane.cycles" "$STATE/$lane.fetched"
    for it in "$@"; do
      grep -qxF "$it" "$STATE/$lane.done" || continue
      grep -vxF "$it" "$STATE/$lane.done" > "$STATE/$lane.done.tmp" || true
      mv "$STATE/$lane.done.tmp" "$STATE/$lane.done"
      echo "$it $today" >> "$STATE/$lane.cycles"  # a new cycle (the last line wins)
      echo "$it $today -" >> "$STATE/$lane.fetched"  # not fetched yet (a second refresh in a day)
    done
  }
  # Documents given up for good a month ago, on any source (its own lane done long since): fetched
  # again with it, once a month (`wsp collections given-up`; the fetch that tries them records it).
  partitioned=""
  due=$("$WSP" collections given-up 2>> "$STATE/$NAME.log") || due=""
  while read -r s p; do
    [ -n "$s" ] || continue
    if [ -n "$p" ]; then
      partitioned="$partitioned $s:$p"
    else
      case " $* " in *" $s "*) ;; *) set -- "$@" "$s" ;; esac
    fi
  done <<DUE
$due
DUE
  rc=0
  export LANE_ROUNDS="${LANE_ROUNDS:-2}" LANE_ITEM_LOG_PREFIX="${LANE_ITEM_LOG_PREFIX:-refresh-}"
  if [ $# -ge 1 ]; then
    reopen "$NAME" "$@"
    "$REPO/tools/lanes/lane-run.sh" "$NAME" - "$@" || rc=1
  fi
  # A partitioned source's partitions: one lane per source (refresh-<source>), its own logs.
  for s in $(printf '%s\n' $partitioned | cut -d: -f1 | sort -u); do
    parts=$(printf '%s\n' $partitioned | awk -F: -v s="$s" '$1 == s { print $2 }')
    # shellcheck disable=SC2086  # partition names hold no spaces
    reopen "$NAME-$s" $parts
    # shellcheck disable=SC2086
    # Another lane on the source: wait half an hour at most, then leave it for next week.
    LANE_LOCK_WAIT=${LANE_REFRESH_LOCK_WAIT:-1800} "$REPO/tools/lanes/lane-run.sh" "$NAME-$s" "$s" $parts || rc=1
  done
  exit "$rc"
fi
# shellcheck disable=SC2086  # the list is split on purpose
[ $# -ge 1 ] || set -- ${WSP_REFRESH_SOURCES:-crs-reports nist-csrc usmc-mcpel itu-recommendations etsi-deliverables}
case $(systemctl --user show "whisipedia-job-$JOB" -p ActiveState --value 2>/dev/null) in
  active | activating | reloading | deactivating)
    echo "refresh.sh: whisipedia-job-$JOB is still running; this week's refresh is left to it"
    exit 0 ;;
esac
exec "${WSP_BG:-$REPO/tools/wsp-bg}" --fetch "$JOB" 1G -- "$REPO/tools/lanes/refresh.sh" --run "$@"
