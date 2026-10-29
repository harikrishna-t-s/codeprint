#!/usr/bin/env bash
# End-to-end demo. Uses its OWN private library under output/demo/, so your real data is untouched.
#   ./demo.sh [--force]    (--force: replace a previous demo without asking)
set -uo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$PROJECT_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
DEMO="$PROJECT_DIR/output/demo"

if [ -e "$DEMO" ]; then
    if [ "${1:-}" != "--force" ]; then
        [ -t 0 ] || { echo "$DEMO exists; re-run with --force to replace it." >&2; exit 1; }
        read -r -p "$DEMO exists. Delete and regenerate it? [y/N] " a
        case "$a" in y|Y) ;; *) echo "Aborted."; exit 1 ;; esac
    fi
    rm -rf "$DEMO"
fi
mkdir -p "$DEMO/src" "$DEMO/export"
export CODEPRINT_DATA_DIR="$DEMO/data" CODEPRINT_CONFIG_DIR="$DEMO/config"
cp_() { "$PY" "$PROJECT_DIR/codeprint.py" "$@"; }
step() { printf '\n\033[1m### %s\033[0m\n' "$*"; }

step "Setup (private demo library)"
cp_ setup || exit 1

step "Writing sample programs"
cat > "$DEMO/src/factorial.py" <<'PY'
def factorial(n):
    return 1 if n <= 1 else n * factorial(n - 1)

n = int(input())
print(f"{n}! = {factorial(n)}")
PY
echo 6 > "$DEMO/src/input.txt"
cat > "$DEMO/src/sum.c" <<'C'
#include <stdio.h>

int main(void) {
    int total = 0;
    for (int i = 1; i <= 10; i++) total += i;
    printf("Sum of 1..10 = %d\n", total);
    return 0;
}
C
cat > "$DEMO/src/crash.py" <<'PY'
data = {"a": 1}
print("Looking up key b...")
print(data["b"])
PY
cat > "$DEMO/src/count.sh" <<'SH'
#!/usr/bin/env bash
for i in 1 2 3; do echo "Count: $i"; done
SH
cat > "$DEMO/src/hello.js" <<'JS'
const langs = ["Python", "C", "Java"];
langs.forEach((l, i) => console.log(`${i + 1}. ${l}`));
JS

step "Generating screenshots (languages whose toolchain is missing are skipped)"
cp_ generate "$DEMO/src/factorial.py" --title "Experiment 01: Factorial" -i "$DEMO/src/input.txt"
cp_ generate "$DEMO/src/crash.py" --title "Experiment 02: Error handling"
command -v gcc >/dev/null || command -v cc >/dev/null \
    && cp_ generate "$DEMO/src/sum.c" --title "Experiment 03: Sum in C" \
    || echo "(skipping C: no compiler)"
command -v node >/dev/null && cp_ generate "$DEMO/src/hello.js" --title "Experiment 04: Node.js" \
    || echo "(skipping JavaScript: node not installed)"
cp_ generate "$DEMO/src/count.sh" --code-only --title "Experiment 05: Bash (code only)"
# a long program: screenshots are compact now, so we need a tall one to show a full page
{ echo "total = 0"; for i in $(seq 1 45); do echo "total += $i   # step $i"; done; echo "print(total)"; } > "$DEMO/src/long.py"
cp_ generate "$DEMO/src/long.py" --title "Experiment 06: Long program"

step "Library"
cp_ screenshot list

step "Preview a screenshot (metadata only; --no-open because demos may run headless)"
cp_ preview latest --no-open

step "Creating a page and adding screenshots"
cp_ page create "Lab Record - Page 1"
for id in 1 2 3 4 5; do cp_ page add "$id" --page 1 || echo "(screenshot $id rejected: page full)"; done

step "Demonstrating capacity rejection (CAPACITY_MAX) and --auto-page"
cp_ page add latest --page 1; rc=$?; echo "exit code: $rc (4 means CAPACITY_MAX)"
cp_ page add latest --page 1 --auto-page

step "Reorder, then preview page 1"
cp_ page reorder 2 --page 1 --position 1
cp_ page preview 1 --no-open

step "Export PDFs"
cp_ page export 1 --output "$DEMO/export/page1.pdf" --force
cp_ export --all-pages --format pdf --output "$DEMO/export/lab-record.pdf" --force

step "State survives restarts (each command is a fresh process)"
cp_ page list

echo
echo "Demo finished. Artifacts:"
echo "  PDF:        $DEMO/export/lab-record.pdf"
echo "  Page image: $DEMO/data/previews/page_1.png"
echo "  Library:    $DEMO/data"
