import pytest
from PIL import ImageFont

from blueprint.model import SheetOptions
from blueprint.theme import font_context, font_for, theme_for


@pytest.mark.parametrize('panel', ['colour', 'grey16', 'grey4', 'spectra', 'bw'])
@pytest.mark.parametrize('palette', [('#141310', '#F1EEE4', '#B9B4A6', '#69B7B0'),
                                     ('#F7F5F0', '#1B1A16', '#4D4A42', '#256E6B')])
def test_cell_palette_survives_every_panel(panel, palette):
    opts = SheetOptions(palette=palette, panel=panel)
    th = theme_for(opts)
    assert th['bg'] != th['ink']
    if panel in ('grey16', 'grey4', 'bw'):
        for key in ('bg', 'ink', 'rule', 'accent'):
            assert len(set(th[key])) == 1
    if panel == 'spectra':
        from blueprint.theme import SPECTRA
        assert set(th.values()) <= {v[0] for v in SPECTRA.values()}
    assert theme_for(SheetOptions())['bg'] == (28, 70, 136)


def test_cell_font_metrics_are_local_and_missing_characters_use_osifont():
    from blueprint.theme import FONT_PATH
    # Pillow ships no fonts; use the widget's real font to exercise selection/fallback.
    with font_context(FONT_PATH):
        assert font_for('PLA', 20).getname()
    with font_context('/not-a-font'):
        assert font_for('PLA', 20).getname() == ImageFont.truetype(FONT_PATH, 20).getname()
