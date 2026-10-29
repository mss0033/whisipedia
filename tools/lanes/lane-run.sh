#!/bin/sh
# Resumable collection lane: fetch and ingest a list of sources or partitions, one item at a time.
#
#   tools/lanes/lane-run.sh NAME SOURCE|- ITEM...
#   tools/wsp-bg --fetch collections-cia -- tools/lanes/lane-run.sh lane-cia cia-reading-room b0001 b0002
#
# SOURCE "-": each ITEM is a source id (fetch --source ITEM); otherwise ITEM is a --partition of SOURCE.
# State lives in $WSP_LANE_STATE (default $XDG_STATE_HOME/whisipedia/collections):
#   NAME.done     items finished for good; skipped by every later run
#   NAME.cycles   "ITEM CYCLE": the cycle label an item is on (the last line wins)
#   NAME.fetched  "ITEM CYCLE CODE": that cycle's fetch completed; a restart goes straight to the ingest
#   NAME.log      what the lane did; ITEM's own output goes to ITEM.log (NAME-ITEM.log for partitions)
#
# An item is done only when its fetch was complete (exit 0, 7 or 8) AND `wsp ingest --version CYCLE`
# ingested that cycle (exit 0; or 8, "nothing planned", after a fetch that found nothing new). Audit
# 2026-10-12 C1: the old lane marked an item done whenever `wsp ingest --source` exited 0, even after
# a killed, crashed or refused fetch, or one whose every download failed. Exit codes of the commands:
# docs/runbooks/document-collections.md, "Exit codes". Per fetch outcome:
#   0, 7, 8   complete: ingest this cycle; done when it is in
#   4         a listing failed, nothing fetched: retry the same cycle after LANE_LISTING_WAIT seconds
#   5         an earlier fetch of the source is not ingested yet: ingest it (`wsp ingest --source`), then
#             fetch again
# When the cycle asked for is already sealed, the fetch uses the next one ("CYCLE rN") and says so; the
# lane follows it (the ingest must name the version that was fetched).
#   6         partial: ingest what came; once sealed, the next fetch uses a new cycle (CYCLE r2, r3...:
#             a sealed generation takes no more files); retry after LANE_PARTIAL_WAIT seconds
#   anything else (1 crash, 2 refused, 137/143 killed): not done, nothing ingested; the next run retries
#
# Failures raise an alert lane:NAME:ITEM (`wsp alerts raise`; the morning digest and the System Monitor
# show it): critical when an ingest fails (a batch then waits for someone to look, as an interrupted
# adoption did on 2026-10-14), a warning when downloads keep failing, a listing stays incomplete or a
# fetch crashes. The item's alert is resolved once it is done. LANE_ALERT_CMD replaces `$WSP` for them.
#
# One lane per source at a time: each item holds <state>/locks/<source>.lock (flock) for its fetch and
# ingest, so two lanes listing the same source never fetch it at once (audit M6); the second one leaves
# the item for its next run. SIGTERM/SIGINT are passed on to the running command and the lane stops
# after it, leaving the item undone (audit M5). Exit: 0 when every item is done, 1 if any is left.
set -u
usage() { echo "usage: $0 NAME SOURCE|- ITEM..." >&2; exit 2; }
[ $# -ge 3 ] || usage
NAME=$1 SRC=$2
LANE_ARGS="$*"  # for another round (below); item names hold no spaces
shift 2
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WSP=${WSP:-$REPO/.venv/bin/wsp}
ALERT_CMD=${LANE_ALERT_CMD:-$WSP}
STATE=${WSP_LANE_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/whisipedia/collections}
LISTING_TRIES=${LANE_LISTING_TRIES:-8}     # fetch attempts while the listing is incomplete (exit 4)
LISTING_WAIT=${LANE_LISTING_WAIT:-900}
PARTIAL_TRIES=${LANE_PARTIAL_TRIES:-3}     # fetch attempts while downloads fail (exit 6)
PARTIAL_WAIT=${LANE_PARTIAL_WAIT:-900}
# Ingest attempts while another ingest runs (exit 3): each waits up to QUEUE_WAIT in the lock's queue
# (`wsp ingest --wait`), so it takes the lock when the holder lets go. Retrying once a minute lost
# to the maps lane, whose next ingest took the lock within a second of each release (2026-10-24).
# Four waits of 6 hours: a day. Four hours gave up behind a USGS topo ingest (lane-doe, 2026-10-21).
# A wait longer than an ingest slice (an hour) keeps the lane's place in the queue, which is first come,
# first served; a 30-minute wait gave up and came back last, and OpenAlex waited 8 hours (2026-10-25).
BUSY_TRIES=${LANE_BUSY_TRIES:-4}
QUEUE_WAIT=${LANE_QUEUE_WAIT:-21600}
BUSY_WAIT=${LANE_BUSY_WAIT:-5}
# An ingest gives the lock up after this long (exit 3, "yielded") and queues again at once, so one
# large version does not keep every other lane's ingest waiting for hours.
INGEST_SLICE=${LANE_INGEST_SLICE:-3600}
ROUNDS=${LANE_ROUNDS:-3}  # passes over the items left for later, an hour apart
ROUND_WAIT=${LANE_ROUND_WAIT:-3600}
MAX_SLICES=${LANE_MAX_SLICES:-48}  # one-hour ingest slices of one version in a row, at most
mkdir -p "$STATE/locks"
DONE=$STATE/$NAME.done CYCLES=$STATE/$NAME.cycles FETCHED=$STATE/$NAME.fetched LOG=$STATE/$NAME.log
touch "$DONE" "$CYCLES" "$FETCHED"
cd "$REPO" || exit 1

say() { echo "$(date -Is) $*" >> "$LOG"; }

# alert SEVERITY MESSAGE: open lane:NAME:ITEM (never stops the lane: a failure to alert is only logged).
alert() {
  "$ALERT_CMD" alerts raise --key "lane:$NAME:$item" --severity "$1" --message "lane $NAME, $item: $2" >> "$LOG" 2>&1 || true
}
clear_alert() { "$ALERT_CMD" alerts resolve --key "lane:$NAME:$item" >> "$LOG" 2>&1 || true; }
# The last error line of the item's own log since this step began (for the alert's message).
last_error() { tail -c +"$((${1:-0} + 1))" "$out" 2>/dev/null | grep -E 'Error|error:' | tail -n 1 | cut -c1-300; }

# --- signals: pass SIGTERM/SIGINT on to the running command, then stop ---------------------------------
child=""
stopping=0
# shellcheck disable=SC2329  # invoked by the trap below
on_signal() {
  stopping=1
  [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
}
trap on_signal TERM INT HUP

# run OUT CMD...: run CMD (output appended to OUT) as a child the trap can reach; returns its exit code.
run() {
  out=$1
  shift
  "$@" >> "$out" 2>&1 &
  child=$!
  while :; do
    wait "$child"
    rc=$?
    # A trapped signal interrupts `wait` (rc > 128) while the child may still be stopping: wait again.
    if [ "$stopping" = 1 ] && kill -0 "$child" 2>/dev/null; then continue; fi
    break
  done
  child=""
  if [ "$stopping" = 1 ]; then
    say "stopped by a signal (exit $rc); $item is left for the next run"
    exit 143
  fi
  return "$rc"
}

pause() {  # pause SECONDS: an interruptible sleep
  sleep "$1" &
  child=$!
  wait "$child" 2>/dev/null
  child=""
  [ "$stopping" = 1 ] && { say "stopped by a signal; $item is left for the next run"; exit 143; }
  return 0
}

# --- state files ----------------------------------------------------------------------------------------
cycle_of() { awk -v i="$1" '$1 == i { c = $2 } END { print c }' "$CYCLES"; }
set_cycle() { echo "$1 $2" >> "$CYCLES"; }
fetched_code() { awk -v i="$1" -v c="$2" '$1 == i && $2 == c { r = $3 } END { print r }' "$FETCHED"; }
set_fetched() { echo "$1 $2 $3" >> "$FETCHED"; }
forget_fetched() { echo "$1 $2 -" >> "$FETCHED"; }  # "-": fetch again (the last line wins)

# next_cycle CYCLE: the next label after a sealed partial cycle. 2026-10-12 -> 2026-10-12r2 -> ...r3;
# a hand-made 2026-10-12-r4 -> 2026-10-12r5. Natural order (wsp_core.versions) puts each after the one
# before, also with a partition suffix (2026-10-12r2.ak after 2026-10-12.ak; "-r2" would sort before).
next_cycle() {
  base=$(printf '%s\n' "$1" | sed -E 's/-?r([0-9]+)$//')
  n=$(printf '%s\n' "$1" | sed -nE 's/.*-?r([0-9]+)$/\1/p')
  echo "${base}r$(( ${n:-1} + 1 ))"
}

# ingest_cycle: `wsp ingest --source $src --version $cyc` (partition too), waiting while another ingest
# runs (exit 3). Sets irc.
ingest_cycle() {
  tries=0 slices=0
  ingest_from=$(wc -c < "$out" 2>/dev/null || echo 0)
  while :; do
    slice_from=$(wc -c < "$out" 2>/dev/null || echo 0)
    # shellcheck disable=SC2086  # $part is "--partition X" or empty, split on purpose
    run "$out" "$WSP" ingest --max-seconds "$INGEST_SLICE" --wait "$QUEUE_WAIT" --source "$src" $part --version "$cyc"
    irc=$?
    [ "$irc" -ne 3 ] && break
    # Exit 3 is also "yielded after --max-seconds": progress, not a busy lock. It goes on at once (to
    # the back of the queue) and is not a try; a 5-hour version used to be dropped halfway after four
    # slices (2026-10-26 review #31). MAX_SLICES only stops a loop that never ends.
    if tail -c +"$((slice_from + 1))" "$out" | grep -q 'ingest yielded (--max-seconds)'; then
      slices=$((slices + 1))
      [ "$slices" -lt "$MAX_SLICES" ] && { say "ingest $src $part --version $cyc: slice $slices done; going on"; continue; }
      break
    fi
    tries=$((tries + 1))
    [ "$tries" -ge "$BUSY_TRIES" ] && break
    pause "$BUSY_WAIT"
  done
  say "ingest $src $part --version $cyc: exit $irc"
  case $irc in
    0 | 8) ;;
    3)
      if [ "$slices" -ge "$MAX_SLICES" ]; then
        alert warning "ingest of cycle $cyc not finished after $slices one-hour slices; it goes on next run"
      else
        alert warning "ingest of cycle $cyc still waiting for another ingest after $tries tries"
      fi
      ;;
    *) alert critical "ingest of cycle $cyc failed (exit $irc): $(last_error "$ingest_from"). See $out; it is fetched again on the next run." ;;
  esac
}

# --- the items ------------------------------------------------------------------------------------------
left=0
item=""
for item in "$@"; do
  grep -qxF "$item" "$DONE" && continue
  if [ "$SRC" = - ]; then src=$item part="" out=$STATE/$item.log
  else src=$SRC part="--partition $item" out=$STATE/$NAME-$item.log; fi
  cyc=$(cycle_of "$item")
  [ -n "$cyc" ] || { cyc=$(date -u +%F); set_cycle "$item" "$cyc"; }

  # One lane per source: fd 8 holds <locks>/<source>.lock for this item (the children inherit it).
  exec 8>> "$STATE/locks/$src.lock"
  if ! flock --nonblock 8; then
    say "$item: another lane is working on $src; left for the next run"
    exec 8>&-
    left=1
    continue
  fi

  listing=0 partial=0 pending=0 done_item=0
  while :; do
    frc=$(fetched_code "$item" "$cyc")
    case $frc in
      0 | 7 | 8) say "=== $item: cycle $cyc already fetched (exit $frc); ingesting" ;;
      *)
        say "=== $item: fetch, cycle $cyc"
        start=$(wc -c < "$out" 2>/dev/null || echo 0)
        # shellcheck disable=SC2086
        run "$out" "$WSP" collections fetch --source "$src" $part --version "$cyc"
        frc=$?
        say "fetch exit $frc"
        # "<v> is already sealed; fetching as <v'>": follow the cycle the fetch used (partition suffix off).
        moved=$(tail -c +"$((start + 1))" "$out" | sed -n 's/.* is already sealed; fetching as \([^ ]*\)$/\1/p' | tail -n 1)
        if [ -n "$moved" ]; then
          [ -n "$part" ] && moved=${moved%".$item"}
          say "$item: cycle $cyc is sealed; the fetch used $moved"
          cyc=$moved
          set_cycle "$item" "$cyc"
        fi
        ;;
    esac
    case $frc in
      0 | 7 | 8)
        set_fetched "$item" "$cyc" "$frc"
        ingest_cycle
        if [ "$irc" -eq 0 ] || { [ "$irc" -eq 8 ] && [ "$frc" -ne 0 ]; }; then
          done_item=1
        elif [ "$irc" -ne 3 ]; then
          # Failed (1), files missing (4), or a plan that vanished (8 after exit 0): fetch this cycle
          # again next time (a re-fetch of an unsealed cycle replaces what it staged, audit H2).
          forget_fetched "$item" "$cyc"
        fi
        break
        ;;
      5)
        pending=$((pending + 1))
        [ "$pending" -ge 3 ] && { say "earlier fetch still not ingested; $item left for a later run"; alert warning "an earlier fetch is still not ingested after 3 tries"; break; }
        # "<src>: fetched but not ingested yet: V1, V2; run ..." names the versions: ingest each by name
        # (a partition's ingest needs --version), then fetch again.
        waiting=$(tail -c +"$((start + 1))" "$out" | sed -n 's/.*: fetched but not ingested yet: \(.*\); run .*/\1/p' | tail -n 1 | tr -d ',')
        for v in $waiting; do
          [ -n "$part" ] && v=${v%".$item"}
          # shellcheck disable=SC2086
          run "$out" "$WSP" ingest --max-seconds "$INGEST_SLICE" --wait "$QUEUE_WAIT" --source "$src" $part --version "$v"
          say "ingest of the pending $src $part --version $v: exit $?"
        done
        ;;
      4)
        listing=$((listing + 1))
        [ "$listing" -ge "$LISTING_TRIES" ] && { say "listing still incomplete; $item left for a later run"; alert warning "the source's listing stayed incomplete after $LISTING_TRIES tries"; break; }
        pause "$LISTING_WAIT"
        ;;
      6)
        ingest_cycle  # what did arrive is planned: keep it
        if [ "$irc" -eq 0 ]; then
          # Sealed: that generation takes no more files. The rest comes in a new cycle, which carries
          # over everything this one holds (the fetch skips documents held byte-identically).
          new=$(next_cycle "$cyc")
          say "$item: cycle $cyc sealed with some downloads failed; next cycle $new"
          cyc=$new
          set_cycle "$item" "$cyc"
        fi
        partial=$((partial + 1))
        [ "$partial" -ge "$PARTIAL_TRIES" ] && { say "downloads still failing; $item left for a later run"; alert warning "downloads still failing after $PARTIAL_TRIES tries (cycle $cyc)"; break; }
        pause "$PARTIAL_WAIT"
        ;;
      *)
        say "fetch did not complete (exit $frc); nothing ingested, $item left for a later run"
        alert warning "fetch did not complete (exit $frc): $(last_error "$start"). See $out."
        break
        ;;
    esac
  done
  exec 8>&-
  if [ "$done_item" = 1 ]; then
    echo "$item" >> "$DONE"
    say "$item: done (cycle $cyc)"
    clear_alert
  else
    left=1
  fi
done
say "=== lane $NAME: $( [ "$left" = 0 ] && echo "every item done" || echo "items left for a later run" )"
# Items left for a later run are tried again after ROUND_WAIT, up to ROUNDS times in all: a lane that
# simply ended left a failed partition untouched until someone restarted it (2026-10-28, NARA rg-23).
round=${LANE_ROUND:-1}
if [ "$left" = 1 ] && [ "$round" -lt "$ROUNDS" ]; then
  say "=== lane $NAME: round $((round + 1)) of $ROUNDS in $ROUND_WAIT s"
  pause "$ROUND_WAIT"
  LANE_ROUND=$((round + 1)) exec "$0" $LANE_ARGS  # shellcheck disable=SC2086
fi
exit "$left"
