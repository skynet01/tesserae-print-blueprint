"""Read slicer facts out of a Bambu Studio / Orca .3mf (a zip with G-code and metadata)."""
from __future__ import annotations

import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile

from .gcode import layer_heights
from .model import SliceFacts

_HEADER_BYTES = 200_000
_BED_KEYS = (  # (substring of curr_bed_type, header key)
    ("high temp", "hot_plate_temp"),
    ("textured", "textured_plate_temp"),
    ("supertack", "supertack_plate_temp"),  # before "cool": "Cool Plate SuperTack"
    ("cool", "cool_plate_temp"),
    ("eng", "eng_plate_temp"),
)


def _first(cfg: dict[str, str], key: str) -> str | None:
    """First element of a possibly quoted, ';' separated header value (commas can be part of a name)."""
    v = cfg.get(key)
    if v is None:
        return None
    v = v.split(";")[0].strip().strip('"').strip()
    return v or None


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.split(",")[0].strip().rstrip("%"))
    except ValueError:
        return None


def _bed_temp(cfg: dict[str, str], bed_type: str | None) -> float | None:
    b = (bed_type or "").lower()
    for sub, key in _BED_KEYS:
        if sub in b:
            return _num(_first(cfg, key))
    return None


def read_3mf(data: bytes) -> SliceFacts:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return _read(z)


def _read(z: zipfile.ZipFile) -> SliceFacts:
    names = z.namelist()
    gname = next((n for n in sorted(names) if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)), None)
    if gname is None:
        raise ValueError("no plate gcode in 3MF")
    pn = re.search(r"plate_(\d+)", gname).group(1)
    gcode = z.read(gname).decode("utf-8", "replace")
    head = gcode[:_HEADER_BYTES]

    cfg: dict[str, str] = {m.group(1): m.group(2).strip() for m in re.finditer(r"^; (\w+) = (.*)$", head, re.M)}
    for m in re.finditer(r"^; (total layer number|max_z_height): (.*)$", head, re.M):
        cfg[m.group(1)] = m.group(2).strip()

    meta: dict[str, str] = {}
    objects: list[str] = []
    fil: list[dict[str, str]] = []
    if "Metadata/slice_info.config" in names:
        si = ET.fromstring(z.read("Metadata/slice_info.config"))
        meta = {e.get("key"): e.get("value") for e in si.iter("metadata")}
        objects = [o.get("name") for o in si.iter("object") if o.get("name")]
        fil = [dict(f.attrib) for f in si.iter("filament")]

    plate: dict = {}
    if f"Metadata/plate_{pn}.json" in names:
        plate = json.loads(z.read(f"Metadata/plate_{pn}.json"))
    bb = plate.get("bbox_all")
    bbox_all = tuple(float(v) for v in bb) if bb and len(bb) == 4 else None
    preview = z.read(f"Metadata/plate_{pn}.png") if f"Metadata/plate_{pn}.png" in names else None

    layers = _num(cfg.get("total layer number"))
    max_z = _num(cfg.get("max_z_height"))
    env = (bbox_all[2] - bbox_all[0], bbox_all[3] - bbox_all[1], max_z or 0.0) if bbox_all else (0.0, 0.0, max_z or 0.0)

    weight = sum(_num(f.get("used_g")) or 0.0 for f in fil)
    length = sum(_num(f.get("used_m")) or 0.0 for f in fil)
    if not fil:  # no per-filament rows: fall back to the plate totals
        weight = _num(meta.get("weight")) or 0.0

    bed_type = _first(cfg, "curr_bed_type")
    fname = _first(cfg, "filament_settings_id")
    if fname:
        fname = re.sub(r"\s*@.*$", "", fname) or None
    density_pct = _num(_first(cfg, "sparse_infill_density"))
    walls = _num(_first(cfg, "wall_loops"))
    material = (fil[0].get("type") if fil else None) or _first(cfg, "filament_type")

    return SliceFacts(
        layer_h=_num(cfg.get("layer_height")) or 0.0,
        layers=int(layers) if layers is not None else 0,
        walls=int(walls) if walls is not None else None,
        infill_pct=density_pct,
        pattern=_first(cfg, "sparse_infill_pattern"),
        support={"true": True, "false": False}.get(str(meta.get("support_used")).lower()),  # None: unknown
        support_type=_first(cfg, "support_type"),
        support_angle=_num(_first(cfg, "support_threshold_angle")),
        envelope_mm=env,
        prediction_s=_num(meta.get("prediction")) or 0.0,
        weight_g=weight,
        length_m=length,
        density=_num(_first(cfg, "filament_density")),
        line_w=_num(_first(cfg, "line_width")),
        first_layer_s=_num(meta.get("first_layer_time")),
        nozzle_d=_num(meta.get("nozzle_diameters")) or _num(_first(cfg, "nozzle_diameter")),
        nozzle_temp=_num(_first(cfg, "nozzle_temperature")),
        bed_temp=_bed_temp(cfg, bed_type),
        bed_type=bed_type,
        material=material,
        filament_name=fname,
        colors=[f["color"] for f in fil if f.get("color")],
        objects=objects,
        bbox_all=bbox_all,
        gcode=gcode,
        preview_png=preview,
        layer_z=layer_heights(gcode),
    )
