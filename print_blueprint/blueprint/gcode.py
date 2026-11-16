"""Read wall toolpaths out of Bambu/Orca G-code."""
from __future__ import annotations

import math
import re
from typing import Iterator

import numpy as np

WALL_FEATURES = ("Outer wall", "Overhang wall")
_NUM = re.compile(r"([XYZEIJ])(-?\d*\.?\d+)")
_MOVE_CMDS = ("G0", "G1", "G2", "G3")
_ARC_STEP_MM = 0.5
_BBOX_MARGIN = 3.0
# comment lines and G0-G3 moves, found by the regex engine so the G-code is never split into a list
_LINES = re.compile(r"^(?:;|[ \t]*G[0-3](?![0-9A-Za-z.]))[^\n]*", re.M)


def _arc_points(cmd: str, x: float, y: float, nx: float, ny: float, i: float, j: float) -> list[tuple[float, float]]:
    """Points after (x, y) along a G2 (cw) / G3 (ccw) arc, ending at (nx, ny)."""
    cx, cy = x + i, y + j
    r = math.hypot(x - cx, y - cy)
    a0 = math.atan2(y - cy, x - cx)
    a1 = math.atan2(ny - cy, nx - cx)
    if cmd == "G2":
        if a1 >= a0:
            a1 -= 2 * math.pi
    elif a1 <= a0:
        a1 += 2 * math.pi
    steps = max(2, int(abs(a1 - a0) * r / _ARC_STEP_MM))
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / steps), cy + r * math.sin(a0 + (a1 - a0) * k / steps))
            for k in range(1, steps + 1)]


def _moves(gcode: str) -> Iterator[tuple]:
    """Yield ("layer",), ("z", z), ("feature", name) and ("move", cmd, axis_values) events."""
    for mt in _LINES.finditer(gcode):
        line = mt.group(0)
        if line.startswith(";"):
            if line.startswith("; CHANGE_LAYER"):
                yield ("layer",)
            elif line.startswith("; Z_HEIGHT:"):
                yield ("z", float(line.split(":")[1]))
            elif line.startswith("; FEATURE:"):
                yield ("feature", line[10:].strip())
            continue
        cmd = line.split(None, 1)[0]
        if cmd not in _MOVE_CMDS:
            continue
        vals = {k: float(v) for k, v in _NUM.findall(line.split(";", 1)[0])}
        yield ("move", cmd, vals)


def parse_walls(gcode: str, bbox: tuple | None) -> list[tuple[float, list[np.ndarray]]]:
    """Outer and overhang wall polylines per layer as (z, [Nx2 arrays]).

    Arcs are interpolated at 0.5 mm. With a bbox (x0, y0, x1, y1), a path whose centre lies far
    outside it is dropped: more than half the box's longer side (and at least 3 mm) away, where
    purge lines and a prime tower sit. The slicer's box is a few mm off on some prints, and a tight
    clip cut real walls out of the picture (a gun barrel, the top of a skull, half a heart box)."""
    layers: list[tuple[float, list[list[tuple[float, float]]]]] = []
    paths: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] | None = None
    x = y = 0.0
    layer_z = 0.0
    in_wall = started = False

    def flush() -> None:
        nonlocal cur
        if cur and len(cur) > 1:
            paths.append(cur)
        cur = None

    for ev in _moves(gcode):
        kind = ev[0]
        if kind == "layer":
            flush()
            if started and paths:
                layers.append((layer_z, paths))
            paths = []
            started = True
        elif kind == "z":
            layer_z = ev[1]
        elif kind == "feature":
            flush()
            in_wall = ev[1] in WALL_FEATURES
        else:
            _, cmd, vals = ev
            nx, ny = vals.get("X", x), vals.get("Y", y)
            if not (vals.get("E", 0) > 0 and in_wall and started):
                flush()
                x, y = nx, ny
                continue
            if cur is None:
                cur = [(x, y)]
            if cmd in ("G2", "G3"):
                cur.extend(_arc_points(cmd, x, y, nx, ny, vals.get("I", 0), vals.get("J", 0)))
            else:
                cur.append((nx, ny))
            x, y = nx, ny
    flush()
    if paths:
        layers.append((layer_z, paths))

    out = []
    if bbox:
        m = max(_BBOX_MARGIN, 0.5 * max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
        x0, y0, x1, y1 = bbox[0] - m, bbox[1] - m, bbox[2] + m, bbox[3] + m
    for z, ps in layers:
        arrs = [np.asarray(p, dtype=np.float64) for p in ps]
        if bbox:
            arrs = [a for a in arrs if x0 <= a[:, 0].mean() <= x1 and y0 <= a[:, 1].mean() <= y1]
        if arrs:
            out.append((z, arrs))
    return out


def layer_segments(gcode: str, index: int) -> list[tuple[str, tuple, tuple]]:
    """Every extruding segment of layer `index` (0-based, in CHANGE_LAYER order) as
    (feature, (x0, y0), (x1, y1)). Arcs are interpolated at 0.5 mm. Empty if the layer is absent."""
    segs: list[tuple[str, tuple, tuple]] = []
    layer = -1
    feature = ""
    x = y = 0.0
    for ev in _moves(gcode):
        kind = ev[0]
        if kind == "layer":
            layer += 1
            if layer > index:
                break
        elif kind == "feature":
            feature = ev[1]
        elif kind == "move":
            _, cmd, vals = ev
            nx, ny = vals.get("X", x), vals.get("Y", y)
            if layer == index and vals.get("E", 0) > 0:
                if cmd in ("G2", "G3"):
                    pts = [(x, y)] + _arc_points(cmd, x, y, nx, ny, vals.get("I", 0), vals.get("J", 0))
                    segs.extend((feature, pts[k], pts[k + 1]) for k in range(len(pts) - 1))
                elif (nx, ny) != (x, y):
                    segs.append((feature, (x, y), (nx, ny)))
            x, y = nx, ny
    return segs


def layer_heights(gcode: str) -> list[float]:
    """Z of every layer, in print order, from the slicer's "; Z_HEIGHT:" comments."""
    return [float(v) for v in re.findall(r"^; Z_HEIGHT: *([-\d.]+)", gcode, re.M)]
