"""Deterministic A4 page layout (pure functions, no I/O).

Both the preview and the PDF/PNG exporters call `compute_layout`, so what you see
is what gets printed.

Rules shared by both modes
  * items keep their aspect ratio and are never scaled UP (native size = 96 css px per inch)
  * an item is scaled DOWN only as far as needed to fit the printable width/cell and page height
  * if that would make the code smaller than `min_font_pt` the item is rejected (UNREADABLE_SCALE)
  * if there is no room left the addition is rejected with CAPACITY_MAX
Modes
  grid     N equal-width columns, rows filled left-to-right in list order.
  compact  shelf packing, "first fit": each item goes on the existing shelf (row) where it
           fits with the least width left over, otherwise a new shelf is opened. Because
           later items can fill gaps beside earlier ones, visual order can differ slightly
           from list order. This is a heuristic - it is NOT guaranteed to be optimal.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from cp_config import CodePrintError, EXIT_CAPACITY, EXIT_USAGE

A4_MM = (210.0, 297.0)
EPS = 1e-6


class CapacityError(CodePrintError):
    """Raised when an item cannot be placed (code CAPACITY_MAX or UNREADABLE_SCALE)."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(code, message, EXIT_CAPACITY, details)


@dataclass(frozen=True)
class LayoutItem:
    id: int
    w_mm: float        # native printed size
    h_mm: float
    font_pt: float     # native code font size in points


@dataclass(frozen=True)
class PageSpec:
    orientation: str = "portrait"
    margin_mm: float = 12.0
    spacing_mm: float = 4.0
    mode: str = "compact"
    grid_columns: int = 1
    allow_rotation: bool = False
    min_font_pt: float = 7.0

    @property
    def page_w(self) -> float:
        return A4_MM[0] if self.orientation == "portrait" else A4_MM[1]

    @property
    def page_h(self) -> float:
        return A4_MM[1] if self.orientation == "portrait" else A4_MM[0]

    @property
    def printable_w(self) -> float:
        return self.page_w - 2 * self.margin_mm

    @property
    def printable_h(self) -> float:
        return self.page_h - 2 * self.margin_mm

    def validate(self) -> None:
        if self.orientation not in ("portrait", "landscape"):
            raise CodePrintError("INVALID_LAYOUT", "orientation must be portrait or landscape", EXIT_USAGE)
        if self.mode not in ("compact", "grid"):
            raise CodePrintError("INVALID_LAYOUT", "layout mode must be compact or grid", EXIT_USAGE)
        if self.margin_mm < 0 or self.spacing_mm < 0:
            raise CodePrintError("INVALID_LAYOUT", "margin and spacing must be >= 0", EXIT_USAGE)
        if self.printable_w < 20 or self.printable_h < 20:
            raise CodePrintError("INVALID_LAYOUT", "margins leave no printable area", EXIT_USAGE)
        if self.grid_columns < 1:
            raise CodePrintError("INVALID_LAYOUT", "grid columns must be >= 1", EXIT_USAGE)


@dataclass(frozen=True)
class Placement:
    item_id: int
    x_mm: float        # from the PAGE's top-left corner (margin already included)
    y_mm: float
    w_mm: float        # size as placed (after scaling / rotation)
    h_mm: float
    scale: float
    rotated: bool


@dataclass(frozen=True)
class Layout:
    spec: PageSpec
    placements: tuple
    used_area_mm2: float
    utilization_pct: float          # placed screenshot area / printable area * 100
    bottom_mm: float                # lowest occupied y, relative to the printable area top
    remaining_height_mm: float      # printable height below the lowest item
    last_row_free_width_mm: float


def _fail_unreadable(item: LayoutItem, scale: float, spec: PageSpec) -> CapacityError:
    printed = item.font_pt * scale
    return CapacityError(
        "UNREADABLE_SCALE",
        f"Screenshot #{item.id} would print at {printed:.1f} pt (minimum {spec.min_font_pt:g} pt) "
        f"to fit the {spec.printable_w:.0f} x {spec.printable_h:.0f} mm printable area. "
        "Try landscape, smaller margins, a lower --min-font-pt, or regenerate it narrower "
        "(--columns) / split the program.",
        {"item_id": item.id, "printed_font_pt": round(printed, 2), "min_font_pt": spec.min_font_pt})


def _fail_capacity(item: LayoutItem, w: float, h: float, remaining: float, spec: PageSpec) -> CapacityError:
    return CapacityError(
        "CAPACITY_MAX",
        f"Screenshot #{item.id} ({w:.1f} x {h:.1f} mm when printed) does not fit: only "
        f"{max(remaining, 0):.1f} mm of printable height is left. Add it to another page "
        "(use --auto-page to create one), reduce margins/spacing with `page set`, "
        "or switch layout mode.",
        {"item_id": item.id, "needed_w_mm": round(w, 2), "needed_h_mm": round(h, 2),
         "remaining_height_mm": round(max(remaining, 0), 2)})


def _scale_for(item: LayoutItem, cell_w: float, cell_h: float, rotated: bool) -> tuple:
    w, h = (item.h_mm, item.w_mm) if rotated else (item.w_mm, item.h_mm)
    s = min(1.0, cell_w / w, cell_h / h)
    return s, w * s, h * s


def _finish(spec: PageSpec, placements: list, bottom: float, last_free_w: float) -> Layout:
    used = sum(p.w_mm * p.h_mm for p in placements)
    area = spec.printable_w * spec.printable_h
    return Layout(spec, tuple(placements), used, used / area * 100.0, bottom,
                  spec.printable_h - bottom, last_free_w)


def compute_layout(spec: PageSpec, items: list) -> Layout:
    """Place `items` (in order) on a page or raise CapacityError."""
    spec.validate()
    if spec.mode == "grid":
        return _grid(spec, items)
    return _compact(spec, items)


# ---------------------------------------------------------------- grid ---
def _grid(spec: PageSpec, items: list) -> Layout:
    pw, ph, gap, cols = spec.printable_w, spec.printable_h, spec.spacing_mm, spec.grid_columns
    cell_w = (pw - (cols - 1) * gap) / cols
    placements, y, row_h, bottom, col = [], 0.0, 0.0, 0.0, 0
    for it in items:
        s, w, h = _scale_for(it, cell_w, ph, False)
        if it.font_pt * s < spec.min_font_pt - EPS:
            raise _fail_unreadable(it, s, spec)
        if col == cols:                      # start a new row
            y += row_h + gap
            row_h, col = 0.0, 0
        row_h = max(row_h, h)
        if y + row_h > ph + EPS:
            raise _fail_capacity(it, w, h, ph - y, spec)
        x = col * (cell_w + gap)
        placements.append(Placement(it.id, spec.margin_mm + x, spec.margin_mm + y, w, h, s, False))
        bottom = y + row_h
        col += 1
    free_w = (cols - col) * (cell_w + gap) if placements and col < cols else 0.0
    return _finish(spec, placements, bottom, free_w)


# ------------------------------------------------------------- compact ---
@dataclass
class _Shelf:
    y: float
    h: float
    x: float = 0.0       # next free x (already includes trailing gap handling)
    count: int = 0


def _compact(spec: PageSpec, items: list) -> Layout:
    pw, ph, gap = spec.printable_w, spec.printable_h, spec.spacing_mm
    shelves: list[_Shelf] = []
    placements = []
    for it in items:
        orients = []
        for rot in ([False, True] if spec.allow_rotation else [False]):
            s, w, h = _scale_for(it, pw, ph, rot)
            if it.font_pt * s >= spec.min_font_pt - EPS:
                orients.append((rot, s, w, h))
        if not orients:
            s, _, _ = _scale_for(it, pw, ph, False)
            raise _fail_unreadable(it, s, spec)

        best = None   # (cost, shelf_index|None, orient)
        for rot, s, w, h in orients:
            for i, sh in enumerate(shelves):
                x = sh.x + (gap if sh.count else 0.0)
                if x + w > pw + EPS:
                    continue
                growth = max(0.0, h - sh.h)
                if growth > EPS and not (i == len(shelves) - 1 and sh.y + h <= ph + EPS):
                    continue
                cost = (0, round(growth, 6), round(pw - (x + w), 6))
                if best is None or cost < best[0]:
                    best = (cost, i, (rot, s, w, h))
            ny = (shelves[-1].y + shelves[-1].h + gap) if shelves else 0.0
            if ny + h <= ph + EPS:
                cost = (1, round(h, 6), 0.0)
                if best is None or cost < best[0]:
                    best = (cost, None, (rot, s, w, h))
        if best is None:
            rot, s, w, h = orients[0]
            bottom = (shelves[-1].y + shelves[-1].h) if shelves else 0.0
            raise _fail_capacity(it, w, h, ph - bottom - (gap if shelves else 0.0), spec)

        _, idx, (rot, s, w, h) = best
        if idx is None:
            ny = (shelves[-1].y + shelves[-1].h + gap) if shelves else 0.0
            shelves.append(_Shelf(ny, h))
            idx = len(shelves) - 1
        sh = shelves[idx]
        x = sh.x + (gap if sh.count else 0.0)
        sh.h = max(sh.h, h)
        sh.x = x + w
        sh.count += 1
        placements.append(Placement(it.id, spec.margin_mm + x, spec.margin_mm + sh.y, w, h, s, rot))

    bottom = (shelves[-1].y + shelves[-1].h) if shelves else 0.0
    last_free = (pw - shelves[-1].x) if shelves else pw
    return _finish(spec, placements, bottom, last_free)
