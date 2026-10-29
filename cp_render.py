"""Turn source code + program output into a clean white PNG.

Two interchangeable engines:
  pillow      draws text itself on a character grid (default fallback, no browser needed)
  playwright  builds HTML/CSS (Pygments spans) and screenshots it with a local Chromium
`engine = auto` uses Playwright only when its browser is installed, else Pillow.
Both use the same layout parameters (columns, wrapping, spacing) and the same colours.
"""
from __future__ import annotations

import html as htmllib
import io
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pygments.lexers import get_lexer_by_name
from pygments.style import Style
from pygments.token import (Comment, Error, Generic, Keyword, Name, Number, Operator,
                            String, Token)
from pygments.util import ClassNotFound

from cp_config import CodePrintError, EXIT_ENV, EXIT_USAGE


# ------------------------------------------------------------------ model ---
@dataclass
class Section:
    label: str
    text: str
    kind: str = "text"            # code | text | muted
    lexer: str | None = None
    footer: str = ""              # muted note under the body (exit status, timeout...)


@dataclass
class RenderSpec:
    sections: list
    settings: dict


@dataclass
class RenderResult:
    width_px: int
    height_px: int
    scale: float
    font_px: float
    engine: str
    warnings: list = field(default_factory=list)


def build_sections(mode: str, code: str, lexer: str, rec: dict) -> list:
    """Create the sections for mode code_output | code_only | output_only.

    `rec` holds: status, exit_code, stdout, stderr, compile_output, timeout_s,
    truncated, input_text, max_output_kb.
    """
    sections = []
    if mode != "output_only":
        sections.append(Section("SOURCE CODE", code, "code", lexer))
    if mode == "code_only":
        return sections
    status = rec.get("status", "not_run")
    if status == "compile_error":
        sections.append(Section("COMPILER ERRORS",
                                rec.get("compile_output") or "(no diagnostics captured)", "text"))
        return sections
    out_secs = []
    stdout, stderr = rec.get("stdout") or "", rec.get("stderr") or ""
    if stdout.strip("\n") or not stderr.strip("\n"):
        out_secs.append(Section("PROGRAM OUTPUT", stdout if stdout.strip("\n") else "(no output)",
                                "text" if stdout.strip("\n") else "muted"))
    if stderr.strip("\n"):
        out_secs.append(Section("STANDARD ERROR", stderr, "text"))
    notes = []
    if rec.get("truncated"):
        notes.append(f"[output truncated at {rec.get('max_output_kb', 256)} KiB]")
    if status == "timeout":
        notes.append(f"[timed out after {rec.get('timeout_s'):g}s - process killed]")
    elif status == "failed":
        notes.append(f"[exit status {rec.get('exit_code')}]")
    out_secs[-1].footer = "\n".join(notes)
    return sections + out_secs


# ----------------------------------------------------------- text helpers ---
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def clean_text(text: str, tab_width: int = 4) -> list:
    """Normalise text into display lines (ANSI removed, \\r handled, tabs expanded)."""
    text = _ANSI.sub("", text.replace("\r\n", "\n"))
    out = []
    for line in text.split("\n"):
        if "\r" in line:                      # progress-bar style overwrite: keep last non-empty part
            parts = [p for p in line.split("\r") if p]
            line = parts[-1] if parts else ""
        line = line.expandtabs(tab_width)
        line = "".join(ch if (ch >= " " and ch != "\x7f") else "^" + chr((ord(ch) + 64) % 128)
                       for ch in line)
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return out or [""]


def char_cols(ch: str) -> int:
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def line_cols(line: str) -> int:
    return sum(char_cols(c) for c in line)


def _chunk_plain(line: str, max_cols: int) -> list:
    rows, cur, col = [], [], 0
    for ch in line:
        w = char_cols(ch)
        if col + w > max_cols and col > 0 and w > 0:
            rows.append("".join(cur))
            cur, col = [], 0
        cur.append(ch)
        col += w
    rows.append("".join(cur))
    return rows


# ----------------------------------------------------------------- styles ---
class PrintStyle(Style):
    """Dark colours that stay distinguishable (bold/italic) when printed in grey."""
    styles = {
        Token: "#111111",
        Comment: "italic #555555",
        Comment.Preproc: "noitalic bold #444444",
        Keyword: "bold #0a2f7a",
        Keyword.Type: "nobold #0a2f7a",
        Name.Function: "bold #5b2a86",
        Name.Class: "bold #5b2a86",
        Name.Decorator: "#5b2a86",
        Name.Builtin: "#0a5c5c",
        String: "#8a1c1c",
        Number: "#7a4a00",
        Generic.Error: "bold #8a0000",
        Error: "#111111",
    }


class BWStyle(Style):
    styles = {
        Token: "#000000",
        Comment: "italic #444444",
        Comment.Preproc: "bold #444444",
        Keyword: "bold #000000",
        Keyword.Type: "nobold #000000",
        Name.Function: "bold #000000",
        Name.Class: "bold #000000",
        String: "#222222",
        Generic.Error: "bold #000000",
        Error: "#000000",
    }


def _style_class(settings: dict):
    return BWStyle if settings["highlight_style"] == "bw" else PrintStyle


def _get_lexer(name: str | None):
    try:
        return get_lexer_by_name(name or "text", stripnl=False, ensurenl=False)
    except ClassNotFound:
        return get_lexer_by_name("text", stripnl=False, ensurenl=False)


# ------------------------------------------------------------------ fonts ---
_FONT_FILES = {
    "DejaVu Sans Mono": ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf",
                         "DejaVuSansMono-Oblique.ttf", "DejaVuSansMono-BoldOblique.ttf"),
    "Liberation Mono": ("LiberationMono-Regular.ttf", "LiberationMono-Bold.ttf",
                        "LiberationMono-Italic.ttf", "LiberationMono-BoldItalic.ttf"),
    "FreeMono": ("FreeMono.ttf", "FreeMonoBold.ttf", "FreeMonoOblique.ttf", "FreeMonoBoldOblique.ttf"),
}
_FONT_ROOTS = ("/usr/share/fonts", "/usr/local/share/fonts", "~/.local/share/fonts", "~/.fonts",
               "/Library/Fonts", "/System/Library/Fonts")
_STYLES = ("regular", "bold", "italic", "bolditalic")


@lru_cache(maxsize=None)
def _locate(filename: str) -> str | None:
    for root in _FONT_ROOTS:
        r = Path(root).expanduser()
        if r.is_dir():
            for p in r.rglob(filename):
                return str(p)
    return None


def _fc_match(family: str, style: str) -> str | None:
    if not shutil.which("fc-match"):
        return None
    pat = {"regular": "", "bold": ":bold", "italic": ":italic", "bolditalic": ":bold:italic"}[style]
    try:
        out = subprocess.run(["fc-match", "-f", "%{family}|%{file}", family + pat],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    fam, _, path = out.partition("|")
    if family.lower() in fam.lower() and path and Path(path).is_file() and not path.endswith(".pcf.gz"):
        return path
    return None


@lru_cache(maxsize=None)
def find_font_files(family: str) -> tuple:
    """Return ({style: path}, family_used, warning|None). Falls back to DejaVu/Liberation/Free Mono."""
    tried = [family] + [f for f in _FONT_FILES if f.lower() != family.lower()]
    for fam in tried:
        paths = {}
        for i, style in enumerate(_STYLES):
            p = _fc_match(fam, style)
            if not p and fam in _FONT_FILES:
                p = _locate(_FONT_FILES[fam][i])
            if p:
                paths[style] = p
        if "regular" in paths:
            for style in _STYLES:
                paths.setdefault(style, paths["regular"])
            warn = None if fam.lower() == family.lower() else \
                f"font '{family}' not found; using '{fam}'"
            return paths, fam, warn
    raise CodePrintError("FONT_NOT_FOUND",
                         "No monospace font found. Install one (Ubuntu: sudo apt install fonts-dejavu-core) "
                         "or set font_family.", EXIT_ENV)


# ------------------------------------------------------------- preparation ---
def _prepare(spec: RenderSpec):
    """Clean text of every section and compute total column count and gutter."""
    s = spec.settings
    prepared = []
    for sec in spec.sections:
        lines = clean_text(sec.text, s["tab_width"])
        foot = clean_text(sec.footer, s["tab_width"]) if sec.footer else []
        prepared.append((sec, lines, foot))
    code_lines = [len(l) for sec, l, _ in prepared if sec.kind == "code"]
    digits = len(str(max(code_lines))) if code_lines else 0
    digits = max(digits, 2) if code_lines else 0          # constant gutter up to 99 lines
    gutter = digits + 2 if (s["line_numbers"] and code_lines) else 0
    need = 0
    for sec, lines, foot in prepared:
        extra = gutter if sec.kind == "code" else 0
        need = max(need, max(line_cols(l) for l in lines + foot) + extra)
    max_total = s["columns"] + (4 if (s["line_numbers"] and code_lines) else 0)   # same width for every image
    if s["wrap"]:
        total = max_total if s["width_mode"] == "fixed" else min(max(need, s["min_columns"]), max_total)
    else:
        total = max(max_total if s["width_mode"] == "fixed" else s["min_columns"], need)
    return prepared, max(total, gutter + 10), gutter, digits


# ------------------------------------------------------------ pillow engine ---
def _code_cells(lines: list, lexer_name: str | None, settings: dict) -> list:
    """Return per-line lists of (char, (hexcolour, bold, italic))."""
    code = "\n".join(lines)
    style = _style_class(settings)
    default = ("#111111", False, False)
    if not settings["highlight"]:
        return [[(c, default) for c in l] for l in lines]
    out, cur = [], []
    for ttype, value in _get_lexer(lexer_name).get_tokens(code):
        st = style.style_for_token(ttype)
        sty = ("#" + (st["color"] or "111111"), bool(st["bold"]), bool(st["italic"]))
        for ch in value:
            if ch == "\n":
                out.append(cur)
                cur = []
            else:
                cur.append((ch, sty))
    out.append(cur)
    # Lexers may append a trailing newline; keep exactly one entry per source line.
    out = out[:len(lines)]
    while len(out) < len(lines):
        out.append([])
    return out


def _wrap_cells(cells: list, max_cols: int, wrap: bool) -> list:
    if not wrap:
        return [cells]
    rows, cur, col = [], [], 0
    for cell in cells:
        w = char_cols(cell[0])
        if col + w > max_cols and col > 0 and w > 0:
            rows.append(cur)
            cur, col = [], 0
        cur.append(cell)
        col += w
    rows.append(cur)
    return rows


def _render_pillow(spec: RenderSpec) -> tuple:
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


# ----------------------------------------------------------- playwright engine ---
_PW_STATE: dict = {}


def playwright_available() -> bool:
    """True if the playwright package and its Chromium build are installed."""
    if "ok" in _PW_STATE:
        return _PW_STATE["ok"]
    ok = False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ok = Path(p.chromium.executable_path).exists()
    except Exception:
        ok = False
    _PW_STATE["ok"] = ok
    return ok


def build_html(spec: RenderSpec) -> str:
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


def _render_playwright(spec: RenderSpec) -> Image.Image:
    from playwright.sync_api import sync_playwright
    html = build_html(spec)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            ctx = browser.new_context(device_scale_factor=float(spec.settings["scale"]),
                                      viewport={"width": 1600, "height": 1000})
            page = ctx.new_page()
            page.set_content(html)
            png = page.locator("#sheet").screenshot(type="png")
        finally:
            browser.close()
    return Image.open(io.BytesIO(png)).convert("RGB")


# ------------------------------------------------------------------ public ---
def render_png(spec: RenderSpec, out_path: Path, engine: str | None = None) -> RenderResult:
    """Render `spec` to `out_path` (atomically). Returns dimensions and the engine used."""
    s = spec.settings
    engine = engine or s.get("engine", "auto")
    warnings: list = []
    img = None
    used = "pillow"
    if engine in ("auto", "playwright"):
        if playwright_available():
            try:
                img, used = _render_playwright(spec), "playwright"
            except Exception as exc:     # browser problems must not lose the screenshot in auto mode
                if engine == "playwright":
                    raise CodePrintError("RENDER_FAILED", f"Playwright rendering failed: {exc}", EXIT_ENV)
                warnings.append(f"Playwright failed ({exc}); used the Pillow engine instead")
        elif engine == "playwright":
            raise CodePrintError("RENDER_FAILED", "Playwright/Chromium is not installed "
                                 "(run: playwright install chromium) or use engine=pillow.", EXIT_ENV)
    if img is None:
        img, w = _render_pillow(spec)
        warnings.extend(w)
    dpi = 96.0 * float(s["scale"])
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out_path.parent), prefix=".render-", suffix=".png")
    os.close(fd)
    try:
        img.save(tmp, format="PNG", dpi=(dpi, dpi))
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return RenderResult(img.width, img.height, float(s["scale"]), float(s["font_size"]), used, warnings)
