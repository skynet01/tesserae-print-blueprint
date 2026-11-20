import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

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


def test_open_bore_stays_clear_of_ink_and_grid_mask():
    cover = Image.new("RGBA", (96, 96))
    draw = ImageDraw.Draw(cover)
    draw.ellipse((8, 8, 87, 87), fill=(96, 96, 96, 255))
    draw.ellipse((33, 33, 62, 62), fill=(0, 0, 0, 0))
    buf = io.BytesIO()
    cover.save(buf, "PNG")
    art = preview_art(buf.getvalue(), (300, 300))
    y, x = (n // 2 for n in art.ink.shape)
    assert not art.solid[y-10:y+10, x-10:x+10].any()
    assert not art.ink[y-10:y+10, x-10:x+10].any()
    for ray in (art.ink[y, :x], art.ink[y, x:], art.ink[:y, x], art.ink[y:, x]):
        visible = ray > 80  # both walls of the ring survive a one-bit panel
        assert np.count_nonzero(visible[1:] & ~visible[:-1]) >= 2


def test_preview_is_deterministic():
    png = COVER.read_bytes()
    a, b = preview_art(png, (800, 480)), preview_art(png, (800, 480))
    np.testing.assert_array_equal(a.ink, b.ink)
    np.testing.assert_array_equal(a.solid, b.solid)


def test_mezzotint_keeps_shadow_dots_and_face_boundary():
    im = Image.new("RGBA", (120, 80), (210, 210, 210, 255))
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, 59, 79), fill=(50, 50, 50, 255))
    # Transparent margin distinguishes the cover from an opaque background.
    canvas = Image.new("RGBA", (130, 90))
    canvas.paste(im, (5, 5))
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    art = preview_art(buf.getvalue(), (120, 80))
    shadow = art.ink[10:-10, 10:50] > 80
    # At maximum darkness each 4x4 screen cell retains four paper pixels.
    assert shadow.mean() == .75
    assert not art.ink[10:-10, 70:110].any()
    assert (art.ink[10:-10, 58:62] > 80).any(axis=1).all()
