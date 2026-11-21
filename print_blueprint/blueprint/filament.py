"""Offline display names; never infer a brand from a material type or colour."""

# filament_id in BambuStudio/resources/profiles/BBL/filament/* @base.json.
PROFILES = {
    'GFA00': 'Bambu PLA Basic', 'GFA01': 'Bambu PLA Matte',
    'GFA06': 'Bambu PLA Silk+', 'GFA20': 'Bambu PLA Palette',
    'GFB00': 'Bambu ABS', 'GFG00': 'Bambu PETG Basic',
    'GFG01': 'Bambu PETG Translucent', 'GFG02': 'Bambu PETG HF',
    'GFG03': 'Bambu PETG Matte', 'GFG99': 'Generic PETG',
    'GFL99': 'Generic PLA', 'GFU00': 'Bambu TPU 95A HF',
    'GFU06': 'Bambu TPU 75A(2.85mm)',
}
_TYPES = {'PLA', 'PETG', 'ABS', 'ASA', 'TPU', 'PC', 'PA', 'PVA', 'HIPS', 'PET', 'PCTG'}


def _text(value):
    value = value.strip() if isinstance(value, str) else ''
    return '' if value.lower() in {'', 'unknown', 'unavailable', 'none', 'empty'} else value


def profile(*sources: dict) -> str:
    """Prefer a reported profile, then a known ID, then the reported type."""
    fallback = mapped = ''
    for source in sources:
        kind = _text(source.get('type')) or _text(source.get('filament_type'))
        for key in ('name', 'filament_name', 'sub_brands'):
            name = _text(source.get(key))
            if name and name.upper() not in _TYPES and name.casefold() != kind.casefold():
                return name
            fallback = fallback or name
        fallback = fallback or kind
        for key in ('filament_id', 'tray_info_idx'):
            mapped = mapped or PROFILES.get(_text(source.get(key)).upper(), '')
    return mapped or fallback
