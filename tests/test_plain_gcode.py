from pathlib import Path

import pytest

from blueprint.plain_gcode import read_gcode
from blueprint.gcode import parse_walls

FIX = Path(__file__).parent / 'fixtures' / 'octoprint'


@pytest.mark.parametrize('wall', ['External perimeter', 'Outer wall', 'WALL-OUTER'])
@pytest.mark.parametrize('mode', ['M83', 'M82'])
def test_prusa_and_cura_walls_layers_extrusion_resets(wall, mode):
    code = f'{mode}\n;LAYER_CHANGE\n;Z:0.2\nG1 Z0.2\nG0 X10 Y10\n;TYPE:{wall}\n'
    code += 'G1 X30 Y10 E1\nG1 X30 Y30 E' + ('1' if mode == 'M83' else '2') + '\n'
    code += 'G92 E0\nG1 X10 Y30 E1\nG1 X10 Y10 E' + ('1' if mode == 'M83' else '2') + '\n'
    facts = read_gcode(code.encode())
    assert facts.layer_z == [.2]
    assert facts.bbox_all == (10., 10., 30., 30.)
    walls = parse_walls(facts.gcode, None)
    assert len(walls) == 1 and walls[0][1][0].shape == (5, 2)


@pytest.mark.parametrize('comments', [';LAYER_CHANGE\n', ''])
def test_layer_z_falls_back_to_moves(comments):
    code = 'M83\n;TYPE:External perimeter\n'
    for z in (.2, .4):
        code += comments + f'G1 Z{z}\nG0 X10 Y10\nG1 X30 Y10 E1\n'
    facts = read_gcode(code.encode())
    assert facts.layer_z == [.2, .4]
    assert len(parse_walls(facts.gcode, None)) == 2


def test_prusa_comment_metadata_and_thumbnail():
    facts = read_gcode((FIX / 'block-thumbnail.gcode').read_bytes())
    assert facts.material == 'PLA'
    assert facts.weight_g == 1.2 and facts.length_m == .4
    assert facts.prediction_s == 120 and facts.nozzle_d == .4
    assert facts.preview_png.startswith(b'\x89PNG')
    assert read_gcode((FIX / 'block.gcode').read_bytes()).preview_png is None
