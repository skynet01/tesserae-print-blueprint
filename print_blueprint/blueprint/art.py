"""Line art for the hero drawing: toolpath study, slicer-preview pen drawing, AI-image engraving.

Every function returns an `Art` (uint8 ink alpha + uint8 solid silhouette, same shape), cropped
tightly to the part and no larger than the requested box."""
from __future__ import annotations

import io
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .gcode import parse_walls
from .model import SliceFacts

SS = 2  # supersample for the toolpath drawing
_MAX_LINES = 140
_MIN_GAP_PX = 3.5  # output px between drawn layers (below this the lines moire)
WALLS_KEEP = 2  # parsed walls kept in memory, newest (print, 3MF) pairs first
_WALLS: OrderedDict = OrderedDict()
_WALLS_LOCK = threading.Lock()


@dataclass
class Art:
    ink: np.ndarray  # uint8 HxW line alpha
    solid: np.ndarray  # uint8 HxW silhouette (hides the grid behind the part)


def _empty() -> Art:
    return Art(np.zeros((1, 1), np.uint8), np.zeros((1, 1), np.uint8))


def _fill_window(loops: list[np.ndarray], x0: int, y0: int, x1: int, y1: int) -> Image.Image:
    """Even-odd fill of closed loops inside the window: each loop is drawn alone and XORed, so
    holes stay open (Pillow's polygon fill has no even-odd rule)."""
    win = np.zeros((y1 - y0, x1 - x0), np.uint8)
    for p in loops:
        if len(p) < 3:
            continue
        lx0, ly0 = p.min(0)
        lx1, ly1 = p.max(0)
        if lx1 < x0 or lx0 >= x1 or ly1 < y0 or ly0 >= y1:
            continue
        lx0, ly0 = max(int(lx0), x0), max(int(ly0), y0)
        lx1, ly1 = min(int(lx1), x1 - 1), min(int(ly1), y1 - 1)
        small = Image.new("L", (lx1 - lx0 + 1, ly1 - ly0 + 1), 0)
        q = (p - (lx0, ly0)).ravel().tolist()
        ImageDraw.Draw(small).polygon(q, fill=255)
        win[ly0 - y0: ly1 - y0 + 1, lx0 - x0: lx1 - x0 + 1] ^= np.asarray(small)
    return Image.fromarray(win)


def _dash_runs(p: np.ndarray, dash: float):
    """Polylines covering the 'on' stretches of p, dashed by arc length."""
    if len(p) < 2:
        return
    seg = np.hypot(*np.diff(p, axis=0).T)
    start = np.concatenate([[0.0], np.cumsum(seg)[:-1]])
    on = (start // dash).astype(np.int64) % 2 == 0
    k = 0
    n = len(on)
    while k < n:
        if not on[k]:
            k += 1
            continue
        j = k
        while j < n and on[j]:
            j += 1
        yield p[k: j + 1]
        k = j


def walls_for(facts: SliceFacts, job_key: str = "") -> list:
    """The 3MF's wall polylines, parsed once per (print, 3MF) and kept for the last WALLS_KEEP pairs
    (each progress step and cell size of a print redraws from the same walls)."""
    key = (job_key, facts.source) if facts.source else None
    if key is not None:
        with _WALLS_LOCK:
            hit = _WALLS.get(key)
            if hit is not None:
                _WALLS.move_to_end(key)
                return hit
    layers = parse_walls(facts.gcode, facts.bbox_all)
    if key is not None:
        with _WALLS_LOCK:
            _WALLS[key] = layers
            while len(_WALLS) > WALLS_KEEP:
                _WALLS.popitem(last=False)
    return layers


def toolpath_art(facts: SliceFacts, box: tuple[int, int], progress: float, az: float = -35, el: float = 28,
                 u: float | None = None, walls: list | None = None) -> Art:
    """u is the scale unit (default min(box) / 1200); line width, dashes and layer spacing follow it.
    `walls` are the parsed walls when the caller already has them (see walls_for)."""
    box_w, box_h = int(box[0]), int(box[1])
    u = u if u else min(box_w, box_h) / 1200
    layers = walls if walls is not None else parse_walls(facts.gcode, facts.bbox_all)
    if not layers or box_w < 8 or box_h < 8:
        return _empty()
    a, e = math.radians(az), math.radians(el)
    ca, sa, ce, se = math.cos(a), math.sin(a), math.cos(e), math.sin(e)

    # project every point in one pass, then split back per layer / path
    flat, zs, counts = [], [], []
    for z, paths in layers:
        for p in paths:
            flat.append(np.asarray(p, dtype=np.float64))
            zs.append(np.full(len(p), z))
            counts.append(len(p))
    xy, z = np.concatenate(flat), np.concatenate(zs)
    xr = xy[:, 0] * ca - xy[:, 1] * sa
    yr = xy[:, 0] * sa + xy[:, 1] * ca
    pr = np.stack([xr, -(z * ce + yr * se)], axis=1)
    mn, mx = pr.min(0), pr.max(0)
    bw, bh = box_w * SS, box_h * SS
    margin = 2 * SS  # room for the outline and its antialiasing
    scale = min((bw - 2 * margin) / max(mx[0] - mn[0], 1e-6), (bh - 2 * margin) / max(mx[1] - mn[1], 1e-6))
    off = np.array([bw, bh]) / 2 - (mn + mx) / 2 * scale
    pts_all = np.round(pr * scale + off).astype(np.int32)
    bounds = np.concatenate([[0], np.cumsum(counts)])

    projected: list[list[np.ndarray]] = []
    k = 0
    for _, paths in layers:
        projected.append([pts_all[bounds[k + i]: bounds[k + i + 1]] for i in range(len(paths))])
        k += len(paths)

    ink = Image.new("L", (bw, bh), 0)
    solid = Image.new("L", (bw, bh), 0)
    idraw = ImageDraw.Draw(ink)
    n = len(projected)
    zl = np.array([zz for zz, _ in layers])
    dz = float(np.median(np.diff(zl))) if n > 1 else 0.0
    gap_px = dz * ce * scale / SS if dz > 0 else 0.0  # output px between adjacent layers
    want = _MIN_GAP_PX * max(1.0, u)
    stride = max(1, math.ceil(n / _MAX_LINES), math.ceil(want / gap_px) if gap_px > 0 else 1)
    cut = int(n * progress)
    lw = max(1, round(1.1 * u)) * SS
    dash = max(6.0, 9 * SS * u)
    for i, pts in enumerate(projected):
        allp = np.concatenate(pts)
        x0, y0 = max(int(allp[:, 0].min()) - 1, 0), max(int(allp[:, 1].min()) - 1, 0)
        x1, y1 = min(int(allp[:, 0].max()) + 2, bw), min(int(allp[:, 1].max()) + 2, bh)
        if x1 > x0 and y1 > y0:
            mask = _fill_window(pts, x0, y0, x1, y1)
            ink.paste(0, (x0, y0), mask)  # the slice hides what is behind it
            solid.paste(255, (x0, y0), mask)
        if not (i % stride == 0 or i == n - 1 or i == cut):
            continue
        if i >= cut:
            if (i // stride) % 2:  # not printed yet: every other drawn layer, dashed by arc length
                continue
            for p in pts:
                for run in _dash_runs(p, dash):
                    idraw.line(run.ravel().tolist(), fill=255, width=lw)
        else:
            w = lw + (SS if i == cut and progress < 1 else 0)
            for p in pts:
                idraw.line(p.ravel().tolist(), fill=255, width=w, joint="curve")

    edge = ImageChops.subtract(solid.filter(ImageFilter.MaxFilter(3)), solid.filter(ImageFilter.MinFilter(3)))
    ink = ImageChops.lighter(ink, edge)
    solid = solid.filter(ImageFilter.MaxFilter(3))  # covers the outline too
    bb = solid.getbbox()
    if bb is None:
        return _empty()
    x0, y0 = bb[0] & ~1, bb[1] & ~1
    x1, y1 = (bb[2] + 1) & ~1, (bb[3] + 1) & ~1
    ow, oh = (x1 - x0) // SS, (y1 - y0) // SS
    crop = (x0, y0, x1, y1)
    ink = ink.crop(crop).resize((ow, oh), Image.BOX)
    solid = solid.crop(crop).resize((ow, oh), Image.BOX)
    return Art(np.asarray(ink).copy(), np.asarray(solid).copy())


def _gauss(a: np.ndarray, sigma: float) -> np.ndarray:
    return np.asarray(Image.fromarray(a).filter(ImageFilter.GaussianBlur(sigma)))


def _pa_smooth(a: np.ndarray, sigma: float) -> np.ndarray:
    """Separable Gaussian in float: 8-bit blur makes false edges in gentle shading."""
    radius = max(1, round(3 * sigma))
    kernel = np.exp(-np.arange(-radius, radius + 1, dtype=np.float32) ** 2 / (2 * sigma * sigma))
    kernel /= kernel.sum()
    for axis in (0, 1):
        pads = [(0, 0), (0, 0)]
        pads[axis] = (radius, radius)
        padded = np.pad(a, pads, mode="edge")
        out = np.zeros_like(a)
        for i, weight in enumerate(kernel):
            sl = [slice(None), slice(None)]
            sl[axis] = slice(i, i + a.shape[axis])
            out += padded[tuple(sl)] * weight
        a = out
    return a


def _pa_peaks(mag: np.ndarray, gx: np.ndarray, gy: np.ndarray) -> np.ndarray:
    """Non-maximum suppression across the edge normal, sampled between pixels."""
    h, w = mag.shape
    y, x = np.mgrid[:h, :w]
    norm = np.maximum(np.hypot(gx, gy), 1e-8)
    dx, dy = gx / norm, gy / norm
    sides = []
    for sign in (-1, 1):
        xx, yy = np.clip(x + sign * dx, 0, w - 1), np.clip(y + sign * dy, 0, h - 1)
        ix, iy = xx.astype(np.intp), yy.astype(np.intp)
        fx, fy = xx - ix, yy - iy
        jx, jy = np.minimum(ix + 1, w - 1), np.minimum(iy + 1, h - 1)
        sides.append((mag[iy, ix] * (1 - fx) + mag[iy, jx] * fx) * (1 - fy) +
                     (mag[jy, ix] * (1 - fx) + mag[jy, jx] * fx) * fy)
    return (mag > sides[0]) & (mag >= sides[1])


def preview_art(png: bytes, box: tuple[int, int]) -> Art:
    """Sharp shading contours, a thin silhouette and separated short shadow strokes."""
    box_w, box_h = int(box[0]), int(box[1])
    if not png or min(box_w, box_h) < 8:
        return _empty()
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    arr = np.asarray(im)
    alpha = arr[:, :, 3]
    if alpha.min() == 255:  # opaque cover: pixels near the corner colour are background
        rgb = arr[:, :, :3].astype(np.int32)
        corners = np.array([rgb[0, 0], rgb[0, -1], rgb[-1, 0], rgb[-1, -1]])
        bgc = np.median(corners, axis=0)
        far = np.abs(rgb - bgc).sum(-1) > 36
        alpha = np.where(far, 255, 0).astype(np.uint8)
        im = Image.fromarray(np.dstack([arr[:, :, :3], alpha]), "RGBA")
    ys, xs = np.where(alpha > 10)
    if not len(ys):
        return _empty()
    x0, x1 = max(xs.min() - 4, 0), min(xs.max() + 5, im.width)
    y0, y1 = max(ys.min() - 4, 0), min(ys.max() + 5, im.height)
    im = im.crop((x0, y0, x1, y1))
    scale = min(box_w / im.width, box_h / im.height)
    ow, oh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
    # One-pixel pen at small sizes; grow the pen and spacing together on large sheets.
    pen_scale = max(1.0, min(box_w, box_h) / 900)
    nw, nh = max(2, round(ow / pen_scale)), max(2, round(oh / pen_scale))
    big = im.resize((nw, nh), Image.Resampling.LANCZOS)
    mask = np.asarray(big)[:, :, 3] > 128
    lum = np.asarray(big.convert("RGB").convert("L")).astype(np.float32)
    inside = lum[mask]
    if not inside.size:
        return _empty()
    lo, hi = np.percentile(inside, (3, 97))
    t = np.clip((lum - lo) / max(12.0, hi - lo), 0, 1)
    t = np.where(mask, t, 1).astype(np.float32)
    unit = max(1.0, min(box_w, box_h) / pen_scale / 650)
    fine = _pa_smooth(t, unit)
    gy, gx = np.gradient(fine)
    mag = np.hypot(gx, gy)
    m_img = Image.fromarray(mask.astype(np.uint8) * 255)
    inner = np.asarray(m_img.filter(ImageFilter.MinFilter(3))) > 0
    edges = _pa_peaks(mag, gx, gy) & (mag > 0.045 / unit) & inner
    silhouette = mask & ~inner
    yy, xx = np.mgrid[0:nh, 0:nw]
    spacing = max(5, round(7 * unit))
    shade = _pa_smooth(t, 1.8 * unit)
    hatch = ((xx + yy) % spacing == 0) & (shade < 0.45)
    hatch &= (xx - yy) % (spacing * 3) < spacing * 1.6
    # A paper gap keeps tone marks from swelling the structural lines.
    structure = edges | silhouette
    busy = np.asarray(Image.fromarray(structure.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(3))) > 0
    ink = (structure | (hatch & inner & ~busy)).astype(np.uint8) * 255
    solid = mask.astype(np.uint8) * 255
    if (nw, nh) != (ow, oh):
        ink = np.asarray(Image.fromarray(ink).resize((ow, oh), Image.Resampling.NEAREST))
        solid = np.asarray(Image.fromarray(solid).resize((ow, oh), Image.Resampling.NEAREST))
    return Art(np.ascontiguousarray(ink), np.ascontiguousarray(solid))


def _fill_holes(m: Image.Image) -> Image.Image:
    """Fill everything not reachable from the border (so a hollow outline becomes a silhouette)."""
    pad = Image.new("L", (m.width + 2, m.height + 2), 0)
    pad.paste(m, (1, 1))
    ImageDraw.floodfill(pad, (0, 0), 128)
    a = np.asarray(pad)[1:-1, 1:-1]
    return Image.fromarray(np.where(a == 128, 0, 255).astype(np.uint8))


def ai_art(png: bytes, box: tuple[int, int]) -> Art:
    """Dark-on-white AI engraving: darkness becomes ink, the filled outer outline the silhouette."""
    box_w, box_h = int(box[0]), int(box[1])
    im = Image.open(io.BytesIO(png))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, "white")
        bg.alpha_composite(im)
        im = bg
    g = np.asarray(im.convert("L")).astype(np.float32)
    ink = np.clip((242 - g) / 190, 0, 1)
    ys, xs = np.where(ink > 0.08)
    if not len(ys):
        return _empty()
    pad = 6
    sl = (slice(max(0, ys.min() - pad), ys.max() + pad), slice(max(0, xs.min() - pad), xs.max() + pad))
    g, ink = g[sl], ink[sl]
    h0, w0 = g.shape
    scale = min(box_w / w0, box_h / h0)
    nw, nh = max(8, int(w0 * scale)), max(8, int(h0 * scale))
    ink8 = Image.fromarray((ink * 255).astype(np.uint8)).resize((nw, nh), Image.BICUBIC)
    dark = Image.fromarray((g < 236).astype(np.uint8) * 255).resize((nw, nh), Image.BILINEAR).point(
        lambda v: 255 if v > 127 else 0)
    k = 9
    m = dark.filter(ImageFilter.MaxFilter(k)).filter(ImageFilter.MinFilter(k))  # close small gaps
    m = _fill_holes(m)
    m = m.filter(ImageFilter.MinFilter(5)).filter(ImageFilter.MaxFilter(5))  # drop specks
    return Art(np.asarray(ink8).copy(), np.asarray(m).copy())
