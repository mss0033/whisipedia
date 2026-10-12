#!/bin/sh
# Resumable lexical rebuild: build each source, then rank and spell, skipping the steps already done.
#
#   tools/wsp-bg --wait index-rebuild 6G -- tools/lanes/rebuild-resume.sh [--enable-timer] DONE_FILE SOURCE...
#
# DONE_FILE records finished steps (build:<source>, rank, spell), so a restart after a reboot carries
# on where it stopped. Run it under tools/wsp-bg WITH the heavy lock (no --no-heavy-lock): wsp-bg holds
# HEAVY for the whole rebuild and passes it down, so the orchestrator cannot take it between two steps
# for a multi-hour refine (audit 2026-10-12, M2). 6G fits the background slice only with the lanes
# stopped (deploy/systemd/whisipedia-background.slice).
#
# A step that finds a lock busy (exit 3) waits a minute and tries again, up to REBUILD_BUSY_TRIES
# (720: 12 hours). Any other failure (an OOM kill, a traceback) is a failed step: the remaining builds
# still run, but rank and spell run only when every build is in DONE_FILE, and the script exits 1
# without "all done" (they used to run without the failed source and were then never recomputed).
# --enable-timer enables whisipedia-index.timer once everything is done. SIGTERM is passed on
# to the running step.
set -u
timer=0
[ "${1:-}" = --enable-timer ] && { timer=1; shift; }
[ $# -ge 2 ] || { echo "usage: $0 [--enable-timer] DONE_FILE SOURCE..." >&2; exit 2; }
DONE=$1
shift
for s in "$@"; do [ "$s" = --enable-timer ] && timer=1; done  # also accepted among the sources
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WSP=${WSP:-$REPO/.venv/bin/wsp}
BUSY_TRIES=${REBUILD_BUSY_TRIES:-720}
BUSY_WAIT=${REBUILD_BUSY_WAIT:-60}
touch "$DONE"
cd "$REPO" || exit 1

child=""
stopping=0
# shellcheck disable=SC2329  # invoked by the trap below
on_signal() {
  stopping=1
  [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
}
trap on_signal TERM INT HUP

stopped() { echo "=== $(date -Is) stopped by a signal; run again to resume"; exit 143; }

run() {  # run CMD...: as a child the trap can reach
  "$@" &
  child=$!
  while :; do
    wait "$child"
    rc=$?
    if [ "$stopping" = 1 ] && kill -0 "$child" 2>/dev/null; then continue; fi
    break
  done
  child=""
  [ "$stopping" = 1 ] && stopped
  return "$rc"
}

pause() {
  sleep "$1" &
  child=$!
  wait "$child" 2>/dev/null
  child=""
  [ "$stopping" = 1 ] && stopped
  return 0
}

step() {  # step NAME CMD...: 0 when done (now or before), else the step's exit code
  name=$1
  shift
  grep -qxF "$name" "$DONE" && return 0
  try=1
  while :; do
    echo "=== $(date -Is) $name (try $try)"
    run "$@"
    rc=$?
    echo "=== exit $rc $name"
    [ "$rc" -eq 0 ] && { echo "$name" >> "$DONE"; return 0; }
    [ "$rc" -ne 3 ] && return "$rc"
    [ "$try" -ge "$BUSY_TRIES" ] && { echo "=== $name: the lock stayed busy for $try tries"; return 3; }
    try=$((try + 1))
    pause "$BUSY_WAIT"
  done
}

failed=""
for s in "$@"; do
  [ "$s" = --enable-timer ] && continue
  step "build:$s" "$WSP" index build --no-rank --no-spell --source "$s" || failed="$failed $s"
done
if [ -n "$failed" ]; then
  echo "=== $(date -Is) NOT done: build failed for:$failed. Rank and spell were not run; fix the cause and run again."
  exit 1
fi
step rank "$WSP" index rank || { echo "=== $(date -Is) NOT done: rank failed"; exit 1; }
step spell "$WSP" index spell || { echo "=== $(date -Is) NOT done: spell failed"; exit 1; }
if [ "$timer" = 1 ] && ! grep -qx timer "$DONE"; then
  systemctl --user enable --now whisipedia-index.timer && echo timer >> "$DONE"
fi
echo "=== $(date -Is) all done"
