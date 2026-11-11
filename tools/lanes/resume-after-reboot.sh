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
# A nightly index build that was cut short (a shutdown, a kill) is started again here, at most once
# in 20 hours: see "the nightly build" below. $WSP_INDEX_MARK names the build's mark (for tests).
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
rm -f "$S/halted"  # tools/wsp-shutdown's, before the reboot: the library runs again

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
  # The done list belongs to this rebuild.conf: a new one starts over (and so reruns rank and spell);
  # a fixed path skipped the builds a new conf repeated (2026-10-26 review).
  conf_id=$(sed 's/#.*//' "$S/index/rebuild.conf" | tr -s ' \t\n' ' ' | cksum | cut -d' ' -f1)
  tools/wsp-bg --wait index-rebuild "${REBUILD_MEMORY_MAX:-6G}" -- \
    tools/lanes/rebuild-resume.sh "$S/index/rebuild.done.$conf_id" $(sed 's/#.*//' "$S/index/rebuild.conf") ||
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
    if sh "$S/jobs/$1.cmd" </dev/null; then
      echo "restarted $unit"
    else
      echo "<4>$unit not restarted (sh $S/jobs/$1.cmd failed)" >&2
      return 1
    fi
  else
    echo "<4>$unit: how it was started is not known ($S/jobs/$1.cmd); start it by hand" >&2
    return 1
  fi
}
if [ -f "$S/jobs.conf" ]; then
  sed 's/#.*//' "$S/jobs.conf" | while read -r name _; do
    [ -z "$name" ] || start_saved "$name"
  done
fi
# The nightly build: cut short (a shutdown, a kill), it runs again now, not next night. 2026-11-10:
# killed at 04:59 by a shutdown, nothing built until 04:43 the next day (the timer's Persistent=true
# only replays a run that never started). The build keeps a mark (wsp_cli.ops.FULL_BUILD_MARK) with
# "started", and "ended" on every way out it sees itself; killed, it leaves no "ended".
#   - only a mark younger than 14 hours: later, the 04:30 timer is nearer, and a rerun would run
#     through it;
#   - only while the nightly timer is enabled (a lexical rebuild disables it) and no build runs;
#   - at most once in 20 hours (index/rerun.stamp): a build that dies every time it runs, or that
#     the owner stopped by hand, also leaves an open mark, and must not be started at every resume;
#   - one that tools/wsp-yield stopped for memory waits for its timer, as before: the stamp is
#     touched for it too, so it still waits after the list below is gone.
# A build that failed by itself has "ended" and is left to the next night (tools/wsp-stall raises
# index-missed when none finished cleanly for 36 hours).
MARK=${WSP_INDEX_MARK:-${XDG_DATA_HOME:-$HOME/.local/share}/whisipedia/state/index-full-build.json}
STAMP="$S/index/rerun.stamp"
if [ -f "$MARK" ] && ! grep -q '"ended"' "$MARK" &&
   [ -n "$(find "$MARK" -mmin -840 2>/dev/null)" ] &&
   systemctl --user is-enabled -q whisipedia-index.timer 2>/dev/null; then
  case $(systemctl --user is-active whisipedia-index.service 2>/dev/null) in
    active | activating | deactivating | reloading)
      echo "the nightly index build is running: left alone" ;;
    *)
      if grep -qx 'whisipedia-index.service' "$S/jobs/yield-stopped" 2>/dev/null; then
        mkdir -p "$S/index" && touch "$STAMP"
        echo "the nightly index build was stopped for memory: it waits for its timer"
      elif [ -n "$(find "$STAMP" -mmin -1200 2>/dev/null)" ]; then
        echo "the nightly index build was interrupted again within 20 hours of a rerun" \
          "(or was stopped for memory): it waits for its timer"
      elif mkdir -p "$S/index" && touch "$STAMP"; then
        echo "the nightly index build was interrupted: starting it again"
        systemctl --user start --no-block whisipedia-index.service ||
          echo "<4>whisipedia-index.service not started" >&2
      else
        echo "<4>$STAMP cannot be written: the interrupted index build is left to its timer" >&2
      fi ;;
  esac
fi
# Jobs tools/wsp-yield stopped when the machine ran short of memory.
# Those that could not be started stay listed for the next resume (removed with the rest, the list
# lost them: 2026-10-26 review).
if [ -f "$S/jobs/yield-stopped" ]; then
  mv "$S/jobs/yield-stopped" "$S/jobs/yield-stopped.resuming"
  sort -u "$S/jobs/yield-stopped.resuming" | while read -r unit; do
    case $unit in
      whisipedia-job-*.service) ;;
      *) continue ;;  # not a tools/wsp-bg job (the index waits for its timer: see the nightly build above)
    esac
    name=${unit#whisipedia-job-}
    name=${name%.service}
    start_saved "$name" || echo "$unit" >>"$S/jobs/yield-stopped"
  done
  rm -f "$S/jobs/yield-stopped.resuming"
fi
echo "resumed $(date -Is)"
exit "$status"
