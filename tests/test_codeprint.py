"""CodePrint tests. Everything runs against temporary directories/databases.

Language-specific tests are skipped (with a reason) when the interpreter/compiler is missing.
"""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import codeprint  # noqa: E402
import cp_config  # noqa: E402
import cp_export  # noqa: E402
import cp_layout  # noqa: E402
import cp_render  # noqa: E402
import cp_runner  # noqa: E402
from cp_config import CodePrintError  # noqa: E402
from cp_layout import CapacityError, LayoutItem, PageSpec, compute_layout  # noqa: E402
from cp_store import Store  # noqa: E402

SAMPLES = ROOT / "samples"
runner = CliRunner()


def needs(*tools):
    missing = [t for t in tools if not any(shutil.which(n) for n in t.split("|"))]
    return pytest.mark.skipif(bool(missing), reason=f"{', '.join(missing)} not installed")


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Private data/config dirs + Pillow engine so tests never touch the real library."""
    monkeypatch.setenv("CODEPRINT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CODEPRINT_CONFIG_DIR", str(tmp_path / "config"))
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.json").write_text(json.dumps({"engine": "pillow"}))
    cp_config.ensure_dirs()
    return tmp_path


def cli(*args, ok=True):
    res = runner.invoke(codeprint.app, [str(a) for a in args])
    if ok:
        assert res.exit_code == 0, f"exit {res.exit_code}: {res.output}"
    return res


def write(tmp, name, text):
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return p


def make_shot(tmp, name="a.py", body="print('hi')\n", *extra):
    src = write(tmp, name, body)
    res = cli("generate", src, "--json", *extra)
    return json.loads(res.stdout)["screenshot"]["id"]


# ------------------------------------------------------------- 1. language detection
@pytest.mark.parametrize("name,key", [("a.py", "python"), ("a.c", "c"), ("a.cpp", "cpp"), ("A.java", "java"),
                                      ("a.js", "javascript"), ("a.sh", "bash")])
def test_language_detection(name, key):
    assert cp_runner.detect_language(Path(name)).key == key


def test_language_explicit_and_unsupported(tmp_path):
    assert cp_runner.detect_language(Path("x.txt"), "python").key == "python"
    assert cp_runner.detect_language(Path("x.txt"), "c++").key == "cpp"
    with pytest.raises(CodePrintError) as e:
        cp_runner.detect_language(Path("x.txt"))
    assert e.value.code == "UNSUPPORTED_LANGUAGE"
    with pytest.raises(CodePrintError):
        cp_runner.detect_language(Path("x.py"), "cobol")
    script = write(tmp_path, "tool", "#!/usr/bin/env python3\nprint(1)\n")
    assert cp_runner.detect_language(script).key == "python"


# ------------------------------------------------------------- 2. source loading
def test_read_source_unicode_crlf_and_untouched(tmp_path):
    p = tmp_path / "u.py"
    raw = "print('héllo λ')\r\nx = 1\r\n".encode("utf-8")
    p.write_bytes(raw)
    text = cp_runner.read_source(p)
    assert text == "print('héllo λ')\nx = 1\n"
    assert p.read_bytes() == raw          # source never modified


def test_read_source_errors(tmp_path):
    with pytest.raises(CodePrintError) as e:
        cp_runner.read_source(tmp_path / "missing.py")
    assert e.value.code == "FILE_NOT_FOUND"
    empty = write(tmp_path, "e.py", "  \n\n")
    with pytest.raises(CodePrintError) as e:
        cp_runner.read_source(empty)
    assert e.value.code == "EMPTY_FILE"
    bad = tmp_path / "bad.py"
    bad.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(CodePrintError) as e:
        cp_runner.read_source(bad)
    assert e.value.code == "INVALID_ENCODING"


# ------------------------------------------------------------- 3. syntax highlighting
def test_syntax_highlighting_distinguishes_tokens():
    s = cp_config.defaults()
    cells = cp_render._code_cells(['def f():', '    return "x"  # note'], "python", s)
    styles = {ch: st for ch, st in cells[0]}
    assert styles["d"] != styles["f"] or styles["d"][1]          # keyword is styled (bold) vs name
    kw, name = cells[0][0][1], cells[0][4][1]
    assert kw != name
    flat = {st for line in cells for _, st in line}
    assert len(flat) >= 4                                         # keyword, name, string, comment ...
    plain = cp_render._code_cells(['def f():'], "python", dict(s, highlight=False))
    assert len({st for _, st in plain[0]}) == 1


def test_html_has_highlight_spans_and_no_gradients():
    s = cp_config.defaults()
    secs = cp_render.build_sections("code_only", "int main() { return 0; }\n", "c", {})
    html = cp_render.build_html(cp_render.RenderSpec(secs, s))
    assert "<span style=" in html and "#fff" in html and "gradient" not in html
    assert 'id="sheet"' in html


# ------------------------------------------------------------- 4-8. execution
def run_src(tmp, name, text, lang=None, **kw):
    p = write(tmp, name, text)
    return cp_runner.run_program(cp_runner.detect_language(p, lang), p, **kw)


def test_run_python_stdout_stderr_exit(tmp_path):
    r = run_src(tmp_path, "a.py", "import sys\nprint('out')\nprint('err', file=sys.stderr)\nsys.exit(3)\n")
    assert r.status == "failed" and r.exit_code == 3
    assert r.stdout.strip() == "out" and r.stderr.strip() == "err"
    assert r.duration_s >= 0


def test_run_success_and_stdin(tmp_path):
    inp = write(tmp_path, "in.txt", "Asha\n20\n")
    r = cp_runner.run_program(cp_runner.LANGUAGES["python"], SAMPLES / "greet_stdin.py", inp)
    assert r.status == "ok" and "Hello, Asha! Next year you will be 21." in r.stdout


def test_stdin_defaults_to_eof(tmp_path):
    r = run_src(tmp_path, "a.py", "import sys\nprint(repr(sys.stdin.read()))\n")
    assert r.stdout.strip() == "''"


def test_timeout(tmp_path):
    r = run_src(tmp_path, "a.py", "while True:\n    pass\n", timeout=1)
    assert r.status == "timeout" and r.exit_code is None
    assert 0.9 <= r.duration_s < 5


def test_output_truncation_is_flagged(tmp_path):
    r = run_src(tmp_path, "a.py", "print('x' * 5000)\n", max_output_kb=1)
    assert r.truncated and len(r.stdout) == 1024


def test_tempdir_path_hidden_in_output(tmp_path):
    r = run_src(tmp_path, "boom.py", "raise ValueError('x')\n")
    assert "codeprint-run-" not in r.stderr and 'File "boom.py"' in r.stderr


@needs("gcc|cc")
def test_c_run_and_compile_error(tmp_path):
    ok = cp_runner.run_program(cp_runner.LANGUAGES["c"], SAMPLES / "hello.c")
    assert ok.status == "ok" and "Hello, World!" in ok.stdout
    bad = cp_runner.run_program(cp_runner.LANGUAGES["c"], SAMPLES / "broken.c")
    assert bad.status == "compile_error" and "error" in bad.compile_output and bad.stdout == ""


@needs("g++|c++")
def test_cpp(tmp_path):
    assert "Hello, World!" in cp_runner.run_program(cp_runner.LANGUAGES["cpp"], SAMPLES / "hello.cpp").stdout


@needs("javac", "java")
def test_java():
    assert "Hello, World!" in cp_runner.run_program(cp_runner.LANGUAGES["java"], SAMPLES / "Hello.java").stdout


@needs("node|nodejs")
def test_node():
    assert "Hello, World!" in cp_runner.run_program(cp_runner.LANGUAGES["javascript"], SAMPLES / "hello.js").stdout


@needs("bash")
def test_bash():
    assert "7 x 7" not in cp_runner.run_program(cp_runner.LANGUAGES["bash"], SAMPLES / "hello.sh").stdout
    assert "5 x 7 = 35" in cp_runner.run_program(cp_runner.LANGUAGES["bash"], SAMPLES / "hello.sh").stdout


def test_missing_tool_is_clear_error(monkeypatch):
    monkeypatch.setattr(cp_runner, "find_tool", lambda spec: None)
    with pytest.raises(CodePrintError) as e:
        cp_runner.run_program(cp_runner.LANGUAGES["python"], SAMPLES / "hello.py")
    assert e.value.code == "TOOL_MISSING"


# ------------------------------------------------------------- 9-11. rendering
def render(tmp, code="print('hi')\n", out="hi\n", **over):
    s = {**cp_config.defaults(), "engine": "pillow", **over}
    secs = cp_render.build_sections("code_output", code, "python",
                                    dict(status="ok", stdout=out, stderr="", exit_code=0))
    path = tmp / "r.png"
    res = cp_render.render_png(cp_render.RenderSpec(secs, s), path)
    return res, path


def test_image_generation_white_background_and_dpi(tmp_path):
    res, path = render(tmp_path)
    with Image.open(path) as im:
        assert im.size == (res.width_px, res.height_px)
        assert im.mode == "RGB"
        assert im.getpixel((5, 5)) == (255, 255, 255) or im.getpixel((10, 10)) == (255, 255, 255)
        assert round(im.info["dpi"][0]) == 288
        extrema = im.convert("L").getextrema()
        assert extrema[0] < 80            # dark text present


def test_image_dimensions_predictable(tmp_path):
    a, _ = render(tmp_path, "print(1)\n", width_mode="fixed")
    b, _ = render(tmp_path, "x = 1\n" * 30 + "print(2)\n", width_mode="fixed")
    assert a.width_px == b.width_px and b.height_px > a.height_px


def test_unicode_rendering_has_no_missing_glyphs(tmp_path):
    res, path = render(tmp_path, "print('héllo wörld → λ ✓ ∑ ñ')\n", "héllo wörld → λ ✓ ∑ ñ\n")
    assert res.warnings == []


def test_unicode_through_whole_pipeline(tmp_path):
    p = write(tmp_path, "u.py", "print('héllo → λ')\n")
    sid = json.loads(cli("generate", p, "--json").stdout)["screenshot"]["id"]
    assert "héllo → λ" in json.loads(cli("screenshot", "show", sid, "--json").stdout)["screenshot"]["stdout"]


def test_long_lines_wrap_without_losing_text(tmp_path):
    long = "x = '" + "abcdefghij" * 30 + "'"
    s = cp_config.defaults()
    cells = cp_render._code_cells([long], "python", s)
    rows = cp_render._wrap_cells(cells[0], 80, True)
    assert len(rows) > 3 and "".join(c for r in rows for c, _ in r) == long
    wrapped, _ = render(tmp_path, long + "\n", width_mode="fixed")
    short, _ = render(tmp_path, "x = 1\n", width_mode="fixed")
    assert wrapped.width_px == short.width_px and wrapped.height_px > short.height_px
    unwrapped, _ = render(tmp_path, long + "\n", wrap=False, width_mode="fixed")
    assert unwrapped.width_px > wrapped.width_px          # horizontal alternative: image widens


def test_output_cleanup_ansi_and_cr():
    lines = cp_render.clean_text("\x1b[31mred\x1b[0m\nprogress 10%\rprogress 100%\n\ttab\n\n\n")
    assert lines == ["red", "progress 100%", "    tab"]


def test_fit_width_mode_and_no_line_numbers(tmp_path):
    fit, _ = render(tmp_path, "x=1\n", "1\n", width_mode="fit")
    fixed, _ = render(tmp_path, "x=1\n", "1\n", width_mode="fixed")
    assert fit.width_px < fixed.width_px


def test_status_footers():
    secs = cp_render.build_sections("code_output", "x", "python",
                                    dict(status="timeout", stdout="a\n", stderr="", timeout_s=2.0))
    assert "timed out after 2s" in secs[-1].footer
    secs = cp_render.build_sections("code_output", "x", "c", dict(status="compile_error", compile_output="boom"))
    assert secs[-1].label == "COMPILER ERRORS"
    secs = cp_render.build_sections("output_only", "x", "c", dict(status="failed", exit_code=2, stdout="", stderr="e\n"))
    assert [s.label for s in secs] == ["STANDARD ERROR"] and "exit status 2" in secs[0].footer


@pytest.mark.skipif(not cp_render.playwright_available(), reason="Playwright Chromium is not installed")
def test_playwright_engine(tmp_path):
    res, path = render(tmp_path, engine="playwright")
    assert res.engine == "playwright" and Image.open(path).width == res.width_px


# ------------------------------------------------------------- 12-13. SQLite + screenshot CRUD
def test_sqlite_persistence_across_connections(tmp_path):
    sid = make_shot(tmp_path)
    store = Store()
    shot = store.get_screenshot(sid)
    store.close()
    assert shot["language"] == "python" and shot["stdout"].strip() == "hi" and shot["status"] == "ok"
    assert shot["source_code"].startswith("print") and shot["width_px"] > 0 and shot["exit_code"] == 0
    store2 = Store()                       # "restart"
    assert [r["id"] for r in store2.list_screenshots()] == [sid]
    store2.close()
    assert (cp_config.db_path()).exists()


def test_screenshot_crud(tmp_path):
    sid = make_shot(tmp_path)
    listing = json.loads(cli("screenshot", "list", "--json").stdout)["screenshots"]
    assert listing[0]["id"] == sid and listing[0]["image_exists"]
    cli("screenshot", "rename", sid, "Updated Title")
    shown = json.loads(cli("screenshot", "show", sid, "--json").stdout)["screenshot"]
    assert shown["title"] == "Updated Title"
    out = tmp_path / "exp.png"
    cli("screenshot", "export", sid, "--format", "png", "--output", out)
    assert Image.open(out).size == (shown["width_px"], shown["height_px"])
    img = Path(shown["image_path"])
    assert cli("screenshot", "delete", sid, ok=False).exit_code == 2     # needs confirmation
    assert img.exists()
    cli("screenshot", "delete", sid, "--yes")
    assert not img.exists() and not json.loads(cli("screenshot", "list", "--json").stdout)["screenshots"]
    assert cli("screenshot", "show", sid, ok=False).exit_code == 3


def test_ids_are_stable_not_reused(tmp_path):
    a = make_shot(tmp_path)
    cli("screenshot", "delete", a, "--yes")
    assert make_shot(tmp_path) == a + 1


def test_invalid_ids():
    assert cli("screenshot", "show", "abc", ok=False).exit_code == 2
    assert cli("page", "show", "99", ok=False).exit_code == 3
    assert cli("preview", "latest", "--no-open", ok=False).exit_code == 3


def test_delete_never_touches_files_outside_managed_dir(tmp_path):
    sid = make_shot(tmp_path)
    victim = tmp_path / "precious.txt"
    victim.write_text("keep me")
    store = Store()
    store.update_screenshot(sid, image_file="../precious.txt")
    store.close()
    res = cli("screenshot", "delete", sid, "--yes", ok=False)
    assert res.exit_code != 0 and victim.exists() and "UNSAFE_PATH" in res.output


def test_generate_never_overwrites_copies(tmp_path):
    src = write(tmp_path, "a.py", "print(1)\n")
    outdir = tmp_path / "o"
    cli("generate", src, "--output-dir", outdir)
    cli("generate", src, "--output-dir", outdir)
    assert {p.name for p in outdir.iterdir()} == {"a.png", "a-1.png"}


def test_generate_options_and_modes(tmp_path):
    src = write(tmp_path, "a.py", "print(1)\n")
    only = json.loads(cli("generate", src, "--code-only", "--json").stdout)["screenshot"]
    assert only["mode"] == "code_only" and only["status"] == "not_run"
    outp = json.loads(cli("generate", src, "--output-only", "--json").stdout)["screenshot"]
    assert outp["mode"] == "output_only" and outp["height_px"] < only["height_px"] + 400
    assert cli("generate", src, "--code-only", "--output-only", ok=False).exit_code == 2
    assert cli("generate", tmp_path / "nope.py", ok=False).exit_code == 3
    assert cli("generate", write(tmp_path, "z.xyz", "x"), ok=False).exit_code == 2
    assert cli("generate", write(tmp_path, "empty.py", ""), ok=False).exit_code == 2


def test_failing_program_still_produces_screenshot(tmp_path):
    sid = make_shot(tmp_path, "bad.py", "import sys\nprint('a')\nsys.exit(4)\n")
    shot = json.loads(cli("screenshot", "show", sid, "--json").stdout)["screenshot"]
    assert shot["status"] == "failed" and shot["exit_code"] == 4


def test_timeout_via_cli(tmp_path):
    src = write(tmp_path, "loop.py", "while True: pass\n")
    shot = json.loads(cli("generate", src, "--timeout", "1", "--json").stdout)["screenshot"]
    assert shot["status"] == "timeout"


# ------------------------------------------------------------- 14-16. pages
def test_page_crud_and_ordering(tmp_path):
    a, b = make_shot(tmp_path, "a.py"), make_shot(tmp_path, "b.py", "print(2)\n")
    pid = json.loads(cli("page", "create", "Experiment 01", "--json").stdout)["page_id"]
    cli("page", "rename", pid, "Experiment 01 - Part 2")
    cli("page", "add", a, "--page", pid)
    cli("page", "add", b, "--page", pid)
    order = lambda: [i["screenshot_id"] for i in json.loads(cli("page", "show", pid, "--json").stdout)["page"]["items"]]
    assert order() == [a, b]
    cli("page", "reorder", b, "--page", pid, "--position", 1)
    assert order() == [b, a]
    assert cli("page", "reorder", b, "--page", pid, "--position", 9, ok=False).exit_code == 2
    assert cli("page", "add", a, "--page", pid, ok=False).exit_code == 2         # duplicate
    cli("page", "remove", b, "--page", pid)
    assert order() == [a]
    assert cli("page", "remove", b, "--page", pid, ok=False).exit_code == 2
    # persisted + page deletion keeps screenshots; a screenshot can be reused on another page
    pid2 = json.loads(cli("page", "create", "Second", "--json").stdout)["page_id"]
    cli("page", "add", a, "--page", pid2)
    assert json.loads(cli("page", "list", "--json").stdout)["pages"][0]["title"] == "Experiment 01 - Part 2"
    assert cli("page", "delete", pid, ok=False).exit_code == 2
    cli("page", "delete", pid, "--yes")
    shots = json.loads(cli("screenshot", "list", "--json").stdout)["screenshots"]
    assert {s["id"] for s in shots} == {a, b} and [s["pages"] for s in shots if s["id"] == a] == [[pid2]]


def test_page_capacity_enforced_and_nothing_saved(tmp_path):
    ids = [make_shot(tmp_path, f"p{i}.py", "print(1)\n" * 18) for i in range(5)]
    pid = json.loads(cli("page", "create", "Full", "--json").stdout)["page_id"]
    added = 0
    for sid in ids:
        res = cli("page", "add", sid, "--page", pid, "--json", ok=False)
        if res.exit_code == 0:
            added += 1
        else:
            err = json.loads(res.stdout)["error"]
            assert res.exit_code == 4 and err["code"] == "CAPACITY_MAX" and err["remaining_height_mm"] >= 0
            break
    assert 1 <= added < 5
    assert len(json.loads(cli("page", "show", pid, "--json").stdout)["page"]["items"]) == added
    # --auto-page spills into a new page instead
    res = json.loads(cli("page", "add", ids[-1], "--page", pid, "--auto-page", "--json").stdout)
    assert res["added"][0]["new_page"] is not None
    assert len(json.loads(cli("page", "list", "--json").stdout)["pages"]) == 2


def test_page_set_layout_rejected_if_unfitting(tmp_path):
    ids = [make_shot(tmp_path, f"p{i}.py", ('print("' + "a" * 60 + '")\n') * 7) for i in range(3)]
    pid = json.loads(cli("page", "create", "P", "--json").stdout)["page_id"]
    cli("page", "add", *ids, "--page", pid)
    res = cli("page", "set", pid, "--margin", "130", ok=False)          # no printable area -> invalid
    assert res.exit_code == 2
    res = cli("page", "set", pid, "--orientation", "landscape", ok=False)  # 3 items cannot fit landscape
    assert res.exit_code == 4
    assert json.loads(cli("page", "show", pid, "--json").stdout)["page"]["orientation"] == "portrait"


# ------------------------------------------------------------- layout engine
def items(*dims, font=10.5):
    return [LayoutItem(i + 1, w, h, font) for i, (w, h) in enumerate(dims)]


def assert_valid(layout):
    sp, ps = layout.spec, layout.placements
    for p in ps:
        assert p.x_mm >= sp.margin_mm - 1e-6 and p.y_mm >= sp.margin_mm - 1e-6
        assert p.x_mm + p.w_mm <= sp.page_w - sp.margin_mm + 1e-6
        assert p.y_mm + p.h_mm <= sp.page_h - sp.margin_mm + 1e-6
    for i, a in enumerate(ps):
        for b in ps[i + 1:]:
            apart = (a.x_mm + a.w_mm <= b.x_mm + 1e-6 or b.x_mm + b.w_mm <= a.x_mm + 1e-6 or
                     a.y_mm + a.h_mm <= b.y_mm + 1e-6 or b.y_mm + b.h_mm <= a.y_mm + 1e-6)
            assert apart, f"overlap {a} {b}"


def test_a4_dimensions():
    p, l = PageSpec(), PageSpec(orientation="landscape")
    assert (p.page_w, p.page_h) == (210.0, 297.0) and (l.page_w, l.page_h) == (297.0, 210.0)
    assert p.printable_w == 210 - 24 and p.printable_h == 297 - 24


@pytest.mark.parametrize("mode", ["compact", "grid"])
def test_layout_never_overlaps_or_escapes(mode):
    its = items((90, 40), (120, 70), (60, 60), (90, 30), (50, 50), (186, 20), (70, 45))
    lay = compute_layout(PageSpec(mode=mode, grid_columns=2, min_font_pt=4), its)
    assert len(lay.placements) == len(its)
    assert_valid(lay)


def test_compact_beats_grid_on_waste():
    its = items(*[(80, 50)] * 4)           # four small items
    compact = compute_layout(PageSpec(mode="compact"), its)
    grid1 = compute_layout(PageSpec(mode="grid", grid_columns=1), its)
    assert compact.bottom_mm < grid1.bottom_mm
    assert compact.utilization_pct == pytest.approx(grid1.utilization_pct)   # same ink, less height used
    ys = {round(p.y_mm, 3) for p in compact.placements}
    assert len(ys) < 4                    # several items share a row


def test_utilization_formula():
    lay = compute_layout(PageSpec(), items((186, 136.5)))
    assert lay.utilization_pct == pytest.approx(50.0, abs=0.01)
    assert lay.remaining_height_mm == pytest.approx(273 - 136.5)
    assert compute_layout(PageSpec(), []).utilization_pct == 0


def test_aspect_ratio_preserved_and_never_upscaled():
    lay = compute_layout(PageSpec(min_font_pt=5), items((300, 150), (50, 25)))
    big, small = lay.placements
    assert big.w_mm / big.h_mm == pytest.approx(2.0) and big.scale == pytest.approx(186 / 300)
    assert small.scale == 1.0 and small.w_mm == 50


def test_unreadable_scale_rejected():
    with pytest.raises(CapacityError) as e:
        compute_layout(PageSpec(min_font_pt=9), items((400, 100)))        # needs 46% -> 4.9pt
    assert e.value.code == "UNREADABLE_SCALE" and e.value.exit_code == 4


def test_rotation_used_only_when_enabled():
    wide = items((300, 100))
    with pytest.raises(CapacityError):
        compute_layout(PageSpec(min_font_pt=8), wide)
    lay = compute_layout(PageSpec(min_font_pt=8, allow_rotation=True), wide)
    assert lay.placements[0].rotated and lay.placements[0].h_mm > lay.placements[0].w_mm
    assert_valid(lay)


def test_capacity_error_is_structured():
    with pytest.raises(CapacityError) as e:
        compute_layout(PageSpec(), items((186, 150), (186, 150)))
    assert e.value.code == "CAPACITY_MAX" and e.value.details["item_id"] == 2
    assert "another page" in e.value.message


def test_layout_is_deterministic():
    its = items((90, 40), (120, 70), (60, 60))
    assert compute_layout(PageSpec(), its) == compute_layout(PageSpec(), its)


# ------------------------------------------------------------- 17-18. export, PDF
def pdf_info(path):
    data = Path(path).read_bytes()
    assert data.startswith(b"%PDF")
    boxes = [tuple(float(x) for x in m) for m in re.findall(rb"/MediaBox \[ ?([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) ?\]", data)]
    return data, boxes


def test_pdf_generation_a4_size(tmp_path):
    sid = make_shot(tmp_path)
    pid = json.loads(cli("page", "create", "T", "--json").stdout)["page_id"]
    cli("page", "add", sid, "--page", pid)
    out = tmp_path / "t.pdf"
    cli("page", "export", pid, "--output", out)
    data, boxes = pdf_info(out)
    assert len(re.findall(rb"/Type /Page\b(?!s)", data)) == 1
    w, h = boxes[0][2:]
    assert w == pytest.approx(210 / 25.4 * 72, abs=0.05) and h == pytest.approx(297 / 25.4 * 72, abs=0.05)


def test_landscape_pdf_and_multi_page(tmp_path):
    sid = make_shot(tmp_path)
    p1 = json.loads(cli("page", "create", "A", "--json").stdout)["page_id"]
    p2 = json.loads(cli("page", "create", "B", "--orientation", "landscape", "--json").stdout)["page_id"]
    cli("page", "add", sid, "--page", p1)
    cli("page", "add", sid, "--page", p2)
    out = tmp_path / "multi.pdf"
    cli("export", "--pages", f"{p1},{p2}", "--format", "pdf", "--output", out)
    data, boxes = pdf_info(out)
    assert len(re.findall(rb"/Type /Page\b(?!s)", data)) == 2
    sizes = sorted((round(b[2]), round(b[3])) for b in boxes)
    assert sizes == [(595, 842), (842, 595)]
    out2 = tmp_path / "all.pdf"
    cli("export", "--all-pages", "--output", out2)
    assert len(re.findall(rb"/Type /Page\b(?!s)", out2.read_bytes())) == 2


def test_page_png_export_dimensions(tmp_path):
    sid = make_shot(tmp_path)
    pid = json.loads(cli("page", "create", "T", "--json").stdout)["page_id"]
    cli("page", "add", sid, "--page", pid)
    out = tmp_path / "p.png"
    cli("page", "export", pid, "--format", "png", "--output", out)
    assert Image.open(out).size == (2480, 3508)          # A4 at 300 dpi


def test_preview_uses_same_layout_as_export(tmp_path):
    ids = [make_shot(tmp_path, f"{i}.py") for i in range(2)]
    pid = json.loads(cli("page", "create", "T", "--json").stdout)["page_id"]
    cli("page", "add", *ids, "--page", pid)
    prev = json.loads(cli("page", "preview", pid, "--no-open", "--json").stdout)
    shown = json.loads(cli("page", "show", pid, "--json").stdout)["page"]
    assert prev["page"]["items"] == shown["items"] and prev["page"]["utilization_pct"] == shown["utilization_pct"]
    assert Image.open(prev["preview_image"]).size == (827, 1169)


def test_export_path_handling(tmp_path):
    sid = make_shot(tmp_path)
    pid = json.loads(cli("page", "create", "T", "--json").stdout)["page_id"]
    cli("page", "add", sid, "--page", pid)
    out = tmp_path / "sub" / "dir" / "t.pdf"
    cli("page", "export", pid, "--output", out)               # creates parent folders
    first = out.read_bytes()
    res = cli("page", "export", pid, "--output", out, ok=False)
    assert res.exit_code == 6 and out.read_bytes() == first    # refuses to overwrite silently
    cli("page", "export", pid, "--output", out, "--force")
    assert cli("page", "export", pid, "--output", tmp_path, ok=False).exit_code == 2    # directory
    assert cli("page", "export", pid, "--format", "docx", ok=False).exit_code == 2
    assert cli("export", ok=False).exit_code == 2
    assert not list(out.parent.glob(".cp-*"))                  # no temp files left behind


def test_default_export_location_uses_output_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sid = make_shot(tmp_path)
    pid = json.loads(cli("page", "create", "My Page", "--json").stdout)["page_id"]
    cli("page", "add", sid, "--page", pid)
    cli("page", "export", pid)
    assert (tmp_path / "output" / f"page_{pid}_My_Page.pdf").exists()


def test_failed_export_leaves_no_partial_file(tmp_path, monkeypatch):
    sid = make_shot(tmp_path)
    pid = json.loads(cli("page", "create", "T", "--json").stdout)["page_id"]
    cli("page", "add", sid, "--page", pid)
    out = tmp_path / "x.pdf"
    monkeypatch.setattr(cp_export, "write_pdf", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    res = cli("page", "export", pid, "--output", out, ok=False)
    assert res.exit_code == 1 and not out.exists()
    assert json.loads(cli("page", "show", pid, "--json").stdout)["page"]["items"]   # page data intact


# ------------------------------------------------------------- 19-20. recovery
def test_recovery_from_missing_and_corrupt_images(tmp_path):
    sid = make_shot(tmp_path)
    pid = json.loads(cli("page", "create", "T", "--json").stdout)["page_id"]
    cli("page", "add", sid, "--page", pid)
    img = Path(json.loads(cli("screenshot", "show", sid, "--json").stdout)["screenshot"]["image_path"])
    listing = json.loads(cli("screenshot", "list", "--json").stdout)["screenshots"][0]
    original = img.read_bytes()
    img.unlink()                                                  # missing
    assert not json.loads(cli("screenshot", "list", "--json").stdout)["screenshots"][0]["image_exists"]
    cli("page", "export", pid, "--output", tmp_path / "a.pdf")
    assert img.exists() and Image.open(img).size == (listing["width_px"], listing["height_px"])
    img.write_bytes(b"this is not a png")                         # corrupt
    cli("preview", sid, "--no-open")
    assert Image.open(img).size == (listing["width_px"], listing["height_px"])
    img.write_bytes(original[:200])                               # truncated
    cli("screenshot", "export", sid, "--output", tmp_path / "e.png")
    assert Image.open(tmp_path / "e.png").size == (listing["width_px"], listing["height_px"])


def test_regenerate_checks_page_fit(tmp_path):
    ids = [make_shot(tmp_path, f"{i}.py", ('print("' + "a" * 60 + '")\n') * 6) for i in range(2)]
    pid = json.loads(cli("page", "create", "T", "--json").stdout)["page_id"]
    cli("page", "add", *ids, "--page", pid)
    before = json.loads(cli("screenshot", "show", ids[0], "--json").stdout)["screenshot"]["height_px"]
    res = cli("screenshot", "regenerate", ids[0], "--columns", "20", ok=False)    # would overflow the page
    assert res.exit_code == 4
    after = json.loads(cli("screenshot", "show", ids[0], "--json").stdout)["screenshot"]["height_px"]
    assert before == after
    cli("screenshot", "regenerate", ids[0], "--no-line-numbers")


# ------------------------------------------------------------- preview, config, doctor
def test_preview_metadata_and_headless_message(tmp_path, monkeypatch):
    sid = make_shot(tmp_path)
    res = cli("preview", "screenshot", sid, "--no-open")
    assert "width_px" in res.output and "printed_mm" in res.output
    res = cli("preview", "--file", Path(json.loads(cli("preview", sid, "--no-open", "--json").stdout)["preview"]["image_path"]), "--no-open")
    assert "PNG" in res.output
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    res = cli("preview", "latest")
    assert "No graphical session" in res.output


def test_config_set_show_precedence(tmp_path):
    cli("config", "set", "font_size", "12")
    cli("config", "set", "line_numbers", "no")
    assert cp_config.load_config()["font_size"] == 12.0 and cp_config.load_config()["line_numbers"] is False
    assert cp_config.load_config({"font_size": 16})["font_size"] == 16.0          # CLI > file
    assert cp_config.load_config()["columns"] == 76                               # default
    shown = json.loads(cli("config", "show", "--json").stdout)
    assert "font_size" in shown["from_file"]
    assert cli("config", "set", "nonsense", "1", ok=False).exit_code == 2
    assert cli("config", "set", "font_size", "abc", ok=False).exit_code == 2
    assert cli("config", "set", "engine", "magic", ok=False).exit_code == 2
    sid = make_shot(tmp_path)                                                     # setting is really applied
    assert json.loads(cli("screenshot", "show", sid, "--json").stdout)["screenshot"]["width_px"] > 0
    assert json.loads(cli("screenshot", "show", sid, "--json").stdout)["screenshot"]["settings"]["font_size"] == 12.0


def test_corrupt_config_is_reported(tmp_path):
    cp_config.config_path().write_text("{not json")
    res = cli("config", "show", ok=False)
    assert res.exit_code == 2 and "BAD_CONFIG" in res.output


def test_doctor_and_setup():
    res = json.loads(cli("doctor", "--json").stdout)
    names = {c["name"]: c for c in res["checks"]}
    assert names["lang:python"]["ok"] is True and names["engine:pillow"]["ok"] and res["ok"]
    info = json.loads(cli("setup", "--json").stdout)
    assert info["pdf"] == "ok"
    assert json.loads(cli("setup", "--json").stdout)["config_created"] is False     # safe to rerun


def test_help_lists_every_command():
    out = cli("--help").output
    for cmd in ("generate", "preview", "export", "doctor", "screenshot", "page", "config"):
        assert cmd in out
    assert "reorder" in cli("page", "--help").output and "regenerate" in cli("screenshot", "--help").output


# ------------------------------------------------------------- end to end
@needs("python3")
def test_end_to_end_sample_to_pdf(tmp_path):
    sid = json.loads(cli("generate", SAMPLES / "hello.py", "--title", "Experiment 01", "--json").stdout)["screenshot"]["id"]
    sid2 = json.loads(cli("generate", SAMPLES / "greet_stdin.py", "-i", SAMPLES / "input.txt", "--json").stdout)["screenshot"]["id"]
    cli("page", "create", "Experiment 01")
    cli("page", "add", sid, "--page", 1)
    cli("page", "add", sid2, "--page", 1)
    cli("page", "preview", 1, "--no-open")
    pdf = tmp_path / "lab.pdf"
    cli("page", "export", 1, "--output", pdf)
    data, boxes = pdf_info(pdf)
    assert len(boxes) >= 1 and b"/Image" in data and len(data) > 5000
    stored = json.loads(cli("screenshot", "show", sid, "--json").stdout)["screenshot"]
    assert "Hello, World!" in stored["stdout"]


# ------------------------------------------------------------- compact images (no titles / labels)
def test_image_has_only_code_and_output(tmp_path):
    secs = cp_render.build_sections("code_output", "print(1)\n", "python",
                                    dict(status="ok", stdout="1\n", exit_code=0))
    html = cp_render.build_html(cp_render.RenderSpec(secs, cp_config.defaults()))
    for word in ("SOURCE CODE", "PROGRAM OUTPUT", "INPUT", "STANDARD ERROR", 'class="title"', 'class="label"'):
        assert word not in html
    res, _ = render(tmp_path)
    assert res.height_px / res.scale < 80            # 2 text rows + 2 thin paddings + 1 rule, nothing else


def test_default_images_print_at_full_size(tmp_path):
    res, _ = render(tmp_path, "x = 1\n" * 3 + "y = '" + "a" * 200 + "'\n")
    assert res.width_px / res.scale * 25.4 / 96 <= 186.0     # fits the default printable width, no downscale


def test_narrow_screenshots_share_a_row(tmp_path):
    a, b = make_shot(tmp_path, "a.py", "print(1)\n"), make_shot(tmp_path, "b.py", "print(2)\n")
    pid = json.loads(cli("page", "create", "T", "--json").stdout)["page_id"]
    cli("page", "add", a, b, "--page", pid)
    items_ = json.loads(cli("page", "show", pid, "--json").stdout)["page"]["items"]
    assert items_[0]["y_mm"] == items_[1]["y_mm"] and items_[0]["x_mm"] < items_[1]["x_mm"]


def test_legacy_config_keys_ignored_silently(tmp_path):
    cp_config.config_path().write_text(json.dumps({"engine": "pillow", "show_input": True}))
    res = cli("config", "show")
    assert "warning" not in res.output and "show_input" not in res.output


def test_title_is_metadata_only(tmp_path):
    src = write(tmp_path, "t.py", "print(1)\n")
    a = json.loads(cli("generate", src, "--json").stdout)["screenshot"]
    b = json.loads(cli("generate", src, "--title", "A very long experiment title", "--json").stdout)["screenshot"]
    assert a["height_px"] == b["height_px"] and b["title"] == "A very long experiment title"
