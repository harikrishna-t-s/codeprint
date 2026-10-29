# CodePrint

A Linux CLI that turns source code **and its program output** into clean white-background screenshots
(no terminal theme changes needed), packs them onto A4 pages with little wasted paper, and exports
print-ready PDFs for college lab records.

## Features
- Runs your program (subprocess, timeout, stdin from a file) and captures stdout, stderr, exit status, duration, compiler errors.
- One compact PNG with only the source code and its output - no titles or labels (or code-only / output-only), syntax highlighting, optional line numbers, Unicode, wrapping of long lines, ~288 dpi.
- SQLite library of screenshots and pages; stable IDs; rename/delete/export/regenerate; auto-repair of missing or corrupt images.
- A4 pages (portrait/landscape), configurable margins and spacing, `grid` and `compact` layouts, utilization %, `CAPACITY_MAX` rejection, reorder/remove, `--auto-page`.
- Page preview (PNG with margin guides) and PDF/PNG export use the **same layout engine**.
- `--json` output for automation; no daemon, no network at runtime.

## Supported languages
Python, C, C++, Java, JavaScript (Node.js), Bash. Detected from the extension (or a shebang); override with `--language`.
`codeprint doctor` reports which interpreters/compilers are installed. Compilers are **not** installed for you.

## Prerequisites
Ubuntu 24.04 (or similar), Python 3.9+, `python3-venv`, a monospace font (`fonts-dejavu-core`), internet during install only.

## Installation and first run
```bash
./install.sh [--no-browser] [--bin-dir DIR]   # user-local, no sudo
./setup.sh                                     # safe to rerun
```
`install.sh` creates `.venv/` in the project, installs `requirements.txt`, tries to download Chromium for Playwright
(optional; skipped with `--no-browser`, and the built-in Pillow renderer is used if unavailable) and writes a launcher to `~/.local/bin/codeprint`.
Data: `~/.local/share/codeprint/` (database, images, previews). Config: `~/.config/codeprint/config.json`.

## Basic usage
```bash
codeprint generate main.c --title "Experiment 01"        # title = library/page name only, never drawn on the image
codeprint generate quiz.py --input-file input.txt --timeout 10
codeprint generate Main.java --output-dir ./output          # also copies the PNG (never overwrites)
codeprint generate algo.cpp --code-only --no-line-numbers
codeprint preview latest                                    # metadata + open in viewer (--browser, --no-open)
```
Programs that crash or fail to compile still produce a screenshot showing the error and exit status.
Any command accepts `--help`; most accept `--json`.

## Screenshot management
```bash
codeprint screenshot list | show 1 [--full] | rename 1 "New title" | delete 1 [--yes]
codeprint screenshot export 1 --format png --output exp1.png
codeprint screenshot regenerate 1 --columns 70      # re-render from saved data (program is not re-run)
```
Delete only ever removes files inside the managed images folder.

## Page management
```bash
codeprint page create "Experiment 01" [--orientation landscape --margin 10 --spacing 3 --layout grid --grid-columns 2]
codeprint page add 1 2 3 --page 1 [--position 2] [--auto-page]
codeprint page remove 3 --page 1
codeprint page reorder 2 --page 1 --position 1
codeprint page set 1 --margin 8                   # refused if screenshots would no longer fit
codeprint page show 1 | list | rename 1 "..." | delete 1 | preview 1 [--open|--no-open|--browser]
```
Adding something that does not fit fails with `CAPACITY_MAX` (exit code 4, nothing saved) and suggests another page.
Screenshots are never cropped; they are only scaled down (never up) to the printable width/height, and never below
`min_font_pt` (default 7 pt, error `UNREADABLE_SCALE`). **Utilization** = placed screenshot area / printable area x 100.

Layouts: `grid` = equal columns, row by row. `compact` = first-fit shelf packing (best fit by leftover width). It is a heuristic,
**not guaranteed optimal**; a later screenshot may fill a gap beside an earlier one. Rotation (`--rotate`) is only used when it
makes an otherwise unreadable/oversized item fit.

## PDF export
```bash
codeprint page export 1 --format pdf --output experiment01.pdf     # or --format png (300 dpi)
codeprint export --pages 1,2,3 --format pdf
codeprint export --all-pages --format pdf --output lab-record.pdf
```
A4 size is preserved (210 x 297 mm; landscape pages are 297 x 210). Images are embedded losslessly (Flate), not re-compressed as JPEG.
Existing files are never overwritten silently (prompt, or `--force`; non-interactive runs fail with exit 6). Files are written atomically.

## Configuration
`codeprint config show | set KEY VALUE | reset | path`. Precedence: **command-line flag > config file > built-in default**.
A page keeps its own layout (copied from config at creation; change with `page set`); a screenshot keeps the render settings it was made with.
Keys include `font_family`, `font_size`, `scale`, `line_numbers`, `highlight`, `highlight_style` (`print`/`bw`), `columns`, `wrap`, `width_mode`
(`fixed`/`fit`), `padding`, `timeout`, `output_dir`, `engine`, `page_margin_mm`, `page_spacing_mm`, `layout_mode`, `min_font_pt`.

Rendering engines: `pillow` (always available) and `playwright` (HTML/CSS in headless Chromium). `engine=auto` uses Playwright only if
its browser is installed and falls back to Pillow if it fails. Decision: Pillow is the guaranteed baseline so the tool works without a ~150 MB browser download.

## Security
**Running untrusted code is dangerous.** Programs run in a plain subprocess with your permissions; this is *not* a sandbox. Hygiene only:
no shell, fixed commands, throw-away working directory, timeout + process-group kill, CPU/file-size limits, stdin = /dev/null, output size cap
(a visible "truncated" note is added). As root, programs are run as `nobody`. Use `--cwd-source` only for trusted code needing neighbouring files.

## Troubleshooting
- `codeprint: command not found` -> add `~/.local/bin` to PATH. `codeprint doctor` shows missing pieces.
- `TOOL_MISSING` -> install the compiler (`sudo apt install build-essential default-jdk nodejs`) or use `--code-only`.
- Boxes in the image -> the font lacks those characters (warning is printed); the Playwright engine uses browser font fallback.
- Playwright fails -> `.venv/bin/python -m playwright install chromium` (system libs: `... install-deps chromium`, needs sudo); or `config set engine pillow`.
- Java: the public class must match the file name (`Main.java` -> `Main`).
- Preview does nothing -> no desktop session (`DISPLAY`/`WAYLAND_DISPLAY`); open the printed file path yourself.

## Known limitations
- Typed stdin is not echoed in program output and input text is not drawn. stdout and stderr share one output block (stdout first), not interleaved.
- Wrapping is a hard column break (no hanging indent). Pillow engine has no per-character font fallback (CJK/Indic need a suitable `font_family` or the Playwright engine).
- Compact packing is a heuristic. No cache of compilations. Pages hold each screenshot at most once.
- The Playwright engine could not be exercised in the development sandbox (no browser download); its HTML builder is unit-tested, the browser step is not.

## Tests and demo
`./run-tests.sh` (temporary databases only; language tests skip when a toolchain is missing) and `./demo.sh [--force]` (private library in `output/demo/`).

## Uninstall
`./uninstall.sh` removes the launcher and `.venv/` but keeps your data. `./uninstall.sh --purge-data` also deletes `~/.local/share/codeprint` and `~/.config/codeprint` (asks first; `--yes` skips).
