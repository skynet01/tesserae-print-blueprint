"""Sheet composition: frame, fixed blocks, hero sizing and collision-checked callouts.

Everything here is planning: `compose()` returns drawing ops in 1x coordinates plus the
LayoutReport; `render.py` turns the ops into pixels. All sizes are multiples of the scale unit
u = min(w, h) / 1200 (with a legibility floor for text on very small cells)."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from PIL import Image

from .art import Art
from .model import PrintJob, SheetOptions
from .report import (MAX_DIAG_ON_PART, MIN_HERO, Box, LayoutReport, boxes_touch, hero_fill, inside, on_mask,
                     seg_box, seg_samples, seg_seg, sheet_unit)
from .theme import _font
from .titles import pretty_title

SS = 2  # supersampling of the drawing canvas
LEADER_DEG = 32
TAN = math.tan(math.radians(LEADER_DEG))
RISES = (1.8, 2.6, 3.6, 5.0, 7.0, 9.5, 12.5)  # x heading size
DRIFTS = (0, -0.05, 0.05, -0.1, 0.1, -0.16, 0.16, -0.24, 0.24, -0.32, 0.32, -0.42, 0.42)
DRIFTS_TIGHT = (0, -0.04, 0.04, -0.08, 0.08)  # callouts that name a place on the part
NEAR = 0.08  # share of the part height that still counts as its top / bottom
TB_FRACS = tuple(np.linspace(0.04, 0.96, 13))
SCALES = (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5)
LADDER = 5  # steps of optional chrome a cramped sheet may give up for the part (see compose)
RERENDER_BELOW = 0.85  # final hero / first render: below this the art is drawn again at the final size
LIVE = {"running", "pause", "paused", "prepare"}  # statuses that get live wording
_KEEP = {"≈"}


# ---------------------------------------------------------------- metrics and text
@dataclass
class Metrics:
    w: int
    h: int

    def __post_init__(self):
        u = self.u = sheet_unit((self.w, self.h))  # every size below is a multiple of u
        self.pad = 20 * u
        self.tm = 2.5 * u  # padding margin around every recorded text box
        self.gap = 6 * u  # label <-> leader joint
        self.lw = 1.25 * u
        self.head = 18.5 * u
        self.note = 15 * u
        self.icon = 104 * u
        self.title = 54 * u
        self.sub = 16.5 * u
        self.row = 18 * u
        self.prow = 16 * u
        self.rack = 16.5 * u
        self.tlabel = 16.5 * u
        self.tdigit = 48 * u
        self.status = 16 * u
        self.hero_gap = 3 * u  # extra air between labels and the part
        self.lgap = 5 * u  # extra air between a label and other labels, blocks and leaders


def clean(s: str) -> str:
    return "".join(ch for ch in str(s) if ord(ch) < 0x250 or ch in _KEEP)


def tlen(s: str, size: float) -> float:
    return _font(int(size * SS)).getlength(s) / SS


def tbox(s: str, size: float, x: float, y: float, anchor: str = "la") -> Box:
    """Ink box of text drawn by Pen.text at (x, y)."""
    l, t, r, b = _font(int(size * SS)).getbbox(s, anchor=anchor)
    return (x + l / SS, y + t / SS, x + r / SS, y + b / SS)


def pitch(size: float, factor: float, m: "Metrics") -> float:
    """Line spacing: factor x size, but never so tight that padded text boxes touch."""
    l, t, r, b = _font(int(size * SS)).getbbox("ÅÉgjØ,", anchor="la")
    return max(factor * size, (b - t) / SS + 2 * m.tm + max(0.5, 0.5 * m.u))


def grow(b: Box, d: float) -> Box:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def union(boxes) -> Box:
    bs = list(boxes)
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))


def hm(ts: float, clock: str = "h23", tz: str = "") -> tuple[str, str]:
    """(digits, AM/PM or "") for a clock time in the device locale's style (`h12` or `h23`), in
    the zone `tz` (else the process's local time)."""
    try:
        t = datetime.fromtimestamp(ts, ZoneInfo(tz)) if tz else datetime.fromtimestamp(ts)
    except (ZoneInfoNotFoundError, ValueError):
        t = datetime.fromtimestamp(ts)
    if clock != "h12":
        return f"{t.hour:02d}:{t.minute:02d}", ""
    return f"{t.hour % 12 or 12}:{t.minute:02d}", ("AM" if t.hour < 12 else "PM")


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}H {(sec % 3600) // 60:02d}M"


def _pos(v) -> float | None:
    """Numbers <= 0 are unknown."""
    try:
        return float(v) if v is not None and float(v) > 0 else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- sheet accumulator
@dataclass
class Sheet:
    m: Metrics
    ops: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    tgroups: list = field(default_factory=list)
    segs: list = field(default_factory=list)
    sgroups: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    bgroups: list = field(default_factory=list)
    dropped: list = field(default_factory=list)

    def text(self, xy, s, size, group, anchor="la", col="ink") -> Box:
        s = clean(s)
        b = grow(tbox(s, size, xy[0], xy[1], anchor), self.m.tm)
        self.ops.append(("text", xy, s, size, anchor, col))
        self.texts.append((s, b))
        self.tgroups.append(group)
        return b

    def block(self, box, group):
        self.blocks.append(box)
        self.bgroups.append(group)


class Group:
    """Ops and text boxes of one fixed block, built at the origin and moved into place."""

    def __init__(self, m: Metrics, name: str):
        self.m, self.name = m, name
        self.ops: list = []
        self.texts: list = []  # (s, box)
        self.extra: list = []  # non-text extents (lines, rects)

    def text(self, xy, s, size, anchor="la", col="ink") -> Box:
        s = clean(s)
        b = grow(tbox(s, size, xy[0], xy[1], anchor), self.m.tm)
        self.ops.append(("text", xy, s, size, anchor, col))
        self.texts.append((s, b))
        return b

    def line(self, pts, k=1.0, col="ink"):
        self.ops.append(("line", pts, k, col))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.extra.append((min(xs), min(ys), max(xs), max(ys)))

    def rect(self, x0, y0, x1, y1, k=1.0, col="ink"):
        self.ops.append(("rect", x0, y0, x1, y1, k, col))
        self.extra.append((x0, y0, x1, y1))

    def dots(self, x0, x1, y, step):
        self.ops.append(("dots", x0, x1, y, step))

    @property
    def box(self) -> Box:
        return union([b for _, b in self.texts] + self.extra)

    def moved(self, dx, dy) -> "Group":
        g = Group(self.m, self.name)
        g.texts = [(s, (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)) for s, b in self.texts]
        g.extra = [(b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy) for b in self.extra]
        for op in self.ops:
            k = op[0]
            if k == "text":
                g.ops.append((k, (op[1][0] + dx, op[1][1] + dy), *op[2:]))
            elif k == "line":
                g.ops.append((k, [(x + dx, y + dy) for x, y in op[1]], *op[2:]))
            elif k in ("rect", "swatch"):
                g.ops.append((k, op[1] + dx, op[2] + dy, op[3] + dx, op[4] + dy, *op[5:]))
            elif k == "dots":
                g.ops.append((k, op[1] + dx, op[2] + dx, op[3] + dy, op[4]))
        return g

    def commit(self, sh: Sheet):
        sh.ops += self.ops
        for s, b in self.texts:
            sh.texts.append((s, b))
            sh.tgroups.append(self.name)
        sh.block(self.box, self.name)


def _fit_text(s: str, size: float, width: float, ellipsis: str = "..") -> str | None:
    """s, cut with an ellipsis until it fits (None if nothing useful is left)."""
    if tlen(s, size) <= width:
        return s
    while len(s) > 2:
        s = s[:-1].rstrip()
        if tlen(s + ellipsis, size) <= width:
            return s + ellipsis
    return None


def dotted_rows(g: Group, x, y, w, rows, fs, head=None) -> float:
    """Key ........ value rows (a heading, underlined, on top). Rows that cannot fit are skipped."""
    m = g.m
    if head:
        hb = g.text((x, y), head, fs * 1.15)
        g.line([(x, hb[3] - m.tm + 1.5 * m.u), (hb[2] - m.tm, hb[3] - m.tm + 1.5 * m.u)], k=0.8)
        y = max(y + fs * 1.9, hb[3] + 2.5 * m.u + m.tm + 1)
    step = pitch(fs, 1.4, m)
    for k, v in rows:
        k, v = clean(k), clean(v)
        kw = tlen(k, fs)
        room = w - kw - 4 * m.tm - 2 * m.u
        v = _fit_text(v, fs, room) if room > 0 else None
        if v is None:
            continue
        kb = g.text((x, y), k, fs)
        vb = g.text((x + w, y), v, fs, anchor="ra")
        a, b = kb[2] + m.u, vb[0] - m.u
        if b - a > fs * 0.6:
            g.dots(a, b, y + fs * 0.8, fs * 0.42)
        y += step
    return y


# ---------------------------------------------------------------- callouts
@dataclass
class Callout:
    key: str
    head: str
    notes: list
    frac: float  # preferred height on the part, 0 top .. 1 bottom
    side: str  # preferred side L | R
    icon: str | None
    weight: int
    where: str = "free"  # free | top | bottom | sides: where on the part the dot may sit


def callouts(job: PrintJob, opts: SheetOptions, toolpath: bool) -> list[Callout]:
    """Each callout only when its data exists (slicer callouts need the 3MF)."""
    sl = job.slice
    out: list[Callout] = []
    hz = _pos(sl.envelope_mm[2]) if sl else None
    end = _end_ts(job)
    layer, layers = job.layer, job.layers or (sl.layers if sl and sl.layers > 0 else None)
    printing = job.status in LIVE and job.progress < 1
    if printing and toolpath and opts.show_progress and layer and layers:
        notes = []
        left = max(0, layers - layer)
        zs = sl.layer_z if sl else []
        z = zs[layer - 1] if len(zs) == layers and 0 < layer <= layers else None
        notes.append((f"Z {z:.1f} MM, " if z is not None else "") + f"{left} LAYERS TO GO.")
        if end:
            notes.append(f"THE LAST ONE LANDS AT {' '.join(hm(end, opts.clock, opts.tz)).strip()}.")
        out.append(Callout("layer", f"NOZZLE · LAYER {layer} OF {layers}", notes,
                           (1 - layer / layers) * 0.97, "L", None, 5, "sides"))
    if hz:
        out.append(Callout("top", f"TOP OF PART · Z {hz:.1f}", [], 0.03, "L", None, 2, "top"))

    d = _pos(job.nozzle_d) or (_pos(sl.nozzle_d) if sl else None)
    # one source for the temperature: the live target while printing, else what the slicer set
    live_t = _pos(job.nozzle_temp[1]) if job.nozzle_temp and job.status in LIVE else None
    t = live_t or (_pos(sl.nozzle_temp) if sl else None)
    if d or t:
        head = " · ".join(x for x in (f"Ø{d:g} NOZZLE" if d else "NOZZLE", f"{t:.0f} °C" if t else "") if x)
        notes = []
        if job.nozzle_type:
            notes.append(job.nozzle_type.replace("_", " ").upper())
        if sl and _pos(sl.density) and _pos(sl.weight_g) and _pos(sl.prediction_s):
            notes.append(f"≈ {sl.weight_g / sl.density * 1000 / sl.prediction_s:.1f} MM³ / S")
        out.append(Callout("nozzle", head, notes, 0.12, "R", "nozzle", 3))

    if sl and _pos(sl.walls) and _pos(sl.infill_pct):
        w, inf = int(sl.walls), sl.infill_pct
        head = f"{w} WALL{'S' if w != 1 else ''} · {inf:g}%" + (f" {sl.pattern.upper()}" if sl.pattern else "")
        notes = []
        if _pos(sl.line_w):
            notes.append(f"{w * sl.line_w:.2f} MM SHELL")
        notes.append(f"{100 - inf:.0f} % OF THE INFILL IS AIR")
        out.append(Callout("walls", head, notes, 0.4, "R", "infill", 2))

    name = (sl.filament_name if sl else None) or job.material or (sl.material if sl else None)
    grams = (_pos(sl.weight_g) if sl else None) or _pos(job.weight_g)
    if name or grams:
        head = " · ".join(x for x in ((name or "").upper(), f"{grams:.0f} G" if grams else "") if x)
        metres = (_pos(sl.length_m) if sl else None) or _pos(job.length_m)
        first = []
        if metres:
            first.append(f"{metres:.1f} M")
        if grams and sl and _pos(sl.density):
            first.append(f"{grams / sl.density:.0f} CM³")
        notes = [" · ".join(first)] if first else []
        if metres and hz:
            notes.append(f"{metres * 1000 / hz:.0f}× ITS HEIGHT")
        out.append(Callout("material", head, notes, 0.86, "R", "spool", 3))

    bed = (sl.bed_type if sl else None) or job.bed_type
    # a finished print's bed is cooling: its live reading is only used while the print is live
    live_bt = (_pos(job.bed_temp[1]) or _pos(job.bed_temp[0])) if job.bed_temp and job.status in LIVE else None
    bt = (_pos(sl.bed_temp) if sl else None) or live_bt
    if bed or bt:
        parts = ["FIRST LAYER"] + ([bed.replace("_", " ").upper()] if bed else []) + ([f"{bt:.0f} °C"] if bt else [])
        out.append(Callout("first", " · ".join(parts), [], 0.98, "L", None, 2, "bottom"))

    if sl and sl.support is not None:  # unknown: no callout rather than a wrong one
        if sl.support:
            kind = re.split(r"[(\s]", (sl.support_type or "").strip())[0].upper()
            head = f"{kind + ' ' if kind else ''}SUPPORT" + (f" · {sl.support_angle:g}°" if _pos(sl.support_angle) else "")
        else:
            head = "NO SUPPORT"
        out.append(Callout("support", head, [], 0.66, "R", None, 1))

    for c in out:
        if not opts.callout_notes:
            c.notes = []
        if not opts.show_icons:
            c.icon = None
    return out


@dataclass
class Shape:
    """A callout's label laid out relative to (x0 = left of the text, y = shoulder height)."""
    co: Callout
    width: float  # shoulder length
    rel: list  # (s, box) relative boxes (inflated); icon as "icon:<kind>"
    ops: list  # relative ops
    ubox: Box  # union of rel boxes


def shape(co: Callout, m: Metrics) -> Shape:
    hs, ns = m.head, m.note
    rel, ops = [], []
    hb = tbox(clean(co.head), hs, 0, 0, "ls")
    lift = hb[3] + m.tm + 1.5 * m.u  # padded box ends just above the shoulder
    rel.append((co.head, grow((hb[0], hb[1] - lift, hb[2], hb[3] - lift), m.tm)))
    ops.append(("text", (0, -lift), clean(co.head), hs, "ls", "ink"))
    hw = tlen(co.head, hs)
    nws = [tlen(t, ns) for t in co.notes]
    nw = max(nws or [0])
    ic = m.icon if co.icon else 0
    top = 6 * m.u + m.tm
    step = pitch(ns, 1.3, m)
    if co.icon:
        rel.append((f"icon:{co.icon}", grow((0, top, ic, top + ic), m.tm)))
        ops.append(("icon", co.icon, (0, top, ic, ic)))
        x = ic + 10 * m.u
        yy = top + max(0.0, (ic - len(co.notes) * step) / 2)
        width = max(hw, x + nw if co.notes else ic)
    else:
        x = 0
        yy = top + ns * 0.2
        width = max(hw, nw)
    for t in co.notes:
        rel.append((t, grow(tbox(clean(t), ns, x, yy), m.tm)))
        ops.append(("text", (x, yy), clean(t), ns, "la", "ink"))
        yy += step
    width = width + 2 * m.gap
    return Shape(co, width, rel, ops, union(b for _, b in rel))


class Outline:
    """Outline lookups on the placed silhouette (sheet coordinates)."""

    def __init__(self, sol: np.ndarray, x0: int, y0: int):
        m = sol
        self.x0, self.y0 = x0, y0
        rows = np.where(m.any(1))[0]
        cols = np.where(m.any(0))[0]
        self.rows, self.cols = rows, cols
        self.rmin = np.argmax(m, 1)
        self.rmax = m.shape[1] - 1 - np.argmax(m[:, ::-1], 1)
        self.cmin = np.argmax(m, 0)
        self.cmax = m.shape[0] - 1 - np.argmax(m[::-1, :], 0)

    def side(self, side: str, frac: float):
        top, bot = self.rows[0], self.rows[-1]
        y = top + (bot - top) * frac
        yy = int(self.rows[np.argmin(np.abs(self.rows - y))])
        x = self.rmin[yy] if side == "L" else self.rmax[yy]
        return self.x0 + x + (0 if side == "L" else 1), self.y0 + yy + 0.5

    def tb(self, where: str, frac: float):
        l, r = self.cols[0], self.cols[-1]
        x = l + (r - l) * frac
        xx = int(self.cols[np.argmin(np.abs(self.cols - x))])
        y = self.cmin[xx] if where == "T" else self.cmax[xx]
        return self.x0 + xx + 0.5, self.y0 + y + (0 if where == "T" else 1)


class Field:
    """What is already on the sheet, for collision checks."""

    def __init__(self, m: Metrics, border: Box, hero: np.ndarray, texts: list, blocks: list):
        self.m, self.border, self.hero = m, border, hero
        self.boxes = list(texts) + list(blocks)  # nothing may touch these
        self.segs: list = []  # (a, b, group)
        self.anchors: list = []  # leader dots already on the part
        sat = np.zeros((hero.shape[0] + 1, hero.shape[1] + 1), np.int32)
        sat[1:, 1:] = hero.astype(np.int32).cumsum(0).cumsum(1)
        self.sat = sat

    def hero_in(self, b: Box) -> bool:
        h, w = self.hero.shape
        x0, y0 = max(0, math.floor(b[0])), max(0, math.floor(b[1]))
        x1, y1 = min(w, math.ceil(b[2]) + 1), min(h, math.ceil(b[3]) + 1)
        if x1 <= x0 or y1 <= y0:
            return False
        s = self.sat
        return bool(s[y1, x1] - s[y0, x1] - s[y1, x0] + s[y0, x0])


def _anchors(co: Callout, ol: Outline):
    """(ax, ay, dd, signs, penalty) candidates on all four sides of the outline."""
    out = []
    tight = co.where != "free"
    top, bot = ol.rows[0], ol.rows[-1]
    near = NEAR * (bot - top)

    def ok(a) -> bool:
        """TOP OF PART / FIRST LAYER dots must sit on the top / bottom of the part, not a dent."""
        y = a[1] - ol.y0
        if co.where == "top":
            return y - top <= near
        if co.where == "bottom":
            return bot + 1 - y <= near
        return True
    for side in ("L", "R"):
        dd = -1 if side == "L" else 1
        other = 0 if side == co.side else 25
        seen = set()
        for df in (DRIFTS_TIGHT if tight else DRIFTS):
            fr = min(0.99, max(0.01, co.frac + df))
            a = ol.side(side, fr)
            if a in seen or not ok(a):
                continue
            seen.add(a)
            out.append((a[0], a[1], dd, (1, -1), abs(df) * 90 + other))
    if co.where == "sides":
        return out
    pref_x = 0.25 if co.side == "L" else 0.75
    for where in ("T", "B"):
        if (co.where == "top" and where != "T") or (co.where == "bottom" and where != "B"):
            continue
        sign = -1 if where == "T" else 1
        other = 10 + (0 if (where == "T") == (co.frac < 0.5) else 15)
        seen = set()
        for fr in TB_FRACS:
            a = ol.tb(where, float(fr))
            if a in seen or not ok(a):
                continue
            seen.add(a)
            for dd in (-1, 1):
                out.append((a[0], a[1], dd, (sign,), abs(fr - pref_x) * 45 + other))
    return out


def _try(sh: Shape, fld: Field, ax, ay, dd, sign, r, m: Metrics):
    """Geometry of one candidate, or None when it breaks a rule."""
    ly = ay + sign * r
    ex = ax + dd * r / TAN
    end = ex + dd * sh.width
    x0 = (ex + m.gap) if dd > 0 else (end + m.gap)
    ub = sh.ubox
    U = (ub[0] + x0, ub[1] + ly, ub[2] + x0, ub[3] + ly)
    B = fld.border
    if not inside(U, B):
        return None
    if not (B[0] <= min(ax, ex, end) and max(ax, ex, end) <= B[2] and B[1] <= min(ay, ly) and max(ay, ly) <= B[3]):
        return None
    Ug = grow(U, m.lgap)
    for o in fld.boxes:
        if boxes_touch(Ug, o):
            return None
    if fld.hero_in(grow(U, m.hero_gap)):
        return None
    diag, shoulder = ((ax, ay), (ex, ly)), ((ex, ly), (end, ly))
    if fld.hero_in((min(ex, end), ly - m.u, max(ex, end), ly + m.u)):
        return None
    boxes = [(b[0] + x0, b[1] + ly, b[2] + x0, b[3] + ly) for _, b in sh.rel]
    for b in boxes:
        if seg_box(*diag, b):
            return None
    h = m.lgap  # leaders keep the same air from other labels
    for o in fld.boxes:
        og = grow(o, h)
        if seg_box(*diag, og) or seg_box(*shoulder, og):
            return None
    grown = [grow(bb, h) for bb in boxes]
    for a, b, _ in fld.segs:
        if seg_seg(*diag, a, b) or seg_seg(*shoulder, a, b):
            return None
        for bb in grown:
            if seg_box(a, b, bb):
                return None
    xs, ys = seg_samples(diag[0], diag[1], skip=10 * m.u)
    hits = on_mask(fld.hero, xs, ys) if len(xs) else 0
    if len(xs) and hits > MAX_DIAG_ON_PART * len(xs) * 0.8:
        return None
    return ly, ex, end, x0, boxes, hits


def place(shapes: list[Shape], fld: Field, ol: Outline | None, m: Metrics):
    """Greedy, in priority order: each callout takes its cheapest valid candidate or is dropped."""
    placed, dropped = [], []
    for sh in shapes:
        best = None
        if ol is not None:
            for ax, ay, dd, signs, pen in _anchors(sh.co, ol):
                if any(math.hypot(ax - px, ay - py) < 2.5 * m.head for px, py in fld.anchors):
                    continue  # two dots crowding one spot read as one
                for si, sign in enumerate(signs):
                    for rise in RISES:
                        r = rise * m.head
                        got = _try(sh, fld, ax, ay, dd, sign, r, m)
                        if got is None:
                            continue
                        hits = got[5]
                        cost = hits * 3 / max(m.u, 0.1) + r / max(m.u, 0.1) * 0.12 + pen + si * 4
                        if best is None or cost < best[0]:
                            best = (cost, ax, ay, dd, got)
        if best is None:
            dropped.append(sh.co.key)
            continue
        _, ax, ay, dd, (ly, ex, end, x0, boxes, _) = best
        g = f"callout:{sh.co.key}"
        fld.segs += [((ax, ay), (ex, ly), g), ((ex, ly), (end, ly), g)]
        fld.anchors.append((ax, ay))
        fld.boxes += boxes
        placed.append((sh, ax, ay, ex, ly, end, x0, boxes))
    return placed, dropped


# ---------------------------------------------------------------- fixed blocks
def _end_ts(job: PrintJob) -> float | None:
    if job.end_ts:
        return job.end_ts
    if job.start_ts and job.slice and _pos(job.slice.prediction_s):
        return job.start_ts + job.slice.prediction_s
    return None


def phase(job: PrintJob) -> str:
    """live | paused | done | failed | last (idle, offline, unknown: never claims completion)."""
    st = (job.status or "").lower()
    if st in ("pause", "paused"):
        return "paused"
    if st in LIVE:
        return "live"
    if st in ("finish", "finished", "completed"):
        return "done"
    if st == "failed":
        return "failed"
    return "last"


def _wrap(text: str, fs: float, w: float) -> list[str]:
    out, line = [], ""
    for word in text.split():
        t = (line + " " + word).strip()
        if tlen(t, fs) > w and line:
            out.append(line)
            line = word
        else:
            line = t
    if line:
        out.append(line)
    return out


def _title_fallback(job: PrintJob) -> str:
    """For a title the font cannot draw (all CJK, only symbols): the printer's name, else the plate id."""
    name = clean(pretty_title(job.printer_name or "", ""))
    if name:
        return name
    return f"PLATE {job.job_key[:8].upper()}" if job.job_key else "PRINT"


def title_group(job: PrintJob, opts: SheetOptions, m: Metrics, x, y, width, max_lines: int = 2,
                subtitle: bool = True) -> Group:
    g = Group(m, "title")
    name = clean(pretty_title(job.title, _title_fallback(job)))
    ts = m.title
    lines = _wrap(name, ts, width)
    while len(lines) > max_lines and ts > m.title * 0.55:
        ts *= 0.94
        lines = _wrap(name, ts, width)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    lines = [_fit_text(ln, ts, width) or "" for ln in lines]
    ty = y
    for ln in lines:
        g.text((x, ty), ln, ts)
        ty += pitch(ts, 1.12, m)
    tw = max(tlen(ln, ts) for ln in lines)
    uy = ty - ts * 0.12 + 2 * m.tm + 8 * m.u  # air between the title and its rule
    g.line([(x, uy), (x + tw, uy)], k=0.8)
    if opts.show_subtitle and subtitle:
        parts = [f"BAMBU LAB {job.model}"] if job.model else []  # no model: no maker line either
        if job.printer_name:
            parts.append(f'"{job.printer_name.upper()}"')
        if job.job_key:
            parts.append(f"DWG {job.job_key[:8].upper()}")
        sub = _fit_text(" · ".join(parts), m.sub, width) if parts else None
        if sub:
            g.text((x, uy + 2 * m.tm + m.u * 4), sub, m.sub)
    return g


def stats_rows(job: PrintJob) -> list:
    sl = job.slice
    rows = []
    if job.model:
        rows.append(("PRINTER", f"BAMBU LAB {job.model}"))
    mat = (sl.filament_name if sl else None) or job.material or (sl.material if sl else None)
    if mat:
        rows.append(("MATERIAL", mat.upper()))
    layers = job.layers or (sl.layers if sl and sl.layers > 0 else None)
    if layers:
        rows.append(("LAYERS", str(layers)))
    grams = (_pos(sl.weight_g) if sl else None) or _pos(job.weight_g)
    if grams:
        rows.append(("MASS", f"{grams:.1f} G"))
    if sl and _pos(sl.envelope_mm[2]):
        e = sl.envelope_mm
        rows.append(("ENVELOPE", f"{e[0]:.0f}×{e[1]:.0f}×{e[2]:.0f} MM"))
    elif job.chamber_c is not None and job.chamber_c > 0:
        rows.append(("CHAMBER", f"{job.chamber_c:.0f} °C"))
    return rows


def times_group(job: PrintJob, opts: SheetOptions, m: Metrics, w: float, accent: bool,
                compact: bool = False) -> Group:
    """Times block with its status line, top-left at (0, 0)."""
    g = Group(m, "times")
    ph = phase(job)
    start, end = job.start_ts, _end_ts(job)
    cols = []  # (label, digits, suffix)
    if start:
        cols.append(("STARTED", *hm(start, opts.clock, opts.tz)))
    if start and end and end > start:
        cols.append(("DURATION", fmt_dur(end - start), ""))
    if end:
        name = {"live": "ETA", "paused": "ETA", "done": "COMPLETED"}.get(ph, "ENDED")
        cols.append((name, *hm(end, opts.clock, opts.tz)))
    if ph in ("live", "paused"):
        status = ("PAUSED · " if ph == "paused" else "") + f"{int(job.progress * 100)} %"
        if job.layer and job.layers:
            status += f" · LAYER {job.layer} / {job.layers}"
    else:
        status = {"done": "PRINT COMPLETE", "failed": "PRINT FAILED"}.get(ph, "LAST PRINT")
    ss = m.status
    while tlen(status, ss) > w - 2 * m.tm and ss > m.status * 0.6:
        ss *= 0.94
    status = _fit_text(status, ss, w - 2 * m.tm) or ""
    sb = g.text((w / 2, 0), status, ss, anchor="mt")
    if not cols:
        return g
    h = (80 if compact else 128) * m.u
    y = sb[3] + m.gap
    pad = 2 * m.tm + 14 * m.u  # inside each column, both sides together
    # one size for every value and one for every label; columns as wide as their value needs
    vs, ls = m.tdigit * (0.62 if compact else 1), m.tlabel
    sfx, sgap = 0.45, 2 * m.tm + 2 * m.u  # AM / PM: size x the value size, and the air before it

    def vneed(vs):
        return [tlen(d, vs) + (tlen(sx, sfx * vs) + sgap if sx else 0) + pad for _, d, sx in cols]

    def need(vs, ls):
        return [max(v, tlen(k, ls) + pad) for v, (k, _, _) in zip(vneed(vs), cols)]

    while sum(vneed(vs)) > w and vs > 6:  # the values shrink together...
        vs *= 0.96
    while sum(need(vs, ls)) > w and ls > 5:  # ...and the labels only when they do not fit
        ls *= 0.95
    widths = need(vs, ls)
    spare = (w - sum(widths)) / len(cols)
    widths = [cw + spare for cw in widths]
    g.rect(0, y, w, y + h, k=0.5, col="rule")
    xx = 0.0
    for i, ((k, d, sx), cw) in enumerate(zip(cols, widths)):
        if i:
            g.line([(xx, y + 10 * m.u), (xx, y + h - 10 * m.u)], k=0.4, col="rule")
        g.text((xx + cw / 2, y + 12 * m.u), k, ls, anchor="mt")
        col = "accent" if (accent and k == "ETA") else "ink"
        base = y + h - 16 * m.u
        if sx:
            tot = tlen(d, vs) + sgap + tlen(sx, sfx * vs)
            x0 = xx + (cw - tot) / 2
            g.text((x0, base), d, vs, anchor="ls", col=col)
            g.text((x0 + tlen(d, vs) + sgap, base), sx, sfx * vs, anchor="ls", col=col)
        else:
            g.text((xx + cw / 2, base), d, vs, anchor="ms", col=col)
        xx += cw
    return g


def rack_group(job: PrintJob, m: Metrics, w: float) -> Group | None:
    if not job.tools:
        return None
    g = Group(m, "rack")
    fs = m.rack
    y = dotted_rows(g, 0, 0, w, [], fs, head="TOOL RACK")
    labels = [f"T{t.id}" for t in job.tools]
    chip_x = max(tlen(label, fs) for label in labels) + 4 * m.tm
    chip = fs * 0.7
    name_x = chip_x + chip + 3 * m.tm
    for t, label in zip(job.tools, labels):
        hrs = f"{t.hours:.1f} H" if t.hours and t.hours > 0 else ""
        if t.state == "mounted":
            parts = ["ACTIVE", f"{t.temp_c:.0f} °C" if t.temp_c is not None and t.temp_c > 0 else "", hrs]
            detail = " · ".join(p for p in parts if p)
        else:
            detail = " · ".join(p for p in (t.state.upper(), hrs) if p)
        # Reserve a short leader even when a filament's brand name fills its column.
        room = w - name_x - tlen(detail, fs) - 4 * m.tm - 2 * m.u - 2 * fs
        name = _fit_text(clean(t.filament.upper()), fs, room, ellipsis="...") if t.filament else None
        if not name:
            y = dotted_rows(g, 0, y, w, [(label, detail)], fs)
            continue
        g.text((0, y), label, fs)
        nb = g.text((name_x, y), name, fs)
        vb = g.text((w, y), detail, fs, anchor="ra")
        if t.state == "mounted":
            ax, ay = vb[0] + m.tm, vb[3] - m.tm + 1.5 * m.u
            g.line([(ax, ay), (ax + tlen("ACTIVE", fs), ay)], k=0.8)
        g.dots(nb[2] + m.u, vb[0] - m.u, y + fs * 0.8, fs * 0.42)
        box = (chip_x, y + fs * 0.25, chip_x + chip, y + fs * 0.95)
        g.ops.append(("swatch", *box, t.color))
        g.extra.append(box)
        y += pitch(fs, 1.4, m)
    return g


def panel_data(job: PrintJob) -> tuple[list, list]:
    """(left, right) sensor panels as (heading, rows); only panels whose data exists."""
    left, right = [], []
    rows = []
    live = job.status in LIVE  # a finished print's readings are just the part cooling down
    if job.nozzle_temp and live:
        rows.append(("NOZZLE", f"{job.nozzle_temp[0]:.0f} / {job.nozzle_temp[1]:.0f} °C"))
    if job.bed_temp and live:
        rows.append(("BED", f"{job.bed_temp[0]:.0f} / {job.bed_temp[1]:.0f} °C"))
    if job.chamber_c is not None and job.chamber_c > 0:
        rows.append(("CHAMBER", f"{job.chamber_c:.0f} °C"))
    if rows:
        left.append(("TEMPERATURES · ACTUAL / TARGET", rows))
    if job.ams:
        rows = []
        for i, t in enumerate(job.ams, 1):
            if t.empty:
                rows.append((f"SLOT {i}", "EMPTY"))
            else:
                col = (t.color or "")[:7].upper()
                rows.append((f"SLOT {i}" + (" · ACTIVE" if t.active else ""), f"{t.type} {col}".strip() or "LOADED"))
        if job.ams_humidity:
            rows.append(("HUMIDITY", job.ams_humidity.upper()))
        left.append(("AMS", rows))
    rows = []
    if job.speed:
        rows.append(("SPEED PROFILE", job.speed.upper()))
    rows += [(k.upper(), f"{v:.0f} %") for k, v in (job.fans or {}).items()]
    if rows:
        right.append(("MOTION + AIRFLOW", rows))
    rows = []
    if job.usage_h:
        rows.append(("MACHINE HOURS", f"{job.usage_h:,.0f} H"))
    if job.objects is not None and job.objects > 0:
        rows.append(("OBJECTS", f"{job.objects}" + (f" · {job.skipped} SKIPPED" if job.skipped is not None else "")))
    if rows:
        right.append(("MACHINE", rows))
    return left, right


def stack_panels(panels, m: Metrics, x, y0, y1, w) -> tuple[list[Group], list[str]]:
    """Stack panels from y0 down; a panel that does not end above y1 is dropped."""
    groups, dropped = [], []
    y = y0
    for head, rows in panels:
        g = Group(m, f"panel:{head.split(' ')[0]}")
        dotted_rows(g, 0, 0, w - 2 * m.tm, rows, m.prow, head=head)
        bx = g.box
        gg = g.moved(x - bx[0], y - bx[1])
        if gg.box[3] > y1:
            dropped.append(g.name)
            continue
        groups.append(gg)
        y = gg.box[3] + m.pad
    return groups, dropped


# ---------------------------------------------------------------- frame
def frame(sh: Sheet, background: str, m: Metrics, show_grid: bool) -> Box:
    """Frame lines and zone marks; returns the border (padded inner rect)."""
    W, H, u = m.w, m.h, m.u
    if background == "blue":
        o, z = 26 * u, 16 * u
        sh.ops.append(("grid", show_grid, o + z, o + z, W - o - z, H - o - z))
        sh.ops.append(("rect", o, o, W - o, H - o, 2.6, "ink"))
        sh.ops.append(("rect", o + z, o + z, W - o - z, H - o - z, 0.8, "ink"))
        zs = 12 * max(u, 0.75)
        if zs <= z * 0.95 and zs >= 6:
            nx, ny = (8, 5) if W > H else (6, 8)
            for i in range(nx):
                xc = o + z + (W - 2 * o - 2 * z) * (i + 0.5) / nx
                for yc in (o + z / 2, H - o - z / 2):
                    sh.text((xc, yc), str(nx - i), zs, "frame", anchor="mm")
            for i in range(ny):
                yc = o + z + (H - 2 * o - 2 * z) * (i + 0.5) / ny
                for xc in (o + z / 2, W - o - z / 2):
                    sh.text((xc, yc), "ABCDEFGH"[i], zs, "frame", anchor="mm")
        inner = o + z
    else:
        inner = 20 * u
        sh.ops.append(("rect", inner, inner, W - inner, H - inner, 0.6, "rule"))
    b = inner + m.pad
    return (b, b, W - b, H - b)


# ---------------------------------------------------------------- compose
@dataclass
class Plan:
    ops: list
    report: LayoutReport
    hero: tuple | None  # (ink, solid, x0, y0) at 1x, or None for a text-only sheet


@dataclass
class _Config:
    name: str
    region: Box
    groups: list  # extra fixed groups (panels)
    dropped: list


def _crop(a: Art) -> Art | None:
    if a is None or a.solid.size == 0 or a.ink.shape != a.solid.shape:
        return None
    ys, xs = np.where(a.solid > 60)
    if not len(ys):
        return None
    sl = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
    return Art(np.ascontiguousarray(a.ink[sl]), np.ascontiguousarray(a.solid[sl]))


def _resize(a: np.ndarray, w: int, h: int, resample=Image.LANCZOS) -> np.ndarray:
    if a.shape == (h, w):
        return a
    return np.asarray(Image.fromarray(a).resize((w, h), resample))


def note_group(note: str, m: Metrics, right: float, top: float, width: float) -> Group | None:
    """The footnote (why the picture fell back), right-aligned in at most two lines at the top
    right of the sheet; None when there is no note or no room for it."""
    text = clean(note).strip().upper()
    if not text or width < 40 * m.u:
        return None
    lines = _wrap(text, m.note, width)
    if len(lines) > 2:
        lines = lines[:1] + [" ".join(lines[1:])]
    lines = [ln for ln in (_fit_text(ln, m.note, width) for ln in lines) if ln]
    if not lines:
        return None
    g = Group(m, "note")
    y = 0.0
    for ln in lines:
        g.text((0, y), ln, m.note, anchor="ra")
        y += pitch(m.note, 1.3, m)
    b = g.box
    return g.moved(right - b[2], top - b[1])


def compose(job: PrintJob, art_fn: Callable[[tuple[int, int]], Art], size: tuple[int, int],
            opts: SheetOptions, accent_eta: bool, level: int = 0, note: str = "", wide_title: bool = True) -> Plan:
    """Plan the sheet. When the part cannot reach the size floor, optional chrome goes, one step
    per `level`: 1 the tool rack, 2 the title's second line, 3 the stats block, 4 the subtitle,
    5 the times block shrinks to a compact row. `note` is a footnote drawn at the top right."""
    if level == 0 and wide_title:
        art_fn = _memo(art_fn)
    rack, title_lines, stats = level < 1, (2 if level < 2 else 1), level < 3
    subtitle, compact = level < 4, level >= 5
    W, H = size
    m = Metrics(W, H)
    sh = Sheet(m)
    border = frame(sh, opts.background, m, opts.show_grid)
    bx0, by0, bx1, by1 = border
    bw = bx1 - bx0

    # bottom right: times, tool rack above it
    tw = min(480 * m.u, 0.45 * bw)
    tg = times_group(job, opts, m, tw, accent_eta, compact)
    tb = tg.box
    tg = tg.moved(bx1 - tw, by1 - tb[3])
    rg = rack_group(job, m, tw - 2 * m.tm) if rack else None
    if not rack and job.tools:
        sh.dropped.append("rack")
    if not stats:
        sh.dropped.append("stats")
    if not subtitle and opts.show_subtitle:
        sh.dropped.append("subtitle")
    if compact:
        sh.dropped.append("times:compact")
    if rg is not None:
        rb = rg.box
        rg = rg.moved(bx1 - tw - rb[0], tg.box[1] - m.pad - rb[3])
    # bottom left: stats, bottom-aligned with the times block
    sw = min(0.36 * bw, 470 * m.u)
    sg = None
    if stats:
        sg = Group(m, "stats")
        dotted_rows(sg, 0, 0, sw - 2 * m.tm, stats_rows(job), m.row)
        if sg.texts:  # a cell too narrow for any row has no stats block
            sb = sg.box
            sg = sg.moved(bx0 - sb[0], by1 - sb[3])
        else:
            sg = None
            sh.dropped.append("stats")
    right_x0 = min(tg.box[0], rg.box[0] if rg else 1e9)
    # title, top left; never reaching the right column. A title that fits on one line across the
    # sheet takes it (no footnote beside it, no rack reaching up to it); else it wraps at 55 %.
    def place_title(width):
        g = title_group(job, opts, m, bx0, by0, width, title_lines, subtitle)
        return g.moved(bx0 - g.box[0], by0 - g.box[1])

    title_w = min(0.55 * bw, right_x0 - m.pad - bx0)
    ttl = place_title(title_w)
    name = clean(pretty_title(job.title, _title_fallback(job)))
    wide_used = False
    if wide_title and not note and title_w < tlen(name, m.title) <= bw - 2 * m.tm:
        wide = place_title(bw)
        if rg is None or not boxes_touch(grow(rg.box, m.pad), wide.box):
            ttl, wide_used = wide, True
    # the footnote: top right, beside the title
    ng = note_group(note, m, bx1, by0, bx1 - max(ttl.box[2] + m.pad, bx0 + 0.45 * bw)) if note else None
    if note and ng is None:
        sh.dropped.append("note")

    fixed = [ttl, tg] + ([sg] if sg else []) + ([rg] if rg else []) + ([ng] if ng else [])
    # a rack too tall for the sheet is dropped rather than drawn over the title
    if rg and (rg.box[1] < by0 or boxes_touch(grow(rg.box, m.pad), ttl.box)
               or (ng is not None and boxes_touch(grow(rg.box, m.pad), ng.box))):
        fixed.remove(rg)
        sh.dropped.append("rack")
        rg = None
        right_x0 = tg.box[0]
    bottom_top = min(g.box[1] for g in fixed if g is not ttl and g is not ng)
    top = max(ttl.box[3], ng.box[3] if ng else 0) + m.pad
    region_a = (bx0, top, bx1, bottom_top - m.pad)
    stats_top = sg.box[1] if sg else by1 + m.pad
    bx_l, bx_r = (sg.box[2] + m.pad if sg else bx0), right_x0 - m.pad
    b_top = top if ttl.box[2] + m.pad > bx_l or ng else by0
    region_b = (bx_l, b_top, bx_r, by1)

    configs = []
    if region_a[2] - region_a[0] > 8 and region_a[3] - region_a[1] > 8:
        configs.append(_Config("band", region_a, [], []))
    if region_b[2] - region_b[0] > 8 and region_b[3] - region_b[1] > 8:
        configs.append(_Config("column", region_b, [], []))
    # left of the times/rack column down to the stats, and right of the stats up to that column
    region_c = (bx0, top, right_x0 - m.pad, stats_top - m.pad)
    region_d = (bx_l, top, bx1, (rg or tg).box[1] - m.pad)
    for name, r in (("left", region_c), ("right", region_d)):
        if r[2] - r[0] > 8 and r[3] - r[1] > 8 and r not in [c.region for c in configs]:
            configs.append(_Config(name, r, [], []))
    left_p, right_p = panel_data(job)
    if opts.fill_space and W > H and (left_p or right_p) and region_b[2] - region_b[0] > 8:
        lg, ld = stack_panels(left_p, m, bx0, top, stats_top - m.pad, sw)
        right_top = by0 if ttl.box[2] + m.pad < right_x0 and not ng else top
        rg_, rd = stack_panels(right_p, m, right_x0, right_top, (rg or tg).box[1] - m.pad, bx1 - right_x0)
        # the part stays right of the left panels (without a stats block region_b starts at the border)
        px0 = max([region_b[0]] + [g.box[2] + m.pad for g in lg])
        region_p = (px0, region_b[1], region_b[2], region_b[3])
        if (lg or rg_) and region_p[2] - region_p[0] > 8:
            configs.append(_Config("panels", region_p, lg + rg_, ld + rd))
        else:
            sh.dropped += ld + rd + [g.name for g in lg + rg_]

    # first render: the art at the largest region any configuration offers
    env = (int(max(c.region[2] - c.region[0] for c in configs)) if configs else 0,
           int(max(c.region[3] - c.region[1] for c in configs)) if configs else 0)
    art_src = art_fn
    art0 = _crop(art_fn(env)) if env[0] > 8 and env[1] > 8 else None
    if art0 is None and env[0] > 8 and env[1] > 8:
        art_src = _fallback(job)
        art0 = _crop(art_src(env)) if art_src else None
    toolpath = art_src is art_fn and opts.method == "toolpath" and job.slice is not None
    shapes = [shape(c, m) for c in callouts(job, opts, toolpath)]

    fixed_texts = [b for g in fixed for _, b in g.texts]
    fixed_blocks = [g.box for g in fixed]

    best = None
    if art0 is not None and configs:
        ah, aw = art0.solid.shape
        fit = min(bw / aw, (by1 - by0) / ah)
        s_floor = fit * math.sqrt(MIN_HERO + 0.02)
        for c in configs:
            extra_blocks = [g.box for g in c.groups]
            res = _search(art0, c.region, s_floor, shapes, m, border, (W, H),
                          fixed_texts, fixed_blocks + extra_blocks)
            if res is None:
                continue
            s, value, hero_box, placed, dropped = res
            if c.name == "panels":
                value *= 1.15
            # a hero that meets the size floor beats any that does not, whatever the callouts
            rank = (hero_fill((0, 0, hero_box[2], hero_box[3]), border) >= MIN_HERO, value)
            if best is None or rank > best[0]:
                best = (rank, c, s, hero_box)

    hero_mask = np.zeros((H, W), np.uint8)
    hero = None
    has_art = art0 is not None  # art without a hero is a violation, not a text-only sheet
    chosen = best[1] if best else next((c for c in configs if c.name == "panels"), None)
    for g in fixed + (chosen.groups if chosen else []):
        g.commit(sh)
    if chosen and chosen.name == "panels":
        sh.dropped += chosen.dropped
    elif any(c.name == "panels" for c in configs):
        sh.dropped.append("panels")  # they would cost the part too much room

    hero_box = (0, 0, 0, 0)
    placed = []
    if best is not None:
        _, c, s, (hx0, hy0, hw, hh) = best
        if s < RERENDER_BELOW:
            again = _crop(art_src((hw, hh)))
            art = again if again is not None else art0
        else:
            art = art0
        ink = _resize(art.ink, hw, hh)
        sol = _resize(art.solid, hw, hh, Image.BILINEAR)
        hero_mask[hy0:hy0 + hh, hx0:hx0 + hw] = (sol > 100)
        hero_box = (hx0, hy0, hx0 + hw, hy0 + hh)
        hero = (ink, sol, hx0, hy0)
        fld = Field(m, border, hero_mask, [b for (_, b), g in zip(sh.texts, sh.tgroups) if g != "frame"],
                    list(sh.blocks))
        ol = Outline(hero_mask[hy0:hy0 + hh, hx0:hx0 + hw].astype(bool), hx0, hy0)
        placed, dropped = place(shapes, fld, ol, m)
        sh.dropped += dropped
    else:
        sh.dropped += [s.co.key for s in shapes]

    for sp, ax, ay, ex, ly, end, x0, boxes in placed:
        g = f"callout:{sp.co.key}"
        sh.ops.append(("leader", [(ax, ay), (ex, ly), (end, ly)]))
        sh.ops.append(("anchor", ax, ay))
        sh.segs += [((ax, ay), (ex, ly)), ((ex, ly), (end, ly))]
        sh.sgroups += [g, g]
        for op in sp.ops:
            if op[0] == "text":
                sh.ops.append(("text", (op[1][0] + x0, op[1][1] + ly), *op[2:]))
            else:
                bx = op[2]
                sh.ops.append(("icon", op[1], (bx[0] + x0, bx[1] + ly, bx[2], bx[3])))
        for (s, _), b in zip(sp.rel, boxes):
            sh.texts.append((s, b))
            sh.tgroups.append(g)

    if has_art and hero_fill(hero_box, border) < MIN_HERO:  # the part matters more
        if wide_used:  # first give the title's row back to the part
            return compose(job, art_fn, size, opts, accent_eta, level, note, wide_title=False)
        if level < LADDER:
            return compose(job, art_fn, size, opts, accent_eta, level + 1, note, wide_title=False)
    rep = LayoutReport(size=(W, H), border=border, hero_box=hero_box, hero_mask=hero_mask,
                       texts=list(sh.texts), segments=list(sh.segs), blocks=list(sh.blocks),
                       dropped=list(sh.dropped), text_groups=list(sh.tgroups),
                       segment_groups=list(sh.sgroups), block_groups=list(sh.bgroups), has_art=has_art)
    return Plan(sh.ops, rep, hero)


def _memo(fn):
    """Remember art per box within one sheet (the fallback ladder asks again)."""
    seen: dict = {}

    def get(box):
        k = tuple(int(v) for v in box)
        if k not in seen:
            seen[k] = fn(k)
        return seen[k]
    return get


def _fallback(job: PrintJob):
    from .art import preview_art
    png = job.cover_png or (job.slice.preview_png if job.slice else None)
    if not png:
        return None
    return lambda box: preview_art(png, box)


def _search(art0: Art, region: Box, s_floor: float, shapes, m: Metrics, border: Box, size,
            fixed_texts, fixed_blocks):
    """Largest hero (scale of the first render) whose callouts still find room.

    Returns (s, value, (x0, y0, w, h), placed, dropped) with value = hero diagonal discounted for
    each dropped callout."""
    W, H = size
    ah, aw = art0.solid.shape
    rw, rh = region[2] - region[0], region[3] - region[1]
    s_max = min(rw / aw, rh / ah, 1.0)
    if s_max <= 0:
        return None
    best = None
    for k in SCALES:
        s = s_max * k
        if best is not None and s < s_floor:
            break
        hw, hh = max(1, int(aw * s)), max(1, int(ah * s))
        hx0 = int(region[0] + (rw - hw) / 2)
        hy0 = int(region[1] + (rh - hh) / 2)
        sol = _resize(art0.solid, hw, hh, Image.BILINEAR) > 100
        mask = np.zeros((H, W), np.uint8)
        mask[hy0:hy0 + hh, hx0:hx0 + hw] = sol
        if not sol.any():
            return None
        fld = Field(m, border, mask, fixed_texts, fixed_blocks)
        placed, dropped = place(shapes, fld, Outline(sol, hx0, hy0), m)
        lost = sum(sh.co.weight for sh in shapes if sh.co.key in dropped)
        value = math.hypot(hw, hh) * (0.94 ** lost)
        if best is None or value > best[1]:
            best = (s, value, (hx0, hy0, hw, hh), placed, dropped)
        if not dropped:
            break
    return best
