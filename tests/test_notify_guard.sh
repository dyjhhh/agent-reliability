#!/bin/bash
# test_notify_guard.sh: regression cases for guards/notify-guard.sh.
#
# The guard file is sourced, so the function under test is the one in guards/ and never a copy
# kept here. The last case matters most: a real report that quotes "Error:" in its prose must
# still pass, which is why the two error-line patterns (Error: and Execution error) are anchored
# and every pattern reads only the first lines.
cd "$(dirname "$0")/.." || exit 1
SRC=guards/notify-guard.sh
[ -f "$SRC" ] || { echo "guard not found: $SRC"; exit 1; }
. "$SRC"
declare -F is_bad_output >/dev/null || { echo "$SRC does not define is_bad_output"; exit 1; }

pass=0; fail=0
check() {  # check <expect: bad|good> <label> <text>
  if is_bad_output "$3"; then got=bad; else got=good; fi
  if [ "$got" = "$1" ]; then
    echo "  ok   $2"; pass=$((pass + 1))
  else
    echo "  FAIL $2 (expected $1, got $got)"; fail=$((fail + 1))
  fi
}

echo "is_bad_output ($SRC)"
check bad  "the max-turns incident string"             "Error: Reached max turns (5)"
check bad  "bare max-turns line"                       "Reached max turns (12)"
check bad  "generic runner error"                      "Error: something exploded"
check bad  "execution error"                           "Execution error: tool failed"
check bad  "API 5xx"                                   "API Error: 500 upstream"
check bad  "authentication failure"                    "Failed to authenticate"
check bad  "rate limit"                                "rate limit exceeded, retry later"
check bad  "empty output is a failure, not silence"    ""
check good "a real report"                             "Three new support tickets arrived; two are duplicates, so no action is needed today."
check good "a real report that quotes Error: in prose" "One ticket quotes the message Error: invalid date format; the fix is already merged."
echo "  -> ${pass} passed, ${fail} failed"
[ "$fail" -eq 0 ]
