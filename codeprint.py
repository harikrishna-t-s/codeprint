#!/usr/bin/env python3
"""codeprint - turn source code + program output into clean, printable lab-record pages.

Run `codeprint --help` for the command overview.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Optional

import typer
from PIL import Image

import cp_config
from cp_config import (CodePrintError, EXIT_NOT_FOUND, EXIT_USAGE, SCHEMA, ensure_dirs, load_config)
from cp_export import (RENDER_KEYS, check_output_path, collect_page, export_pages, ensure_image,
                       image_path, output_targets, preview_page_png, regenerate_screenshot, slugify,
                       write_pdf)
from cp_layout import CapacityError, LayoutItem, PageSpec, compute_layout
from cp_render import RenderSpec, build_sections, render_png
from cp_runner import (LANGUAGES, detect_language, find_tool, missing_tools, read_source, run_program,
                       RunResult)
from cp_store import Store, item_from_row, spec_from_page

__version__ = "1.0.0"

app = typer.Typer(
    name="codeprint", no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False,
    rich_markup_mode=None, context_settings={"help_option_names": ["-h", "--help"]},
    help="Turn source code and its output into clean white screenshots and print-ready A4 pages.\n\n"
         "\b\nTypical workflow:\n"
         "  codeprint generate hello.c --title \"Experiment 01\"\n"
         "  codeprint page create \"Experiment 01\"\n"
         "  codeprint page add 1 --page 1\n"
         "  codeprint page preview 1\n"
         "  codeprint page export 1\n\n"
         "Exit codes: 0 ok, 1 error, 2 bad input, 3 not found, 4 page full (CAPACITY_MAX), "
         "5 missing tool/renderer, 6 refused to overwrite.\n"
         "WARNING: `generate` runs the program you give it with your user's permissions. "
         "Only use code you trust.")
shot_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None, help="Manage saved screenshots.",
                       context_settings={"help_option_names": ["-h", "--help"]})
page_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None, help="Create and manage A4 pages.",
                       context_settings={"help_option_names": ["-h", "--help"]})
config_app = typer.Typer(no_args_is_help=True, rich_markup_mode=None, help="Show or change settings.",
                         context_settings={"help_option_names": ["-h", "--help"]})
app.add_typer(shot_app, name="screenshot")
app.add_typer(page_app, name="page")
app.add_typer(config_app, name="config")

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable JSON output.")]


# ------------------------------------------------------------------ helpers ---
@contextmanager
def guard(as_json: bool = False):
    """Turn expected errors into a clean message + exit code."""
    try:
        yield
    except CodePrintError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": exc.to_dict()}, indent=2))
        else:
            typer.echo(f"error [{exc.code}]: {exc.message}", err=True)
        raise typer.Exit(exc.exit_code)
    except sqlite3.Error as exc:
        typer.echo(f"error [DB_ERROR]: {exc}", err=True)
        raise typer.Exit(1)
    except OSError as exc:
        typer.echo(f"error [IO_ERROR]: {exc}", err=True)
        raise typer.Exit(1)


def emit(obj: dict) -> None:
    print(json.dumps({"ok": True, **obj}, indent=2, default=str))


def open_store() -> Store:
    ensure_dirs()
    return Store()


def parse_id(value: str, kind: str, store: Store) -> int:
    v = str(value).strip().lower()
    if v == "latest":
        return store.latest_screenshot_id() if kind == "screenshot" else store.latest_page_id()
    try:
        n = int(v)
        if n < 1:
            raise ValueError
        return n
    except ValueError:
        raise CodePrintError("INVALID_ID", f"'{value}' is not a valid {kind} id (use a number or 'latest').",
                             EXIT_USAGE)


def confirm(message: str, yes: bool) -> bool:
    """True = go ahead. Non-interactive runs must pass --yes."""
    if yes:
        return True
    if not sys.stdin.isatty():
        raise CodePrintError("CONFIRMATION_REQUIRED", f"{message} Re-run with --yes to confirm.", EXIT_USAGE)
    return typer.confirm(message, default=False)


def confirm_overwrite(message: str) -> bool:
    return sys.stdin.isatty() and typer.confirm(message, default=False)


def table(headers: list, rows: list) -> str:
    cols = list(zip(headers, *rows)) if rows else [(h,) for h in headers]
    widths = [max(len(str(x)) for x in col) for col in cols]
    line = lambda r: "  ".join(str(x).ljust(w) for x, w in zip(r, widths)).rstrip()
    return "\n".join([line(headers), line(["-" * w for w in widths])] + [line(r) for r in rows])


def mm(x: float) -> str:
    return f"{x:.1f}"


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "\u2026"


def warn(msg: str) -> None:
    typer.echo(f"warning: {msg}", err=True)


def gui_available() -> bool:
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_file(path: Path, use_browser: bool = False) -> tuple:
    """Open an image in the default viewer / browser. Returns (opened, message)."""
    if not gui_available():
        return False, "No graphical session detected (DISPLAY/WAYLAND_DISPLAY not set); not opening a viewer."
    try:
        if use_browser:
            ok = webbrowser.open(Path(path).resolve().as_uri())
            return ok, "Opened in browser." if ok else "Could not launch a browser."
        opener = shutil.which("xdg-open") or shutil.which("open")
        if not opener:
            return False, "No image opener found (install xdg-utils) - open the file manually."
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return True, "Opened in the default viewer."
    except OSError as exc:
        return False, f"Could not open a viewer: {exc}"


def shot_summary(shot: dict) -> dict:
    item = item_from_row(shot)
    p = image_path(shot)
    return {"id": shot["id"], "title": shot["title"], "language": shot["language"], "mode": shot["mode"],
            "status": shot["status"], "exit_code": shot["exit_code"], "duration_s": None if shot["status"] in ("not_run", "compile_error") else shot["duration_s"],
            "width_px": shot["width_px"], "height_px": shot["height_px"], "dpi": round(96 * shot["scale"]),
            "printed_mm": [round(item.w_mm, 1), round(item.h_mm, 1)], "engine": shot["engine"],
            "source_path": shot["source_path"], "created_at": shot["created_at"],
            "image_path": str(p), "image_exists": p.exists()}


def layout_dict(page: dict, layout, rows: list) -> dict:
    by_id = {r["id"]: r for r in rows}
    spec = layout.spec
    return {"page_id": page["id"], "title": page["title"], "orientation": spec.orientation,
            "margin_mm": spec.margin_mm, "spacing_mm": spec.spacing_mm, "layout_mode": spec.mode,
            "grid_columns": spec.grid_columns, "allow_rotation": spec.allow_rotation,
            "page_mm": [spec.page_w, spec.page_h],
            "printable_mm": [round(spec.printable_w, 1), round(spec.printable_h, 1)],
            "utilization_pct": round(layout.utilization_pct, 1),
            "remaining_height_mm": round(layout.remaining_height_mm, 1),
            "items": [{"position": n, "screenshot_id": p.item_id, "title": by_id[p.item_id]["title"],
                       "x_mm": round(p.x_mm, 1), "y_mm": round(p.y_mm, 1), "w_mm": round(p.w_mm, 1),
                       "h_mm": round(p.h_mm, 1), "scale_pct": round(p.scale * 100, 1), "rotated": p.rotated}
                      for n, p in enumerate(layout.placements, 1)]}


def page_header(page: dict, layout) -> str:
    sp = layout.spec
    return (f"Page {page['id']} \"{page['title']}\": A4 {sp.orientation}, margins {sp.margin_mm:g} mm, "
            f"spacing {sp.spacing_mm:g} mm, layout {sp.mode}"
            + (f" x{sp.grid_columns}" if sp.mode == "grid" else ""))


def capacity_line(layout) -> str:
    return (f"Utilization {layout.utilization_pct:.1f}% of {layout.spec.printable_w:.0f} x "
            f"{layout.spec.printable_h:.0f} mm printable area; {layout.remaining_height_mm:.1f} mm of "
            f"height remaining below the last row.")


def page_spec_from_options(cfg: dict, orientation=None, margin=None, spacing=None, layout=None,
                           grid_columns=None, rotate=None, min_font_pt=None, base: PageSpec | None = None) -> PageSpec:
    if base is None:
        base = PageSpec(cfg["page_orientation"], cfg["page_margin_mm"], cfg["page_spacing_mm"], cfg["layout_mode"],
                        cfg["grid_columns"], cfg["allow_rotation"], cfg["min_font_pt"])
    return PageSpec(orientation or base.orientation,
                    base.margin_mm if margin is None else margin,
                    base.spacing_mm if spacing is None else spacing,
                    layout or base.mode,
                    base.grid_columns if grid_columns is None else grid_columns,
                    base.allow_rotation if rotate is None else rotate,
                    base.min_font_pt if min_font_pt is None else min_font_pt)


def add_with_capacity(store: Store, pid: int, sid: int, position: int | None, auto_page: bool):
    """Add to a page; with auto_page, spill into a new page when it is full.
    Returns (page_id_used, layout, new_page_id|None)."""
    try:
        return pid, store.add_to_page(pid, sid, position), None
    except CapacityError as exc:
        if not auto_page or exc.code != "CAPACITY_MAX":
            raise
        page = store.get_page(pid)
        spec = spec_from_page(page)
        compute_layout(spec, [item_from_row(store.get_screenshot(sid))])   # must fit an empty page
        new_id = store.create_page(f"{page['title']} (cont.)", spec)
        return new_id, store.add_to_page(new_id, sid), new_id


def default_output(kind: str, cfg: dict, name: str) -> Path:
    return Path(cfg["output_dir"]).expanduser() / name


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cand, n = directory / f"{stem}{suffix}", 1
    while cand.exists():
        cand = directory / f"{stem}-{n}{suffix}"
        n += 1
    return cand


# ------------------------------------------------------------------ generate ---
@app.command()
def generate(
    source: Annotated[Path, typer.Argument(help="Source file (.py .c .cpp .java .js .sh ...).")],
    language: Annotated[Optional[str], typer.Option("--language", "-l", help="Force a language: python, c, cpp, java, javascript, bash.")] = None,
    title: Annotated[Optional[str], typer.Option("--title", "-t", help="Library/page title (not drawn on the image).")] = None,
    input_file: Annotated[Optional[Path], typer.Option("--input-file", "-i", help="File piped to the program's standard input.")] = None,
    timeout: Annotated[Optional[float], typer.Option("--timeout", help="Seconds before the program is killed.")] = None,
    output_dir: Annotated[Optional[Path], typer.Option("--output-dir", "-o", help="Also copy the PNG into this directory (never overwrites).")] = None,
    code_only: Annotated[bool, typer.Option("--code-only", help="Source code only; do not run the program.")] = False,
    output_only: Annotated[bool, typer.Option("--output-only", help="Run the program; image shows output only.")] = False,
    line_numbers: Annotated[Optional[bool], typer.Option("--line-numbers/--no-line-numbers")] = None,
    highlight: Annotated[Optional[bool], typer.Option("--highlight/--no-highlight", help="Syntax highlighting.")] = None,
    font_size: Annotated[Optional[float], typer.Option("--font-size", help="Font size in CSS px.")] = None,
    columns: Annotated[Optional[int], typer.Option("--columns", help="Characters per line before wrapping.")] = None,
    wrap: Annotated[Optional[bool], typer.Option("--wrap/--no-wrap", help="Wrap long lines or widen the image.")] = None,
    width_mode: Annotated[Optional[str], typer.Option("--width-mode", help="fixed (same width) or fit (shrink to content).")] = None,
    engine: Annotated[Optional[str], typer.Option("--engine", help="auto, pillow or playwright.")] = None,
    cwd_source: Annotated[bool, typer.Option("--cwd-source", help="Run with the source file's folder as working directory.")] = False,
    page: Annotated[Optional[str], typer.Option("--page", help="Also add the screenshot to this page id (or 'latest').")] = None,
    auto_page: Annotated[bool, typer.Option("--auto-page", help="With --page: start a new page if it is full.")] = False,
    as_json: JsonOpt = False,
):
    """Generate a screenshot of SOURCE (and its output) and save it in the library.

    \b
    Examples:
      codeprint generate hello.py
      codeprint generate main.c --title "Experiment 01" --input-file input.txt
      codeprint generate Main.java --timeout 20 --output-dir ./output
      codeprint generate algo.cpp --code-only --no-line-numbers
      codeprint generate hello.c --page 1 --auto-page

    The program is executed in a subprocess (NOT a sandbox). Program failures still
    produce a screenshot; the exit status is recorded and shown in the image.
    """
    with guard(as_json):
        if code_only and output_only:
            raise CodePrintError("BAD_OPTIONS", "--code-only and --output-only cannot be combined.", EXIT_USAGE)
        cfg = load_config(dict(timeout=timeout, line_numbers=line_numbers,
                               highlight=highlight, font_size=font_size, columns=columns, wrap=wrap,
                               width_mode=width_mode, engine=engine))
        lang = detect_language(source, language)
        code = read_source(source)
        mode = "code_only" if code_only else "output_only" if output_only else "code_output"
        input_text = None
        if input_file is not None:
            if code_only:
                warn("--input-file ignored with --code-only")
            else:
                if not Path(input_file).is_file():
                    raise CodePrintError("FILE_NOT_FOUND", f"Input file not found: {input_file}", EXIT_NOT_FOUND)
                input_text = Path(input_file).read_text(encoding="utf-8", errors="replace")
        run = RunResult()
        if mode != "code_only":
            run = run_program(lang, Path(source), input_file, cfg["timeout"], cwd_source, cfg["max_output_kb"])
        rec = dict(status=run.status, exit_code=run.exit_code, stdout=run.stdout, stderr=run.stderr,
                   compile_output=run.compile_output, timeout_s=run.timeout_s, truncated=run.truncated,
                   input_text=input_text, max_output_kb=cfg["max_output_kb"])
        spec = RenderSpec(build_sections(mode, code, lang.lexer, rec), cfg)

        store = open_store()
        images = cp_config.images_dir()
        tmp = images / f".new-{os.getpid()}.png"
        sid = None
        try:
            result = render_png(spec, tmp)
            saved = {k: cfg[k] for k in RENDER_KEYS}
            sid = store.add_screenshot(
                title=title or Path(source).name, source_path=str(Path(source).resolve()), language=lang.key,
                created_at=__import__("cp_store").now(), image_file="pending", mode=mode, source_code=code,
                input_text=input_text, stdout=run.stdout, stderr=run.stderr, compile_output=run.compile_output,
                status=run.status, exit_code=run.exit_code, duration_s=run.duration_s,
                truncated=int(run.truncated), timeout_s=run.timeout_s, width_px=result.width_px,
                height_px=result.height_px, scale=result.scale, font_px=result.font_px, engine=result.engine,
                settings_json=json.dumps(saved))
            final = images / f"shot_{sid:04d}.png"
            os.replace(tmp, final)
            store.update_screenshot(sid, image_file=final.name)
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            if sid is not None:
                store.delete_screenshot(sid)
            raise

        copy_path = None
        if output_dir is not None:
            copy_path = unique_path(Path(output_dir).expanduser(), slugify(title or Path(source).stem), ".png")
            shutil.copyfile(final, copy_path)

        page_info = None
        try:
            if page is not None:
                pid = parse_id(page, "page", store)
                used, layout, created = add_with_capacity(store, pid, sid, None, auto_page)
                page_info = {"page_id": used, "new_page": created,
                             "utilization_pct": round(layout.utilization_pct, 1),
                             "remaining_height_mm": round(layout.remaining_height_mm, 1)}
        except CapacityError as exc:
            exc.message += f" (Screenshot #{sid} was saved in the library.)"
            exc.details["screenshot_id"] = sid
            raise
        finally:
            store.close()

        if as_json:
            emit({"screenshot": {**shot_summary(store_reload(sid)), "run": {
                "status": run.status, "exit_code": run.exit_code, "duration_s": round(run.duration_s, 3)}},
                  "copy": str(copy_path) if copy_path else None, "page": page_info,
                  "warnings": result.warnings})
            return
        for w in result.warnings:
            warn(w)
        typer.echo(f"Screenshot #{sid} saved ({result.width_px} x {result.height_px} px, "
                   f"{round(96 * result.scale)} dpi, engine {result.engine})")
        typer.echo(f"  image:    {final}")
        if copy_path:
            typer.echo(f"  copy:     {copy_path}")
        typer.echo(f"  language: {lang.display}   mode: {mode}")
        if mode != "code_only":
            extra = {"ok": "ok", "failed": f"program exited with status {run.exit_code}",
                     "timeout": f"timed out after {run.timeout_s:g}s", "compile_error": "compilation FAILED"}[run.status]
            took = "" if run.status == "compile_error" else f" ({run.duration_s:.2f}s)"
            typer.echo(f"  run:      {extra}{took}")
        if page_info:
            what = f"new page {page_info['page_id']}" if page_info["new_page"] else f"page {page_info['page_id']}"
            typer.echo(f"  page:     added to {what} (utilization {page_info['utilization_pct']}%)")


def store_reload(sid: int) -> dict:
    s = Store()
    try:
        return s.get_screenshot(sid)
    finally:
        s.close()


# ------------------------------------------------------------------- preview ---
def _image_info(path: Path) -> dict:
    try:
        with Image.open(path) as im:
            dpi = im.info.get("dpi")
            return {"format": im.format, "width_px": im.width, "height_px": im.height,
                    "dpi": [round(float(dpi[0])), round(float(dpi[1]))] if dpi else None}
    except Exception as exc:
        raise CodePrintError("NOT_AN_IMAGE", f"{path} is not a readable image: {exc}", EXIT_USAGE)


@app.command()
def preview(
    target: Annotated[Optional[list[str]], typer.Argument(help="'latest', an id, or 'screenshot <id>' / 'page <id>'.")] = None,
    file: Annotated[Optional[Path], typer.Option("--file", help="Preview any image file.")] = None,
    open_: Annotated[bool, typer.Option("--open/--no-open", help="Open in the system viewer (needs a desktop session).")] = True,
    browser: Annotated[bool, typer.Option("--browser", help="Open in the web browser instead of the image viewer.")] = False,
    as_json: JsonOpt = False,
):
    """Show metadata for a screenshot and open it in an image viewer or browser.

    \b
    Examples:
      codeprint preview 12
      codeprint preview screenshot 12
      codeprint preview latest --browser
      codeprint preview --file ./output/program.png --no-open
    """
    with guard(as_json):
        args = list(target or [])
        if args and args[0].lower() == "page":
            if len(args) != 2:
                raise CodePrintError("BAD_OPTIONS", "Usage: codeprint preview page <id>", EXIT_USAGE)
            return page_preview(args[1], open_, browser, as_json)
        if args and args[0].lower() == "screenshot":
            args = args[1:]
        info = None
        if file is not None:
            if not Path(file).is_file():
                raise CodePrintError("FILE_NOT_FOUND", f"File not found: {file}", EXIT_NOT_FOUND)
            path, info, recovered = Path(file).resolve(), {"image_path": str(Path(file).resolve())}, False
            info.update(_image_info(path))
        else:
            if len(args) != 1:
                raise CodePrintError("BAD_OPTIONS", "Give a screenshot id, 'latest', or --file PATH.", EXIT_USAGE)
            store = open_store()
            try:
                sid = parse_id(args[0], "screenshot", store)
                shot = store.get_screenshot(sid)
                path, recovered = ensure_image(store, shot)
                info = {**shot_summary(store.get_screenshot(sid)), **_image_info(path)}
            finally:
                store.close()
            if recovered:
                warn(f"image of screenshot {sid} was missing/corrupt and has been rebuilt from saved data")
        opened, msg = (False, "Not opened (--no-open).")
        if open_:
            opened, msg = open_file(path, browser)
        if as_json:
            emit({"preview": info, "opened": opened, "message": msg})
            return
        for k, v in info.items():
            typer.echo(f"{k:>12}: {'-' if v is None else v}")
        typer.echo(msg if not opened else f"{msg}")
        if open_ and not opened:
            typer.echo(f"Image file: {path}")


# ----------------------------------------------------------------- screenshots ---
@shot_app.command("list")
def shot_list(as_json: JsonOpt = False):
    """List saved screenshots."""
    with guard(as_json):
        store = open_store()
        try:
            shots = store.list_screenshots()
            pages = {s["id"]: [p["id"] for p in store.pages_for_screenshot(s["id"])] for s in shots}
        finally:
            store.close()
        if as_json:
            emit({"screenshots": [{**shot_summary(s), "pages": pages[s["id"]]} for s in shots]})
            return
        if not shots:
            typer.echo("No screenshots yet. Try: codeprint generate samples/hello.py")
            return
        rows = [[s["id"], clip(s["title"], 40), s["language"], s["mode"], s["status"],
                 f"{s['width_px']}x{s['height_px']}", s["created_at"][:16].replace("T", " "),
                 ",".join(map(str, pages[s["id"]])) or "-", "ok" if image_path(s).exists() else "MISSING"]
                for s in shots]
        typer.echo(table(["ID", "TITLE", "LANG", "MODE", "RUN", "SIZE(px)", "CREATED(UTC)", "PAGES", "IMAGE"], rows))


@shot_app.command("show")
def shot_show(
    id: Annotated[str, typer.Argument(help="Screenshot id or 'latest'.")],
    full: Annotated[bool, typer.Option("--full", help="Print the complete source and output.")] = False,
    as_json: JsonOpt = False,
):
    """Show details of one screenshot (metadata, source and output)."""
    with guard(as_json):
        store = open_store()
        try:
            sid = parse_id(id, "screenshot", store)
            shot = store.get_screenshot(sid)
            pages = store.pages_for_screenshot(sid)
        finally:
            store.close()
        if as_json:
            emit({"screenshot": {**shot_summary(shot), "pages": [p["id"] for p in pages],
                                 "source_code": shot["source_code"], "stdout": shot["stdout"],
                                 "stderr": shot["stderr"], "compile_output": shot["compile_output"],
                                 "input_text": shot["input_text"], "settings": json.loads(shot["settings_json"])}})
            return
        for k, v in shot_summary(shot).items():
            typer.echo(f"{k:>12}: {'-' if v is None else v}")
        typer.echo(f"{'pages':>12}: {', '.join(str(p['id']) for p in pages) or '-'}")

        def block(label, text):
            if not text:
                return
            lines = text.rstrip("\n").split("\n")
            shown = lines if full else lines[:15]
            typer.echo(f"\n--- {label} ---")
            typer.echo("\n".join(shown))
            if len(shown) < len(lines):
                typer.echo(f"... ({len(lines) - len(shown)} more lines; use --full)")
        block("source", shot["source_code"])
        block("compiler output", shot["compile_output"])
        block("stdout", shot["stdout"])
        block("stderr", shot["stderr"])


@shot_app.command("rename")
def shot_rename(id: Annotated[str, typer.Argument()], title: Annotated[str, typer.Argument(help="New title.")],
                as_json: JsonOpt = False):
    """Rename a screenshot in the library (the title is not drawn on the image)."""
    with guard(as_json):
        if not title.strip():
            raise CodePrintError("EMPTY_TITLE", "Title cannot be empty.", EXIT_USAGE)
        store = open_store()
        try:
            sid = parse_id(id, "screenshot", store)
            shot = store.get_screenshot(sid)
            store.update_screenshot(sid, title=title)
        finally:
            store.close()
        if as_json:
            emit({"id": sid, "title": title})
        else:
            typer.echo(f"Screenshot #{sid} renamed to \"{title}\".")


@shot_app.command("delete")
def shot_delete(id: Annotated[str, typer.Argument()],
                yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
                as_json: JsonOpt = False):
    """Delete a screenshot and its image file (it is also removed from any page)."""
    with guard(as_json):
        store = open_store()
        try:
            sid = parse_id(id, "screenshot", store)
            shot = store.get_screenshot(sid)
            pages = store.pages_for_screenshot(sid)
            note = f" It will be removed from page(s) {', '.join(str(p['id']) for p in pages)}." if pages else ""
            if not confirm(f"Delete screenshot #{sid} \"{shot['title']}\"?{note}", yes):
                typer.echo("Cancelled.")
                return
            path = image_path(shot)                                   # validates the file name
            managed = cp_config.images_dir().resolve()
            if path.parent.resolve() != managed:
                raise CodePrintError("UNSAFE_PATH", f"Refusing to delete {path}: outside {managed}")
            store.delete_screenshot(sid)
            path.unlink(missing_ok=True)
        finally:
            store.close()
        emit({"deleted": sid}) if as_json else typer.echo(f"Deleted screenshot #{sid}.")


@shot_app.command("export")
def shot_export(
    id: Annotated[str, typer.Argument()],
    fmt: Annotated[str, typer.Option("--format", "-f", help="png")] = "png",
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Destination file.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite without asking.")] = False,
    as_json: JsonOpt = False,
):
    """Export one screenshot as a PNG file.

    \b
    Example: codeprint screenshot export 1 --format png --output exp1.png
    """
    with guard(as_json):
        if fmt.lower() != "png":
            raise CodePrintError("INVALID_FORMAT", "Screenshots can be exported as png only.", EXIT_USAGE)
        cfg = load_config()
        store = open_store()
        try:
            sid = parse_id(id, "screenshot", store)
            shot = store.get_screenshot(sid)
            src, recovered = ensure_image(store, shot)
        finally:
            store.close()
        dest = output or default_output("png", cfg, f"screenshot_{sid}_{slugify(shot['title'])}.png")
        dest = check_output_path(dest, force, confirm_overwrite)
        cp_config.atomic_write_bytes(dest, src.read_bytes())
        if recovered:
            warn("image was missing/corrupt and has been rebuilt from saved data")
        emit({"file": str(dest), "recovered": recovered}) if as_json else typer.echo(f"Exported {dest}")


@shot_app.command("regenerate")
def shot_regenerate(
    id: Annotated[str, typer.Argument()],
    font_size: Annotated[Optional[float], typer.Option("--font-size")] = None,
    columns: Annotated[Optional[int], typer.Option("--columns")] = None,
    line_numbers: Annotated[Optional[bool], typer.Option("--line-numbers/--no-line-numbers")] = None,
    highlight: Annotated[Optional[bool], typer.Option("--highlight/--no-highlight")] = None,
    wrap: Annotated[Optional[bool], typer.Option("--wrap/--no-wrap")] = None,
    width_mode: Annotated[Optional[str], typer.Option("--width-mode")] = None,
    engine: Annotated[Optional[str], typer.Option("--engine")] = None,
    as_json: JsonOpt = False,
):
    """Re-render a screenshot from the source/output saved in the database (program is not re-run).

    Also repairs a missing or corrupted image. Refused if the new size would no longer fit
    on a page that uses the screenshot.
    """
    with guard(as_json):
        overrides = {k: v for k, v in dict(font_size=font_size, columns=columns, line_numbers=line_numbers,
                                           highlight=highlight, wrap=wrap, width_mode=width_mode,
                                           engine=engine).items() if v is not None}
        overrides = {k: cp_config.coerce(k, v) for k, v in overrides.items()}
        store = open_store()
        try:
            sid = parse_id(id, "screenshot", store)
            out = regenerate_screenshot(store, sid, overrides)
        finally:
            store.close()
        r = out["result"]
        for w in r.warnings:
            warn(w)
        if as_json:
            emit({"id": sid, "width_px": r.width_px, "height_px": r.height_px, "engine": r.engine})
        else:
            typer.echo(f"Regenerated screenshot #{sid} ({r.width_px} x {r.height_px} px, engine {r.engine}).")


# ----------------------------------------------------------------------- pages ---
@page_app.command("create")
def page_create(
    title: Annotated[str, typer.Argument(help="Page title.")],
    orientation: Annotated[Optional[str], typer.Option("--orientation", help="portrait or landscape.")] = None,
    margin: Annotated[Optional[float], typer.Option("--margin", help="Margin in mm.")] = None,
    spacing: Annotated[Optional[float], typer.Option("--spacing", help="Gap between screenshots in mm.")] = None,
    layout: Annotated[Optional[str], typer.Option("--layout", help="compact or grid.")] = None,
    grid_columns: Annotated[Optional[int], typer.Option("--grid-columns")] = None,
    rotate: Annotated[Optional[bool], typer.Option("--rotate/--no-rotate", help="Allow 90-degree rotation (compact).")] = None,
    min_font_pt: Annotated[Optional[float], typer.Option("--min-font-pt", help="Smallest allowed printed code size.")] = None,
    as_json: JsonOpt = False,
):
    """Create an empty A4 page (layout defaults come from `codeprint config`)."""
    with guard(as_json):
        if not title.strip():
            raise CodePrintError("EMPTY_TITLE", "Title cannot be empty.", EXIT_USAGE)
        cfg = load_config()
        spec = page_spec_from_options(cfg, orientation, margin, spacing, layout, grid_columns, rotate, min_font_pt)
        store = open_store()
        try:
            pid = store.create_page(title, spec)
        finally:
            store.close()
        emit({"page_id": pid, "title": title}) if as_json else typer.echo(
            f"Created page {pid} \"{title}\" (A4 {spec.orientation}, layout {spec.mode}).")


@page_app.command("list")
def page_list(as_json: JsonOpt = False):
    """List saved pages."""
    with guard(as_json):
        store = open_store()
        try:
            pages = store.list_pages()
            infos = []
            for p in pages:
                try:
                    lay = store.page_layout(p["id"])
                    util, err = round(lay.utilization_pct, 1), None
                except CodePrintError as exc:
                    util, err = None, exc.code
                infos.append((p, util, err))
        finally:
            store.close()
        if as_json:
            emit({"pages": [{"id": p["id"], "title": p["title"], "items": p["item_count"],
                             "orientation": p["orientation"], "layout_mode": p["layout_mode"],
                             "utilization_pct": u, "layout_error": e} for p, u, e in infos]})
            return
        if not pages:
            typer.echo("No pages yet. Try: codeprint page create \"Experiment 01\"")
            return
        typer.echo(table(["ID", "TITLE", "ITEMS", "ORIENT", "LAYOUT", "UTIL"],
                         [[p["id"], clip(p["title"], 40), p["item_count"], p["orientation"], p["layout_mode"],
                           f"{u}%" if u is not None else e] for p, u, e in infos]))


@page_app.command("show")
def page_show(id: Annotated[str, typer.Argument(help="Page id or 'latest'.")], as_json: JsonOpt = False):
    """Show a page: its screenshots, positions on the sheet, utilization and remaining space."""
    with guard(as_json):
        store = open_store()
        try:
            pid = parse_id(id, "page", store)
            page = store.get_page(pid)
            rows = store.page_items(pid)
            layout = store.page_layout(pid, rows)
        finally:
            store.close()
        data = layout_dict(page, layout, rows)
        if as_json:
            emit({"page": data})
            return
        typer.echo(page_header(page, layout))
        if not rows:
            typer.echo("  (empty)")
        else:
            typer.echo(table(["POS", "SHOT", "TITLE", "X(mm)", "Y(mm)", "W(mm)", "H(mm)", "SCALE", "ROT"],
                             [[i["position"], f"#{i['screenshot_id']}", clip(i["title"], 28), i["x_mm"], i["y_mm"],
                               i["w_mm"], i["h_mm"], f"{i['scale_pct']}%", "yes" if i["rotated"] else "-"]
                              for i in data["items"]]))
        typer.echo(capacity_line(layout))


@page_app.command("rename")
def page_rename(id: Annotated[str, typer.Argument()], title: Annotated[str, typer.Argument()], as_json: JsonOpt = False):
    """Rename a page."""
    with guard(as_json):
        if not title.strip():
            raise CodePrintError("EMPTY_TITLE", "Title cannot be empty.", EXIT_USAGE)
        store = open_store()
        try:
            pid = parse_id(id, "page", store)
            store.rename_page(pid, title)
        finally:
            store.close()
        emit({"page_id": pid, "title": title}) if as_json else typer.echo(f"Page {pid} renamed to \"{title}\".")


@page_app.command("add")
def page_add(
    screenshot_ids: Annotated[list[str], typer.Argument(help="One or more screenshot ids (or 'latest').")],
    page: Annotated[str, typer.Option("--page", "-p", help="Target page id (default: latest page).")] = "latest",
    position: Annotated[Optional[int], typer.Option("--position", help="1-based slot (default: end).")] = None,
    auto_page: Annotated[bool, typer.Option("--auto-page", help="If the page is full, start a new page automatically.")] = False,
    as_json: JsonOpt = False,
):
    """Add screenshots to a page. Rejected with CAPACITY_MAX (exit 4) if they do not fit.

    \b
    Examples:
      codeprint page add 1 --page 1
      codeprint page add 2 3 4 --page 1 --auto-page
    """
    with guard(as_json):
        store = open_store()
        results = []
        try:
            pid = parse_id(page, "page", store)
            for raw in screenshot_ids:
                sid = parse_id(raw, "screenshot", store)
                try:
                    used, layout, created = add_with_capacity(store, pid, sid, position, auto_page)
                except CodePrintError as exc:
                    if results:
                        exc.message += f" (Already added: {', '.join('#' + str(r['screenshot_id']) for r in results)}.)"
                    exc.details.update(screenshot_id=sid, page_id=pid)
                    raise
                if created:
                    pid = used            # following screenshots continue on the new page
                    if not as_json:
                        typer.echo(f"Page was full; created page {created}.")
                results.append({"screenshot_id": sid, "page_id": used, "new_page": created,
                                "utilization_pct": round(layout.utilization_pct, 1),
                                "remaining_height_mm": round(layout.remaining_height_mm, 1)})
                position = None
        finally:
            store.close()
        if as_json:
            emit({"added": results})
        else:
            for r in results:
                typer.echo(f"Added screenshot #{r['screenshot_id']} to page {r['page_id']}. "
                           f"Utilization {r['utilization_pct']}%; {r['remaining_height_mm']} mm of height remaining.")


@page_app.command("remove")
def page_remove(screenshot_id: Annotated[str, typer.Argument()],
                page: Annotated[str, typer.Option("--page", "-p", help="Page id.")] = "latest",
                as_json: JsonOpt = False):
    """Remove a screenshot from a page (the screenshot stays in the library)."""
    with guard(as_json):
        store = open_store()
        try:
            pid = parse_id(page, "page", store)
            sid = parse_id(screenshot_id, "screenshot", store)
            layout = store.remove_from_page(pid, sid)
        finally:
            store.close()
        if as_json:
            emit({"removed": sid, "page_id": pid, "utilization_pct": round(layout.utilization_pct, 1)})
        else:
            typer.echo(f"Removed screenshot #{sid} from page {pid}. {capacity_line(layout)}")


@page_app.command("reorder")
def page_reorder(screenshot_id: Annotated[str, typer.Argument()],
                 page: Annotated[str, typer.Option("--page", "-p", help="Page id.")] = "latest",
                 position: Annotated[int, typer.Option("--position", help="New 1-based position.")] = 1,
                 as_json: JsonOpt = False):
    """Move a screenshot to another position on its page.

    \b
    Example: codeprint page reorder 3 --page 1 --position 1
    """
    with guard(as_json):
        store = open_store()
        try:
            pid = parse_id(page, "page", store)
            sid = parse_id(screenshot_id, "screenshot", store)
            layout = store.move_in_page(pid, sid, position)
        finally:
            store.close()
        if as_json:
            emit({"moved": sid, "page_id": pid, "position": position})
        else:
            typer.echo(f"Moved screenshot #{sid} to position {position} on page {pid}. {capacity_line(layout)}")


@page_app.command("set")
def page_set(
    id: Annotated[str, typer.Argument()],
    orientation: Annotated[Optional[str], typer.Option("--orientation")] = None,
    margin: Annotated[Optional[float], typer.Option("--margin", help="mm")] = None,
    spacing: Annotated[Optional[float], typer.Option("--spacing", help="mm")] = None,
    layout: Annotated[Optional[str], typer.Option("--layout", help="compact or grid")] = None,
    grid_columns: Annotated[Optional[int], typer.Option("--grid-columns")] = None,
    rotate: Annotated[Optional[bool], typer.Option("--rotate/--no-rotate")] = None,
    min_font_pt: Annotated[Optional[float], typer.Option("--min-font-pt")] = None,
    as_json: JsonOpt = False,
):
    """Change a page's layout settings. Refused (nothing saved) if its screenshots would no longer fit."""
    with guard(as_json):
        store = open_store()
        try:
            pid = parse_id(id, "page", store)
            page = store.get_page(pid)
            spec = page_spec_from_options({}, orientation, margin, spacing, layout, grid_columns, rotate,
                                          min_font_pt, base=spec_from_page(page))
            store.update_page_layout(pid, spec)
            lay = store.page_layout(pid)
        finally:
            store.close()
        emit({"page_id": pid, "utilization_pct": round(lay.utilization_pct, 1)}) if as_json else \
            typer.echo(f"Updated page {pid}. {capacity_line(lay)}")


def page_preview(id: str, open_: bool, browser: bool, as_json: bool):
    store = open_store()
    try:
        pid = parse_id(id, "page", store)
        page = store.get_page(pid)
        rows = store.page_items(pid)
        out, layout, recovered = preview_page_png(store, pid)
    finally:
        store.close()
    if recovered:
        warn(f"rebuilt missing/corrupt image(s) for screenshot(s) {recovered}")
    opened, msg = (False, "Not opened (--no-open).")
    if open_:
        opened, msg = open_file(out, browser)
    if as_json:
        emit({"page": layout_dict(page, layout, rows), "preview_image": str(out), "opened": opened,
              "message": msg})
        return
    typer.echo(page_header(page, layout))
    for p in layout.placements:
        typer.echo(f"  #{p.item_id:<4} at ({p.x_mm:6.1f}, {p.y_mm:6.1f}) mm  size {p.w_mm:6.1f} x {p.h_mm:6.1f} mm"
                   f"  scale {p.scale * 100:5.1f}%{'  rotated' if p.rotated else ''}")
    typer.echo(capacity_line(layout))
    typer.echo(f"Preview image: {out}")
    typer.echo(msg)


@page_app.command("preview")
def page_preview_cmd(
    id: Annotated[str, typer.Argument(help="Page id or 'latest'.")],
    open_: Annotated[bool, typer.Option("--open/--no-open", help="Open the preview in the system viewer.")] = True,
    browser: Annotated[bool, typer.Option("--browser", help="Open in the web browser.")] = False,
    as_json: JsonOpt = False,
):
    """Render the real A4 layout (margins dashed blue, screenshot ids in red) and show utilization.

    The preview uses the same layout engine as the PDF export.
    """
    with guard(as_json):
        page_preview(id, open_, browser, as_json)


def _do_export(store: Store, page_ids: list, fmt: str, output: Path | None, force: bool, as_json: bool, cfg: dict):
    fmt = fmt.lower()
    if fmt not in ("pdf", "png"):
        raise CodePrintError("INVALID_FORMAT", "Format must be pdf or png.", EXIT_USAGE)
    if output is None:
        if len(page_ids) == 1:
            page = store.get_page(page_ids[0])
            name = f"page_{page_ids[0]}_{slugify(page['title'])}.{fmt}"
        else:
            name = f"codeprint_pages.{fmt}"
        output = default_output(fmt, cfg, name)
    output = Path(output).expanduser()
    if output.is_dir():
        raise CodePrintError("OUTPUT_IS_DIRECTORY", f"{output} is a directory; give a file name.", EXIT_USAGE)
    if output.suffix.lower() != f".{fmt}":
        output = output.with_name(output.name + f".{fmt}")
    targets = output_targets(output, fmt, page_ids)
    for t in targets:
        check_output_path(t, force, confirm_overwrite)
    out = export_pages(store, page_ids, fmt, output)
    if as_json:
        emit(out)
        return
    if out["recovered_screenshots"]:
        warn(f"rebuilt missing/corrupt image(s) for screenshot(s) {out['recovered_screenshots']}")
    for p in out["pages"]:
        typer.echo(f"Page {p['page_id']} \"{p['title']}\": {p['items']} screenshot(s), utilization {p['utilization_pct']}%")
    typer.echo("Exported: " + ", ".join(out["files"]))


@page_app.command("export")
def page_export(
    id: Annotated[str, typer.Argument(help="Page id or 'latest'.")],
    fmt: Annotated[str, typer.Option("--format", "-f", help="pdf (default) or png (300 dpi).")] = "pdf",
    output: Annotated[Optional[Path], typer.Option("--output", "-o", help="Destination file.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite without asking.")] = False,
    as_json: JsonOpt = False,
):
    """Export one page as a print-ready A4 PDF (or a 300 dpi PNG).

    \b
    Example: codeprint page export 1 --format pdf --output experiment1.pdf
    """
    with guard(as_json):
        cfg = load_config()
        store = open_store()
        try:
            pid = parse_id(id, "page", store)
            _do_export(store, [pid], fmt, output, force, as_json, cfg)
        finally:
            store.close()


@page_app.command("delete")
def page_delete(id: Annotated[str, typer.Argument()],
                yes: Annotated[bool, typer.Option("--yes", "-y")] = False, as_json: JsonOpt = False):
    """Delete a page. Its screenshots stay in the library."""
    with guard(as_json):
        store = open_store()
        try:
            pid = parse_id(id, "page", store)
            page = store.get_page(pid)
            if not confirm(f"Delete page {pid} \"{page['title']}\"? (its screenshots are kept)", yes):
                typer.echo("Cancelled.")
                return
            store.delete_page(pid)
        finally:
            store.close()
        emit({"deleted": pid}) if as_json else typer.echo(f"Deleted page {pid}.")


@app.command("export")
def export_cmd(
    pages: Annotated[Optional[str], typer.Option("--pages", help="Comma-separated page ids, e.g. 1,2,3.")] = None,
    all_pages: Annotated[bool, typer.Option("--all-pages", help="Export every page.")] = False,
    fmt: Annotated[str, typer.Option("--format", "-f", help="pdf (one multi-page file) or png.")] = "pdf",
    output: Annotated[Optional[Path], typer.Option("--output", "-o")] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite without asking.")] = False,
    as_json: JsonOpt = False,
):
    """Export several pages as one multi-page PDF.

    \b
    Examples:
      codeprint export --pages 1,2,3 --format pdf
      codeprint export --all-pages --format pdf --output lab-record.pdf
    """
    with guard(as_json):
        if bool(pages) == all_pages:
            raise CodePrintError("BAD_OPTIONS", "Give exactly one of --pages or --all-pages.", EXIT_USAGE)
        cfg = load_config()
        store = open_store()
        try:
            if all_pages:
                ids = [p["id"] for p in store.list_pages()]
                if not ids:
                    raise CodePrintError("PAGE_NOT_FOUND", "There are no pages to export.", EXIT_NOT_FOUND)
            else:
                ids = [parse_id(x, "page", store) for x in pages.split(",") if x.strip()]
                for i in ids:
                    store.get_page(i)
                if not ids:
                    raise CodePrintError("BAD_OPTIONS", "--pages needs at least one id.", EXIT_USAGE)
            _do_export(store, ids, fmt, output, force, as_json, cfg)
        finally:
            store.close()


# ---------------------------------------------------------------------- config ---
@config_app.command("show")
def config_show(as_json: JsonOpt = False):
    """Show effective settings (defaults overridden by the config file).

    Precedence: command-line flag > config file > built-in default.
    """
    with guard(as_json):
        stored = cp_config.load_user_config()
        eff = load_config()
        if as_json:
            emit({"config_file": str(cp_config.config_path()), "values": eff, "from_file": sorted(stored)})
            return
        typer.echo(f"Config file: {cp_config.config_path()}"
                   f"{'' if cp_config.config_path().exists() else ' (not created yet)'}")
        typer.echo(table(["KEY", "VALUE", "SOURCE", "DESCRIPTION"],
                         [[k, eff[k], "file" if k in stored else "default", SCHEMA[k]["help"]] for k in SCHEMA]))


@config_app.command("set")
def config_set(key: Annotated[str, typer.Argument()], value: Annotated[str, typer.Argument()], as_json: JsonOpt = False):
    """Save a setting, e.g.  codeprint config set font_size 13"""
    with guard(as_json):
        val = cp_config.set_config_value(key, value)
        emit({"key": key, "value": val}) if as_json else typer.echo(f"{key} = {val}")


@config_app.command("reset")
def config_reset(yes: Annotated[bool, typer.Option("--yes", "-y")] = False):
    """Delete the config file (back to built-in defaults)."""
    with guard():
        if confirm("Delete the config file and return to defaults?", yes):
            cp_config.reset_config()
            typer.echo("Configuration reset.")


@config_app.command("path")
def config_path_cmd():
    """Print the config file, data directory and database locations."""
    typer.echo(f"config: {cp_config.config_path()}\ndata:   {cp_config.data_dir()}\n"
               f"db:     {cp_config.db_path()}\nimages: {cp_config.images_dir()}")


# -------------------------------------------------------- doctor / setup ---
def _check_modules() -> list:
    out = []
    for mod, required in (("typer", True), ("pygments", True), ("PIL", True), ("reportlab", True),
                          ("playwright", False)):
        try:
            m = __import__(mod)
            out.append({"name": f"python:{mod}", "ok": True, "required": required,
                        "detail": getattr(m, "__version__", None) or getattr(m, "Version", "") or "installed"})
        except ImportError:
            out.append({"name": f"python:{mod}", "ok": False, "required": required,
                        "detail": "not installed (pip install -r requirements.txt)"})
    return out


def run_doctor() -> dict:
    from cp_render import find_font_files, playwright_available
    checks = [{"name": "python", "ok": sys.version_info >= (3, 9), "required": True,
               "detail": sys.version.split()[0]}]
    checks += _check_modules()
    try:
        _, fam, w = find_font_files(load_config()["font_family"])
        checks.append({"name": "font", "ok": True, "required": True, "detail": fam + (f" ({w})" if w else "")})
    except CodePrintError as exc:
        checks.append({"name": "font", "ok": False, "required": True, "detail": exc.message})
    checks.append({"name": "engine:pillow", "ok": True, "required": True, "detail": "always available"})
    try:
        pw = playwright_available()
    except Exception:
        pw = False
    checks.append({"name": "engine:playwright", "ok": pw, "required": False,
                   "detail": "Chromium found" if pw else "no browser (run: playwright install chromium); Pillow is used"})
    try:
        ensure_dirs()
        for d in (cp_config.data_dir(), cp_config.config_dir()):
            tempfile.NamedTemporaryFile(dir=d).close()
        Store().close()
        checks.append({"name": "storage", "ok": True, "required": True, "detail": str(cp_config.data_dir())})
    except Exception as exc:
        checks.append({"name": "storage", "ok": False, "required": True, "detail": str(exc)})
    for lang in LANGUAGES.values():
        miss = missing_tools(lang)
        tools = ", ".join(find_tool(t) or t for t in lang.tools) if not miss else ""
        checks.append({"name": f"lang:{lang.key}", "ok": not miss, "required": False,
                       "detail": tools if not miss else f"missing: {', '.join(miss)}"})
    return {"ok": all(c["ok"] for c in checks if c["required"]), "checks": checks}


@app.command()
def doctor(as_json: JsonOpt = False):
    """Check dependencies: Python packages, fonts, renderer, storage and language toolchains."""
    with guard(as_json):
        res = run_doctor()
        if as_json:
            emit(res)
        else:
            for c in res["checks"]:
                tag = "ok" if c["ok"] else ("MISSING" if c["required"] else "optional")
                typer.echo(f"[{tag:^8}] {c['name']:<20} {c['detail']}")
            typer.echo("\nAll required checks passed." if res["ok"] else "\nSome required checks FAILED.")
        if not res["ok"]:
            raise typer.Exit(5)


@app.command()
def setup(as_json: JsonOpt = False):
    """First-run setup: create folders, database, default config; verify rendering and PDF export.

    Safe to run repeatedly; an existing config file is never overwritten.
    """
    with guard(as_json):
        ensure_dirs()
        Store().close()
        created = False
        if not cp_config.config_path().exists():
            # empty file = "use built-in defaults", so future default changes apply automatically
            cp_config.atomic_write_bytes(cp_config.config_path(), b"{}\n")
            created = True
        cfg = load_config()
        with tempfile.TemporaryDirectory(prefix="codeprint-setup-") as tmp:
            png = Path(tmp) / "t.png"
            spec = RenderSpec(build_sections("code_only", "print('ok')\n", "python", {}), cfg)
            res = render_png(spec, png)
            layout = compute_layout(PageSpec(), [LayoutItem(1, res.width_px / res.scale * 25.4 / 96,
                                                           res.height_px / res.scale * 25.4 / 96, 10.5)])
            pdf = Path(tmp) / "t.pdf"
            write_pdf(pdf, [(layout, {1: png})])
            if not pdf.read_bytes().startswith(b"%PDF"):
                raise CodePrintError("PDF_FAILED", "PDF export self-test produced an invalid file.", 5)
        info = {"data_dir": str(cp_config.data_dir()), "config": str(cp_config.config_path()),
                "config_created": created, "render_engine": res.engine, "pdf": "ok"}
        if as_json:
            emit(info)
        else:
            typer.echo(f"Data directory : {info['data_dir']}")
            typer.echo(f"Config file    : {info['config']} ({'created' if created else 'kept'})")
            typer.echo(f"Rendering      : ok (engine {res.engine})")
            typer.echo("PDF export     : ok")
            typer.echo("Setup complete. Try: codeprint doctor")


def _version(value: bool):
    if value:
        typer.echo(f"codeprint {__version__}")
        raise typer.Exit()


@app.callback()
def _main(version: Annotated[Optional[bool], typer.Option("--version", callback=_version, is_eager=True,
                                                         help="Show version and exit.")] = None):
    pass


def main():
    app()


if __name__ == "__main__":
    main()
