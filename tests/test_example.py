"""Offline option previews must show the selected method at the cell's shape."""
import io
import numpy as np
import pytest
from PIL import Image

from test_server import server, ctx, image_file, wire, p1s_states


@pytest.mark.parametrize("method", ["toolpath", "preview", "ai"])
def test_default_preview_is_offline_example(server, monkeypatch, tmp_path, method):
    monkeypatch.setattr(server, "_core", lambda: pytest.fail("example must work offline"))
    result = server.fetch({"method": method}, {"kie_api_key": "never-use"},
                          ctx=ctx(tmp_path, preview=True))
    image = image_file(server, tmp_path, result)
    assert Image.open(image).size == (480, 640)
    assert result["title"] == "T800" and result["example"] is True
    assert "example" in result["note"].lower()
    assert server._sheet_note(image) == result["note"]
    assert "live_pending" not in result and not (tmp_path / "ai").exists()


@pytest.mark.parametrize("method", ["toolpath", "preview", "ai"])
def test_live_preview_without_printer_uses_chosen_example(server, monkeypatch, tmp_path, method):
    monkeypatch.setattr(server, "_core", lambda: None)
    result = server.fetch({"method": method, "editor_preview": "live"}, {},
                          ctx=ctx(tmp_path, preview=True))
    assert result["example"] is True and result["title"] == "T800"
    assert Image.open(image_file(server, tmp_path, result)).size == (480, 640)


@pytest.mark.parametrize("method", ["toolpath", "preview", "ai"])
@pytest.mark.parametrize("size", [(800, 480), (480, 640), (600, 600)])
@pytest.mark.parametrize("paper", ["blue", "white", "black"])
def test_example_layout(method, size, paper):
    from blueprint.example import render_example
    from blueprint.model import SheetOptions
    image, report = render_example(size, SheetOptions(method=method, background=paper, clock="h12"))
    assert image.size == size
    assert np.asarray(image).std() > 10
    assert report.hero_mask.any()
    assert "note" not in report.dropped
    text = " ".join(t for t, _ in report.texts)
    assert "5:40" in text and "7:20" in text
    assert "EXAMPLE" in text and "T800" in text


@pytest.mark.parametrize("panel", ["colour", "grey16", "grey4", "spectra", "bw"])
@pytest.mark.parametrize("method", ["toolpath", "preview", "ai"])
def test_example_panel(method, panel):
    from blueprint.example import render_example
    from blueprint.model import SheetOptions
    image, report = render_example((480, 640), SheetOptions(method=method, panel=panel, background="white"))
    assert report.hero_mask.any()
    colours = len(image.getcolors(1000000))
    if panel == "bw":
        assert colours <= 2
    elif panel in ("grey4", "spectra"):
        assert colours <= 4
    elif panel == "grey16":
        assert colours <= 16


@pytest.mark.parametrize("method", ["toolpath", "preview", "ai"])
def test_square_examples_keep_hero_centred(method):
    from blueprint.example import render_example
    from blueprint.model import SheetOptions
    _, report = render_example((600, 600), SheetOptions(method=method))
    x0, y0, x1, y1 = report.hero_box
    assert 250 <= (x0 + x1) / 2 <= 350
    assert 260 <= y1 - y0 <= 360


@pytest.mark.parametrize("method", ["toolpath", "preview", "ai"])
def test_live_missing_sheet_never_substitutes_other_method(server, monkeypatch, tmp_path, method):
    wire(server, monkeypatch, p1s_states())
    monkeypatch.setattr(server.Runner, "ensure", lambda *a, **kw: server.RenderState(None, True))
    result = server.fetch({"editor_preview": "live", "method": method}, {"kie_api_key": "unused"},
                          ctx=ctx(tmp_path, preview=True))
    assert result["example"] is True and result["title"] == "T800"
    assert "Example" in server._sheet_note(image_file(server, tmp_path, result))


@pytest.mark.parametrize("status", ["failed", "idle", "finish", "unavailable"])
def test_live_does_not_show_old_failed_or_idle_print(server, monkeypatch, tmp_path, status):
    wire(server, monkeypatch, p1s_states(status))
    result = server.fetch({"editor_preview": "live"}, {}, ctx=ctx(tmp_path, preview=True))
    assert result["example"] is True and result["title"] == "T800"


def test_live_line_art_is_real_and_ai_never_pays(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    monkeypatch.setattr(server.Runner, "_ai", lambda *a: pytest.fail("a preview must never buy or poll AI"))
    journal = tmp_path / "ai" / "state.json"
    journal.parent.mkdir()
    journal.write_text('{"day":"2026-11-15","count":3,"tasks":{}}')
    before = journal.read_bytes()
    options = {"editor_preview": "live", "method": "preview"}
    server.fetch(options, {}, ctx=ctx(tmp_path, preview=True))
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    result = server.fetch(options, {}, ctx=ctx(tmp_path, preview=True))
    assert not result.get("example") and result["title"] == "Synthetic Block"
    for settled in (False, True):
        ai = server.fetch({**options, "method": "ai"}, {"kie_api_key": "unused"},
                          ctx=ctx(tmp_path, preview=True, settled=settled))
        assert ai["example"] is True
    # Already-paid art is usable, even without a key, without changing the journal.
    job = server.current_job(server._core(), "p1s_printer", tmp_path)
    from blueprint.example import ASSETS
    (journal.parent / f"{job.job_key}.png").write_bytes((ASSETS / "engraving.png").read_bytes())
    ai_options = {**options, "method": "ai"}
    server.fetch(ai_options, {"kie_api_key": "unused"}, ctx=ctx(tmp_path, preview=True))
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    result = server.fetch(ai_options, {}, ctx=ctx(tmp_path, preview=True))
    assert not result.get("example")
    assert journal.read_bytes() == before


def test_unavailable_live_toolpaths_do_not_block_a_screen_render(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states(), with_3mf=False)
    options = {"editor_preview": "live", "method": "toolpath"}
    first = server.fetch(options, {}, ctx=ctx(tmp_path, preview=True))
    assert first["example"] is True
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    preview = server.fetch(options, {}, ctx=ctx(tmp_path, preview=True))
    assert preview["example"] is True
    screen = server.fetch({"method": "toolpath"}, {}, ctx=ctx(tmp_path))
    path = image_file(server, tmp_path, screen)
    assert not path.name.endswith("-q.png")
    assert server._sheet_note(path) == server.NOTE_NO_3MF
    assert not screen.get("live_pending")


@pytest.mark.parametrize("size", [(800, 480), (480, 640), (600, 600)])
def test_choices_image_uses_canvas_cell_size(server, monkeypatch, tmp_path, size):
    from flask import Flask
    app = Flask(__name__)
    monkeypatch.setattr(server, "_core", lambda: None)
    monkeypatch.setattr(server, "_own_settings", lambda: {})
    monkeypatch.setattr(server, "_data_dir", lambda: tmp_path)
    app.register_blueprint(server.blueprint(), url_prefix="/plugins/print_blueprint")
    with app.test_request_context("/panels/source-form", method="POST", json={"cell_w": size[0], "cell_h": size[1]}):
        rows = server.choices("method", options={"background": "black", "panel": "grey4"})
    client = app.test_client()
    for row in rows:
        assert "/example/" in row["image"]
        response = client.get(row["image"])
        assert response.status_code == 200
        image = Image.open(io.BytesIO(response.data))
        assert image.width / image.height == pytest.approx(size[0] / size[1], abs=.005)
        assert max(image.size) <= 800
        assert len(image.getcolors(100000)) <= 4
        assert response.data == client.get(row["image"]).data
