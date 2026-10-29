"""SQLite persistence: screenshots, pages and the page<->screenshot relation."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import cp_config
from cp_config import CodePrintError, EXIT_NOT_FOUND, EXIT_USAGE
from cp_layout import LayoutItem, PageSpec, compute_layout

SCHEMA = """
CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_path TEXT,
    language TEXT NOT NULL,
    created_at TEXT NOT NULL,
    image_file TEXT NOT NULL,          -- file name inside the managed images directory
    mode TEXT NOT NULL,                -- code_output | code_only | output_only
    source_code TEXT NOT NULL,
    input_text TEXT,
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    compile_output TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,              -- ok | failed | timeout | compile_error | not_run
    exit_code INTEGER,
    duration_s REAL,
    truncated INTEGER NOT NULL DEFAULT 0,
    timeout_s REAL,
    width_px INTEGER NOT NULL,
    height_px INTEGER NOT NULL,
    scale REAL NOT NULL,
    font_px REAL NOT NULL,
    engine TEXT,
    settings_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    orientation TEXT NOT NULL,
    margin_mm REAL NOT NULL,
    spacing_mm REAL NOT NULL,
    layout_mode TEXT NOT NULL,
    grid_columns INTEGER NOT NULL,
    allow_rotation INTEGER NOT NULL,
    min_font_pt REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS page_items (
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    screenshot_id INTEGER NOT NULL REFERENCES screenshots(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (page_id, screenshot_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def item_from_row(row: dict) -> LayoutItem:
    """Native printed size: 96 CSS px per inch; image pixels = CSS px * scale."""
    css_w, css_h = row["width_px"] / row["scale"], row["height_px"] / row["scale"]
    return LayoutItem(row["id"], css_w * 25.4 / 96.0, css_h * 25.4 / 96.0, row["font_px"] * 0.75)


def spec_from_page(page: dict, **changes) -> PageSpec:
    vals = dict(orientation=page["orientation"], margin_mm=page["margin_mm"],
                spacing_mm=page["spacing_mm"], mode=page["layout_mode"],
                grid_columns=page["grid_columns"], allow_rotation=bool(page["allow_rotation"]),
                min_font_pt=page["min_font_pt"])
    vals.update(changes)
    return PageSpec(**vals)


class Store:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else cp_config.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(str(self.path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        except sqlite3.Error as exc:
            raise CodePrintError("DB_ERROR", f"Cannot open database {self.path}: {exc}")

    def close(self) -> None:
        self.conn.close()

    # ---- helpers
    def _one(self, sql, args=()):
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, args=()):
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # ---- screenshots
    def add_screenshot(self, **f) -> int:
        cols = ", ".join(f)
        marks = ", ".join("?" for _ in f)
        with self.conn:
            cur = self.conn.execute(f"INSERT INTO screenshots ({cols}) VALUES ({marks})", tuple(f.values()))
        return cur.lastrowid

    def get_screenshot(self, sid: int) -> dict:
        row = self._one("SELECT * FROM screenshots WHERE id = ?", (sid,))
        if not row:
            raise CodePrintError("SCREENSHOT_NOT_FOUND", f"No screenshot with id {sid}.", EXIT_NOT_FOUND)
        return row

    def list_screenshots(self) -> list:
        return self._all("SELECT * FROM screenshots ORDER BY id")

    def latest_screenshot_id(self) -> int:
        row = self._one("SELECT MAX(id) AS m FROM screenshots")
        if not row or row["m"] is None:
            raise CodePrintError("SCREENSHOT_NOT_FOUND", "There are no screenshots yet.", EXIT_NOT_FOUND)
        return row["m"]

    def update_screenshot(self, sid: int, **f) -> None:
        sets = ", ".join(f"{k} = ?" for k in f)
        with self.conn:
            self.conn.execute(f"UPDATE screenshots SET {sets} WHERE id = ?", (*f.values(), sid))

    def delete_screenshot(self, sid: int) -> None:
        self.get_screenshot(sid)
        with self.conn:
            self.conn.execute("DELETE FROM screenshots WHERE id = ?", (sid,))

    def pages_for_screenshot(self, sid: int) -> list:
        return self._all("SELECT p.id, p.title FROM pages p JOIN page_items i ON i.page_id = p.id "
                         "WHERE i.screenshot_id = ? ORDER BY p.id", (sid,))

    # ---- pages
    def create_page(self, title: str, spec: PageSpec) -> int:
        spec.validate()
        ts = now()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO pages (title, orientation, margin_mm, spacing_mm, layout_mode, grid_columns,"
                " allow_rotation, min_font_pt, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (title, spec.orientation, spec.margin_mm, spec.spacing_mm, spec.mode, spec.grid_columns,
                 int(spec.allow_rotation), spec.min_font_pt, ts, ts))
        return cur.lastrowid

    def get_page(self, pid: int) -> dict:
        row = self._one("SELECT * FROM pages WHERE id = ?", (pid,))
        if not row:
            raise CodePrintError("PAGE_NOT_FOUND", f"No page with id {pid}.", EXIT_NOT_FOUND)
        return row

    def list_pages(self) -> list:
        return self._all("SELECT p.*, (SELECT COUNT(*) FROM page_items i WHERE i.page_id = p.id) AS item_count "
                         "FROM pages p ORDER BY p.id")

    def latest_page_id(self) -> int:
        row = self._one("SELECT MAX(id) AS m FROM pages")
        if not row or row["m"] is None:
            raise CodePrintError("PAGE_NOT_FOUND", "There are no pages yet. Create one with `codeprint page create`.",
                                 EXIT_NOT_FOUND)
        return row["m"]

    def rename_page(self, pid: int, title: str) -> None:
        self.get_page(pid)
        with self.conn:
            self.conn.execute("UPDATE pages SET title = ?, updated_at = ? WHERE id = ?", (title, now(), pid))

    def delete_page(self, pid: int) -> None:
        self.get_page(pid)
        with self.conn:    # page_items rows cascade; screenshots are untouched
            self.conn.execute("DELETE FROM pages WHERE id = ?", (pid,))

    def page_items(self, pid: int) -> list:
        """Screenshot rows on a page in saved order (each with a 'position' key)."""
        return self._all("SELECT s.*, i.position FROM page_items i JOIN screenshots s ON s.id = i.screenshot_id "
                         "WHERE i.page_id = ? ORDER BY i.position", (pid,))

    def page_layout(self, pid: int, items: list | None = None, spec: PageSpec | None = None):
        """Compute the layout of a page (optionally for a proposed item list / spec)."""
        page = self.get_page(pid)
        rows = self.page_items(pid) if items is None else items
        return compute_layout(spec or spec_from_page(page), [item_from_row(r) for r in rows])

    def update_page_layout(self, pid: int, spec: PageSpec) -> None:
        spec.validate()
        self.page_layout(pid, spec=spec)          # raises CapacityError before anything is saved
        with self.conn:
            self.conn.execute("UPDATE pages SET orientation=?, margin_mm=?, spacing_mm=?, layout_mode=?,"
                              " grid_columns=?, allow_rotation=?, min_font_pt=?, updated_at=? WHERE id=?",
                              (spec.orientation, spec.margin_mm, spec.spacing_mm, spec.mode, spec.grid_columns,
                               int(spec.allow_rotation), spec.min_font_pt, now(), pid))

    # ---- page membership (all changes validated against the layout first)
    def _write_order(self, pid: int, ids: list) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM page_items WHERE page_id = ?", (pid,))
            self.conn.executemany("INSERT INTO page_items (page_id, screenshot_id, position) VALUES (?,?,?)",
                                  [(pid, sid, n) for n, sid in enumerate(ids, 1)])
            self.conn.execute("UPDATE pages SET updated_at = ? WHERE id = ?", (now(), pid))

    def add_to_page(self, pid: int, sid: int, position: int | None = None):
        """Add a screenshot; raises CapacityError (nothing saved) if it does not fit."""
        self.get_page(pid)
        shot = self.get_screenshot(sid)
        current = self.page_items(pid)
        if any(r["id"] == sid for r in current):
            raise CodePrintError("ALREADY_ON_PAGE", f"Screenshot {sid} is already on page {pid}.", EXIT_USAGE)
        pos = len(current) + 1 if position is None else position
        if not 1 <= pos <= len(current) + 1:
            raise CodePrintError("INVALID_POSITION", f"Position must be between 1 and {len(current) + 1}.", EXIT_USAGE)
        rows = current[:pos - 1] + [shot] + current[pos - 1:]
        layout = self.page_layout(pid, rows)
        self._write_order(pid, [r["id"] for r in rows])
        return layout

    def remove_from_page(self, pid: int, sid: int):
        self.get_page(pid)
        current = self.page_items(pid)
        if not any(r["id"] == sid for r in current):
            raise CodePrintError("NOT_ON_PAGE", f"Screenshot {sid} is not on page {pid}.", EXIT_USAGE)
        rows = [r for r in current if r["id"] != sid]
        layout = self.page_layout(pid, rows)       # removing never hurts, but compact order may change
        self._write_order(pid, [r["id"] for r in rows])
        return layout

    def move_in_page(self, pid: int, sid: int, position: int):
        self.get_page(pid)
        current = self.page_items(pid)
        row = next((r for r in current if r["id"] == sid), None)
        if row is None:
            raise CodePrintError("NOT_ON_PAGE", f"Screenshot {sid} is not on page {pid}.", EXIT_USAGE)
        if not 1 <= position <= len(current):
            raise CodePrintError("INVALID_POSITION", f"Position must be between 1 and {len(current)}.", EXIT_USAGE)
        rows = [r for r in current if r["id"] != sid]
        rows.insert(position - 1, row)
        layout = self.page_layout(pid, rows)       # a new order can change packing: validate first
        self._write_order(pid, [r["id"] for r in rows])
        return layout
