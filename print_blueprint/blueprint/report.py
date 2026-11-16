"""What the layout drew, and the invariants it must satisfy.

Boxes are (x0, y0, x1, y1) in output pixels. Text boxes already include the small padding margin,
so two texts that merely touch count as overlapping. Groups tie texts, segments and blocks to the
thing that owns them ("title", "stats", "callout:2", "frame", ...)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

Box = tuple[float, float, float, float]
Pt = tuple[float, float]

MIN_UNIT = 0.7  # smallest scale unit: small cells keep text legible and drop callouts instead
MIN_HERO = 0.30  # hero box area / largest box of its shape that fits the border
MAX_DIAG_ON_PART = 0.15  # share of a leader's diagonal that may run over the part


@dataclass
class LayoutReport:
    size: tuple[int, int]
    border: Box  # padded inner rect: all text stays inside
    hero_box: tuple[int, int, int, int]  # (x0, y0, x1, y1); empty when the sheet has no art
    hero_mask: np.ndarray  # full-sheet uint8 0/1 silhouette
    texts: list[tuple[str, Box]]  # every text/icon box drawn
    segments: list[tuple[Pt, Pt]]  # every leader segment: diagonal (anchor -> joint), shoulder
    blocks: list[Box]  # title / stats / times / rack / panels
    dropped: list[str]  # callouts and panels with no valid spot
    text_groups: list[str] = field(default_factory=list)
    segment_groups: list[str] = field(default_factory=list)
    block_groups: list[str] = field(default_factory=list)
    has_art: bool = True  # art existed; False only for a text-only sheet (no art, no cover)


def sheet_unit(size: tuple[int, int]) -> float:
    """The scale unit every size on the sheet is a multiple of."""
    return max(min(size) / 1200, MIN_UNIT)


# ---------------------------------------------------------------- geometry (shared with the layout)
def boxes_touch(a: Box, b: Box) -> bool:
    """Closed boxes: sharing an edge counts."""
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


EPS = 1e-6  # float sums of u multiples overshoot by ~1e-13 px on some sizes (1000 x 1000)


def inside(a: Box, outer: Box) -> bool:
    return (a[0] >= outer[0] - EPS and a[1] >= outer[1] - EPS and a[2] <= outer[2] + EPS
            and a[3] <= outer[3] + EPS)


def seg_box(a: Pt, b: Pt, box: Box) -> bool:
    """Does segment ab touch the closed box (Liang-Barsky)?"""
    x0, y0 = a
    dx, dy = b[0] - x0, b[1] - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - box[0]), (dx, box[2] - x0), (-dy, y0 - box[1]), (dy, box[3] - y0)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _orient(a: Pt, b: Pt, c: Pt) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on(a: Pt, b: Pt, c: Pt) -> bool:
    return min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9 and \
        min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9


def seg_seg(a: Pt, b: Pt, c: Pt, d: Pt) -> bool:
    """Do segments ab and cd touch or cross (collinear overlap included)?"""
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    eps = 1e-9
    if ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and \
            ((o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)):
        return True
    return (abs(o1) <= eps and _on(a, b, c)) or (abs(o2) <= eps and _on(a, b, d)) or \
        (abs(o3) <= eps and _on(c, d, a)) or (abs(o4) <= eps and _on(c, d, b))


def mask_in_box(mask: np.ndarray, box: Box) -> bool:
    h, w = mask.shape
    x0, y0 = max(0, math.floor(box[0])), max(0, math.floor(box[1]))
    x1, y1 = min(w, math.ceil(box[2]) + 1), min(h, math.ceil(box[3]) + 1)
    return x1 > x0 and y1 > y0 and bool(mask[y0:y1, x0:x1].any())


def seg_samples(a: Pt, b: Pt, skip: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Integer pixel samples along ab, one per pixel of length, the first `skip` px left out."""
    n = int(max(abs(b[0] - a[0]), abs(b[1] - a[1]))) + 1
    t = np.linspace(0, 1, n)
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length > 0:
        t = t[t * length >= skip]
    xs = np.floor(a[0] + (b[0] - a[0]) * t).astype(int)
    ys = np.floor(a[1] + (b[1] - a[1]) * t).astype(int)
    return xs, ys


def on_mask(mask: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> int:
    h, w = mask.shape
    ok = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
    return int(mask[ys[ok], xs[ok]].astype(bool).sum())


def hero_fill(hero_box, border: Box) -> float:
    """Hero box area over the largest box of the same shape that fits inside the border."""
    w, h = hero_box[2] - hero_box[0], hero_box[3] - hero_box[1]
    if w <= 0 or h <= 0:
        return 0.0
    bw, bh = border[2] - border[0], border[3] - border[1]
    s = min(bw / w, bh / h)
    return 1.0 / (s * s)


# ---------------------------------------------------------------- checker
def check_layout(r: LayoutReport) -> list[str]:
    """Human-readable violations; empty means clean."""
    out: list[str] = []
    n = len(r.texts)
    tg = r.text_groups if len(r.text_groups) == n else ["?"] * n
    sg = r.segment_groups if len(r.segment_groups) == len(r.segments) else [f"?{i}" for i in range(len(r.segments))]
    bg = r.block_groups if len(r.block_groups) == len(r.blocks) else [f"?{i}" for i in range(len(r.blocks))]
    mask = r.hero_mask.astype(bool) if r.hero_mask is not None else np.zeros(r.size[::-1], bool)
    u = sheet_unit(r.size)

    for (s, b), g in zip(r.texts, tg):
        if g != "frame" and not inside(b, r.border):
            out.append(f"text {s!r} outside the border")
        if g != "frame" and mask_in_box(mask, b):
            out.append(f"text {s!r} overlaps the part")
    for i in range(n):
        for j in range(i + 1, n):
            if boxes_touch(r.texts[i][1], r.texts[j][1]):
                out.append(f"texts {r.texts[i][0]!r} and {r.texts[j][0]!r} overlap")
    for (s, b), g in zip(r.texts, tg):
        for blk, gb in zip(r.blocks, bg):
            if gb != g and boxes_touch(b, blk):
                out.append(f"text {s!r} ({g}) overlaps block {gb}")
    for i, blk in enumerate(r.blocks):
        if not inside(blk, r.border):
            out.append(f"block {bg[i]} outside the border")
        if mask_in_box(mask, blk):
            out.append(f"block {bg[i]} overlaps the part")
        for j in range(i + 1, len(r.blocks)):
            if boxes_touch(blk, r.blocks[j]):
                out.append(f"blocks {bg[i]} and {bg[j]} overlap")

    for k, ((a, b), g) in enumerate(zip(r.segments, sg)):
        shoulder = abs(a[1] - b[1]) < 1e-6
        if not (inside((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), r.border)):
            out.append(f"leader {g} leaves the border")
        for (s, box), gt in zip(r.texts, tg):
            if shoulder and gt == g and box[3] <= a[1] + 1e-6:
                continue  # its own heading, sitting on the shoulder (notes and icon hang below it)
            if seg_box(a, b, box):
                out.append(f"leader {g} crosses text {s!r}")
        for blk, gb in zip(r.blocks, bg):
            if seg_box(a, b, blk):
                out.append(f"leader {g} crosses block {gb}")
        for m in range(k + 1, len(r.segments)):
            if sg[m] != g and seg_seg(a, b, *r.segments[m]):
                out.append(f"leaders {g} and {sg[m]} cross")
        if shoulder:
            if on_mask(mask, *seg_samples(a, b)):
                out.append(f"shoulder {g} runs over the part")
        else:
            xs, ys = seg_samples(a, b, skip=10 * u)
            if len(xs) and on_mask(mask, xs, ys) > MAX_DIAG_ON_PART * len(xs):
                out.append(f"leader {g} runs through the part")

    if r.has_art:
        fill = hero_fill(r.hero_box, r.border)
        if fill < MIN_HERO:
            out.append(f"hero fills {fill:.2f} of its largest fit (< {MIN_HERO})")
    return out
