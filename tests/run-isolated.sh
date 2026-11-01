#!/bin/bash
# Run unittest suites with a fresh temporary HOME and no inherited environment, then prove the
# caller's real state directory did not change.
# Usage: bash tests/run-isolated.sh [START_DIR]   (default: tests)
set -u
cd "$(dirname "$0")/.." || exit 2
START="${1:-tests}"
PY="$(command -v python3)" || { echo "run-isolated: python3 not found" >&2; exit 2; }
REAL_STATE="${HOME:?run-isolated: HOME is not set}/.local/state/agent-reliability"
before="$("$PY" -I state_guard.py fingerprint "$REAL_STATE")" || exit 2
TMPH="$(mktemp -d)" || exit 2
trap 'rm -rf "$TMPH"' EXIT
# env -i drops every inherited variable (tokens, CI secrets, a stray HOME); only these reach the tests.
env -i HOME="$TMPH" PATH="$(dirname "$PY"):/usr/bin:/bin" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -m unittest discover -s "$START" -v
rc=$?
after="$("$PY" -I state_guard.py fingerprint "$REAL_STATE")" || exit 2
if [ "$before" != "$after" ]; then
  echo "run-isolated: the real state directory changed during the test run: $REAL_STATE" >&2
  exit 3
fi
exit "$rc"
