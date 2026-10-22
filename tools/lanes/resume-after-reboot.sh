#!/bin/sh
# whisipedia-resume.service (tools/lanes/whisipedia-resume.service), on every start of the user
# manager: wait for the NAS, then (re)start the long background jobs. Each job skips what it has
# finished (its .done list); a job already running is left alone (tools/wsp-bg refuses a second unit
# with the same name).
#
# Jobs are listed in $WSP_STATE (default $XDG_STATE_HOME/whisipedia):
#   lanes.conf          one lane per line: UNIT LANE SOURCE|- ITEM...  ('#' starts a comment), e.g.
#                         collections-cia lane-cia cia-reading-room b0001 b0002 b0003
#                         collections-small lane-small - neets cia-csi crs-reports
#                       run as: tools/wsp-bg --fetch UNIT -- tools/lanes/lane-run.sh LANE SOURCE ITEM...
#   jobs.conf           one job NAME per line ('#' starts a comment), started again as tools/wsp-bg
#                       last started it (jobs/NAME.cmd): the jobs that are not lanes (the ERA5
#                       backfill, the maps lane, bulk lanes), or lanes whose command lanes.conf
#                       cannot say. A job without its command is reported, not guessed.
#   index/rebuild.conf  the sources of a lexical rebuild, whitespace separated (a first word
#                       --enable-timer enables the nightly index timer at the end); resumed with
#                       tools/lanes/rebuild-resume.sh unless index/rebuild.paused exists. Memory:
#                       $REBUILD_MEMORY_MAX (default 6G: stop the lanes, see the slice's budget).
#
# The NAS is polled for up to $WSP_NAS_WAIT seconds (default 900). If it never mounts, the script logs
# a critical message to the journal and fails the unit (`systemctl --user --failed` shows it); start
# it again once the share is there: systemctl --user restart whisipedia-resume (audit 2026-10-12, M6).
# An item listed in two lanes is reported here; lane-run.sh's per-source lock keeps the two lanes from
# fetching it at the same time.
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
S=${WSP_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/whisipedia}
NAS=${WSP_NAS_MOUNT:-/mnt/z}
NAS_WAIT=${WSP_NAS_WAIT:-900}
NAS_POLL=${WSP_NAS_POLL:-15}
SETTLE=${WSP_RESUME_SETTLE:-60}  # after the mount: let the API and the orchestrator start first
cd "$REPO" || exit 1

waited=0
until mountpoint -q "$NAS"; do
  if [ "$waited" -ge "$NAS_WAIT" ]; then
    echo "<2>CRITICAL: $NAS is not mounted after ${waited}s: no lane and no rebuild was resumed." \
      "Mount the share, then: systemctl --user restart whisipedia-resume" >&2
    exit 1
  fi
  sleep "$NAS_POLL"
  waited=$((waited + NAS_POLL))
done
echo "$NAS mounted (waited ${waited}s)"
sleep "$SETTLE"

status=0
if [ -f "$S/index/rebuild.conf" ] && [ ! -e "$S/index/rebuild.paused" ]; then
  # shellcheck disable=SC2046  # the words of rebuild.conf: [--enable-timer] SOURCE...
  tools/wsp-bg --wait index-rebuild "${REBUILD_MEMORY_MAX:-6G}" -- \
    tools/lanes/rebuild-resume.sh "$S/index/rebuild.done" $(sed 's/#.*//' "$S/index/rebuild.conf") ||
    { echo "<3>index-rebuild not started (already running, or refused)" >&2; status=1; }
fi

if [ -f "$S/lanes.conf" ]; then
  dup=$(sed 's/#.*//' "$S/lanes.conf" | awk 'NF >= 4 { for (i = 4; i <= NF; i++) { k = ($3 == "-" ? $i : $3 "/" $i); n[k]++ } }
    END { for (k in n) if (n[k] > 1) printf "%s ", k }')
  [ -z "$dup" ] || echo "<4>warning: listed in more than one lane (they take turns, per-source lock): $dup" >&2
  sed 's/#.*//' "$S/lanes.conf" | while read -r unit lane src items; do
    [ -n "$unit" ] || continue
    if [ -z "${lane:-}" ] || [ -z "${src:-}" ] || [ -z "${items:-}" ]; then
      echo "<3>lanes.conf: '$unit $lane $src': needs UNIT LANE SOURCE|- ITEM..." >&2
      continue
    fi
    # shellcheck disable=SC2086  # the items, one word each
    tools/wsp-bg --fetch "$unit" -- tools/lanes/lane-run.sh "$lane" "$src" $items </dev/null ||
      echo "<4>$unit not started (already running?)" >&2
  done
fi
# A job started again as tools/wsp-bg last started it (jobs/NAME.cmd); one still running, or without
# its command, is left alone.
start_saved() {
  unit="whisipedia-job-$1.service"
  if systemctl --user is-active -q "$unit" 2>/dev/null; then
    return 0
  fi
  if [ -f "$S/jobs/$1.cmd" ]; then
    sh "$S/jobs/$1.cmd" </dev/null && echo "restarted $unit" ||
      echo "<4>$unit not restarted (sh $S/jobs/$1.cmd failed)" >&2
  else
    echo "<4>$unit: how it was started is not known ($S/jobs/$1.cmd); start it by hand" >&2
  fi
}
if [ -f "$S/jobs.conf" ]; then
  sed 's/#.*//' "$S/jobs.conf" | while read -r name _; do
    [ -z "$name" ] || start_saved "$name"
  done
fi
# Jobs tools/wsp-yield stopped when the machine ran short of memory.
if [ -f "$S/jobs/yield-stopped" ]; then
  sort -u "$S/jobs/yield-stopped" | while read -r unit; do
    name=${unit#whisipedia-job-}
    name=${name%.service}
    [ "$name" != "$unit" ] || continue  # not a tools/wsp-bg job (the index waits for its timer)
    start_saved "$name"
  done
  rm -f "$S/jobs/yield-stopped"
fi
echo "resumed $(date -Is)"
exit "$status"
