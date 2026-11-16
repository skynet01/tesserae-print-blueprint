import math
import re
import zipfile
from pathlib import Path

import pytest

from blueprint.gcode import layer_segments, parse_walls
from blueprint.threemf import read_3mf
from blueprint.titles import pretty_title
from blueprint.units import to_c, to_m, to_mm

FIX = Path(__file__).parent / "fixtures"
PLATE = FIX / "synthetic-plate.3mf"
BLOCK = FIX / "synthetic-block.3mf"


def _header_layers(facts):
    m = re.search(r"^; total layer number: (\d+)", facts.gcode[:200000], re.M)
    return int(m.group(1))


def test_read_3mf_fixture():
    f = read_3mf(PLATE.read_bytes())
    assert f.walls == 2
    assert f.infill_pct == 15
    assert f.pattern == "grid"
    assert f.layer_h == 0.2
    assert f.layers == _header_layers(f) == 12
    assert f.prediction_s == 120
    assert f.gcode
    assert f.weight_g == pytest.approx(1.2)
    assert f.length_m == pytest.approx(0.4)
    assert f.material == "PLA"
    assert f.colors == ["#FFFFFF"]
    assert f.bed_type == "Textured PEI Plate"
    assert f.bed_temp == 55 and f.nozzle_temp == 220 and f.nozzle_d == 0.4
    assert f.support is False
    assert f.filament_name == "Generic PLA"
    assert f.objects == ["Test plate", "Test tab"]
    assert f.bbox_all is not None and f.preview_png and f.preview_png[:4] == b"\x89PNG"
    assert f.envelope_mm == pytest.approx((30, 12, 2.4))


def test_read_3mf_missing_keys_are_none(tmp_path):
    src = zipfile.ZipFile(BLOCK)
    out = tmp_path / "bare.3mf"
    with zipfile.ZipFile(out, "w") as z:
        for n in src.namelist():
            data = src.read(n)
            if n == "Metadata/plate_1.gcode":
                text = data.decode()
                text = re.sub(r"^; (wall_loops|sparse_infill_density|sparse_infill_pattern|"
                              r"filament_density|line_width|nozzle_temperature|curr_bed_type|"
                              r"support_threshold_angle|filament_settings_id) = .*\n", "", text, flags=re.M)
                data = text.encode()
            z.writestr(n, data)
    f = read_3mf(out.read_bytes())
    assert f.walls is None and f.infill_pct is None and f.pattern is None
    assert f.density is None and f.line_w is None and f.nozzle_temp is None
    assert f.bed_type is None and f.bed_temp is None and f.support_angle is None
    assert f.filament_name is None


def test_parse_walls_layer_count_matches_header():
    f = read_3mf(BLOCK.read_bytes())
    layers = parse_walls(f.gcode, f.bbox_all)
    assert len(layers) == _header_layers(f) == 16
    z, paths = layers[0]
    assert z == pytest.approx(.2)
    assert paths[0].tolist() == [[80, 80], [88, 80], [88, 86], [80, 86], [80, 80]]


def test_layer_segments():
    f = read_3mf(BLOCK.read_bytes())
    segs = layer_segments(f.gcode, 5)
    assert segs
    feat, a, b = segs[0]
    assert isinstance(feat, str) and len(a) == 2 and len(b) == 2
    assert {s[0] for s in segs} >= {"Outer wall"}
    assert layer_segments(f.gcode, 10_000) == []


SYN = """; CHANGE_LAYER
; Z_HEIGHT: 0.2
; FEATURE: Outer wall
G1 X100 Y100 F3000
G1 X110 Y100 E1
G1 X240 Y265 E1
; CHANGE_LAYER
; Z_HEIGHT: 0.4
; FEATURE: Outer wall
G1 X240 Y265 F3000
G1 X245 Y265 E1
"""


def test_parse_walls_drops_purge_outside_bbox():
    bbox = (80, 30, 170, 200)
    layers = parse_walls(SYN, bbox)
    pts = [p for _, ps in layers for a in ps for p in a.tolist()]
    assert all(not (abs(x - 240) < 1 and abs(y - 265) < 1) for x, y in pts)
    assert len(parse_walls(SYN, None)) == 2


def test_arcs_interpolated():
    g = """; CHANGE_LAYER
; Z_HEIGHT: 0.2
; FEATURE: Outer wall
G1 X110 Y100 F3000
G3 X100 Y110 I-10 J0 E1
"""
    (z, [path]), = parse_walls(g, None)
    assert len(path) >= 3
    r = [math.hypot(x - 100, y - 100) for x, y in path]
    assert all(abs(v - 10) < 1e-6 for v in r)


def test_units():
    assert to_c(473, "°F") == pytest.approx(245, abs=0.05)
    assert to_c(245, "°C") == 245
    assert to_mm(0.015748, "in") == pytest.approx(0.4, abs=1e-3)
    assert to_mm(0.4, "mm") == 0.4
    assert to_m(79.1667, "ft") == pytest.approx(24.13, abs=0.01)
    assert to_m(24.13, "m") == pytest.approx(24.13)
    # a unit we do not know is an unknown value, never an exception
    assert to_mm(3, "furlong") is None and to_m(3, "parsec") is None and to_c(20, "W") is None
    assert to_c(293.15, "K") == pytest.approx(20)


def test_pretty_title_strips_unsupported_glyphs():
    assert pretty_title("Calibration Bracket 中文 (Layer Study)", "X") == "CALIBRATION BRACKET (LAYER STUDY)"
    assert pretty_title("中文夹具", "PLATE 001") == "PLATE 001"
    assert pretty_title("Calibration_Bracket_plate_2", "X") == "CALIBRATION BRACKET"


@pytest.mark.parametrize("bed,key", [
    ("High Temp Plate", "hot_plate_temp"),
    ("Textured PEI Plate", "textured_plate_temp"),
    ("Cool Plate", "cool_plate_temp"),
    ("Cool Plate SuperTack", "supertack_plate_temp"),
    ("Engineering Plate", "eng_plate_temp"),
])
def test_bed_temp_per_bed_type(bed, key):
    from blueprint.threemf import _bed_temp
    cfg = {k: str(i) for i, k in enumerate(
        ["hot_plate_temp", "textured_plate_temp", "cool_plate_temp", "supertack_plate_temp", "eng_plate_temp"], 50)}
    assert _bed_temp(cfg, bed) == float(cfg[key])


def test_read_3mf_without_gcode_raises_value_error(tmp_path):
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/slice_info.config", "<config/>")
    with pytest.raises(ValueError, match="no plate gcode"):
        read_3mf(buf.getvalue())


def test_first_keeps_commas_splits_semicolons():
    from blueprint.threemf import _first, _num
    assert _first({"k": '"Bambu PLA, Basic;Other"'}, "k") == "Bambu PLA, Basic"
    assert _num("220,230") == 220


def test_non_move_g_commands_ignored():
    g = """; CHANGE_LAYER
; Z_HEIGHT: 0.2
; FEATURE: Outer wall
G28 X5 Y5 E1
G10 X7 Y7 E1
G1 X10 Y10 F3000
G1 X20 Y10 E1
"""
    (z, [path]), = parse_walls(g, None)
    assert path.tolist() == [[10, 10], [20, 10]]


def test_arcs_clockwise_g2():
    g = """; CHANGE_LAYER
; Z_HEIGHT: 0.2
; FEATURE: Outer wall
G1 X100 Y110 F3000
G2 X110 Y100 I0 J-10 E1
"""
    (z, [path]), = parse_walls(g, None)
    assert len(path) >= 3
    assert all(abs(math.hypot(x - 100, y - 100) - 10) < 1e-6 for x, y in path)
    assert all(x >= 100 - 1e-9 and y >= 100 - 1e-9 for x, y in path)  # quarter in +x,+y


def test_layer_z_on_slice_facts_and_no_module_cache():
    import blueprint.gcode as G
    f = read_3mf(BLOCK.read_bytes())
    assert len(f.layer_z) == _header_layers(f) and f.layer_z == sorted(f.layer_z)
    assert not hasattr(G, "_Z_CACHE")  # nothing outlives the parsed 3MF


def test_crlf_and_indented_gcode_parse_the_same():
    lf = parse_walls(SYN, None)
    crlf = parse_walls(SYN.replace("\n", "\r\n").replace("G1 X110", "  G1 X110"), None)
    assert [(z, [a.tolist() for a in ps]) for z, ps in lf] == [(z, [a.tolist() for a in ps]) for z, ps in crlf]
    # a G10 / G28 or a glued "G1X" is not a move
    assert parse_walls(SYN.replace("G1 X240 Y265 E1", "G10 X240 Y265 E1"), None)[0][1][0].tolist()[-1] == [110, 100]
