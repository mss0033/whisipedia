#!/bin/sh
# Scheduled refresh of collection sources that keep publishing (whisipedia-refresh.timer, weekly).
#
#   tools/lanes/refresh.sh [SOURCE...]          start the refresh lane as a background job
#   tools/lanes/refresh.sh --run SOURCE...      (inside the job) the lane itself
#
# A lane marks an item done for good (tools/lanes/lane-run.sh, NAME.done), so a source that keeps
# publishing (CRS reports: several a week) took no new document once its lane was done (2026-11-09).
# This runs the lane "refresh" over SOURCE... (default $WSP_REFRESH_SOURCES, else crs-reports) as the
# background job whisipedia-job-collections-refresh (tools/wsp-bg --fetch: the download lanes' slice,
# capped at 1G). Each run first opens the items the last refresh finished: out of refresh.done, on a
# new cycle (today's date); an item a refresh left undone keeps its cycle and resumes.
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
  [ $# -ge 1 ] || { echo "refresh.sh --run: no sources" >&2; exit 2; }
  STATE=${WSP_LANE_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/whisipedia/collections}
  mkdir -p "$STATE"
  touch "$STATE/$NAME.done" "$STATE/$NAME.cycles" "$STATE/$NAME.fetched"
  today=$(date -u +%F)
  for src in "$@"; do
    if grep -qxF "$src" "$STATE/$NAME.done"; then
      grep -vxF "$src" "$STATE/$NAME.done" > "$STATE/$NAME.done.tmp" || true
      mv "$STATE/$NAME.done.tmp" "$STATE/$NAME.done"
      echo "$src $today" >> "$STATE/$NAME.cycles"  # a new cycle (the last line wins)
      echo "$src $today -" >> "$STATE/$NAME.fetched"  # not fetched yet (a second refresh in a day)
    fi
  done
  # Two passes an hour apart at most: next week's run takes up what is left.
  LANE_ROUNDS=${LANE_ROUNDS:-2} exec "$REPO/tools/lanes/lane-run.sh" "$NAME" - "$@"
fi
# shellcheck disable=SC2086  # the list is split on purpose
[ $# -ge 1 ] || set -- ${WSP_REFRESH_SOURCES:-crs-reports}
case $(systemctl --user show "whisipedia-job-$JOB" -p ActiveState --value 2>/dev/null) in
  active | activating | reloading | deactivating)
    echo "refresh.sh: whisipedia-job-$JOB is still running; this week's refresh is left to it"
    exit 0 ;;
esac
exec "${WSP_BG:-$REPO/tools/wsp-bg}" --fetch "$JOB" 1G -- "$REPO/tools/lanes/refresh.sh" --run "$@"
