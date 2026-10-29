"""Shared errors, exit codes, storage paths and configuration handling."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

APP = "codeprint"

# ---- exit codes -----------------------------------------------------------
EXIT_OK = 0
EXIT_ERROR = 1          # unexpected / general failure
EXIT_USAGE = 2          # bad input (empty file, bad option value, ...)
EXIT_NOT_FOUND = 3      # file / screenshot / page does not exist
EXIT_CAPACITY = 4       # CAPACITY_MAX (or unreadable-scale) layout rejection
EXIT_ENV = 5            # missing tool / renderer failure
EXIT_EXISTS = 6         # refused to overwrite


class CodePrintError(Exception):
    """An expected, user-facing error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_ERROR,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


# ---- directories ----------------------------------------------------------
def data_dir() -> Path:
    env = os.environ.get("CODEPRINT_DATA_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP


def config_dir() -> Path:
    env = os.environ.get("CODEPRINT_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP


def config_path() -> Path:
    return config_dir() / "config.json"


def db_path() -> Path:
    return data_dir() / "codeprint.db"


def images_dir() -> Path:
    return data_dir() / "images"


def previews_dir() -> Path:
    return data_dir() / "previews"


def ensure_dirs() -> None:
    for d in (data_dir(), config_dir(), images_dir(), previews_dir()):
        d.mkdir(parents=True, exist_ok=True)


# ---- atomic file writing (used by config and exports) ---------------------
def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---- configuration schema -------------------------------------------------
# Precedence (highest first):  CLI flag  >  config file  >  built-in default.
# Page layout keys (page_*, layout_mode, ...) are copied into a page when it is
# created; after that the page's own saved layout wins (change it with `page set`).
SCHEMA: dict[str, dict] = {
    "engine":          dict(default="auto", type=str, choices=["auto", "pillow", "playwright"],
                            help="Renderer: auto (Playwright if its browser exists, else Pillow)"),
    "font_family":     dict(default="DejaVu Sans Mono", type=str, help="Monospace font family"),
    "font_size":       dict(default=14.0, type=float, min=6, max=48, help="Font size in CSS px (14px = 10.5pt)"),
    "scale":           dict(default=3.0, type=float, min=1, max=6, help="Render scale (3 = ~288 dpi)"),
    "line_numbers":    dict(default=True, type=bool, help="Show line numbers"),
    "highlight":       dict(default=True, type=bool, help="Syntax highlighting"),
    "highlight_style": dict(default="print", type=str, choices=["print", "bw"],
                            help="print = dark colours, bw = black only (bold/italic)"),
    "columns":         dict(default=76, type=int, min=20, max=400, help="Max characters per line before wrapping"),
    "wrap":            dict(default=True, type=bool, help="Wrap long lines (false = widen image instead)"),
    "width_mode":      dict(default="fit", type=str, choices=["fixed", "fit"],
                            help="fixed = same width for every image, fit = shrink to content"),
    "min_columns":     dict(default=20, type=int, min=10, max=400, help="Minimum width in 'fit' mode"),
    "tab_width":       dict(default=4, type=int, min=1, max=16, help="Tab stop width"),
    "padding":         dict(default=8, type=int, min=0, max=100, help="Left/right image padding (CSS px)"),
    "section_spacing": dict(default=6, type=int, min=0, max=100, help="Padding above/below the code and output blocks (CSS px)"),
    "timeout":         dict(default=10.0, type=float, min=0.1, max=3600, help="Execution timeout in seconds"),
    "max_output_kb":   dict(default=256, type=int, min=1, max=65536, help="Max captured output per stream (KiB)"),
    "output_dir":      dict(default="output", type=str, help="Default directory for exports"),
    "page_orientation": dict(default="portrait", type=str, choices=["portrait", "landscape"], help="A4 orientation for new pages"),
    "page_margin_mm":  dict(default=12.0, type=float, min=0, max=60, help="Page margin (mm)"),
    "page_spacing_mm": dict(default=4.0, type=float, min=0, max=60, help="Space between screenshots (mm)"),
    "layout_mode":     dict(default="compact", type=str, choices=["compact", "grid"], help="Page packing mode"),
    "grid_columns":    dict(default=1, type=int, min=1, max=4, help="Columns in grid mode"),
    "allow_rotation":  dict(default=False, type=bool, help="Allow 90-degree rotation in compact mode"),
    "min_font_pt":     dict(default=7.0, type=float, min=3, max=24, help="Smallest printed code size allowed (pt)"),
}


# settings that existed in older versions; silently ignored if still present in config.json
REMOVED_KEYS = {"show_input"}


def defaults() -> dict:
    return {k: v["default"] for k, v in SCHEMA.items()}


def coerce(key: str, raw) -> object:
    """Validate and convert a raw value for config key `key`."""
    if key not in SCHEMA:
        raise CodePrintError("UNKNOWN_CONFIG_KEY",
                             f"Unknown config key '{key}'. Valid keys: {', '.join(SCHEMA)}", EXIT_USAGE)
    spec = SCHEMA[key]
    typ = spec["type"]
    try:
        if typ is bool:
            if isinstance(raw, bool):
                val = raw
            else:
                s = str(raw).strip().lower()
                if s in ("1", "true", "yes", "on"):
                    val = True
                elif s in ("0", "false", "no", "off"):
                    val = False
                else:
                    raise ValueError("expected true/false")
        elif typ is int:
            val = int(raw)
        elif typ is float:
            val = float(raw)
        else:
            val = str(raw)
    except (TypeError, ValueError) as exc:
        raise CodePrintError("INVALID_CONFIG_VALUE",
                             f"Invalid value {raw!r} for '{key}': {exc}", EXIT_USAGE)
    if "choices" in spec and val not in spec["choices"]:
        raise CodePrintError("INVALID_CONFIG_VALUE",
                             f"'{key}' must be one of: {', '.join(spec['choices'])}", EXIT_USAGE)
    if typ in (int, float):
        if "min" in spec and val < spec["min"] or "max" in spec and val > spec["max"]:
            raise CodePrintError("INVALID_CONFIG_VALUE",
                                 f"'{key}' must be between {spec['min']} and {spec['max']}", EXIT_USAGE)
    return val


def load_user_config() -> dict:
    """Return only the values stored in the config file (validated)."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")
    except (OSError, ValueError) as exc:
        raise CodePrintError("BAD_CONFIG", f"Cannot read config file {path}: {exc}. "
                             "Fix or delete it.", EXIT_USAGE)
    out = {}
    for key, raw in data.items():
        if key in REMOVED_KEYS:
            continue
        if key not in SCHEMA:
            print(f"warning: ignoring unknown key '{key}' in {path}", file=sys.stderr)
            continue
        out[key] = coerce(key, raw)
    return out


def load_config(overrides: dict | None = None) -> dict:
    """defaults < config file < explicit overrides (None values are ignored)."""
    cfg = defaults()
    cfg.update(load_user_config())
    for key, val in (overrides or {}).items():
        if val is not None:
            cfg[key] = coerce(key, val)
    return cfg


def set_config_value(key: str, raw) -> object:
    val = coerce(key, raw)
    stored = load_user_config()
    stored[key] = val
    atomic_write_bytes(config_path(), (json.dumps(stored, indent=2, sort_keys=True) + "\n").encode())
    return val


def reset_config() -> None:
    p = config_path()
    if p.exists():
        p.unlink()
