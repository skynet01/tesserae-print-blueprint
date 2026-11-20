"""Shade completed image rows without replacing the Mezzotint drawing."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .art import Art
from .model import PrintJob, SheetOptions

_FONT = Path(__file__).resolve().parents[1] / "fonts" / "osifont-lgpl3fe.ttf"


def _label(ink, mask, cut, text):
    """Seat the small knockout label against the cut, without covering a hole."""
    h, w = mask.shape
    font = ImageFont.truetype(str(_FONT), max(10, round(max(h, w) / 48)))
    l, t, r, b = font.getbbox(text)
    width, height = int(r - l) + 4, int(b - t) + 4
    for y in (cut - height, cut + 1):
        if y < 0 or y + height > h:
            continue
        clear = mask[y:y + height].all(axis=0)
        ends = np.flatnonzero(np.diff(clear, prepend=False, append=False)).reshape(-1, 2)
        if not len(ends):
            continue
        start, end = max(ends, key=lambda span: span[1] - span[0])
        if end - start < width:
            continue
        x = (start + end - width) // 2
        stamp = Image.new('L', (width, height))
        pen = ImageDraw.Draw(stamp)
        pen.rectangle((0, 0, width - 1, height - 1), outline=255)
        pen.text((2 - l, 2 - t), text, font=font, fill=255)
        ink[y:y + height, x:x + width] = np.asarray(stamp)
        return


def apply_progress(art: Art, job: PrintJob, opts: SheetOptions) -> Art:
    if not opts.show_progress:
        return art
    fraction = job.progress
    if not np.isfinite(fraction) or fraction <= 0:
        return art
    mask = art.solid > 127
    rows = np.flatnonzero(mask.any(axis=1))
    if not rows.size:
        return art
    stop = int(rows[-1]) + 1
    cut = stop - round(min(1., fraction) * (stop - int(rows[0])))
    if cut == stop:
        return art
    ink = art.ink.copy()
    region = ink[cut:stop]
    inside = mask[cut:stop]
    if opts.panel in ('bw', 'spectra', 'grey4'):
        # A deliberate pen hatch, not palette dithering. Open gaps preserve texture.
        y, x = np.ogrid[cut:stop, :ink.shape[1]]
        unit = max(1, round(max(ink.shape) / 720))
        region[inside & ((x + y) % (7 * unit) < 2 * unit)] = 255
    else:
        # Composite ink over a flat 1/3-strength wash. Even the dark tones retain contrast.
        region[inside] = 85 + (region[inside].astype(np.uint16) * 2 // 3)
    if fraction < 1:
        region[0, inside[0]] = 255
        text = f'L{job.layer}' if job.layer is not None else f'{round(100 * fraction)}%'
        _label(ink, mask, cut, text)
    return Art(ink, art.solid)
