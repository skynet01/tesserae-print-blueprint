"""Public entry: draw the blueprint sheet for a print job."""
from __future__ import annotations

from typing import Callable

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFilter

from .art import Art
from .icons import draw_icon
from .layout import SS, Metrics, compose
from .model import PrintJob, SheetOptions
from .report import LayoutReport
from .theme import SPECTRA, SPECTRA_THEMES, THEMES, Pen, quantise

TWO_TONE = ("spectra", "bw", "grey4")  # thresholded art, >= 1 panel pixel lines, few colours


def render_sheet(job: PrintJob, art_fn: Callable[[tuple[int, int]], Art], size: tuple[int, int],
                 opts: SheetOptions, note: str = "") -> tuple[Image.Image, LayoutReport]:
    """Render the sheet at `size`.

    `art_fn(box)` returns the hero Art fitted to box (w, h). It is called once with the largest
    free region and again with the final hero size when that is much smaller, so heavy line art
    is redrawn rather than downsampled. Empty art falls back to the cover image, then to a
    text-only sheet. `note` is a footnote drawn into the sheet (why the picture fell back)."""
    W, H = size
    two = opts.panel in TWO_TONE
    th = dict((SPECTRA_THEMES if two else THEMES)[opts.background])
    plan = compose(job, art_fn, size, opts, accent_eta=opts.background == "white", note=note)
    m = Metrics(W, H)
    pen = Pen.canvas(W, H, th, lw=m.lw, ss=SS)
    if two:
        pen.minw = SS

    for op in plan.ops:
        if op[0] == "grid" and op[1]:
            _grid(pen, th, m, op[2:], two)
    if plan.hero is not None:
        _hero(pen, th, m, plan.hero, opts.background == "blue" and opts.show_grid, two)
    for op in plan.ops:
        _draw(pen, th, m, op, job)

    img = pen.finish()
    if opts.panel == "grey16":
        img = img.convert("L").point(lambda v: (v // 17) * 17).convert("RGB")
    elif opts.panel == "spectra":
        img = quantise(img, th)
    elif opts.panel == "grey4":
        img = _snap(img, th).convert("L").point(lambda v: min(255, int(round(v / 85)) * 85)).convert("RGB")
    elif opts.panel == "bw":
        img = _snap(img, th).convert("L").point(lambda v: 255 if v >= 128 else 0).convert("RGB")
    # Chips use native panel colours after quantisation; the sheet's text keeps its own inks.
    for op in plan.ops:
        if op[0] == "swatch":
            _swatch(img, th, m, op, opts.panel)
    return img, plan.report


def _swatch(img: Image.Image, th: dict, m: Metrics, op, panel: str) -> None:
    _, x0, y0, x1, y1, color = op
    x0, y0, x1, y1 = (round(v) for v in (x0, y0, x1, y1))
    ink, bg = th["ink"], th["bg"]
    mono = panel in ("grey16", "grey4", "bw")
    if mono:
        def grey(c):
            v = Image.new("RGB", (1, 1), c).convert("L").getpixel((0, 0))
            return (v // 17 * 17 if panel == "grey16" else
                    min(255, round(v / 85) * 85) if panel == "grey4" else 255 if v >= 128 else 0, ) * 3
        ink, bg = grey(ink), grey(bg)
    try:
        fill = ImageColor.getrgb(color) if not mono else None
    except ValueError:
        fill = None
    if fill is not None and panel == "spectra":
        fill = min((c[0] for c in SPECTRA.values()), key=lambda c: sum((a - b) ** 2 for a, b in zip(c, fill)))
    d = ImageDraw.Draw(img)
    d.rectangle((x0, y0, x1, y1), fill=fill or bg, outline=ink, width=max(1, round(m.lw)))
    if fill is None:
        # A visible hatch identifies a loaded spool even when its colour is white or unknown.
        for x in range(x0 + 3, x1 - 1, max(3, round(4 * m.u))):
            d.line((x, y1 - 2, min(x1 - 2, x + 3), y0 + 2), fill=ink, width=1)


def _grid(pen: Pen, th, m: Metrics, rect, two: bool) -> None:
    x0, y0, x1, y1 = rect
    g = 40 * m.u
    s = pen.ss
    if two:  # sparse 1 px dots: a two-colour panel has no lighter blue
        ink = th["ink"]
        yy = y0 + g
        while yy < y1 - 1:
            for xx in np.arange(x0 + 10, x1 - 1, 10):
                pen.dr.rectangle([int(xx) * s, int(yy) * s, int(xx) * s + s - 1, int(yy) * s + s - 1], fill=ink)
            yy += g
        xx = x0 + g
        while xx < x1 - 1:
            for yv in np.arange(y0 + 10, y1 - 1, 10):
                pen.dr.rectangle([int(xx) * s, int(yv) * s, int(xx) * s + s - 1, int(yv) * s + s - 1], fill=ink)
            xx += g
        return
    tint = tuple(min(255, c + 9) for c in th["bg"])
    yy = y0 + g
    while yy < y1 - 1:
        pen.dr.line([(x0 * s, int(yy) * s), (x1 * s, int(yy) * s)], fill=tint, width=s)
        yy += g
    xx = x0 + g
    while xx < x1 - 1:
        pen.dr.line([(int(xx) * s, y0 * s), (int(xx) * s, y1 * s)], fill=tint, width=s)
        xx += g


def _hero(pen: Pen, th, m: Metrics, hero, clean_grid: bool, two: bool) -> None:
    ink, sol, x0, y0 = hero
    h, w = ink.shape
    s = pen.ss
    size = (w * s, h * s)
    if clean_grid:  # keep the grid out from behind the part
        k = 2 * max(1, round(2 * m.u)) + 1
        clean = Image.fromarray(np.where(sol > 60, 255, 0).astype(np.uint8)).filter(ImageFilter.MaxFilter(k))
        pen.img.paste(th["bg"], (x0 * s, y0 * s), clean.resize(size, Image.NEAREST))
    if two:
        a = Image.fromarray(np.where(ink > 80, 255, 0).astype(np.uint8)).resize(size, Image.NEAREST)
    else:
        a = Image.fromarray(ink).resize(size, Image.BILINEAR)
    pen.img.paste(th["ink"], (x0 * s, y0 * s), a)


def _draw(pen: Pen, th, m: Metrics, op, job: PrintJob) -> None:
    k = op[0]
    if k == "text":
        _, xy, s, size, anchor, col = op
        pen.text(xy, s, size, col=th[col], anchor=anchor)
    elif k == "line":
        _, pts, kk, col = op
        pen.line(pts, k=kk, col=th[col])
    elif k == "rect":
        _, x0, y0, x1, y1, kk, col = op
        pen.rect(x0, y0, x1, y1, k=kk, col=th[col])
    elif k == "dots":
        _, x0, x1, y, step = op
        r = max(0.5, 0.6 * m.u)
        xx = x0
        while xx <= x1:
            pen.circ(xx, y, r, k=0.3, fill=th["ink"], col=th["ink"])
            xx += step
    elif k == "leader":
        pen.line(op[1], k=0.45, col=th["rule"])
    elif k == "anchor":
        pen.circ(op[1], op[2], 2.4 * m.u + 0.6, k=0.4, fill=th["ink"], col=th["ink"])
    elif k == "icon":
        draw_icon(op[1], pen, op[2], job)


def _snap(img: Image.Image, th: dict) -> Image.Image:
    """Nearest of the theme's nominal colours (accent only where the pixel really is accent)."""
    cols = []
    for c in (th["bg"], th["ink"], th["accent"]):
        if tuple(c) not in cols:
            cols.append(tuple(c))
    a = np.asarray(img.convert("RGB")).astype(np.int32)
    dist = ((a[:, :, None, :] - np.array(cols)[None, None]) ** 2).sum(-1)
    if len(cols) > 2:
        dist[:, :, 2] = np.where(dist[:, :, 2] < 6000, 0, 10 ** 9)
    return Image.fromarray(np.array(cols, np.uint8)[dist.argmin(-1)])
