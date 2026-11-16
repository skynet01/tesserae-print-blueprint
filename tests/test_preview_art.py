import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from blueprint.art import preview_art

COVER = Path(__file__).parents[1] / "print_blueprint" / "static" / "example" / "cover.png"


@pytest.mark.parametrize("box", [(240, 320), (800, 480), (600, 600), (1600, 1200), (3, 7)])
@pytest.mark.parametrize("opaque", [False, True])
def test_preview_contract(box, opaque):
    im = Image.open(COVER).convert("RGBA")
    if opaque:
        bg = Image.new("RGBA", im.size, "white")
        bg.alpha_composite(im)
        im = bg.convert("RGB")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    art = preview_art(buf.getvalue(), box)
    assert art.ink.shape == art.solid.shape
    assert art.ink.dtype == art.solid.dtype == np.uint8
    assert 0 < art.ink.shape[0] <= box[1]
    assert 0 < art.ink.shape[1] <= box[0]
    if min(box) >= 8:
        assert art.ink.any() and art.solid.any()


@pytest.mark.parametrize("colour", [(0, 0, 0, 0), (255, 255, 255, 255), (128, 128, 128, 255)])
def test_blank_cover_is_empty(colour):
    buf = io.BytesIO()
    Image.new("RGBA", (32, 32), colour).save(buf, "PNG")
    art = preview_art(buf.getvalue(), (240, 320))
    assert not art.ink.any() and not art.solid.any()


def test_missing_cover_is_empty():
    art = preview_art(b"", (240, 320))
    assert not art.ink.any() and not art.solid.any()


def test_preview_is_deterministic():
    png = COVER.read_bytes()
    a, b = preview_art(png, (800, 480)), preview_art(png, (800, 480))
    np.testing.assert_array_equal(a.ink, b.ink)
    np.testing.assert_array_equal(a.solid, b.solid)


@pytest.mark.parametrize("box, block_size", [((240, 320), 2), ((800, 480), 2), ((1600, 1200), 3)])
def test_preview_leaves_paper_between_lines(box, block_size):
    art = preview_art(COVER.read_bytes(), box)
    ink = art.ink > 80  # the renderer's one-bit threshold
    assert ink.sum() / (art.solid > 0).sum() < 0.38
    # Allow a wider pen on large sheets, but reject merged fingers and joints.
    blocks = np.lib.stride_tricks.sliding_window_view(ink, (block_size, block_size)).all(axis=(-1, -2))
    assert blocks.sum() / max(1, ink.sum()) < 0.12
