"""Themes, scale unit, the supersampled drawing pen and panel quantiser."""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_PATH = str(Path(__file__).resolve().parent.parent / "fonts" / "osifont-lgpl3fe.ttf")

# Spectra 6: the colours the panel's packer takes as its own inks (Tesserae's nominal palette), and
# what each looks like on the glass. Sheets carry the nominal ones: Tesserae dithers anything else,
# and a measured profile first squeezes 0..255 into the measured black..white, so the measured
# colours themselves come out as dither noise (thin text breaks up). Only pure ink stays solid.
SPECTRA = {
    "black": ((0, 0, 0), (0x1F, 0x22, 0x26)),
    "white": ((255, 255, 255), (0xB9, 0xC7, 0xC9)),
    "yellow": ((255, 255, 0), (0xC1, 0xBB, 0x1E)),
    "red": ((255, 0, 0), (0x62, 0x20, 0x1E)),
    "blue": ((0, 0, 255), (0x23, 0x3F, 0x8E)),
    "green": ((0, 255, 0), (0x35, 0x56, 0x3A)),
}

THEMES = {
    "blue": dict(bg=(28, 70, 136), ink=(236, 242, 255), rule=(150, 176, 214), accent=(236, 242, 255)),
    "white": dict(bg=(244, 241, 234), ink=(34, 46, 76), rule=(140, 146, 160), accent=(168, 44, 38)),
    "black": dict(bg=(6, 10, 18), ink=(70, 168, 255), rule=(40, 96, 150), accent=(140, 210, 255)),
}
SPECTRA_THEMES = {
    "blue": dict(bg=SPECTRA["blue"][0], ink=(255, 255, 255), rule=(255, 255, 255), accent=(255, 255, 255)),
    "white": dict(bg=(255, 255, 255), ink=(0, 0, 0), rule=(0, 0, 0), accent=SPECTRA["red"][0]),
    "black": dict(bg=(0, 0, 0), ink=(255, 255, 255), rule=(255, 255, 255), accent=(255, 255, 255)),
}


class Scale:
    """Scale unit u = min(w, h) / 1200; every size on the sheet is a multiple of u."""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.u = min(w, h) / 1200

    def px(self, v: float) -> float:
        return v * self.u


@lru_cache(maxsize=256)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_PATH, max(1, size))


class Pen:
    """ImageDraw wrapper working in 1x coordinates on a canvas `ss` times larger.

    `minw` is the thinnest line in supersampled pixels (use `ss` to guarantee one output pixel)."""

    minw = 1

    def __init__(self, img: Image.Image, th: dict, lw: float = 1.0, ss: int = 2):
        self.img, self.th, self.lw, self.ss = img, th, lw, ss
        self.dr = ImageDraw.Draw(img)

    @classmethod
    def canvas(cls, w: int, h: int, th: dict, lw: float = 1.0, ss: int = 2) -> "Pen":
        return cls(Image.new("RGB", (int(w * ss), int(h * ss)), th["bg"]), th, lw, ss)

    def finish(self) -> Image.Image:
        """Downsample the canvas to 1x."""
        w, h = self.img.width // self.ss, self.img.height // self.ss
        return self.img.resize((w, h), Image.LANCZOS)

    def w(self, k: float = 1.0) -> int:
        return max(self.minw, int(round(k * self.lw * self.ss)))

    def _p(self, pts):
        s = self.ss
        return [(x * s, y * s) for x, y in pts]

    def line(self, pts, k=1.0, col=None):
        self.dr.line(self._p(pts), fill=col or self.th["ink"], width=self.w(k), joint="curve")

    def poly(self, pts, k=1.0, fill=None, col=None):
        self.dr.polygon(self._p(pts), fill=fill, outline=col or self.th["ink"], width=self.w(k))

    def rect(self, x0, y0, x1, y1, k=1.0, fill=None, col=None):
        s = self.ss
        self.dr.rectangle([x0 * s, y0 * s, x1 * s, y1 * s], fill=fill, outline=col or self.th["ink"], width=self.w(k))

    def circ(self, cx, cy, r, k=1.0, fill=None, col=None):
        s = self.ss
        self.dr.ellipse([(cx - r) * s, (cy - r) * s, (cx + r) * s, (cy + r) * s], fill=fill,
                        outline=col or self.th["ink"], width=self.w(k))

    def ell(self, x0, y0, x1, y1, k=1.0, fill=None, col=None):
        s = self.ss
        self.dr.ellipse([x0 * s, y0 * s, x1 * s, y1 * s], fill=fill, outline=col or self.th["ink"], width=self.w(k))

    def text(self, xy, s, size, col=None, anchor="la"):
        self.dr.text((xy[0] * self.ss, xy[1] * self.ss), s, font=_font(int(size * self.ss)),
                     fill=col or self.th["ink"], anchor=anchor)

    def tlen(self, s, size) -> float:
        return self.dr.textlength(s, font=_font(int(size * self.ss))) / self.ss

    def arrow(self, x, y, ang, size=9, col=None):
        a = math.radians(ang)
        p1 = (x - size * math.cos(a - 0.35), y - size * math.sin(a - 0.35))
        p2 = (x - size * math.cos(a + 0.35), y - size * math.sin(a + 0.35))
        self.dr.polygon(self._p([(x, y), p1, p2]), fill=col or self.th["ink"])


def quantise(img: Image.Image, th: dict) -> Image.Image:
    """Snap to the theme's own panel inks (bg, ink, accent), as nominal panel colours.

    Accent only where the pixel really is accent, not anti-aliasing grey. `glass()` shows the result
    as the panel paints it."""
    cols: list = []
    for c in (th["bg"], th["ink"], th["accent"]):
        if tuple(c) not in cols:
            cols.append(tuple(c))
    a = np.asarray(img.convert("RGB")).astype(np.int32)
    dist = ((a[:, :, None, :] - np.array(cols)[None, None]) ** 2).sum(-1)
    if len(cols) > 2:
        dist[:, :, 2] = np.where(dist[:, :, 2] < 6000, 0, 10 ** 9)
    return Image.fromarray(np.array(cols, np.uint8)[dist.argmin(-1)])


def glass(img: Image.Image) -> Image.Image:
    """A quantised Spectra sheet as the glass shows it (measured inks), for previews only."""
    a = np.asarray(img.convert("RGB"))
    out = a.copy()
    for nom, meas in SPECTRA.values():
        out[(a == nom).all(-1)] = meas
    return Image.fromarray(out)
