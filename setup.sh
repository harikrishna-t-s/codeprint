#!/usr/bin/env bash
# First-run setup: creates folders + database + default config and self-tests rendering and PDF export.
# Safe to run any number of times (an existing config file is never overwritten).
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$PROJECT_DIR/.venv/bin/python"
[ -x "$PY" ] || { echo "ERROR: virtual environment missing. Run ./install.sh first." >&2; exit 1; }
"$PY" "$PROJECT_DIR/codeprint.py" setup
echo
if doctor_out="$("$PY" "$PROJECT_DIR/codeprint.py" doctor 2>&1)"; then
    echo "Environment check: all required items OK (details: codeprint doctor)"
else
    echo "$doctor_out"; echo "doctor reported missing REQUIRED items (see above)." >&2; exit 1
fi
