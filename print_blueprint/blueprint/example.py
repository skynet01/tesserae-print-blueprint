"""Bundled T800 study. No printer access, G-code parsing or AI requests."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from .art import ai_art, preview_art, toolpath_art
from .model import PrintJob, SheetOptions, SliceFacts
from .render import render_sheet
from .report import sheet_unit

EXAMPLE_NOTE = "Example: T800"
EXAMPLE_REV = 2
ASSETS = Path(__file__).resolve().parents[1] / "static" / "example"


@lru_cache(maxsize=1)
def _assets():
    facts = SliceFacts(**json.loads((ASSETS / "facts.json").read_text()))
    layers = []
    with np.load(ASSETS / "walls.npz", allow_pickle=False) as data:
        points = data["points"].astype(np.float64) / 100
        lengths, counts = data["lengths"], data["counts"]
        pos = path = 0
        for z, count in zip(data["zs"], counts):
            paths = []
            for length in lengths[path:path + count]:
                paths.append(points[pos:pos + length])
                pos += length
            path += count
            layers.append((float(z), paths))
    return facts, layers, (ASSETS / "cover.png").read_bytes(), (ASSETS / "engraving.png").read_bytes()


def example_job(tz: str = "") -> PrintJob:
    facts, _, cover, _ = _assets()
    zone = ZoneInfo(tz) if tz else None
    start = datetime(2026, 11, 15, 17, 40, tzinfo=zone).timestamp()
    return PrintJob(
        prefix="example", model="P1S", printer_name="P1S", serial="", title="T800",
        status="running", progress=319 / 761, layer=319, layers=761,
        start_ts=start, end_ts=start + 6000, nozzle_d=.4, nozzle_type=None,
        nozzle_temp=(220, 220), bed_temp=(55, 55), chamber_c=None,
        bed_type=facts.bed_type, material="PLA", weight_g=facts.weight_g,
        length_m=facts.length_m, fans={}, speed=None, usage_h=None,
        objects=None, skipped=None, ams=[], ams_humidity=None, tools=[],
        slice=facts, cover_png=cover, job_key="example-t800",
    )


def render_example(size: tuple[int, int], opts: SheetOptions):
    job = example_job(opts.tz)
    facts, walls, cover, engraving = _assets()

    def art(box):
        if opts.method == "toolpath":
            return toolpath_art(facts, box, job.progress if opts.show_progress else 1.0,
                                az=-55, u=sheet_unit(size), walls=walls)
        if opts.method == "ai":
            return ai_art(engraving, box)
        return preview_art(cover, box)

    # Use the same callouts for all three examples. The toolpath-only layer
    # callout otherwise pushes its hero aside; the times block still shows the layer.
    layout_opts = replace(opts, method="preview")
    image, report = render_sheet(job, art, size, layout_opts, note=EXAMPLE_NOTE)
    image.info["note"] = EXAMPLE_NOTE
    return image, report
