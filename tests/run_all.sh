#!/usr/bin/env bash
# Run every test module in this directory. Exits non-zero if any fails.
#
# Deliberately does not pipe test output through head/tail: a pipe reports the
# exit status of the last command, so a broken import would pass silently and a
# caller's `&&` chain would carry on.
set -uo pipefail
cd "$(dirname "$0")/.."          # repository root, so relative paths resolve

# Interpreter: $PYTHON if set, else the project venv, else python3.
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

failed=0
ran=0
for module in tests/test_*.py; do
  echo "=== $module"
  if ! "$PY" "$module"; then
    failed=$((failed + 1))
  fi
  ran=$((ran + 1))
done

if [ "$failed" -ne 0 ]; then
  echo
  echo "FAILED: $failed of $ran test module(s)"
  exit 1
fi
echo
echo "all $ran test modules passed"
