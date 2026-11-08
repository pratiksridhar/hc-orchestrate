#!/bin/sh
# Stand-in for a real job. usage: fake_job.sh CASE VARIANT OUT
# Sleeps FAKE_JOB_MIN..FAKE_JOB_MAX seconds (default 1..5; a case containing SLOW sleeps FAKE_JOB_SLOW, default 8),
# writes a small JSON result to OUT, exits 1 if CASE contains FAIL. Appends "case variant pid" to $FAKE_JOB_LOG
# (if set) so tests can see how often each job ran. No network, no LLM, no mainframe.
case_=$1 variant=$2 out=$3
[ -n "$FAKE_JOB_LOG" ] && echo "$case_ $variant $$" >> "$FAKE_JOB_LOG"
min=${FAKE_JOB_MIN:-1} max=${FAKE_JOB_MAX:-5}
case "$case_" in
  *SLOW*) secs=${FAKE_JOB_SLOW:-8} ;;
  *) secs=$(awk -v lo="$min" -v hi="$max" -v s="$$$(date +%s)" 'BEGIN{srand(s); printf "%.2f", lo+rand()*(hi-lo)}') ;;
esac
sleep "$secs"
case "$case_" in
  *FAIL*) echo "fake_job: $case_ failed on purpose" >&2; exit 1 ;;
esac
printf '{"case":"%s","variant":"%s","ok":true,"slept":%s}\n' "$case_" "$variant" "$secs" > "$out"
