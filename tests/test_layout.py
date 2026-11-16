import dataclasses
import os
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from blueprint.art import Art, preview_art, toolpath_art
from blueprint.model import SheetOptions
from blueprint.render import render_sheet
from blueprint.gcode import layer_heights
from blueprint.layout import NEAR
from blueprint.report import LayoutReport, check_layout, sheet_unit
from blueprint.theme import SPECTRA_THEMES, quantise

import corpus

SIZES = [(1200, 1600), (1440, 2560), (3840, 2160), (2560, 1440), (480, 800), (600, 400)]
NATURAL_PANEL = {(1200, 1600): "spectra", (1440, 2560): "grey16", (3840, 2160): "colour",
                 (2560, 1440): "colour", (480, 800): "grey4", (600, 400): "colour"}
BACKGROUNDS = ["blue", "white", "black"]
# Both original test designs exercise every normal screen size and paper.
REPRESENTATIVE = ("synthetic-",)
FULL = os.environ.get("PB_FULL_CORPUS") == "1"
OUT = os.environ.get("PB_OUT")

_ART: dict = {}  # (key, box, progress, u) -> Art, shared by the whole session


def art_factory(job, size, progress=None):
    """art_fn for render_sheet, cached per (job, box)."""
    u = sheet_unit(size)
    pr = job.progress if progress is None else progress

    def fn(box):
        k = (job.job_key, tuple(box), round(pr, 3), round(u, 4))
        if k not in _ART:
            if job.slice is not None:
                _ART[k] = toolpath_art(job.slice, box, pr, u=u)
            else:
                _ART[k] = preview_art(job.cover_png, box)
        return _ART[k]
    return fn


def render(job, size, **opts):
    o = SheetOptions(**opts)
    return render_sheet(job, art_factory(job, size, None if o.show_progress else 1.0), size, o)


def _save(img, name):
    if OUT:
        Path(OUT).mkdir(parents=True, exist_ok=True)
        img.save(Path(OUT) / f"{name}.png")


def assert_anchor_semantics(rep):
    """TOP OF PART sits on the top of the part, FIRST LAYER on its bottom, wherever they are drawn."""
    ys = np.where(rep.hero_mask.any(1))[0]
    if not len(ys):
        return
    top, bot = ys[0], ys[-1]
    near = NEAR * (bot - top) + 2
    for (a, b), g in zip(rep.segments, rep.segment_groups):
        if a[1] == b[1]:
            continue  # shoulder
        if g == "callout:top":
            assert a[1] - top <= near, (g, a, top)
        if g == "callout:first":
            assert bot - a[1] <= near, (g, a, bot)


def _cases():
    files = corpus.corpus_files()
    cases = []
    for i, f in enumerate(files):
        rep = f.name.startswith(REPRESENTATIVE)
        sizes = SIZES if (FULL or rep) else [(1200, 1600)]
        for size in sizes:
            bgs = BACKGROUNDS if (FULL or rep) else ["blue"]
            panels = ["spectra", "grey16", "colour", "grey4"] if FULL else (
                ["spectra"] if size == (1200, 1600) and not rep else [NATURAL_PANEL[size]])
            for bg in bgs:
                for panel in panels:
                    cases.append(pytest.param(f, i, size, bg, panel, id=f"{f.name[:12]}-{size[0]}x{size[1]}-{bg}-{panel}"))
    return cases


@pytest.mark.skipif(not corpus.corpus_files(), reason="no corpus at $PB_CORPUS")
@pytest.mark.parametrize("path,idx,size,bg,panel", _cases())
def test_corpus_layout_clean(path, idx, size, bg, panel):
    # alternate the live data: P1S states, H3C states (tool rack, 9 AMS slots); every third print finished
    states = "h3c" if idx % 2 else "p1s"
    job = corpus.job_from_3mf(path, states, progress=1.0 if idx % 3 == 2 else None,
                              model="H3C" if states == "h3c" else "P1S")
    img, rep = render(job, size, background=bg, panel=panel)
    _save(img, f"{path.stem[:20]}-{size[0]}x{size[1]}-{bg}-{panel}")
    assert img.size == size
    assert check_layout(rep) == []
    assert_anchor_semantics(rep)


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("bg", BACKGROUNDS)
def test_synthetic_cover_layout_clean(size, bg):
    job = corpus.h3c_job()
    img, rep = render(job, size, method="preview", background=bg, panel=NATURAL_PANEL[size])
    _save(img, f"h3c-{size[0]}x{size[1]}-{bg}")
    assert check_layout(rep) == []
    assert_anchor_semantics(rep)


def test_wide_part_uses_vertical_room():
    job = corpus.h3c_job()
    img, rep = render(job, (1200, 1600), method="preview", background="blue", panel="spectra")
    x0, y0, x1, y1 = rep.hero_box
    b = rep.border
    assert (x1 - x0) >= 0.8 * (b[2] - b[0]), (rep.hero_box, b)
    assert check_layout(rep) == []


def test_no_slicer_callouts_without_3mf():
    job = corpus.h3c_job()
    _, rep = render(job, (1200, 1600), method="preview")
    words = ("WALLS", "INFILL", "SUPPORT", "AIR")
    bad = [s for s, _ in rep.texts if any(w in s for w in words)]
    assert bad == []
    assert any(s.startswith("FIRST LAYER") for s, _ in rep.texts)


def _heading_h(rep):
    hs = [b[3] - b[1] for s, b in rep.texts if s.startswith("Ø")]
    assert hs, [s for s, _ in rep.texts]
    return hs[0]


@pytest.fixture(scope="module")
def plate_job():
    return corpus.job_from_3mf(corpus.FIX / "synthetic-plate.3mf", "p1s", progress=.5)


def test_text_scales_with_size(plate_job):
    _, small = render(plate_job, (1200, 1600))
    _, big = render(plate_job, (3840, 2160))
    assert _heading_h(big) >= 1.6 * _heading_h(small)


def test_spectra_uses_only_theme_colours(plate_job):
    for bg in BACKGROUNDS:
        img, rep = render(plate_job, (1200, 1600), background=bg, panel="spectra")
        th = SPECTRA_THEMES[bg]
        allowed = {tuple(quantise(Image.new("RGB", (1, 1), c), th).getpixel((0, 0)))
                   for c in (th["bg"], th["ink"], th["accent"])}
        cols = {c for _, c in img.convert("RGB").getcolors(1 << 20)}
        assert cols <= allowed, cols - allowed


@pytest.mark.parametrize("panel,levels", [("grey16", 16), ("grey4", 4), ("bw", 2)])
def test_grey_panels_level_count(plate_job, panel, levels):
    img, _ = render(plate_job, (600, 800), panel=panel)
    g = np.asarray(img.convert("L"))
    assert len(np.unique(g)) <= levels


def test_show_progress_false_hides_layer_callout(plate_job):
    _, on = render(plate_job, (1200, 1600))
    _, off = render(plate_job, (1200, 1600), show_progress=False)
    assert any(s.startswith("NOZZLE · LAYER") for s, _ in on.texts)
    assert not any("LAYER" in s and s.startswith("NOZZLE") for s, _ in off.texts)


def test_options_drop_notes_and_icons(plate_job):
    _, rep = render(plate_job, (1200, 1600), show_icons=False, callout_notes=False)
    assert not any(s.startswith("icon:") for s, _ in rep.texts)
    assert not any("SHELL" in s or "TO GO" in s for s, _ in rep.texts)
    assert check_layout(rep) == []


def test_tool_rack_and_panels_when_data(plate_job):
    job = corpus.h3c_job()
    _, rep = render(job, (2560, 1440), method="preview")
    texts = [s for s, _ in rep.texts]
    assert "TOOL RACK" in texts
    assert any(s.startswith("TEMPERATURES") for s in texts)
    _, rep2 = render(plate_job, (1200, 1600))
    assert "TOOL RACK" not in [s for s, _ in rep2.texts]


def test_art_rerendered_at_final_size(plate_job, monkeypatch):
    """When the hero ends up well below the first render, the art is requested again at the final
    size instead of downsampling heavy line art."""
    import blueprint.layout as L
    monkeypatch.setattr(L, "SCALES", (0.75,))  # force a hero at 0.75 of the first render
    calls = []
    base = art_factory(plate_job, (1200, 1600))

    def fn(box):
        calls.append(tuple(box))
        return base(box)
    _, rep = render_sheet(plate_job, fn, (1200, 1600), SheetOptions())
    assert len(calls) == 2, calls
    x0, y0, x1, y1 = rep.hero_box
    assert calls[1] == (x1 - x0, y1 - y0)
    monkeypatch.setattr(L, "SCALES", (1.0,))
    calls.clear()
    render_sheet(plate_job, fn, (1200, 1600), SheetOptions())
    assert len(calls) == 1  # full size: the first render is used as is


def test_empty_art_falls_back_to_cover_or_text_only():
    job = corpus.h3c_job()
    empty = Art(np.zeros((1, 1), np.uint8), np.zeros((1, 1), np.uint8))
    img, rep = render_sheet(job, lambda box: empty, (1200, 1600), SheetOptions())
    assert rep.hero_box[2] > rep.hero_box[0]  # cover used
    assert check_layout(rep) == []
    job.cover_png = None
    img, rep = render_sheet(job, lambda box: empty, (1200, 1600), SheetOptions())
    assert img.size == (1200, 1600)
    assert rep.hero_box[2] == rep.hero_box[0]
    assert all(s != "FIRST LAYER" for s, _ in rep.texts)
    assert check_layout(rep) == []


# ---------------------------------------------------------------- checker unit tests
def _rep(**kw):
    base = dict(size=(400, 400), border=(10, 10, 390, 390), hero_box=(100, 100, 320, 320),
                hero_mask=np.zeros((400, 400), np.uint8), texts=[], segments=[], blocks=[], dropped=[])
    base["hero_mask"][100:320, 100:320] = 1
    base.update(kw)
    return LayoutReport(**base)


def test_checker_clean():
    assert check_layout(_rep()) == []


def _one(**kw):
    v = check_layout(_rep(**kw))
    assert len(v) >= 1, v
    return " | ".join(v)


def test_checker_text_rules():
    assert "overlap" in _one(texts=[("A", (20, 20, 60, 40)), ("B", (50, 30, 90, 50))], text_groups=["a", "b"])
    assert "outside the border" in _one(texts=[("A", (0, 20, 60, 40))], text_groups=["a"])
    assert "overlaps the part" in _one(texts=[("A", (150, 150, 160, 160))], text_groups=["a"])
    assert "overlaps block b" in _one(texts=[("A", (20, 20, 60, 40))], text_groups=["a"],
                                      blocks=[(15, 15, 80, 80)], block_groups=["b"])


def test_checker_block_rules():
    assert "blocks a and b overlap" in _one(blocks=[(20, 20, 60, 60), (50, 50, 90, 90)], block_groups=["a", "b"])
    assert "block a overlaps the part" in _one(blocks=[(90, 90, 120, 120)], block_groups=["a"])


def test_checker_leader_rules():
    assert "crosses text 'A'" in _one(texts=[("A", (20, 20, 60, 40))], text_groups=["a"],
                                      segments=[((10, 30), (80, 30))], segment_groups=["c"])
    assert "crosses block b" in _one(blocks=[(20, 330, 60, 380)], block_groups=["b"],
                                     segments=[((70, 320), (10, 350))], segment_groups=["c"])
    assert "leaders a and b cross" in _one(segments=[((20, 20), (80, 80)), ((20, 80), (80, 20))],
                                           segment_groups=["a", "b"])
    assert "shoulder c runs over the part" in _one(segments=[((90, 200), (200, 200))], segment_groups=["c"])
    assert "leader c runs through the part" in _one(segments=[((110, 110), (300, 300))], segment_groups=["c"])


def test_checker_hero_rules():
    m = np.zeros((400, 400), np.uint8)
    m[190:210, 190:210] = 1
    assert "hero fills" in _one(hero_box=(190, 190, 210, 210), hero_mask=m)
    # art was available but no hero was drawn
    assert "hero fills 0.00" in _one(hero_box=(0, 0, 0, 0), hero_mask=np.zeros((400, 400), np.uint8))
    # a text-only sheet (no art at all) is fine
    assert check_layout(_rep(hero_box=(0, 0, 0, 0), hero_mask=np.zeros((400, 400), np.uint8), has_art=False)) == []


def test_checker_own_heading_only():
    head = ("HEAD", (330, 20, 380, 40))
    note = ("NOTE", (330, 42, 380, 55))
    shoulder = ((325, 40), (385, 40))
    diag = ((300, 60), (325, 40))
    ok = _rep(texts=[head], text_groups=["c1"], segments=[diag, shoulder], segment_groups=["c1", "c1"])
    assert check_layout(ok) == []  # its heading sits on the shoulder
    bad = _rep(texts=[head, note], text_groups=["c1", "c1"], segments=[diag, shoulder], segment_groups=["c1", "c1"])
    bad.texts[1] = ("NOTE", (330, 39, 380, 55))  # a note reaching up over its own shoulder
    assert "crosses text 'NOTE'" in " ".join(check_layout(bad))
    low = _rep(texts=[("HEAD", (330, 20, 380, 45))], text_groups=["c1"], segments=[diag, shoulder],
               segment_groups=["c1", "c1"])  # heading box hanging below the line
    assert "crosses text 'HEAD'" in " ".join(check_layout(low))


def test_layer_z_is_the_real_layer_height(plate_job):
    _, rep = render(plate_job, (1200, 1600))
    zs = layer_heights(plate_job.slice.gcode)
    want = f"Z {zs[plate_job.layer - 1]:.1f} MM,"
    assert any(s.startswith(want) for s, _ in rep.texts), [s for s, _ in rep.texts if s.startswith("Z ")]


def test_layer_z_omitted_without_heights(plate_job):
    import dataclasses
    sl = dataclasses.replace(plate_job.slice, layer_z=[])
    job = dataclasses.replace(plate_job, slice=sl, job_key="plate_job-noz")
    _, rep = render(job, (1200, 1600))
    notes = [s for s, _ in rep.texts if "LAYERS TO GO" in s]
    assert notes and not any(s.startswith("Z ") for s in notes)


@pytest.mark.parametrize("status,want,label", [
    ("running", "%", "ETA"), ("pause", "PAUSED", "ETA"), ("finish", "PRINT COMPLETE", "COMPLETED"),
    ("failed", "PRINT FAILED", "ENDED"), ("idle", "LAST PRINT", "ENDED"), ("offline", "LAST PRINT", "ENDED"),
    ("unknown", "LAST PRINT", "ENDED")])
def test_status_wording(plate_job, status, want, label):
    import dataclasses
    _, rep = render(dataclasses.replace(plate_job, status=status), (1200, 1600))
    texts = [s for s, _ in rep.texts]
    assert any(want in s for s in texts), texts
    assert label in texts
    if status not in ("running", "pause"):
        assert "ETA" not in texts and not any(s.startswith("NOZZLE · LAYER") for s in texts)
    if status in ("idle", "offline", "unknown", "failed"):
        assert "PRINT COMPLETE" not in texts and "COMPLETED" not in texts


def test_skipped_only_when_known():
    import dataclasses
    job = corpus.h3c_job()
    job = dataclasses.replace(job, objects=3, skipped=None)
    _, rep = render(job, (2560, 1440), method="preview")
    objs = [s for s, _ in rep.texts if s.startswith("3")]
    assert "3" in objs and not any("SKIPPED" in s for s, _ in rep.texts)
    _, rep = render(dataclasses.replace(job, skipped=1), (2560, 1440), method="preview")
    assert any(s == "3 · 1 SKIPPED" for s, _ in rep.texts)


def test_unit_floor_keeps_small_text_proportional(plate_job):
    """One floor on the unit (0.7), so a tiny cell is a scaled-down door sheet, nothing else."""
    _, door = render(plate_job, (1200, 1600))
    _, mini = render(plate_job, (480, 800))
    _, small = render(plate_job, (600, 400))
    for r in (mini, small):
        st = [b[3] - b[1] for s, b in r.texts if s == "STARTED"]
        st0 = [b[3] - b[1] for s, b in door.texts if s == "STARTED"]
        assert st and abs(st[0] / st0[0] - 0.7) < 0.08, (st, st0)


@pytest.mark.parametrize("filename,states", [("synthetic-plate.3mf", "p1s"), ("synthetic-plate.3mf", "h3c"), ("synthetic-block.3mf", "h3c")])
def test_cramped_cell_gives_up_chrome_for_the_part(filename, states):
    """600 x 400 at the unit floor: wide parts only reach the hero floor once optional chrome goes
    (rack, second title line, stats, subtitle, full-height times), each listed in `dropped`."""
    job = corpus.job_from_3mf(corpus.FIX / filename, states, model="H3C" if states == "h3c" else "P1S")
    _, rep = render(job, (600, 400), background="blue", panel="colour")
    assert check_layout(rep) == []
    assert {"stats", "subtitle"} & set(rep.dropped), rep.dropped


def test_tiny_sheet_with_no_room_for_stats():
    """A cell too narrow for even one stats row draws without the stats block (no crash)."""
    import dataclasses
    job = dataclasses.replace(corpus.h3c_job(), model="X1C", material=None, layers=None, weight_g=None,
                              chamber_c=None, slice=None)
    img, rep = render_sheet(job, lambda box: preview_art(job.cover_png, box), (240, 320), SheetOptions())
    assert img.size == (240, 320)
    assert "stats" not in rep.block_groups
    assert check_layout(rep) == []  # the status line fits the narrow times block too


def test_panels_keep_the_part_out_without_stats():
    """600x400: the stats block is given up, the left sensor panels stay; the part must not run under them."""
    job = corpus.h3c_job()
    img, rep = render(job, (600, 400), method="preview", background="blue", panel="colour")
    assert check_layout(rep) == []


def test_wide_part_fills_a_tall_sheet(plate_job):
    """A wide part on a tall sheet uses the full width (callouts go above and below)."""
    def art(box):  # a 4:1 part fitted to the box, as the real art functions do
        w = int(min(box[0], box[1] * 4))
        a = np.zeros((w // 4, w), np.uint8)
        a[w // 40:-(w // 40), :] = 255
        return Art(a.copy(), a.copy())
    for size in [(1440, 2560), (1200, 1600)]:
        img, rep = render_sheet(plate_job, art, size, SheetOptions(background="black", panel="grey16"))
        b, h = rep.border, rep.hero_box
        assert (h[2] - h[0]) >= 0.95 * (b[2] - b[0]), size
        assert check_layout(rep) == []


@pytest.mark.parametrize("printer,key,want", [("H3C Printer", "a1b2c3d4e5", "H3C PRINTER"), ("", "001", "PLATE 001"),
                                              ("", "", "PRINT")])
def test_cjk_title_falls_back_to_printer_or_plate(plate_job, printer, key, want):
    import dataclasses
    job = dataclasses.replace(plate_job, title="中文夹具", printer_name=printer, job_key=key)
    img, rep = render_sheet(job, lambda box: Art(np.zeros((1, 1), np.uint8), np.zeros((1, 1), np.uint8)),
                            (1200, 1600), SheetOptions())
    assert rep.texts[[g for g in rep.text_groups].index("title")][0] == want


@pytest.mark.parametrize("size", [(1000, 1000), (1000, 1200)])
def test_square_cells_with_a_tool_rack_are_clean(size):
    """1e-13 px float overshoot at the border is not a violation."""
    job = corpus.h3c_job(progress=0.6)
    _, rep = render(job, size, method="preview")
    assert check_layout(rep) == []


def test_infill_note_says_what_is_air(plate_job):
    _, rep = render(plate_job, (1200, 1600))
    texts = [s for s, _ in rep.texts]
    assert any(s.endswith("% OF THE INFILL IS AIR") for s in texts), texts
    assert not any("AIR INSIDE" in s for s in texts)


def test_nozzle_heading_has_one_temperature_source(plate_job):
    """Live target while printing, else the slicer's temperature; the icon carries no number."""
    import dataclasses
    from blueprint.layout import callouts
    slicer = plate_job.slice.nozzle_temp
    assert slicer
    live = dataclasses.replace(plate_job, status="running", nozzle_temp=(180.0, slicer + 7))
    head = next(c.head for c in callouts(live, SheetOptions(), True) if c.key == "nozzle")
    assert head.endswith(f"{slicer + 7:.0f} °C"), head
    done = dataclasses.replace(plate_job, status="finish", progress=1.0, nozzle_temp=(31.0, 0.0))
    head = next(c.head for c in callouts(done, SheetOptions(), True) if c.key == "nozzle")
    assert head.endswith(f"{slicer:.0f} °C"), head
    idle = dataclasses.replace(plate_job, status="idle", nozzle_temp=(31.0, 0.0), slice=None, nozzle_d=0.4)
    head = next(c.head for c in callouts(idle, SheetOptions(), False) if c.key == "nozzle")
    assert "°C" not in head  # no slicer value and not printing: no temperature at all


def test_nozzle_icon_draws_no_numbers(plate_job):
    import dataclasses
    from blueprint.icons import draw_icon
    from blueprint.theme import THEMES, Pen

    def pixels(job):
        pen = Pen.canvas(240, 240, THEMES["blue"], ss=2)
        draw_icon("nozzle", pen, (10, 10, 200, 200), job)
        return np.asarray(pen.finish())
    a = pixels(dataclasses.replace(plate_job, nozzle_d=0.4, nozzle_temp=(215.0, 220.0)))
    b = pixels(dataclasses.replace(plate_job, nozzle_d=None, nozzle_temp=None))
    assert (a == b).all()


def test_unknown_model_never_prints_a_bare_maker(plate_job):
    import dataclasses
    job = dataclasses.replace(plate_job, model="", printer_name="H3C Printer")
    _, rep = render(job, (1200, 1600))
    texts = [s for s, _ in rep.texts]
    assert "PRINTER" not in texts and "BAMBU LAB" not in texts
    assert not any(s.startswith("BAMBU LAB") for s in texts), texts
    assert any(s.startswith('"H3C PRINTER"') for s in texts), texts
    _, rep = render(dataclasses.replace(plate_job, model="P1S"), (1200, 1600))
    assert "BAMBU LAB P1S" in [s for s, _ in rep.texts]


def _temp_texts(job):
    _, rep = render(job, (2560, 1440), method="preview")
    return [s for s, _ in rep.texts]


def test_finished_print_without_slice_shows_no_live_temperatures():
    """A finished cloud print has no 3MF: the cooling bed (26 °C) must not read as the first-layer temperature."""
    job = dataclasses.replace(corpus.h3c_job(), status="finish", bed_temp=(26.0, 0.0), nozzle_temp=(31.0, 0.0))
    texts = _temp_texts(job)
    assert not any("26 °C" in s or "31 °C" in s for s in texts), texts
    assert "BED" not in texts and not any("NOZZLE ·" in s and "°C" in s for s in texts)


def test_running_print_without_slice_shows_live_temperatures():
    job = dataclasses.replace(corpus.h3c_job(), status="running", bed_temp=(55.0, 60.0), nozzle_temp=(215.0, 220.0))
    texts = _temp_texts(job)
    assert any(s.startswith("FIRST LAYER") and "60 °C" in s for s in texts), texts
    assert any(s.startswith("TEMPERATURES") for s in texts)


def test_finished_print_with_slice_uses_slicer_temperatures(plate_job):
    sl = dataclasses.replace(plate_job.slice, bed_temp=55.0, nozzle_temp=210.0)
    job = dataclasses.replace(plate_job, slice=sl, status="finish", bed_temp=(26.0, 0.0), nozzle_temp=(31.0, 0.0))
    texts = _temp_texts(job)
    assert any(s.startswith("FIRST LAYER") and "55 °C" in s for s in texts), texts
    assert not any("26 °C" in s for s in texts)


def test_clock_follows_the_locale_style():
    from blueprint.layout import hm
    t = time.mktime((2026, 11, 14, 14, 7, 0, 0, 0, -1))
    assert hm(t, "h12") == ("2:07", "PM") and hm(t, "h23") == ("14:07", "")


def test_spectra_sheet_uses_pure_panel_inks():
    """Tesserae dithers any colour that is not one of the panel's own inks; thin text then breaks up."""
    from blueprint.theme import SPECTRA
    nominal = {v[0] for v in SPECTRA.values()}
    for th in SPECTRA_THEMES.values():
        assert {tuple(th[k]) for k in ("bg", "ink", "rule", "accent")} <= nominal


def test_clock_follows_the_tesserae_timezone():
    from blueprint.layout import hm
    t = 1794700000.0  # 2026-11-14 23:46:40 UTC
    assert hm(t, "h23", "UTC") == ("23:46", "")
    assert hm(t, "h12", "America/Phoenix") == ("4:46", "PM")
    assert hm(t, "h23", "Not/AZone") == hm(t, "h23")  # an unknown zone falls back to local time


def test_unknown_support_draws_no_support_callout():
    from blueprint.layout import callouts
    """Slice metadata that doesn't say whether supports were used must not print NO SUPPORT."""
    import dataclasses
    block = Path(__file__).parent / "fixtures" / "synthetic-block.3mf"
    job = corpus.job_from_3mf(block, "p1s")
    assert job.slice is not None
    assert any(c.key == "support" for c in callouts(job, SheetOptions(), False))
    job = dataclasses.replace(job, slice=dataclasses.replace(job.slice, support=None))
    assert not any(c.key == "support" for c in callouts(job, SheetOptions(), False))
