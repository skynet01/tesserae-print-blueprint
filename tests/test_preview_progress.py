"""Progress adds ink below the cut instead of hiding the unfinished sketch."""
from types import SimpleNamespace

import numpy as np
import pytest

from blueprint.art import Art
from blueprint.model import SheetOptions
from blueprint.preview_progress import apply_progress


def sample():
    solid = np.zeros((90, 120), np.uint8)
    solid[20:60, 10:110] = 255
    solid[:, 70:80] = 0
    ink = solid.copy()
    ink[:, ::2] = 0
    return Art(ink, solid)


@pytest.mark.parametrize('panel', ['colour', 'grey16', 'grey4', 'spectra', 'bw'])
def test_half_shaded_but_both_halves_keep_detail(panel):
    art = sample()
    job = SimpleNamespace(progress=.5, layer=25)
    result = apply_progress(art, job, SheetOptions(panel=panel))
    assert result.solid is art.solid
    assert np.array_equal(result.ink[:25], art.ink[:25])
    assert np.array_equal(result.ink[40], art.solid[40])  # midpoint of part, not canvas
    assert not result.ink[art.solid == 0].any()
    region = result.ink[45:60, 15:65]
    before = art.ink[45:60, 15:65]
    assert np.all(region >= before) and np.count_nonzero(region > before) > 80
    assert region.min() < region.max()
    if panel in ('bw', 'spectra', 'grey4'):
        assert set(np.unique(region)) == {0, 255}
    assert np.array_equal(art.ink, sample().ink)


@pytest.mark.parametrize('progress,enabled', [(0, True), (.5, False), (1, False)])
def test_plain_sketch(progress, enabled):
    art = sample()
    job = SimpleNamespace(progress=progress, layer=None)
    assert apply_progress(art, job, SheetOptions(show_progress=enabled)) is art


def test_completed_part_shaded_to_its_top():
    art = sample()
    result = apply_progress(art, SimpleNamespace(progress=1, layer=50), SheetOptions())
    assert np.all(result.ink[art.solid > 0] >= 85)
    assert np.all(result.ink[art.ink == 255] == 255)
