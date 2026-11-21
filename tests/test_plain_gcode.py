from pathlib import Path

import pytest
from blueprint.gcode import parse_walls
from blueprint.plain_gcode import read_gcode

FIX = Path(__file__).parent / "fixtures" / "octoprint"


@pytest.mark.parametrize("wall", ["External perimeter", "Outer wall", "WALL-OUTER"])
@pytest.mark.parametrize("mode", ["M83", "M82"])
def test_prusa_and_cura_walls_layers_extrusion_resets(wall, mode):
    code = f"{mode}\n;LAYER_CHANGE\n;Z:0.2\nG1 Z0.2\nG0 X10 Y10\n;TYPE:{wall}\n"
    code += "G1 X30 Y10 E1\nG1 X30 Y30 E" + ("1" if mode == "M83" else "2") + "\n"
    code += (
        "G92 E0\nG1 X10 Y30 E1\nG1 X10 Y10 E" + ("1" if mode == "M83" else "2") + "\n"
    )
    facts = read_gcode(code.encode())
    assert facts.layer_z == [0.2]
    assert facts.bbox_all == (10.0, 10.0, 30.0, 30.0)
    walls = parse_walls(facts.gcode, None)
    assert len(walls) == 1 and walls[0][1][0].shape == (5, 2)


@pytest.mark.parametrize("comments", [";LAYER_CHANGE\n", ""])
def test_layer_z_falls_back_to_moves(comments):
    code = "M83\n;TYPE:External perimeter\n"
    for z in (0.2, 0.4):
        code += comments + f"G1 Z{z}\nG0 X10 Y10\nG1 X30 Y10 E1\n"
    facts = read_gcode(code.encode())
    assert facts.layer_z == [0.2, 0.4]
    assert len(parse_walls(facts.gcode, None)) == 2


def test_prusa_comment_metadata_and_thumbnail():
    facts = read_gcode((FIX / "block-thumbnail.gcode").read_bytes())
    assert facts.material == "PLA"
    assert facts.weight_g == 1.2 and facts.length_m == 0.4
    assert facts.prediction_s == 120 and facts.nozzle_d == 0.4
    assert facts.preview_png.startswith(b"\x89PNG")
    assert read_gcode((FIX / "block.gcode").read_bytes()).preview_png is None


def test_orca_colon_totals_and_multiple_prusa_filaments():
    orca = read_gcode(
        b"; total filament length [mm] : 400\n; total filament weight [g] : 1.2\n"
    )
    assert orca.length_m == 0.4 and orca.weight_g == 1.2
    prusa = read_gcode(
        b"; filament used [mm] = 400, 200\n; filament used [g] = 1.2, 0.6\n"
    )
    assert prusa.length_m == 0.6 and prusa.weight_g == pytest.approx(1.8)


def test_cura_layer_comments_and_header():
    facts = read_gcode(
        b";TIME:120\n;Filament used: 0.4m\nM83\n;LAYER:0\nG0 Z0.2\n;TYPE:WALL-OUTER\nG0 X10 Y10\nG1 X30 Y10 E1\n"
    )
    assert facts.layer_z == [0.2]
    assert facts.prediction_s == 120 and facts.length_m == 0.4
    assert parse_walls(facts.gcode, None)
