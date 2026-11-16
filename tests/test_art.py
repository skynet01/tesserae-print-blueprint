import os
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from blueprint.art import Art, ai_art, preview_art, toolpath_art
from blueprint.icons import draw_icon
from blueprint.model import PrintJob, Tray
from blueprint.theme import SPECTRA_THEMES, THEMES, Pen, Scale, quantise
from blueprint.threemf import read_3mf

FIX = Path(__file__).parent / "fixtures"
PLATE = FIX / "synthetic-plate.3mf"
COVER = FIX / "synthetic-cover.png"


@pytest.fixture(scope="module")
def plate_facts():
    return read_3mf(PLATE.read_bytes())


def test_toolpath_art_nonempty_and_cropped(plate_facts):
    art = toolpath_art(plate_facts, (800, 1000), 1.0)
    assert isinstance(art, Art)
    assert art.ink.shape == art.solid.shape
    assert art.ink.dtype == np.uint8
    assert art.ink.sum() > 0
    assert art.ink.shape[0] <= 1000 and art.ink.shape[1] <= 800
    s = art.solid > 0
    assert s[0].any() and s[-1].any() and s[:, 0].any() and s[:, -1].any()


def test_toolpath_progress_dashes_future(plate_facts):
    full = toolpath_art(plate_facts, (800, 1000), 1.0)
    half = toolpath_art(plate_facts, (800, 1000), 0.5)
    assert full.ink.shape == half.ink.shape
    h = full.ink.shape[0] // 2
    assert half.ink[:h].sum() < full.ink[:h].sum()


@pytest.mark.skipif(os.environ.get("PB_SKIP_PERF") == "1", reason="PB_SKIP_PERF")
def test_toolpath_perf():
    from make_fixtures import sliced_model
    # Keep a substantial layer count without storing a large model in the repo.
    facts = read_3mf(sliced_model("Load test", [("Test block", 80, 80, 8, 6)], 1000, 2.4, .8))
    t0 = time.time()
    art = toolpath_art(facts, (1200, 1200), 1.0)
    assert time.time() - t0 < 8
    assert art.ink.sum() > 0


def test_preview_art_from_cover():
    art = preview_art(COVER.read_bytes(), (600, 600))
    assert art.ink.shape == art.solid.shape
    assert art.ink.sum() > 0 and art.solid.sum() > 0
    assert max(art.ink.shape) <= 600


def test_ai_art_silhouette():
    im = Image.new("L", (400, 400), 255)
    from PIL import ImageDraw
    d = ImageDraw.Draw(im)
    d.ellipse([100, 100, 300, 300], outline=20, width=4)  # hollow ring: solid must be filled
    d.line([(150, 200), (250, 200)], fill=40, width=3)
    import io
    buf = io.BytesIO()
    im.save(buf, "PNG")
    art = ai_art(buf.getvalue(), (300, 300))
    assert art.ink.sum() > 0
    cy, cx = art.solid.shape[0] // 2, art.solid.shape[1] // 2
    assert art.solid[cy, cx] > 0  # centre is filled, ink is not
    assert art.solid.sum() > art.ink.astype(bool).sum()


def test_scale_and_themes():
    sc = Scale(2400, 1200)
    assert sc.u == 1.0 and sc.px(15.6) == 15.6
    for t in (*THEMES.values(), *SPECTRA_THEMES.values()):
        assert {"bg", "ink", "rule", "accent"} <= set(t)


def test_quantise_snaps_to_theme_colours():
    th = SPECTRA_THEMES["white"]
    im = Image.new("RGB", (4, 1), th["bg"])
    im.putpixel((1, 0), (60, 60, 60))
    im.putpixel((2, 0), (190, 40, 40))
    out = quantise(im, th)
    assert len({out.getpixel((x, 0)) for x in range(4)}) <= 3


def _job(**kw):
    from blueprint.model import SliceFacts
    base = dict(
        prefix="p1s", model="P1S", printer_name="x", serial="s", title="t", status="running", progress=0.5,
        layer=1, layers=2, start_ts=None, end_ts=None, nozzle_d=0.4, nozzle_type="hardened_steel",
        nozzle_temp=(220.0, 220.0), bed_temp=(60.0, 60.0), chamber_c=None, bed_type=None, material="PLA",
        weight_g=None, length_m=None, fans={}, speed=None, usage_h=None, objects=None, skipped=None,
        ams=[Tray("PLA", "ff0000", True, False), Tray("PETG", "00ff00", False, False),
             Tray("", "", False, True), Tray("PLA", "0000ff", False, False)],
        ams_humidity=None, tools=[], slice=None, cover_png=None, job_key="k")
    base.update(kw)
    return PrintJob(**base)


@pytest.mark.parametrize("kind", ["nozzle", "infill", "spool", "ams"])
@pytest.mark.parametrize("theme", ["blue", "white", "black"])
def test_icons_small_not_blobs(kind, theme):
    th = SPECTRA_THEMES[theme]
    pen = Pen.canvas(104, 104, th, 1.0)
    pen.minw = 2
    draw_icon(kind, pen, (0, 0, 104, 104), _job())
    out = quantise(pen.finish(), th)
    a = np.asarray(out).astype(int)
    bg = np.asarray(quantise(Image.new("RGB", (1, 1), th["bg"]), th))[0, 0].astype(int)
    frac = (np.abs(a - bg).sum(-1) > 0).mean()
    assert 0.01 < frac < 0.35, frac


def test_unknown_kind_raises():
    pen = Pen.canvas(50, 50, THEMES["blue"], 1.0)
    with pytest.raises(KeyError):
        draw_icon("tree", pen, (0, 0, 50, 50), _job())


@pytest.mark.parametrize("box", [(900, 500), (2400, 2000), (600, 400)])
def test_drawn_layer_gap_measured(plate_facts, box):
    """Measure the gap directly in a column through the wall face."""
    art = toolpath_art(plate_facts, box, 1.0)
    col = art.ink[:, art.ink.shape[1] // 4] > 120
    rows = np.where(col)[0]
    # collapse runs
    starts = rows[np.r_[True, np.diff(rows) > 1]]
    gaps = np.diff(starts)
    assert len(gaps) > 3
    assert np.median(gaps) >= 3


def test_preview_art_opaque_cover():
    im = Image.open(COVER).convert("RGBA")
    bg = Image.new("RGBA", im.size, (240, 240, 240, 255))
    bg.alpha_composite(im)
    import io
    buf = io.BytesIO()
    bg.convert("RGB").save(buf, "PNG")
    art = preview_art(buf.getvalue(), (600, 600))
    frac = (art.solid > 0).mean()
    assert 0.1 < frac < 0.8  # background not treated as part
