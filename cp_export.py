"""Screenshot regeneration/recovery and page export (PDF / PNG / preview).

Preview, PNG and PDF all draw the placements returned by cp_layout.compute_layout,
so they cannot disagree about where things go.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import cp_config
from cp_config import CodePrintError, EXIT_ENV, EXIT_EXISTS, EXIT_USAGE, atomic_write_bytes
from cp_layout import A4_MM
from cp_render import RenderSpec, build_sections, render_png
from cp_runner import LANGUAGES
from cp_store import Store, item_from_row

# settings captured with each screenshot so it can be regenerated identically
RENDER_KEYS = ["engine", "font_family", "font_size", "scale", "line_numbers", "highlight",
               "highlight_style", "columns", "wrap", "width_mode", "min_columns", "tab_width",
               "padding", "section_spacing", "max_output_kb"]


def slugify(text: str, fallback: str = "item") -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return s[:60] or fallback


# ------------------------------------------------------- screenshot images ---
def image_path(shot: dict) -> Path:
    name = shot["image_file"]
    if name != os.path.basename(name) or name in ("", ".", ".."):
        raise CodePrintError("UNSAFE_PATH", f"Refusing suspicious image file name in database: {name!r}")
    return cp_config.images_dir() / name


def image_is_valid(shot: dict) -> bool:
    p = image_path(shot)
    try:
        with Image.open(p) as im:
            im.verify()
        with Image.open(p) as im:
            return im.size == (shot["width_px"], shot["height_px"])
    except Exception:
        return False


def spec_from_record(shot: dict, overrides: dict | None = None) -> RenderSpec:
    saved = json.loads(shot["settings_json"])
    saved.pop("show_title", None)          # keys written by older versions
    saved.pop("show_input", None)
    settings = {**cp_config.defaults(), **saved, **(overrides or {})}
    rec = dict(status=shot["status"], exit_code=shot["exit_code"], stdout=shot["stdout"],
               stderr=shot["stderr"], compile_output=shot["compile_output"],
               timeout_s=shot["timeout_s"], truncated=bool(shot["truncated"]),
               input_text=shot["input_text"],
               max_output_kb=settings["max_output_kb"])
    lexer = LANGUAGES[shot["language"]].lexer if shot["language"] in LANGUAGES else "text"
    sections = build_sections(shot["mode"], shot["source_code"], lexer, rec)
    return RenderSpec(sections, settings)


def regenerate_screenshot(store: Store, sid: int, overrides: dict | None = None) -> dict:
    """Re-render a screenshot from the data saved in SQLite (the program is NOT re-run).

    The new image must still fit on every page that uses it; otherwise nothing changes.
    """
    shot = store.get_screenshot(sid)
    spec = spec_from_record(shot, overrides)
    final = image_path(shot)
    tmp = final.with_name(f".regen-{sid}.png")
    result = render_png(spec, tmp)
    try:
        new = dict(shot, width_px=result.width_px, height_px=result.height_px,
                   scale=result.scale, font_px=result.font_px)
        for page in store.pages_for_screenshot(sid):
            rows = [new if r["id"] == sid else r for r in store.page_items(page["id"])]
            store.page_layout(page["id"], rows)          # CapacityError -> keep the old image
        saved = {k: spec.settings[k] for k in RENDER_KEYS}
        os.replace(tmp, final)
        store.update_screenshot(sid, width_px=result.width_px, height_px=result.height_px,
                                scale=result.scale, font_px=result.font_px, engine=result.engine,
                                settings_json=json.dumps(saved))
    finally:
        if tmp.exists():
            tmp.unlink()
    return {"result": result, "screenshot": store.get_screenshot(sid)}


def ensure_image(store: Store, shot: dict) -> tuple:
    """Return (path, recovered: bool); rebuild a missing/corrupt image from saved data."""
    if image_is_valid(shot):
        return image_path(shot), False
    try:
        regenerate_screenshot(store, shot["id"])
    except CodePrintError as exc:
        raise CodePrintError("IMAGE_RECOVERY_FAILED",
                             f"Image for screenshot {shot['id']} is missing/corrupt and could not be "
                             f"rebuilt: {exc.message}", exc.exit_code)
    return image_path(shot), True


# ---------------------------------------------------------------- pages ----
def _mm_px(mm: float, dpi: float) -> int:
    return round(mm / 25.4 * dpi)


def collect_page(store: Store, pid: int):
    """Return (page_row, layout, {screenshot_id: Path}, recovered_ids)."""
    page = store.get_page(pid)
    rows = store.page_items(pid)
    layout = store.page_layout(pid, rows)
    paths, recovered = {}, []
    for r in rows:
        p, rec = ensure_image(store, r)
        paths[r["id"]] = p
        if rec:
            recovered.append(r["id"])
    # recovery may have changed dimensions in rare cases, so lay out again from fresh rows
    if recovered:
        layout = store.page_layout(pid)
    return page, layout, paths, recovered


def page_raster(layout, paths: dict, dpi: float, guides: bool = False) -> Image.Image:
    spec = layout.spec
    W, H = _mm_px(spec.page_w, dpi), _mm_px(spec.page_h, dpi)
    canvas = Image.new("RGB", (W, H), "white")
    for p in layout.placements:
        with Image.open(paths[p.item_id]) as im:
            im = im.convert("RGB")
            if p.rotated:
                im = im.rotate(90, expand=True)
            size = (max(1, _mm_px(p.w_mm, dpi)), max(1, _mm_px(p.h_mm, dpi)))
            canvas.paste(im.resize(size, Image.LANCZOS), (_mm_px(p.x_mm, dpi), _mm_px(p.y_mm, dpi)))
    if guides:
        d = ImageDraw.Draw(canvas)
        m = _mm_px(spec.margin_mm, dpi)
        for x in range(m, W - m, 12):                         # dashed printable-area outline
            d.line([(x, m), (min(x + 6, W - m), m)], fill="#4a90d9")
            d.line([(x, H - m), (min(x + 6, W - m), H - m)], fill="#4a90d9")
        for y in range(m, H - m, 12):
            d.line([(m, y), (m, min(y + 6, H - m))], fill="#4a90d9")
            d.line([(W - m, y), (W - m, min(y + 6, H - m))], fill="#4a90d9")
        try:
            font = ImageFont.load_default(size=max(12, round(dpi / 7)))
        except TypeError:
            font = ImageFont.load_default()
        for p in layout.placements:
            x0, y0 = _mm_px(p.x_mm, dpi), _mm_px(p.y_mm, dpi)
            x1, y1 = x0 + _mm_px(p.w_mm, dpi), y0 + _mm_px(p.h_mm, dpi)
            d.rectangle([x0, y0, x1, y1], outline="#d9534f", width=2)
            label = f"#{p.item_id}"
            tb = d.textbbox((0, 0), label, font=font)
            d.rectangle([x0, y0, x0 + tb[2] + 8, y0 + tb[3] + 6], fill="#d9534f")
            d.text((x0 + 4, y0 + 2), label, font=font, fill="white")
        d.rectangle([0, 0, W - 1, H - 1], outline="#999999")
    return canvas


def write_page_png(path: Path, layout, paths: dict, dpi: float = 300, guides: bool = False) -> None:
    img = page_raster(layout, paths, dpi, guides)
    fd, tmp = tempfile.mkstemp(dir=str(Path(path).parent), prefix=".cp-", suffix=".png")
    os.close(fd)
    try:
        img.save(tmp, format="PNG", dpi=(dpi, dpi))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def preview_page_png(store: Store, pid: int) -> tuple:
    """Render the on-screen preview (A4 with margin guides) and return (path, layout, recovered)."""
    page, layout, paths, recovered = collect_page(store, pid)
    out = cp_config.previews_dir() / f"page_{pid}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_page_png(out, layout, paths, dpi=100, guides=True)
    return out, layout, recovered


def write_pdf(path: Path, pages: list, title: str = "CodePrint") -> None:
    """`pages` = [(layout, paths)]. Images are embedded losslessly at their placed size."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cp-", suffix=".pdf")
    os.close(fd)
    try:
        c = canvas.Canvas(tmp, pagesize=(A4_MM[0] * mm, A4_MM[1] * mm), pageCompression=1)
        c.setTitle(title)
        c.setCreator("codeprint")
        for layout, paths in pages:
            spec = layout.spec
            c.setPageSize((spec.page_w * mm, spec.page_h * mm))
            for p in layout.placements:
                bottom = (spec.page_h - p.y_mm - p.h_mm) * mm
                if p.rotated:      # image rotated 90 degrees counter-clockwise, like Pillow's rotate(90)
                    c.saveState()
                    c.translate((p.x_mm + p.w_mm) * mm, bottom)
                    c.rotate(90)
                    c.drawImage(str(paths[p.item_id]), 0, 0, width=p.h_mm * mm, height=p.w_mm * mm)
                    c.restoreState()
                else:
                    c.drawImage(str(paths[p.item_id]), p.x_mm * mm, bottom,
                                width=p.w_mm * mm, height=p.h_mm * mm)
            c.showPage()
        c.save()
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def check_output_path(path: Path, force: bool, confirm) -> Path:
    """Validate an output path; ask (via `confirm(msg)->bool`) before overwriting."""
    path = Path(path).expanduser()
    if path.is_dir():
        raise CodePrintError("OUTPUT_IS_DIRECTORY", f"{path} is a directory; give a file name.", EXIT_USAGE)
    if path.exists() and not force:
        if not confirm(f"{path} already exists. Overwrite?"):
            raise CodePrintError("FILE_EXISTS", f"Not overwriting {path} (use --force).", EXIT_EXISTS)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".cp-probe-")
        probe.close()
    except OSError as exc:
        raise CodePrintError("OUTPUT_NOT_WRITABLE", f"Cannot write to {path.parent}: {exc}", EXIT_ENV)
    return path


def output_targets(out: Path, fmt: str, page_ids: list) -> list:
    """Files an export will write (several PNGs when exporting many pages as png)."""
    if fmt == "pdf" or len(page_ids) == 1:
        return [out]
    return [out.with_name(f"{out.stem}_page{pid}{out.suffix}") for pid in page_ids]


def export_pages(store: Store, page_ids: list, fmt: str, out: Path) -> dict:
    """Export pages as one PDF, or as PNG file(s). Returns info incl. utilization per page."""
    if fmt not in ("pdf", "png"):
        raise CodePrintError("INVALID_FORMAT", "Format must be pdf or png.", EXIT_USAGE)
    collected = [collect_page(store, pid) for pid in page_ids]    # fails before writing anything
    recovered = sorted({i for *_, rec in collected for i in rec})
    info = [{"page_id": page["id"], "title": page["title"], "items": len(layout.placements),
             "utilization_pct": round(layout.utilization_pct, 1)}
            for page, layout, _, _ in collected]
    files = []
    if fmt == "pdf":
        title = collected[0][0]["title"] if len(collected) == 1 else "CodePrint pages"
        write_pdf(out, [(layout, paths) for _, layout, paths, _ in collected], title)
        files.append(str(out))
    else:
        for (page, layout, paths, _), target in zip(collected, output_targets(out, fmt, page_ids)):
            write_page_png(target, layout, paths, dpi=300)
            files.append(str(target))
    return {"files": files, "pages": info, "recovered_screenshots": recovered}
