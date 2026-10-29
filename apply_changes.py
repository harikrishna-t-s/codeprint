#!/usr/bin/env python3
"""Apply the CodePrint update to an existing repo.

Usage (from the repo root, or pass --repo):
    python3 apply_changes.py            # apply
    python3 apply_changes.py --dry-run  # check that every edit matches, write nothing

What changes
  * Screenshots contain ONLY source code + output: no title, no SOURCE CODE / PROGRAM OUTPUT /
    INPUT / error labels. Tighter padding, one thin rule between code and output.
  * --title is now library/page metadata only. --show-input and the show_input setting are removed.
  * Defaults: width_mode=fit, columns=76, padding=8, section_spacing=6, min_columns=20.
  * Pillow and Playwright engines now produce the same image width (no outer border in layout).
  * Fixes: false font warning in install.sh, exec bits, title clipping, '-' for empty metadata,
    setup writes an empty config (so new defaults apply), setup.sh no longer prints doctor twice.
  * Your config.json is migrated (removed keys / old defaults dropped). Backups: .backup-before-update/

All edits are checked in memory first; if any required edit does not match, NOTHING is written.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--repo", default=str(Path(__file__).resolve().parent))
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()
REPO = Path(args.repo).resolve()
if not (REPO / "codeprint.py").exists() or not (REPO / "cp_render.py").exists():
    sys.exit(f"{REPO} does not look like the codeprint repo (codeprint.py / cp_render.py missing)")

files: dict = {}
touched: set = set()
problems: list = []
warnings: list = []


def load(rel):
    if rel not in files:
        p = REPO / rel
        if not p.exists():
            return None
        files[rel] = p.read_text(encoding="utf-8")
    return files[rel]


def edit(rel, old, new, count=1, required=True):
    text = load(rel)
    if text is None:
        (problems if required else warnings).append(f"{rel}: file not found")
        return
    n = text.count(old)
    if n != count:
        msg = f"{rel}: expected {count} match(es), found {n} for: {old.strip()[:70]!r}"
        (problems if required else warnings).append(msg)
        return
    files[rel] = text.replace(old, new)
    touched.add(rel)


def between(rel, start, end, new):
    """Replace text from `start` (inclusive) up to `end` (exclusive)."""
    text = load(rel)
    i, j = text.find(start), text.find(end)
    if i < 0 or j < 0 or j < i or text.count(start) != 1:
        problems.append(f"{rel}: markers not found: {start[:40]!r} .. {end[:40]!r}")
        return
    files[rel] = text[:i] + new + text[j:]
    touched.add(rel)


if "REMOVED_KEYS" in (load("cp_config.py") or ""):
    sys.exit("Update already applied (REMOVED_KEYS found in cp_config.py). Nothing to do.")

# =============================================================== cp_render.py
R = "cp_render.py"
edit(R, "    truncated, input_text, show_input, max_output_kb.", "    truncated, input_text, max_output_kb.")
edit(R, '''    if rec.get("show_input") and rec.get("input_text"):
        sections.append(Section("INPUT", rec["input_text"], "text"))
''', "")
edit(R, '''class RenderSpec:
    title: str | None
    sections: list''', '''class RenderSpec:
    sections: list''')

between(R, "def _render_pillow(spec: RenderSpec) -> tuple:",
        "# ----------------------------------------------------------- playwright engine ---", r'''def _render_pillow(spec: RenderSpec) -> tuple:
    """Draw code block + output block. No titles or labels: only the code and its output."""
    s = spec.settings
    S = float(s["scale"])
    paths, family_used, warn = find_font_files(s["font_family"])
    warnings = [warn] if warn else []
    fs = s["font_size"] * S

    def load(style, size):
        return ImageFont.truetype(paths[style], max(1, round(size)))

    fonts = {(False, False): load("regular", fs), (True, False): load("bold", fs),
             (False, True): load("italic", fs), (True, True): load("bolditalic", fs)}
    f_reg = fonts[(False, False)]
    cw = f_reg.getlength("M")
    notdef = f_reg.getmask("\U0010FFFF")
    notdef_sig = (notdef.size, bytes(notdef))
    lh = math.ceil(fs * 1.4)
    pad = s["padding"] * S              # left/right padding
    vp = s["section_spacing"] * S       # padding above and below each block
    rule_w = max(1, round(S))
    hair_w = max(1, round(S * 0.5))

    prepared, total, gutter, digits = _prepare(spec)
    width = int(round(2 * pad + total * cw))

    # ---- rows: the code block, then ONE output block (stdout, stderr, compiler errors, notes)
    blocks = []
    for sec, lines, foot in prepared:
        if sec.kind == "code":
            rows = []
            for n, cells in enumerate(_code_cells(lines, sec.lexer, s), 1):
                for k, r in enumerate(_wrap_cells(cells, total - gutter, s["wrap"])):
                    rows.append((n if k == 0 else None, r))
            blocks.append({"code": True, "rows": rows, "foot": []})
            continue
        color = "#555555" if sec.kind == "muted" else "#111111"
        ital = sec.kind == "muted"
        rows, frows = [], []
        for line in lines:
            for part in ([line] if not s["wrap"] else _chunk_plain(line, total)):
                rows.append((None, [(c, (color, False, ital)) for c in part]))
        for line in foot:
            for part in ([line] if not s["wrap"] else _chunk_plain(line, total)):
                frows.append((None, [(c, ("#555555", False, True)) for c in part]))
        if blocks and not blocks[-1]["code"]:
            blocks[-1]["rows"].extend(rows)
            blocks[-1]["foot"].extend(frows)
        else:
            blocks.append({"code": False, "rows": rows, "foot": frows})

    # ---- vertical positions
    y = 0.0
    for i, b in enumerate(blocks):
        b["rule_y"] = None
        if i:
            b["rule_y"] = y
            y += rule_w
        y += vp
        b["top"] = y
        y += len(b["rows"]) * lh
        if b["foot"]:
            y += lh * 0.3
            b["foot_top"] = y
            y += len(b["foot"]) * lh
        y += vp
    height = int(math.ceil(y))

    # ---- draw
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    missing = set()
    x0 = pad

    def baseline(top, font):
        asc, desc = font.getmetrics()
        return top + (lh - (asc + desc)) / 2 + asc

    def draw_row(top, cells, xstart):
        col, last = 0, 0
        for ch, (color, bold, ital) in cells:
            w = char_cols(ch)
            if w == 0:
                x = xstart + last * cw
            else:
                x = xstart + col * cw
                last = col
                col += w
            if ch == " ":
                continue
            font = fonts[(bold, ital)]
            if not ch.isascii() and ch not in missing:
                m = font.getmask(ch)
                if (m.size, bytes(m)) == notdef_sig:
                    missing.add(ch)
            d.text((x, baseline(top, font)), ch, font=font, fill=color, anchor="ls")

    for b in blocks:
        if b["rule_y"] is not None:
            d.rectangle([0, b["rule_y"], width, b["rule_y"] + rule_w - 1], fill="#000000")
        for k, (lineno, cells) in enumerate(b["rows"]):
            rt = b["top"] + k * lh
            if b["code"]:
                if lineno is not None and gutter:
                    for j, ch in enumerate(str(lineno).rjust(digits)):
                        if ch != " ":
                            d.text((x0 + j * cw, baseline(rt, f_reg)), ch, font=f_reg,
                                   fill="#777777", anchor="ls")
                draw_row(rt, cells, x0 + gutter * cw)
            else:
                draw_row(rt, cells, x0)
        for k, (_, cells) in enumerate(b["foot"]):
            draw_row(b["foot_top"] + k * lh, cells, x0)
        if b["code"] and gutter:
            gx = x0 + (digits + 0.9) * cw
            d.rectangle([gx, b["top"], gx + hair_w - 1, b["top"] + len(b["rows"]) * lh], fill="#bbbbbb")
    d.rectangle([0, 0, width - 1, height - 1], outline="#222222", width=rule_w)
    if missing:
        shown = " ".join(sorted(missing)[:10])
        warnings.append(f"{len(missing)} character(s) are missing from font '{family_used}' "
                        f"and appear as boxes: {shown}")
    return img, warnings


''')

between(R, "def build_html(spec: RenderSpec) -> str:", "def _render_playwright(spec: RenderSpec)", r'''def build_html(spec: RenderSpec) -> str:
    """HTML/CSS for one screenshot (#sheet element): code block + output block, nothing else."""
    from pygments.formatters import HtmlFormatter
    from pygments import highlight
    s = spec.settings
    prepared, total, gutter, digits = _prepare(spec)
    fmt = HtmlFormatter(nowrap=True, noclasses=True, style=_style_class(s))
    esc = htmllib.escape

    def code_row(no, inner, cols):
        num = f'<span class="no">{no if no is not None else ""}</span>' if gutter else ""
        return f'<div class="row">{num}<span class="tx" style="width:{cols}ch">{inner or " "}</span></div>'

    def plain_row(text, cls=""):
        return f'<div class="row {cls}"><span class="tx" style="width:{total}ch">{esc(text) or " "}</span></div>'

    blocks = []     # [is_code, rows, footer_rows]
    for sec, lines, foot in prepared:
        foot_rows = [plain_row(l, "muted") for l in foot]
        if sec.kind == "code":
            if s["highlight"]:
                hl = highlight("\n".join(lines), _get_lexer(sec.lexer), fmt).split("\n")
                hl = (hl + [""] * len(lines))[:len(lines)]
            else:
                hl = [esc(l) for l in lines]
            blocks.append([True, [code_row(n, h, total - gutter) for n, h in enumerate(hl, 1)], []])
            continue
        rows = [plain_row(l, "muted" if sec.kind == "muted" else "") for l in lines]
        if blocks and not blocks[-1][0]:
            blocks[-1][1].extend(rows)
            blocks[-1][2].extend(foot_rows)
        else:
            blocks.append([False, rows, foot_rows])

    parts = []
    for _, rows, foot in blocks:
        foot_html = f'<div class="foot">{"".join(foot)}</div>' if foot else ""
        parts.append(f'<section>{"".join(rows)}{foot_html}</section>')
    pad, vp = s["padding"], s["section_spacing"]
    css = f"""
    html,body{{margin:0;background:#fff}}
    #sheet{{display:inline-block;box-sizing:content-box;background:#fff;color:#111;
      box-shadow:inset 0 0 0 1px #222;
      font-family:'{s["font_family"]}','DejaVu Sans Mono','Liberation Mono',monospace;
      font-size:{s["font_size"]}px;line-height:1.4;width:calc({total}ch + {2*pad}px)}}
    section{{border-top:1px solid #000;padding:{vp}px {pad}px}}
    section:first-child{{border-top:none}}
    .foot{{margin-top:.42em}}
    .row{{display:flex}} .no{{flex:none;width:{max(gutter-1,0)}ch;padding-right:1ch;text-align:right;color:#777}}
    .tx{{flex:none;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-all;tab-size:{s["tab_width"]}}}
    .muted{{color:#555;font-style:italic}}"""
    if not s["wrap"]:
        css += " .tx{white-space:pre;overflow-wrap:normal;word-break:normal}"
    return f'<!doctype html><meta charset="utf-8"><style>{css}</style><div id="sheet">{"".join(parts)}</div>'


''')

# =============================================================== cp_config.py
C = "cp_config.py"
edit(C, 'dict(default=80, type=int, min=20, max=400, help="Max characters per line before wrapping")',
     'dict(default=76, type=int, min=20, max=400, help="Max characters per line before wrapping")')
edit(C, '"width_mode":      dict(default="fixed", type=str,', '"width_mode":      dict(default="fit", type=str,')
edit(C, 'dict(default=40, type=int, min=10, max=400, help="Minimum width in \'fit\' mode")',
     'dict(default=20, type=int, min=10, max=400, help="Minimum width in \'fit\' mode")')
edit(C, 'dict(default=16, type=int, min=0, max=100, help="Image padding (CSS px)")',
     'dict(default=8, type=int, min=0, max=100, help="Left/right image padding (CSS px)")')
edit(C, 'dict(default=10, type=int, min=0, max=100, help="Spacing inside sections (CSS px)")',
     'dict(default=6, type=int, min=0, max=100, help="Padding above/below the code and output blocks (CSS px)")')
edit(C, '    "show_input":      dict(default=False, type=bool, help="Add an INPUT section when --input-file is used"),\n', "")
edit(C, "def defaults() -> dict:", '''# settings that existed in older versions; silently ignored if still present in config.json
REMOVED_KEYS = {"show_input"}


def defaults() -> dict:''')
edit(C, '''        if key not in SCHEMA:
            print(f"warning: ignoring unknown key''', '''        if key in REMOVED_KEYS:
            continue
        if key not in SCHEMA:
            print(f"warning: ignoring unknown key''')

# =============================================================== cp_export.py
E = "cp_export.py"
edit(E, '"padding", "section_spacing", "show_input", "max_output_kb"]', '"padding", "section_spacing", "max_output_kb"]')
edit(E, '    show_title = bool(saved.pop("show_title", False))\n',
     '    saved.pop("show_title", None)          # keys written by older versions\n    saved.pop("show_input", None)\n')
edit(E, 'input_text=shot["input_text"], show_input=settings["show_input"],', 'input_text=shot["input_text"],')
edit(E, '    return RenderSpec(shot["title"] if show_title else None, sections, settings)', '    return RenderSpec(sections, settings)')
edit(E, '        saved["show_title"] = spec.title is not None\n', "")

# =============================================================== codeprint.py
P = "codeprint.py"
edit(P, 'help="Title printed at the top of the image.")', 'help="Library/page title (not drawn on the image).")')
edit(P, '    show_input: Annotated[Optional[bool], typer.Option("--show-input/--no-show-input", help="Add an INPUT section.")] = None,\n', "")
edit(P, "cfg = load_config(dict(timeout=timeout, show_input=show_input, line_numbers=line_numbers,",
     "cfg = load_config(dict(timeout=timeout, line_numbers=line_numbers,")
edit(P, 'input_text=input_text, show_input=cfg["show_input"], max_output_kb=cfg["max_output_kb"])',
     'input_text=input_text, max_output_kb=cfg["max_output_kb"])')
edit(P, "spec = RenderSpec(title, build_sections(mode, code, lang.lexer, rec), cfg)",
     "spec = RenderSpec(build_sections(mode, code, lang.lexer, rec), cfg)")
edit(P, '            saved["show_title"] = title is not None\n', "")
edit(P, 'spec = RenderSpec("setup check", build_sections(', 'spec = RenderSpec(build_sections(')
edit(P, '''            cp_config.atomic_write_bytes(cp_config.config_path(),
                                         (json.dumps(cp_config.defaults(), indent=2, sort_keys=True) + "\\n").encode())''',
     '''            # empty file = "use built-in defaults", so future default changes apply automatically
            cp_config.atomic_write_bytes(cp_config.config_path(), b"{}\\n")''')
edit(P, '''    """Rename a screenshot in the library.

    If the image prints its title, run `codeprint screenshot regenerate ID` to update the picture.
    """''', '''    """Rename a screenshot in the library (the title is not drawn on the image)."""''')
edit(P, '''        shows = json.loads(shot["settings_json"]).get("show_title")
        if as_json:
            emit({"id": sid, "title": title, "image_title_outdated": bool(shows)})
        else:
            typer.echo(f"Screenshot #{sid} renamed to \\"{title}\\".")
            if shows:
                typer.echo(f"The image still shows the old title; run: codeprint screenshot regenerate {sid}")
''', '''        if as_json:
            emit({"id": sid, "title": title})
        else:
            typer.echo(f"Screenshot #{sid} renamed to \\"{title}\\".")
''')
edit(P, "def warn(msg: str) -> None:", '''def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "\\u2026"


def warn(msg: str) -> None:''')
edit(P, 's["title"][:30]', 'clip(s["title"], 40)')
edit(P, 'p["title"][:32]', 'clip(p["title"], 40)')
edit(P, 'i["title"][:28]', 'clip(i["title"], 28)')
edit(P, '"duration_s": shot["duration_s"],',
     '"duration_s": None if shot["status"] in ("not_run", "compile_error") else shot["duration_s"],')
edit(P, '            typer.echo(f"{k:>12}: {v}")', '            typer.echo(f"{k:>12}: {\'-\' if v is None else v}")', count=2)

# =============================================================== shell scripts
edit("install.sh", '''command -v fc-list >/dev/null && fc-list 2>/dev/null | grep -qi "mono" \\
    || echo "note: no monospace font detected; install one: sudo apt install fonts-dejavu-core"''',
     '''if command -v fc-list >/dev/null; then
    fonts="$(fc-list 2>/dev/null || true)"        # (grep -q in a pipe + pipefail gave a false warning)
    case "${fonts,,}" in
        *mono*) ;;
        *) echo "note: no monospace font detected; install one: sudo apt install fonts-dejavu-core" ;;
    esac
fi''')
edit("setup.sh", '''echo
"$PY" "$PROJECT_DIR/codeprint.py" doctor || {
    echo "doctor reported missing REQUIRED items (see above)." >&2; exit 1; }''',
     '''echo
if doctor_out="$("$PY" "$PROJECT_DIR/codeprint.py" doctor 2>&1)"; then
    echo "Environment check: all required items OK (details: codeprint doctor)"
else
    echo "$doctor_out"; echo "doctor reported missing REQUIRED items (see above)." >&2; exit 1
fi''')
edit("demo.sh", ' -i "$DEMO/src/input.txt" --show-input', ' -i "$DEMO/src/input.txt"')

edit("demo.sh", 'cp_ generate "$DEMO/src/count.sh" --code-only --title "Experiment 05: Bash (code only)"\n',
     'cp_ generate "$DEMO/src/count.sh" --code-only --title "Experiment 05: Bash (code only)"\n'
     '# a long program: screenshots are compact now, so we need a tall one to show a full page\n'
     '{ echo "total = 0"; for i in $(seq 1 45); do echo "total += $i   # step $i"; done; echo "print(total)"; } > "$DEMO/src/long.py"\n'
     'cp_ generate "$DEMO/src/long.py" --title "Experiment 06: Long program"\n')
edit("demo.sh", 'for id in 1 2 3; do cp_ page add', 'for id in 1 2 3 4 5; do cp_ page add')
edit("demo.sh", 'cp_ page add 4 5 --page 1; rc=$?; echo "exit code: $rc (4 means CAPACITY_MAX)"\ncp_ page add 4 5 --page 1 --auto-page',
     'cp_ page add latest --page 1; rc=$?; echo "exit code: $rc (4 means CAPACITY_MAX)"\ncp_ page add latest --page 1 --auto-page')

# =============================================================== tests
T = "tests/test_codeprint.py"
edit(T, 'cp_render.RenderSpec("T", secs, s)', 'cp_render.RenderSpec(secs, s)', required=False)
edit(T, 'cp_render.RenderSpec(over.get("_title"), secs, s)', 'cp_render.RenderSpec(secs, s)', required=False)
edit(T, '''    a, _ = render(tmp_path, "print(1)\\n")
    b, _ = render(tmp_path, "x = 1\\n" * 30 + "print(2)\\n")''',
     '''    a, _ = render(tmp_path, "print(1)\\n", width_mode="fixed")
    b, _ = render(tmp_path, "x = 1\\n" * 30 + "print(2)\\n", width_mode="fixed")''', required=False)
edit(T, '''    wrapped, _ = render(tmp_path, long + "\\n")
    short, _ = render(tmp_path, "x = 1\\n")''', '''    wrapped, _ = render(tmp_path, long + "\\n", width_mode="fixed")
    short, _ = render(tmp_path, "x = 1\\n", width_mode="fixed")''', required=False)
edit(T, 'unwrapped, _ = render(tmp_path, long + "\\n", wrap=False)',
     'unwrapped, _ = render(tmp_path, long + "\\n", wrap=False, width_mode="fixed")', required=False)
edit(T, 'fixed, _ = render(tmp_path, "x=1\\n", "1\\n")', 'fixed, _ = render(tmp_path, "x=1\\n", "1\\n", width_mode="fixed")', required=False)
edit(T, 'assert cp_config.load_config()["columns"] == 80', 'assert cp_config.load_config()["columns"] == 76', required=False)
edit(T, 'ids = [make_shot(tmp_path, f"p{i}.py", "print(1)\\n" * 5) for i in range(3)]',
     'ids = [make_shot(tmp_path, f"p{i}.py", (\'print("\' + "a" * 60 + \'")\\n\') * 7) for i in range(3)]', required=False)
if load(T) is not None:
    files[T] += '''

# ------------------------------------------------------------- compact images (no titles / labels)
def test_image_has_only_code_and_output(tmp_path):
    secs = cp_render.build_sections("code_output", "print(1)\\n", "python",
                                    dict(status="ok", stdout="1\\n", exit_code=0))
    html = cp_render.build_html(cp_render.RenderSpec(secs, cp_config.defaults()))
    for word in ("SOURCE CODE", "PROGRAM OUTPUT", "INPUT", "STANDARD ERROR", 'class="title"', 'class="label"'):
        assert word not in html
    res, _ = render(tmp_path)
    assert res.height_px / res.scale < 80            # 2 text rows + 2 thin paddings + 1 rule, nothing else


def test_default_images_print_at_full_size(tmp_path):
    res, _ = render(tmp_path, "x = 1\\n" * 3 + "y = '" + "a" * 200 + "'\\n")
    assert res.width_px / res.scale * 25.4 / 96 <= 186.0     # fits the default printable width, no downscale


def test_narrow_screenshots_share_a_row(tmp_path):
    a, b = make_shot(tmp_path, "a.py", "print(1)\\n"), make_shot(tmp_path, "b.py", "print(2)\\n")
    pid = json.loads(cli("page", "create", "T", "--json").stdout)["page_id"]
    cli("page", "add", a, b, "--page", pid)
    items_ = json.loads(cli("page", "show", pid, "--json").stdout)["page"]["items"]
    assert items_[0]["y_mm"] == items_[1]["y_mm"] and items_[0]["x_mm"] < items_[1]["x_mm"]


def test_legacy_config_keys_ignored_silently(tmp_path):
    cp_config.config_path().write_text(json.dumps({"engine": "pillow", "show_input": True}))
    res = cli("config", "show")
    assert "warning" not in res.output and "show_input" not in res.output


def test_title_is_metadata_only(tmp_path):
    src = write(tmp_path, "t.py", "print(1)\\n")
    a = json.loads(cli("generate", src, "--json").stdout)["screenshot"]
    b = json.loads(cli("generate", src, "--title", "A very long experiment title", "--json").stdout)["screenshot"]
    assert a["height_px"] == b["height_px"] and b["title"] == "A very long experiment title"
'''

# ------------------------------------------------------------------ docs (best effort)
D = "README.md"
edit(D, "One PNG with SOURCE CODE + PROGRAM OUTPUT (or code-only / output-only)",
     "One compact PNG with only the source code and its output - no titles or labels (or code-only / output-only)", required=False)
edit(D, "--input-file input.txt --show-input --timeout 10", "--input-file input.txt --timeout 10", required=False)
edit(D, 'codeprint generate main.c --title "Experiment 01"\n',
     'codeprint generate main.c --title "Experiment 01"        # title = library/page name only, never drawn on the image\n', required=False)
edit(D, "- Typed stdin is not echoed in program output (use `--show-input`). stdout and stderr are shown in separate sections, not interleaved.",
     "- Typed stdin is not echoed in program output and input text is not drawn. stdout and stderr share one output block (stdout first), not interleaved.", required=False)

# ===================================================================== finish
if problems:
    print("ABORTED - nothing was written. These required edits did not match (repo differs from expected):")
    for p in problems:
        print("  -", p)
    sys.exit(1)
for w in warnings:
    print("note (optional edit skipped):", w)
print(f"{len(touched)} file(s) will change: {', '.join(sorted(touched))}")
if args.dry_run:
    print("dry run: no files written")
    sys.exit(0)

backup = REPO / ".backup-before-update"
for rel in touched:
    src = REPO / rel
    dst = backup / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
for rel in touched:
    (REPO / rel).write_text(files[rel], encoding="utf-8")
for rel in ("install.sh", "setup.sh", "uninstall.sh", "run-tests.sh", "demo.sh", "codeprint.py", "apply_changes.py"):
    p = REPO / rel
    if p.exists():
        p.chmod(p.stat().st_mode | 0o111)
print(f"backups saved in {backup}")

# ---- migrate the user's config: drop removed key + values equal to the OLD defaults
base = os.environ.get("CODEPRINT_CONFIG_DIR") or str(Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "codeprint")
cfg_path = Path(base).expanduser() / "config.json"
OLD = {"columns": 80, "width_mode": "fixed", "min_columns": 40, "padding": 16, "section_spacing": 10}
if cfg_path.exists():
    try:
        cfg = json.loads(cfg_path.read_text())
        dropped = [k for k, v in list(cfg.items()) if k == "show_input" or OLD.get(k, object()) == v]
        for k in dropped:
            del cfg[k]
        shutil.copy2(cfg_path, cfg_path.with_name("config.json.bak-before-update"))
        cfg_path.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
        print(f"config migrated ({cfg_path}); dropped: {', '.join(dropped) or 'nothing'}")
    except (OSError, ValueError) as exc:
        print(f"could not migrate {cfg_path}: {exc} (edit it by hand: remove show_input and old default values)")

print("""
Done. Next:
  ./run-tests.sh
  ./demo.sh --force
  # re-render screenshots already in your library (drops titles/labels; refused if a page would overflow):
  for id in $(codeprint screenshot list --json | python3 -c "import json,sys;print(*[s['id'] for s in json.load(sys.stdin)['screenshots']])"); do codeprint screenshot regenerate $id; done
""")
