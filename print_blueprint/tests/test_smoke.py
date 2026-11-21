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
    from app.option_conditions import canonical_value, option_visible
    specs = registry.get(PLUGIN).manifest['cell_options']
    grid = next(s for s in specs if s['name'] == 'show_grid')
    for paper in ('blue','white','black'):
        assert option_visible(grid, {'background':paper}, specs)
    assert canonical_value(grid, True) == 'auto'
    assert canonical_value(grid, False) == 'off'


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


def test_ready_route_only_collects_queued_work(app: Flask, client: FlaskClient, mod: ModuleType,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from threading import Event

    data_dir = app.config["PLUGIN_REGISTRY"].get(PLUGIN).data_dir
    gate = Event()
    states = {e['entity_id']: e for e in _states()}
    job = mod.build_job(PREFIX, states,
                        lambda p: b'{"files": []}' if 'print_history' in p else _cover(), data_dir,
                        allow_download=False)
    runner = mod._runner(data_dir)

    def draw(*args):
        gate.wait(2)
        return Image.new("RGB", (64, 64), "white")

    st = runner.ensure(job, (64, 64), mod.SheetOptions(method="preview"), draw)
    monkeypatch.setattr(mod, "AI_WAIT_S", 0)
    url = f"/plugins/{PLUGIN}/ready/{st.key}"
    assert client.get(url).status_code == 204
    gate.set()
    assert runner.wait_idle()
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    image = client.get(response.json['image'])
    assert image.status_code == 200
    image.close()
    assert client.get(f"/plugins/{PLUGIN}/ready/{'0' * 40}").status_code == 204
    assert client.get(f"/plugins/{PLUGIN}/ready/not-a-key").status_code == 404
    assert "print_blueprint_admin.ready" in app.config["RENDER_SAFE_ENDPOINTS"]


@pytest.mark.parametrize('theme', [None, 'dark', 'light'])
def test_explicit_cell_look_and_method_thumbnail(app: Flask, client: FlaskClient, mod: ModuleType,
                                                monkeypatch: pytest.MonkeyPatch, theme: str | None) -> None:
    from types import SimpleNamespace
    from flask import request

    cell = SimpleNamespace(id='look-cell', plugin=PLUGIN, options={}, theme=theme, style=None,
                           font='inter' if theme else None)
    page = SimpleNamespace(cells=[cell], theme='dark', style='standard', font='inter')
    monkeypatch.setitem(app.config, 'PAGE_STORE', SimpleNamespace(get=lambda _: page))
    data_dir = app.config['PLUGIN_REGISTRY'].get(PLUGIN).data_dir
    with app.test_request_context():
        data = mod.fetch({'background':'black'}, {}, ctx={'data_dir':str(data_dir), 'preview':True,
                         'placement_id':'test-page/look-cell', 'cell_w':320, 'cell_h':420})
        if theme:
            assert data['cell_look']['font'] == 'Inter'
            path = mod.cell_look.font_path('Inter')
            assert path.endswith('.woff2')
            import sys
            theme_module = sys.modules[mod.__package__ + '.blueprint.theme']
            with theme_module.font_context(path):
                assert theme_module.font_for('PLA', 20).getname()[0] == 'Inter'
                assert theme_module.font_for('\uffff', 20).getname()[0] != 'Inter'
            expected = '#232019' if theme == 'dark' else '#f7f5f0'
            assert data['colors']['bg'] == expected
        else:
            assert data['cell_look'] is None
            assert data['colors']['bg'] == '#060a12'
        request.view_args = {'page_id':'test-page'}
        thumb = mod._example_url('preview', {'background':'black'})
        assert ('look=' in thumb) == bool(theme)
    response = client.get(data['image'])
    image = Image.open(io.BytesIO(response.data))
    assert image.getpixel((0, 0)) == tuple(bytes.fromhex(data['colors']['bg'][1:]))
    response.close()
    thumb_response = client.get(thumb)
    assert thumb_response.status_code == 200
    thumb_response.close()


def test_browser_look_route_is_free_and_validates_font_and_colours(app: Flask, client: FlaskClient,
                                                                mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.parse import urlencode
    look = {'bg':'#141310','ink':'#f1eee4','rule':'#b9b4a6','accent':'#69b7b0','font':'../../settings.json'}
    with app.test_request_context():
        assert mod.cell_look.font_path(look['font']) == ''
    monkeypatch.setattr(mod, '_own_settings', lambda: {'kie_api_key':'K'})
    monkeypatch.setattr(mod.KieClient, 'repaint', lambda *a, **k: pytest.fail('look route must never spend'))
    url = '/plugins/print_blueprint/look?' + urlencode({'look':json.dumps(look),'preview':1,'w':320,'h':420})
    data = client.get(url).json
    assert data['cell_look']['bg'] == '#141310' and data['image']
    assert client.get('/plugins/print_blueprint/look?look=%7B%22bg%22%3A%22bad%22%7D').status_code == 400


def test_real_sheet_worker_keeps_the_cell_palette(app: Flask, client: FlaskClient, mod: ModuleType,
                                                 printer: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    cell = SimpleNamespace(id='cell', plugin=PLUGIN, options={}, theme='dark', style=None, font='inter')
    page = SimpleNamespace(cells=[cell], theme='light', style='standard', font=None)
    monkeypatch.setitem(app.config, 'PAGE_STORE', SimpleNamespace(get=lambda _: page))
    data_dir = app.config['PLUGIN_REGISTRY'].get(PLUGIN).data_dir
    ctx = {'data_dir':str(data_dir),'cell_w':320,'cell_h':420,'preview':False,
           'settled':True,'placement_id':'page/cell'}
    with app.test_request_context():
        mod.fetch({'method':'preview'}, {}, ctx=ctx)
    assert mod._runner(data_dir).wait_idle()
    with app.test_request_context():
        data = mod.fetch({'method':'preview'}, {}, ctx=ctx)
    response = client.get(data['image'])
    assert Image.open(io.BytesIO(response.data)).getpixel((0,0)) == (35,32,25)
    assert 'live_pending' not in data
    response.close()


@pytest.mark.parametrize('mode', ['grey16', 'grey4', 'bw'])
def test_white_grey_sheet_stays_flat_through_e1004_packing(app: Flask, mod: ModuleType,
                                                         tmp_path: Path, mode: str) -> None:
    import numpy as np
    from app.state.page_store import Panel

    with app.app_context():
        data = mod.fetch({'method': 'preview', 'background': 'white', 'panel': mode}, {},
                         ctx={'data_dir': str(tmp_path), 'preview': True,
                              'cell_w': 480, 'cell_h': 640,
                              'target_device_id': 'seeed_reterminal_e1004'})
    assert data['look_panel'] == 'bw'
    images = list((tmp_path / 'examples').glob('*.png'))
    assert len(images) == 1
    renderer = app.config['RENDERER_REGISTRY'].get('esp32_bin')
    packed = renderer.transform(images[0].read_bytes(), panel=Panel(w=480, h=640, gamut='spectra_6'), settings={})
    b = np.frombuffer(packed, dtype=np.uint8)
    pixels = np.stack((b >> 4, b & 15), axis=1).reshape(640, 480)
    assert set(np.unique(pixels)) <= {0, 1}
    assert (pixels[:6, :] == 1).all()


def test_prusalink_source_options(app: Flask) -> None:
    from app.option_conditions import option_visible
    manifest = app.config['PLUGIN_REGISTRY'].get(PLUGIN).manifest
    specs = manifest['cell_options']
    source = next(s for s in specs if s['name'] == 'source')
    assert source['default'] == 'home_assistant'
    assert any(c['value'] == 'prusalink' for c in source['choices'])
    printer_option = next(s for s in specs if s['name'] == 'printer')
    assert option_visible(printer_option, {}, specs)
    assert not option_visible(printer_option, {'source': 'prusalink'}, specs)
    secrets = {s['name']: s.get('secret') for s in manifest['settings']}
    assert secrets['prusalink_password'] and secrets['prusalink_api_key']


def test_prusalink_example_needs_no_ha(app: Flask, mod: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, '_core', lambda: (_ for _ in ()).throw(AssertionError('PrusaLink cannot require HA')))
    data_dir = app.config['PLUGIN_REGISTRY'].get(PLUGIN).data_dir
    with app.test_request_context():
        data = mod.fetch({'source': 'prusalink'}, {}, ctx={'data_dir': str(data_dir), 'preview': True,
                                                       'cell_w': 240, 'cell_h': 320})
    assert data['image'].startswith('/plugins/print_blueprint/image/')
