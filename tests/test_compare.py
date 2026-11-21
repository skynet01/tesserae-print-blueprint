"""Compare routes use each source's real job builder and never buy AI pictures."""

import html
import sys
import time
import urllib.error

import pytest
from flask import Flask
from PIL import Image

from test_octoprint import SETTINGS, api as api, source
from test_prusalink import printer as printer
from test_server import FIX, p1s_states, server as server, wire


# A stand-in for Tesserae's layout: the base page and the macros compare.html uses, with
# the real macros' markup for selects, number boxes and buttons.
SHELL = {
    "_base.html": "<!doctype html><html><body class=\"admin\"><main>{% block main %}{% endblock %}</main></body></html>",
    "_components.html": """
{%- macro select_field(field_id, name, label, value='', options=[]) -%}
<div class="field"><label for="{{ field_id }}">{{ label }}</label><select id="{{ field_id }}" name="{{ name }}">
{%- for o in options %}<option value="{{ o.value }}" {% if o.value == value %}selected{% endif %}>{{ o.label }}</option>{% endfor -%}
</select></div>
{%- endmacro -%}
{%- macro number_input(field_id, name, label, value=0, min=None, max=None, unit='') -%}
<div class="field"><label for="{{ field_id }}">{{ label }}</label><input type="number" id="{{ field_id }}" name="{{ name }}" value="{{ value }}"></div>
{%- endmacro -%}
{%- macro button(label, variant='primary', type='submit') -%}<button type="{{ type }}" class="{{ variant }}"><span>{{ label }}</span></button>{%- endmacro -%}
{%- macro card_head(title, icon_name=None, level=2) -%}<header class="card-head"><h{{ level }}>{{ title }}</h{{ level }}></header>{%- endmacro -%}
""",
}


def make_app(server, shell=None):
    from jinja2 import DictLoader

    app = Flask(__name__)
    if shell is not None:
        app.jinja_loader = DictLoader(shell)
    app.register_blueprint(server.blueprint(), url_prefix="/plugins/print_blueprint")
    return app


@pytest.fixture(params=["shell", "standalone"])
def layout(request):
    return request.param


@pytest.fixture
def compare(server, monkeypatch, tmp_path, layout):
    monkeypatch.setattr(server, "_core", lambda: None)
    monkeypatch.setattr(server, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "_own_settings", lambda: {})
    monkeypatch.setattr(server, "KieClient", lambda *a, **k: pytest.fail("compare must never buy AI"))
    app = make_app(server, SHELL if layout == "shell" else None)

    def get(source="home_assistant"):
        return app.test_client().get(
            f"/plugins/print_blueprint/compare?source={source}&w=200&h=260"
        )

    return get


def octoprint(server, monkeypatch, thumbnail=False):
    module = sys.modules[server.build_job_octoprint.__module__]
    module._TELEMETRY.clear()
    module._SLICES.clear()
    original = source.Client.get

    def get(self, path, **kwargs):
        if thumbnail and "downloads/files/" in path:
            return (FIX / "octoprint/block-thumbnail.gcode").read_bytes()
        return original(self, path, **kwargs)

    monkeypatch.setattr(module.Client, "get", get)
    monkeypatch.setattr(server, "_own_settings", lambda: SETTINGS)


def test_home_assistant_compare_route(compare, server, monkeypatch):
    wire(server, monkeypatch, p1s_states())
    response = compare()
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert page.count("<img") == 2
    assert 'name="source"' in page
    assert 'name="printer"' in page
    assert 'value="home_assistant" selected' in page
    assert 'value="octoprint"' not in page and 'value="prusalink"' not in page


@pytest.mark.parametrize("thumbnail", [False, True])
def test_octoprint_compare_route(compare, server, monkeypatch, api, thumbnail):
    octoprint(server, monkeypatch, thumbnail)
    response = compare("octoprint")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "block." in page and page.count("<img") == (2 if thumbnail else 1)
    assert 'name="printer"' not in page
    assert 'value="home_assistant"' not in page
    assert 'value="octoprint" selected' in page
    assert any("downloads/files/" in path for path in api) or thumbnail
    if not thumbnail:
        assert page.count("No preview picture") == 2
        assert "Drawing failed" not in page


@pytest.mark.parametrize("binary", [False, True])
def test_prusalink_compare_route(compare, server, monkeypatch, printer, binary):
    settings, counts = printer(binary=binary)
    monkeypatch.setattr(server, "_own_settings", lambda: settings)
    response = compare("prusalink")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Cube." in page and page.count("<img") == 2
    assert 'name="printer"' not in page
    assert 'value="prusalink" selected' in page
    assert any(path.startswith("/usb/") for path in counts)


@pytest.mark.parametrize("selected", ["home_assistant", "octoprint", "prusalink"])
def test_compare_only_reads_existing_ai(compare, server, monkeypatch, tmp_path, api, printer, selected):
    if selected == "home_assistant":
        wire(server, monkeypatch, p1s_states())
    elif selected == "octoprint":
        octoprint(server, monkeypatch, thumbnail=True)
    else:
        settings, _ = printer()
        monkeypatch.setattr(server, "_own_settings", lambda: settings)
    page = compare(selected).get_data(as_text=True)
    assert page.count("<img") == 2
    assert "AI picture not made yet" in page
    # The job builders persist a stable identity; capture it from the actual draw call.
    jobs = []
    draw = server.draw
    monkeypatch.setattr(server, "draw", lambda job, *a, **kw: jobs.append(job) or draw(job, *a, **kw))
    compare(selected)
    (tmp_path / "ai").mkdir()
    Image.new("RGB", (64, 64), "white").save(tmp_path / "ai" / f"{jobs[0].job_key}.png")
    assert compare(selected).get_data(as_text=True).count("<img") == 3


@pytest.mark.parametrize("selected", ["octoprint", "prusalink"])
def test_compare_missing_configuration(compare, selected):
    response = compare(selected)
    assert response.status_code == 200
    assert "widget settings" in response.get_data(as_text=True)


def test_compare_source_picker_survives_ha_failure(compare, server, monkeypatch, api):
    octoprint(server, monkeypatch)
    wire(server, monkeypatch, {}, fail=urllib.error.URLError("offline"))
    page = compare("octoprint").get_data(as_text=True)
    assert 'value="home_assistant"' not in page
    assert 'value="octoprint" selected' in page
    assert page.count("<img") == 1


def test_compare_lists_all_configured_sources(compare, server, monkeypatch):
    wire(server, monkeypatch, p1s_states())
    monkeypatch.setattr(server, "_own_settings", lambda: {
        "octoprint_url": "http://octo.test", "prusalink_url": "http://prusa.test",
    })
    page = compare().get_data(as_text=True)
    for selected in ("home_assistant", "octoprint", "prusalink"):
        assert f'value="{selected}"' in page


@pytest.mark.parametrize("selected", ["octoprint", "prusalink"])
def test_compare_unreachable_source(compare, server, monkeypatch, selected):
    monkeypatch.setattr(server, "_own_settings", lambda: {
        selected + "_url": "http://printer.test", selected + "_api_key": "test",
    })
    builder = getattr(server, "build_job_" + selected)
    module = sys.modules[builder.__module__]
    if selected == "octoprint":
        module._TELEMETRY.clear()

    def offline(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(module.Client, "get", offline)
    page = compare(selected).get_data(as_text=True)
    assert "Couldn't reach" in html.unescape(page)
    assert "widget settings" in page and "<img" not in page


@pytest.mark.parametrize("selected", ["octoprint", "prusalink"])
def test_compare_missing_credentials(compare, server, monkeypatch, selected):
    monkeypatch.setattr(server, "_own_settings", lambda: {selected + "_url": "http://printer.test"})
    page = compare(selected).get_data(as_text=True)
    assert "settings" in page and "<img" not in page
    assert "Home Assistant" not in page


def test_prusalink_compare_without_thumbnail(compare, server, monkeypatch, printer):
    settings, _ = printer()
    monkeypatch.setattr(server, "_own_settings", lambda: settings)
    module = sys.modules[server.build_job_prusalink.__module__]
    original = module.Client.get

    def get(self, path, *args, **kwargs):
        if path.startswith("/usb/"):
            return (FIX / "octoprint/block.gcode").read_bytes()
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(module.Client, "get", get)
    page = compare("prusalink").get_data(as_text=True)
    assert page.count("<img") == 1
    assert page.count("No preview picture") == 2
    assert "Drawing failed" not in page


@pytest.mark.parametrize("selected", ["octoprint", "prusalink"])
def test_compare_no_downloadable_file(compare, server, monkeypatch, api, printer, selected):
    if selected == "octoprint":
        octoprint(server, monkeypatch)
        module = sys.modules[server.build_job_octoprint.__module__]
        original = module.Client.get

        def get(self, path, **kwargs):
            if path == "/api/files/local/block.gcode":
                return b'{"refs": {}}'
            return original(self, path, **kwargs)
    else:
        settings, _ = printer(thumbnail_only=True)
        monkeypatch.setattr(server, "_own_settings", lambda: settings)
        module = sys.modules[server.build_job_prusalink.__module__]
        get = module.Client.get
    monkeypatch.setattr(module.Client, "get", get)
    page = compare(selected).get_data(as_text=True)
    assert "No sliced file available" in page
    assert "Drawing failed" not in page
    assert page.count("<img") == (0 if selected == "octoprint" else 1)


@pytest.mark.parametrize("selected", ["octoprint", "prusalink"])
def test_compare_file_download_budget(compare, server, monkeypatch, api, printer, selected):
    if selected == "octoprint":
        octoprint(server, monkeypatch)
    else:
        settings, _ = printer()
        monkeypatch.setattr(server, "_own_settings", lambda: settings)
    module = sys.modules[getattr(server, "build_job_" + selected).__module__]
    original = module.Client.get
    remaining = []

    def get(self, path, *args, **kwargs):
        if "/downloads/" in path or path.startswith("/usb/"):
            remaining.append(self.deadline - time.monotonic())
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(module.Client, "get", get)
    assert compare(selected).status_code == 200
    assert remaining and all(0 < budget <= 10 for budget in remaining)


@pytest.mark.parametrize("state,status,reason", [
    (None, 503, "Install the Home Assistant Core"),
    ({}, 404, "No Bambu Lab printer"),
])
def test_compare_keeps_ha_errors(compare, server, monkeypatch, state, status, reason):
    if state is not None:
        wire(server, monkeypatch, state)
    response = compare()
    assert response.status_code == status
    assert reason in response.get_data(as_text=True)


def test_compare_rejects_unknown_source(compare):
    assert compare("unknown").status_code == 400


def _shell_page(server, monkeypatch, tmp_path, shell):
    monkeypatch.setattr(server, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "_own_settings", lambda: {})
    monkeypatch.setattr(server, "KieClient", lambda *a, **k: pytest.fail("compare must never buy AI"))
    wire(server, monkeypatch, p1s_states())
    return make_app(server, shell).test_client().get("/plugins/print_blueprint/compare?w=200&h=260")


def test_compare_in_tesserae_layout(server, monkeypatch, tmp_path):
    response = _shell_page(server, monkeypatch, tmp_path, SHELL)
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert '<body class="admin">' in page and "<!doctype html><html><body" in page
    assert 'class="page-head"' in page and "ph-printer" in page and "Compare picture methods" in page
    form = page[page.index("<form"):page.index("</form>")]
    for name in ("source", "printer", "background", "panel", "w", "h"):
        assert f'name="{name}"' in form
    # Show is the form's only button, primary, after every control.
    assert form.count("<button") == 1 and form.rindex('class="primary"') > form.rindex("<input")
    assert ">Blueprint</option>" in form and ">Spectra 6</option>" in form
    cards = page.split('class="card pb-compare-card"')[1:]
    assert [c.split('data-method="')[1].split('"')[0] for c in cards] == ["toolpath", "preview", "ai"]
    for card, name in zip(cards, ("Toolpaths", "Line art from the preview", "AI engraving")):
        assert f"<h2>{name}</h2>" in card
    assert "<img" in cards[0] and "<img" in cards[1]
    assert "AI picture not made yet" in cards[2] and "<img" not in cards[2]


@pytest.mark.parametrize("missing", ["_base.html", "_components.html", "number_input"])
def test_compare_falls_back_on_an_older_tesserae(server, monkeypatch, tmp_path, missing):
    shell = dict(SHELL)
    if missing == "number_input":
        shell["_components.html"] = shell["_components.html"].replace("macro number_input", "macro old_number")
    else:
        del shell[missing]
    response = _shell_page(server, monkeypatch, tmp_path, shell)
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert page.startswith("<!doctype html>") and "<title>Print Blueprint: compare picture methods" in page
    assert '<body class="admin">' not in page
    assert page.count("<img") == 2 and "AI picture not made yet" in page
