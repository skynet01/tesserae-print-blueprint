"""print_blueprint through the Tesserae host: the manifest loads, the cell renders at every size
(with Home Assistant missing and with a fake printer), the sheet route serves PNGs and is render
safe, and the compare page draws. Home Assistant and its files are fakes; nothing touches the
network. Run from a Tesserae checkout with this folder copied to plugins/print_blueprint/."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import ModuleType

import pytest
from flask import Flask
from flask.testing import FlaskClient
from PIL import Image, ImageDraw

PLUGIN = "print_blueprint"
PREFIX = "shop_x1c"  # a made-up printer; nothing in the widget may know it


def _cover() -> bytes:
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon([(40, 200), (128, 40), (216, 200)], fill=(200, 120, 40, 255))
    d.rectangle([90, 150, 166, 210], fill=(90, 60, 30, 255))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _states(status: str = "RUNNING") -> list[dict]:
    s = {
        f"sensor.{PREFIX}_print_status": (status, {"friendly_name": "Shop X1C Print status"}),
        f"sensor.{PREFIX}_task_name": ("Bracket_plate_1", {}),
        f"sensor.{PREFIX}_serial_number": ("SER123", {}),
        f"sensor.{PREFIX}_current_layer": ("40", {}),
        f"sensor.{PREFIX}_total_layer_count": ("100", {}),
        f"sensor.{PREFIX}_nozzle_temperature": ("220", {"unit_of_measurement": "°C"}),
        f"sensor.{PREFIX}_nozzle_target_temperature": ("220", {"unit_of_measurement": "°C"}),
        f"image.{PREFIX}_cover_image": ("2026-11-14", {"entity_picture": f"/api/image_proxy/image.{PREFIX}_cover_image?token=t"}),
    }
    return [{"entity_id": k, "state": v, "attributes": a} for k, (v, a) in s.items()]


@pytest.fixture
def mod(app: Flask) -> ModuleType:
    plugin = app.config["PLUGIN_REGISTRY"].get(PLUGIN)
    assert plugin is not None and plugin.server_module is not None
    return plugin.server_module


@pytest.fixture
def printer(app: Flask, mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A fake Home Assistant with one printer and its cover picture; records every path asked for."""
    core = app.config["PLUGIN_REGISTRY"].get("ha_core").server_module
    monkeypatch.setattr(core, "get_states", lambda: _states())
    monkeypatch.setattr(core, "base_url", lambda: "http://ha.invalid:8123")
    calls: list[str] = []
    cover = _cover()

    def http(path: str) -> bytes:
        calls.append(path)
        if path.startswith("/api/bambu_lab/print_history"):
            return json.dumps({"files": []}).encode()
        if path.startswith("/api/image_proxy/"):
            return cover
        raise OSError(f"404 {path}")

    monkeypatch.setattr(mod, "_http_for", lambda core, **kw: http)
    return calls


def test_manifest_valid(app: Flask) -> None:
    registry = app.config["PLUGIN_REGISTRY"]
    assert registry.get(PLUGIN) is not None
    assert not [e for e in registry.errors if e.plugin_id == PLUGIN]
    assert "print_blueprint_admin.image" in app.config["RENDER_SAFE_ENDPOINTS"]


def test_not_listed_as_an_admin_page(app: Flask, client: FlaskClient) -> None:
    """The compare page is a tool, not settings: no /plugins/<id>/ index, so the
    Widgets menu doesn't list it as an admin page. /compare still answers."""
    assert PLUGIN not in app.config["PLUGIN_ADMIN_IDS"]
    assert client.get(f"/plugins/{PLUGIN}/").status_code == 404
    assert client.get(f"/plugins/{PLUGIN}/compare").status_code != 404


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_renders_without_home_assistant(client: FlaskClient, size: str) -> None:
    resp = client.get(f"/_test/render?plugin={PLUGIN}&size={size}")
    assert resp.status_code == 200
    assert f'data-plugin="{PLUGIN}"' in resp.get_data(as_text=True)


@pytest.mark.parametrize("size", ["sm", "md", "lg"])
def test_renders_a_printer(client: FlaskClient, printer: list[str], size: str) -> None:
    resp = client.get(f"/_test/render?plugin={PLUGIN}&size={size}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert f'data-plugin="{PLUGIN}"' in body
    assert f"/plugins/{PLUGIN}/image/" in body


def test_fetch_and_sheet_route(app: Flask, client: FlaskClient, mod: ModuleType, printer: list[str]) -> None:
    data_dir = app.config["PLUGIN_REGISTRY"].get(PLUGIN).data_dir
    with app.test_request_context():
        data = mod.fetch({}, {}, ctx={"data_dir": str(data_dir), "cell_w": 600, "cell_h": 800,
                                      "preview": True, "settled": False})
    assert data["image"].startswith(f"/plugins/{PLUGIN}/image/")
    assert not any(p.startswith("/api/bambu_lab/file_cache/") for p in printer)  # a preview never downloads
    resp = client.get(data["image"])
    assert resp.status_code == 200 and resp.mimetype == "image/png"
    assert "immutable" in resp.headers["Cache-Control"]
    assert Image.open(io.BytesIO(resp.data)).size[0] > 0
    resp.close()  # send_file holds the sheet open until the response is closed
    assert client.get(f"/plugins/{PLUGIN}/image/..%2Fsettings.json").status_code == 404
    assert client.get(f"/plugins/{PLUGIN}/image/index.json").status_code == 404


def test_choices_lists_the_printer(app: Flask, mod: ModuleType, printer: list[str]) -> None:
    with app.test_request_context():
        assert mod.choices("printer") == [{"value": PREFIX, "label": "Shop X1C"}]


def test_compare_page(client: FlaskClient, printer: list[str]) -> None:
    resp = client.get(f"/plugins/{PLUGIN}/compare?printer={PREFIX}&w=240&h=320")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert page.count("<img") == 1  # line art only: no sliced file, no AI picture yet
    assert "AI picture not made yet" in page and "No sliced file" in page


def test_ships_its_files() -> None:
    here = Path(__file__).resolve().parents[1]
    for name in ("client.js", "client.css", "strings/en.json", "fonts/osifont-lgpl3fe.ttf",
                 "static/methods/toolpath.jpg", "static/methods/preview.jpg", "static/methods/ai.jpg"):
        assert (here / name).is_file(), name
