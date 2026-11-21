import pytest

from blueprint.filament import profile


@pytest.mark.parametrize('attrs,expected', [
    ({'name': 'My PLA', 'filament_id': 'GFA00'}, 'My PLA'),
    ({'filament_name': 'PLA', 'filament_type': 'PLA', 'filament_id': 'GFA20'}, 'Bambu PLA Palette'),
    ({'name': 'unknown', 'tray_info_idx': 'GFA00'}, 'Bambu PLA Basic'),
    ({'type': 'PLA', 'filament_id': 'GFL99'}, 'Generic PLA'),
    ({'type': 'PETG', 'sub_brands': 'Custom PETG'}, 'Custom PETG'),
    ({'filament_type': 'PLA', 'filament_id': 'ZZ999'}, 'PLA'),
    ({'filament_id': 'ZZ999'}, ''),
    ({'name': 'Empty', 'type': 'Empty'}, ''),
])
def test_profile_evidence(attrs, expected):
    assert profile(attrs) == expected
