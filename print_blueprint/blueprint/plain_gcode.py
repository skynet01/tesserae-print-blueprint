"""Read ordinary PrusaSlicer, Orca and Cura G-code without changing the 3MF reader."""
from __future__ import annotations

import base64
import binascii
import io
import re

from PIL import Image

from .gcode import layer_heights, parse_walls
from .model import SliceFacts

_WALLS = {'External perimeter': 'Outer wall', 'Outer wall': 'Outer wall',
          'WALL-OUTER': 'Outer wall', 'Overhang perimeter': 'Overhang wall'}
_AXIS = re.compile(r'([XYZE])\s*(-?\d*\.?\d+)')
_THUMB = re.compile(r'^;\s*thumbnail(?:_JPG|_PNG)? begin[^\n]*\n(.*?)^;\s*thumbnail(?:_JPG|_PNG)? end', re.M | re.S | re.I)


def _thumbnail(text: str) -> bytes | None:
    for match in _THUMB.finditer(text):
        try:
            data = base64.b64decode(re.sub(r'[;\s]', '', match[1]), validate=True)
            with Image.open(io.BytesIO(data)) as im:
                out = io.BytesIO()
                im.save(out, 'PNG')
                return out.getvalue()
        except (ValueError, OSError, binascii.Error):
            continue
    return None


def _normalise(text: str) -> str:
    """Adapt slicer comments and extrusion modes to the existing wall reader's grammar."""
    lines = []
    relative_e = False  # firmware default; M82/M83 override it
    relative_xyz = False
    extrusion = 0.0
    z = 0.0
    explicit_layers = bool(re.search(r'^;\s*(?:LAYER_CHANGE|CHANGE_LAYER|LAYER:\s*\d+)', text, re.M))
    started = False
    height_written = False
    last_layer_z = None
    for raw in text.splitlines():
        line = raw.strip()
        if re.match(r'^;\s*(?:LAYER_CHANGE|CHANGE_LAYER|LAYER:\s*\d+)(?:\s|$)', line):
            lines.append('; CHANGE_LAYER')
            started = True
            height_written = False
            continue
        height = re.match(r'^;\s*(?:Z:|Z_HEIGHT:)\s*([\d.]+)', line)
        if height:
            z = float(height[1])
            if not height_written:
                lines.append(f'; Z_HEIGHT: {z}')
                height_written = True
            continue
        feature = re.match(r'^;\s*(?:TYPE:|FEATURE:)\s*(.*)', line)
        if feature:
            lines.append('; FEATURE: ' + _WALLS.get(feature[1], feature[1]))
            continue
        code = line.split(';', 1)[0].strip()
        cmd = code.split(None, 1)[0].upper() if code else ''
        axes = {k: float(v) for k, v in _AXIS.findall(code)}
        if cmd in ('M82', 'M83'):
            relative_e = cmd == 'M83'
        elif cmd in ('G90', 'G91'):
            relative_xyz = cmd == 'G91'
        elif cmd == 'G92' and 'E' in axes:
            extrusion = axes['E']
        elif cmd in ('G0', 'G1', 'G2', 'G3'):
            if 'Z' in axes:
                z = z + axes['Z'] if relative_xyz else axes['Z']
            delta = axes.get('E', 0.0) if relative_e else axes.get('E', extrusion) - extrusion
            if 'E' in axes:
                extrusion = extrusion + axes['E'] if relative_e else axes['E']
                line = _AXIS.sub(lambda m: f'E{delta:.8f}' if m[1] == 'E' else m[0], line)
            if not explicit_layers and delta > 0 and z != last_layer_z:
                lines.append('; CHANGE_LAYER')
                started = True
                last_layer_z = z
                lines.append(f'; Z_HEIGHT: {z}')
            if explicit_layers and started and 'Z' in axes and not height_written:
                lines.append(f'; Z_HEIGHT: {z}')
                height_written = True
        lines.append(line)
    return '\n'.join(lines)


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r'-?\d+(?:\.\d+)?', value)
    return float(match[0]) if match else None


def read_gcode(data: bytes) -> SliceFacts:
    text = data.decode('utf-8', 'replace')
    cfg = {m[1].strip().lower(): m[2].strip() for m in re.finditer(r'^;\s*([^\n=]+?)\s*=\s*(.*?)\s*$', text, re.M)}
    def num(*keys):
        return next((v for k in keys if (v := _num(cfg.get(k))) is not None), None)
    def first(*keys):
        return next((cfg[k].split(';')[0].strip().strip('"') for k in keys if cfg.get(k)), None)
    gcode = _normalise(text)
    paths = parse_walls(gcode, None)
    zs = layer_heights(gcode)
    points = [p for _, ps in paths for p in ps]
    bbox = (min(float(p[:, 0].min()) for p in points), min(float(p[:, 1].min()) for p in points),
            max(float(p[:, 0].max()) for p in points), max(float(p[:, 1].max()) for p in points)) if points else None
    seconds = num('estimated printing time (normal mode)') or 0.0
    duration = cfg.get('estimated printing time (normal mode)', '')
    if duration:
        seconds = sum(float(n) * {'d': 86400, 'h': 3600, 'm': 60, 's': 1}[unit]
                      for n, unit in re.findall(r'(\d+(?:\.\d+)?)\s*([dhms])', duration))
    cura_time = re.search(r'^;TIME:(\d+)', text, re.M)
    if not seconds and cura_time:
        seconds = float(cura_time[1])
    length = (num('filament used [mm]') or 0.0) / 1000
    cura_length = re.search(r'^;Filament used:\s*([\d.]+)\s*(mm|m)', text, re.M)
    if not length and cura_length:
        length = float(cura_length[1]) / (1000 if cura_length[2] == 'mm' else 1)
    layer_h = num('layer_height') or (zs[1] - zs[0] if len(zs) > 1 else zs[0] if zs else 0.0)
    support = first('support_material', 'enable_support')
    return SliceFacts(
        layer_h=round(layer_h, 5), layers=len(zs), walls=int(v) if (v := num('perimeters', 'wall_loops')) is not None else None,
        infill_pct=num('fill_density', 'sparse_infill_density'), pattern=first('fill_pattern', 'sparse_infill_pattern'),
        support={'1': True, '0': False, 'true': True, 'false': False}.get(support),
        support_type=first('support_type'), support_angle=num('support_material_threshold', 'support_threshold_angle'),
        envelope_mm=(bbox[2] - bbox[0], bbox[3] - bbox[1], max(zs, default=0.0)) if bbox else (0., 0., max(zs, default=0.0)),
        prediction_s=seconds, weight_g=num('filament used [g]') or 0., length_m=length,
        density=num('filament_density'), line_w=num('extrusion_width', 'line_width'), first_layer_s=None,
        nozzle_d=num('nozzle_diameter'), nozzle_temp=num('temperature', 'nozzle_temperature'), bed_temp=num('bed_temperature'),
        bed_type=None, material=first('filament_type'), filament_name=first('filament_settings_id'),
        colors=[c] if (c := first('filament_colour', 'filament_color')) else [], objects=[], bbox_all=bbox,
        gcode=gcode, preview_png=_thumbnail(text), layer_z=zs,
    )
