#!/bin/bash
# notify-guard.sh: the last check between a scheduled agent job and the operator's phone.
#
# A scheduled job runs an agent, captures what it printed and sends that text as the report. When
# the runner itself fails (it hits its turn limit, loses authentication, or gets an API error) it
# still prints something, and that something is its own error message. A check that only asks
# "is the output non-empty?" then delivers the error text as if it were the report.
#
# The incident behind this check: an hourly mail job delivered the literal line
# "Error: Reached max turns (5)" as that morning's summary. Its output check listed authentication
# failures and rate limits but not the runner's own failure text. The same class had been fixed
# three weeks earlier in a different pipeline, and that check was never copied to this job. A check
# that lives inside one script protects only that script.
#
# The rule: a runner's own error text is not a report. Output that fails this check is logged and
# routed to maintenance; it is never shown to the human as content. Empty output fails too.
#
# Two patterns (Error: and Execution error) are anchored to the start of a line, and every pattern
# looks only at the first three lines, so a real report that quotes "Error:" mid-sentence still
# passes. The unanchored patterns (for example "rate limit exceeded") also catch a report that
# mentions them near the top; the guard errs toward holding a report back.
# tests/test_notify_guard.sh pins the quoted-"Error:" case.
#
# The first lines are read with bash builtins rather than a `head` pipe, so a caller running with
# `set -o pipefail` cannot turn a SIGPIPE on long output into "looks fine, send it".
#
# Usage:  . guards/notify-guard.sh
#         if is_bad_output "$REPORT"; then log_it; route_to_maintenance; exit 0; fi
#         send "$REPORT"

is_bad_output() {
  [ -z "$1" ] && return 0
  local first="" line n=0
  while [ "$n" -lt 3 ] && IFS= read -r line; do
    first+="$line"$'\n'
    n=$((n + 1))
  done <<<"$1"
  grep -qiE "Reached max turns|^Error:|^Execution error|API Error: [0-9]|Failed to authenticate|OAuth session expired|Please run /login|Invalid authentication|Credit balance|rate limit exceeded" <<<"$first"
}
