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


@pytest.mark.parametrize('paper,default', [('blue', True), ('white', False), ('black', False)])
def test_grid_default_matches_existing_paper_and_can_be_overridden(paper, default):
    from blueprint.layout import Metrics, Sheet, frame
    for choice, enabled in [(None, default), (True, True), (False, False)]:
        opts = SheetOptions(background=paper, show_grid=choice)
        sh = Sheet(Metrics(1200,1600))
        frame(sh, paper, sh.m, opts.grid_enabled)
        assert any(op[0]=='grid' and op[1] for op in sh.ops) is enabled
        assert any(op[0]=='rect' for op in sh.ops)  # border survives grid off
        if paper=='blue':
            assert any(group=='frame' for group in sh.tgroups)  # frame coordinates are independent


def test_cell_look_grid_off_and_default():
    from blueprint.cell_look import apply
    look={'bg':'#141310','ink':'#f1eee4','rule':'#b9b4a6','accent':'#69b7b0','font':''}
    for paper in ('blue','white','black'):
        assert apply(SheetOptions(background=paper),look).grid_enabled
        assert not apply(SheetOptions(background=paper,show_grid=False),look).grid_enabled
