#!/usr/bin/env bash
# Run the automated tests (temporary databases only; your library is never touched).
# Extra arguments are passed to pytest, e.g. ./run-tests.sh -k pdf
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$PROJECT_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found" >&2; exit 1; }
"$PY" -c 'import pytest' 2>/dev/null || { echo "pytest missing: run ./install.sh" >&2; exit 1; }
cd "$PROJECT_DIR"
"$PY" -m pytest tests -q -rs "$@"
status=$?
echo
if [ $status -eq 0 ]; then echo "RESULT: all tests passed (skips are listed above with reasons)"; else echo "RESULT: tests FAILED (pytest exit code $status)"; fi
exit $status
