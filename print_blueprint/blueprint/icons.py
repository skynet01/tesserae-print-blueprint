"""Small callout icons: hot end, infill block, spool, AMS."""
from __future__ import annotations

import math

import numpy as np

from .model import PrintJob
from .theme import Pen


def _fit(box):
    """Map a local 100 x 100 space into box (x, y, w, h), centred."""
    x, y, w, h = box
    s = min(w, h) / 100
    ox, oy = x + (w - 100 * s) / 2, y + (h - 100 * s) / 2
    return (lambda px, py: (ox + px * s, oy + py * s)), s


def _lod(box) -> float:
    return min(1.0, box[2] / 170)


def _nozzle(p: Pen, box, job: PrintJob) -> None:
    T, s = _fit(box)
    L = lambda pts: [T(*q) for q in pts]  # noqa: E731
    bg = p.th["bg"]
    lod = _lod(box)
    p.line(L([(47, 0), (47, 10)]))
    p.line(L([(53, 0), (53, 10)]))
    p.rect(*T(30, 10), *T(70, 34), k=1.2, fill=bg)
    for yy in range(13, 34, 3 if lod >= 0.9 else 6):
        p.line(L([(30, yy), (70, yy)]), k=0.7)
    p.rect(*T(45, 34), *T(55, 42), fill=bg)
    p.rect(*T(24, 42), *T(76, 62), k=1.4, fill=bg)
    p.circ(*T(35, 52), 5.5 * s)
    p.circ(*T(35, 52), 2 * s)
    p.circ(*T(66, 56), 2.5 * s)
    p.rect(*T(40, 62), *T(60, 67), fill=bg)
    p.poly(L([(40, 67), (60, 67), (52.5, 77), (47.5, 77)]), k=1.2, fill=bg)
    rows = [(50, 96), (4, 96), (4, 96)] if lod >= 0.9 else [(14, 86)]
    bead = 9 if lod >= 0.9 else 14
    for row, (x0, x1) in enumerate(rows):
        y = 78 + row * 5
        x = x0
        while x < x1:
            p.ell(*T(x, y), *T(min(x + bead, x1), y + 5), k=0.8)
            x += bead


def _run(p: Pen, m, u0, v0, u1, v1, box, k) -> None:
    pts = [(u0 + (u1 - u0) * i / 40, v0 + (v1 - v0) * i / 40) for i in range(41)]
    r = [q for q in pts if box[0] <= q[0] <= box[1] and box[2] <= q[1] <= box[3]]
    if len(r) > 1:
        p.line([m(*r[0]), m(*r[-1])], k=k)


def _infill(p: Pen, box, job: PrintJob) -> None:
    """Iso block with its front corner cut away: hatched shell outside, sparse infill inside."""
    T, s = _fit(box)
    bg = p.th["bg"]
    a, c, kk = 52, 0.32, 0.8
    c30, s30 = math.cos(math.radians(30)), math.sin(math.radians(30))
    lod = _lod(box)
    sl = job.slice

    def P3(x, y, z):
        return T(50 + (x - y) * c30 * a * kk, 50 + (x + y) * s30 * a * kk - z * a * kk)

    dens = (sl.infill_pct / 100) if sl and sl.infill_pct and sl.infill_pct > 0 else 0.15
    pat = ((sl.pattern if sl else None) or "grid").lower()
    wall = 0.08

    def face(O, U, V):
        def m(u, v):
            return P3(O[0] + u * U[0] + v * V[0], O[1] + u * U[1] + v * V[1], O[2] + u * U[2] + v * V[2])
        p.poly([m(0, 0), m(1, 0), m(1, 1), m(0, 1)], k=0.9, fill=bg)
        bw = wall / (1 - c)
        if lod >= 0.9:
            for t in np.arange(-1, 2, 0.045):
                for bx in ((1 - bw, 1, 0, 1), (0, 1, 1 - bw, 1)):
                    _run(p, m, t, 0, t + 1, 1, bx, 0.3)
        p.line([m(1 - bw, 0), m(1 - bw, 1 - bw), m(0, 1 - bw)], k=0.6)
        inner = (0, 1 - bw, 0, 1 - bw)
        step = max(0.06, min(0.25, 0.04 / max(dens, 0.05))) / max(lod, 0.45)
        if "gyroid" in pat:
            for i in np.arange(0, 1 - bw, step):
                pts = [(i + 0.04 * math.sin(v * 16), v) for v in np.linspace(0, 1 - bw, 24)]
                pts = [q for q in pts if 0 <= q[0] <= 1 - bw]
                if len(pts) > 1:
                    p.line([m(*q) for q in pts], k=0.5)
        else:
            for i in np.arange(-1.2, 1.2, step):
                _run(p, m, i, 0, i + 1, 1, inner, 0.5)
                if "line" not in pat and "rectilinear" not in pat:
                    _run(p, m, i + 1, 0, i, 1, inner, 0.5)

    for poly_ in (
        [P3(1, 0, 0), P3(1, 1, 0), P3(1, 1, c), P3(1, c, c), P3(1, c, 1), P3(1, 0, 1)],
        [P3(0, 1, 0), P3(1, 1, 0), P3(1, 1, c), P3(c, 1, c), P3(c, 1, 1), P3(0, 1, 1)],
        [P3(0, 0, 1), P3(1, 0, 1), P3(1, c, 1), P3(c, c, 1), P3(c, 1, 1), P3(0, 1, 1)],
    ):
        p.poly(poly_, k=1.1, fill=bg)
    zs = 0.055 if lod >= 0.9 else 0.16
    for z in np.arange(zs, 1.0, zs):
        hi = z > c
        p.line([P3(1, 0, z), P3(1, c if hi else 1, z)], k=0.25)
        p.line([P3(0, 1, z), P3(c if hi else 1, 1, z)], k=0.25)
    span = 1 - c
    face((c, c, c), (span, 0, 0), (0, 0, span))
    face((c, c, c), (0, span, 0), (0, 0, span))
    face((c, c, c), (span, 0, 0), (0, span, 0))


def _spool(p: Pen, box, job: PrintJob) -> None:
    T, s = _fit(box)
    cx, cy = T(44, 54)
    lod = _lod(box)
    p.circ(cx, cy, 40 * s, k=1.4, fill=p.th["bg"])
    for r in np.arange(18, 36, 1.6 if lod >= 0.9 else 4.2):
        p.circ(cx, cy, r * s, k=0.4)
    p.circ(cx, cy, 36 * s, k=0.9)
    p.circ(cx, cy, 15 * s, k=1.1, fill=p.th["bg"])
    p.circ(cx, cy, 5 * s, k=1.0)
    for a in range(0, 360, 60):
        ra = math.radians(a)
        p.line([(cx + 5 * s * math.cos(ra), cy + 5 * s * math.sin(ra)),
                (cx + 15 * s * math.cos(ra), cy + 15 * s * math.sin(ra))], k=0.7)


def _ams(p: Pen, box, job: PrintJob) -> None:
    T, s = _fit(box)
    lod = _lod(box)
    trays = job.ams[:9]
    n = len(trays)
    if not n:
        return
    cols = 3 if n > 4 else n
    rows = math.ceil(n / cols)
    cw, rh = 84 / cols, 84 / rows
    for i, t in enumerate(trays):
        x, y = T(8 + cw * (i % cols) + cw / 2, 8 + rh * (i // cols) + rh / 2)
        r = min(cw, rh) * 0.38
        if t.empty:
            p.circ(x, y, r * s, k=0.6)
            p.line([(x - r * s * 0.6, y - r * s * 0.6), (x + r * s * 0.6, y + r * s * 0.6)], k=0.6)
            continue
        act = t.active
        acc = p.th["accent"] if act else None
        p.circ(x, y, r * s, k=2.4 if act else 1.0, col=acc)
        for rr in np.arange(r * 0.45, r * 0.95, r * 0.12 if lod >= 0.9 else r * 0.25):
            p.circ(x, y, rr * s, k=0.4)
        p.circ(x, y, r * 0.3 * s, k=0.8, fill=p.th["bg"])
        if lod >= 0.9:
            p.text((x, y + r * s + 3.5 * s), f"{i + 1}·{t.type}", 3.6 * s, anchor="mm", col=acc)


_ICONS = {"nozzle": _nozzle, "infill": _infill, "spool": _spool, "ams": _ams}


def draw_icon(kind: str, pen: Pen, box: tuple, job: PrintJob) -> None:
    """Draw icon `kind` into box (x, y, w, h) in 1x pen coordinates. Raises KeyError for unknown kinds."""
    _ICONS[kind](pen, box, job)
