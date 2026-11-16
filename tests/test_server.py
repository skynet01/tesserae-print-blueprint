"""The widget shell (server.py) against a fake ha_core and a fake Home Assistant.

server.py is loaded the way the Tesserae host loads it (by path, as
`_tesserae_plugins.print_blueprint.server`), so its relative imports of the renderer work as deployed."""
import copy
import importlib.util
import json
import sys
import time
import urllib.error
from pathlib import Path

import pytest
from PIL import Image

import corpus

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "print_blueprint"
FIX = Path(__file__).parent / "fixtures"
BLOCK = FIX / "synthetic-block.3mf"
SERIAL = "DEMO_P1S_001"
SIZE = (480, 640)


@pytest.fixture
def server():
    name = "_tesserae_plugins.print_blueprint.server"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    yield mod
    for r in list(mod._RUNNERS.values()):
        r.wait_idle(30)


class FakeCore:
    """The parts of Tesserae's ha_core the widget uses."""

    def __init__(self, states: dict, fail: Exception | None = None):
        self.states, self.fail = states, fail

    def get_states(self):
        if self.fail:
            raise self.fail
        return [dict(v, entity_id=k) for k, v in self.states.items()]

    def base_url(self):
        return "http://ha.test:8123"

    def _headers(self):
        return {"Authorization": "Bearer test"}

    def coerce_error(self, err):
        if isinstance(err, urllib.error.URLError):
            return f"Home Assistant unreachable: {err.reason}"
        return f"{type(err).__name__}: {err}"


def p1s_states(status="running"):
    s = copy.deepcopy(corpus.load_states("p1s_states.json"))
    s["sensor.p1s_printer_task_name"]["state"] = "Synthetic Block"
    s["sensor.p1s_printer_gcode_filename"]["state"] = "synthetic-block.3mf"
    s["sensor.p1s_printer_print_status"]["state"] = status
    return s


def wire(server, monkeypatch, states, *, with_3mf=True, fail=None):
    """Point the server at a fake ha_core + Home Assistant; returns the fake core."""
    core = FakeCore(states, fail)
    files = {BLOCK.name: BLOCK.read_bytes()} if with_3mf else {}
    http = corpus.fake_ha(files, model="P1S", serial=SERIAL, cover=corpus.SYNTHETIC_COVER.read_bytes())
    monkeypatch.setattr(server, "_core", lambda: core)
    monkeypatch.setattr(server, "_http_for", lambda core, **kw: http)
    return core


def ctx(tmp_path, **kw):
    c = {"data_dir": str(tmp_path), "cell_w": SIZE[0], "cell_h": SIZE[1], "preview": False, "settled": True}
    c.update(kw)
    return c


def image_file(server, tmp_path, data) -> Path:
    url = data["image"]
    assert url.startswith("/plugins/print_blueprint/image/")
    name = url.rsplit("/", 1)[1].split("?", 1)[0]
    path = server.sheet_path(tmp_path, name)
    assert path is not None and path.is_file()
    return path


# ---------------------------------------------------------------- manifest
def test_manifest_valid():
    m = json.loads((PLUGIN / "plugin.json").read_text())
    assert m["kind"] == "widget" and m["name"] and m["version"]
    opts = {o["name"]: o for o in m["cell_options"]}
    for name in ("printer", "method", "show_progress", "background", "panel", "show_icons", "callout_notes",
                 "fill_space", "show_grid", "show_subtitle", "idle"):
        assert name in opts, name
        assert opts[name]["label"]
    assert opts["printer"]["type"] == "select" and opts["printer"]["choices_from"] == "printer"
    assert [c["value"] for c in opts["method"]["choices"]] == ["toolpath", "preview", "ai"]
    assert "/plugins/print_blueprint/compare" in opts["method"]["help"]
    assert [c["value"] for c in opts["panel"]["choices"]] == ["auto", "colour", "grey16", "grey4", "spectra", "bw"]
    assert opts["panel"]["default"] == "auto"
    assert [c["value"] for c in opts["idle"]["choices"]] == ["last", "empty"]
    assert opts["show_progress"]["type"] == "boolean" and opts["show_progress"]["default"] is True
    settings = {s["name"]: s for s in m["settings"]}
    assert settings["kie_api_key"]["secret"] is True
    assert settings["ai_daily_cap"]["type"] == "number" and settings["ai_daily_cap"]["default"] == 12
    r = m["render"]
    assert r["full_bleed"] is True and r["dither"] == "none" and r["per_device_id"] is True
    assert m["design"]["palette"] == "extended"
    for c in opts["method"]["choices"]:
        assert c["image"].startswith("/plugins/print_blueprint/example/")
    strings = json.loads((PLUGIN / "strings" / "en.json").read_text())
    assert strings["no_print"]


def test_no_hard_coded_printers():
    for p in [PLUGIN / "server.py", PLUGIN / "client.js", PLUGIN / "plugin.json"]:
        text = p.read_text().lower()
        for word in ("p1s_printer", "h3c_printer", SERIAL.lower()):
            assert word not in text, (p.name, word)


# ---------------------------------------------------------------- fetch
def test_fetch_running_returns_image_url(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    first = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert "error" not in first and "empty" not in first
    assert first["live_pending"] >= 1 and 2 <= first["live_refresh_s"] <= 120
    quick = image_file(server, tmp_path, first)
    assert Image.open(quick).size[0] <= SIZE[0]
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    second = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert "live_pending" not in second and "live_refresh_s" not in second
    sheet = image_file(server, tmp_path, second)
    assert sheet != quick
    assert Image.open(sheet).size == SIZE
    assert (second["w"], second["h"]) == SIZE
    assert (tmp_path / "3mf").is_dir()  # the runner downloaded the sliced file, fetch did not


def test_fetch_itself_never_downloads(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    monkeypatch.setattr(server.Runner, "ensure", lambda self, *a, **k: server.RenderState(None, True, ""))
    server.fetch({}, {}, ctx=ctx(tmp_path))
    assert not (tmp_path / "3mf").exists()


def test_unsettled_renders_draw_the_sheet_but_never_spend_credits(server, monkeypatch, tmp_path):
    """Unsettled screen work queues free drawing; an AI preview stays on its example."""
    wire(server, monkeypatch, p1s_states())
    seen = []

    def ensure(self, job, size, opts, render_fn, **kw):
        seen.append(kw["kie_key"])
        return server.RenderState(None, True, "")
    monkeypatch.setattr(server.Runner, "ensure", ensure)
    for c in (ctx(tmp_path, preview=True, settled=False), ctx(tmp_path, preview=False, settled=False)):
        data = server.fetch({"method": "ai"}, {"kie_api_key": "k"}, ctx=c)
        image_file(server, tmp_path, data)  # still something to show: the inline line-art sheet
    assert seen == [""]


def test_background_change_gets_its_own_sheet_from_a_preview_render(server, monkeypatch, tmp_path):
    """Regression: after blue -> white only previews rendered, and nothing ever drew the white sheet."""
    wire(server, monkeypatch, p1s_states())
    pc = ctx(tmp_path, preview=True, settled=False)
    server.fetch({"background": "blue", "editor_preview": "live"}, {}, ctx=pc)
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    blue = image_file(server, tmp_path, server.fetch({"background": "blue", "editor_preview": "live"}, {}, ctx=pc))
    first = server.fetch({"background": "white", "editor_preview": "live"}, {}, ctx=pc)
    assert image_file(server, tmp_path, first) != blue  # never the blue one standing in
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    white = image_file(server, tmp_path, server.fetch({"background": "white", "editor_preview": "live"}, {}, ctx=pc))
    assert white != blue and not white.name.endswith("-q.png")


def test_settled_previews_draw_but_never_spend_credits(server, monkeypatch, tmp_path):
    """A settled AI preview stays offline; a real screen may ask the journaled runner for AI."""
    wire(server, monkeypatch, p1s_states())
    seen = []

    def ensure(self, job, size, opts, render_fn, **kw):
        seen.append(kw["kie_key"])
        return server.RenderState(None, True, "AI picture needs a kie.ai API key")
    monkeypatch.setattr(server.Runner, "ensure", ensure)
    data = server.fetch({"method": "ai"}, {"kie_api_key": "k"}, ctx=ctx(tmp_path, preview=True, settled=True))
    assert seen == [] and data["example"] is True
    server.fetch({"method": "ai"}, {"kie_api_key": "k"}, ctx=ctx(tmp_path, preview=False, settled=True))
    assert seen == ["k"]


def test_settled_preview_renders_toolpaths(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    server.fetch({"editor_preview": "live"}, {}, ctx=ctx(tmp_path, preview=True, settled=True))
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    data = server.fetch({"editor_preview": "live"}, {}, ctx=ctx(tmp_path, preview=True, settled=True))
    assert not data.get("example") and "live_pending" not in data
    assert Image.open(image_file(server, tmp_path, data)).size == SIZE


def test_little_budget_left_skips_the_inline_sheet(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    monkeypatch.setattr(server, "QUICK_MIN_S", server.FETCH_BUDGET_S + 1)
    monkeypatch.setattr(server, "_quick", lambda *a: pytest.fail("no inline render without budget"))
    data = server.fetch({}, {}, ctx=ctx(tmp_path, settled=False, preview=False))
    assert data["empty"]["kind"] == "drawing" and data["live_refresh_s"] and "image" not in data


def test_inline_sheet_is_capped(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    data = server.fetch({}, {}, ctx=ctx(tmp_path, settled=False, cell_w=3840, cell_h=2160))
    assert max(Image.open(image_file(server, tmp_path, data)).size) <= 1000
    assert (data["w"], data["h"]) == (3840, 2160)


def test_http_respects_the_deadline(server):
    http = server.HaHttp("http://ha.invalid", {}, None, 2.0, deadline=time.monotonic() - 1)
    with pytest.raises(TimeoutError):
        http("/api/bambu_lab/print_history?serial=x")


def test_http_for_survives_a_broken_ha_core(server, monkeypatch, tmp_path):
    core = wire(server, monkeypatch, p1s_states())
    monkeypatch.undo()
    monkeypatch.setattr(server, "_core", lambda: core)
    core.base_url = lambda: (_ for _ in ()).throw(RuntimeError("token unreadable"))
    with pytest.raises(OSError):
        server._http_for(core, timeout=1)("/x")
    data = server.fetch({}, {}, ctx=ctx(tmp_path, settled=False))
    assert "error" not in data  # states still came through; the sheet just has no cover or 3MF


def test_url_of_a_pruned_sheet_is_none(server, tmp_path):
    assert server._url(tmp_path / ("a" * 40 + ".png")) is None


def test_fallback_note_survives_a_restart(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states(), with_3mf=False)
    server.fetch({}, {}, ctx=ctx(tmp_path))
    server._RUNNERS[str(tmp_path)].wait_idle(60)
    server._NOTES.clear()  # a restart: the memory is gone, the sheet and notes.json are not
    data = server.fetch({}, {}, ctx=ctx(tmp_path, preview=False, settled=False))
    assert data["note"] == server.NOTE_NO_3MF


def test_fetch_idle_shows_last_print(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states("FINISH"))  # mixed case from HA
    server.fetch({}, {}, ctx=ctx(tmp_path))
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    done = server.fetch({}, {}, ctx=ctx(tmp_path))
    finished = image_file(server, tmp_path, done)
    # the printer goes offline: every entity unavailable, nothing to draw from Home Assistant
    off = {k: dict(v, state="unavailable") for k, v in p1s_states().items()}
    wire(server, monkeypatch, off)
    data = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert image_file(server, tmp_path, data) == finished


def test_fetch_idle_empty_option(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states("finish"))
    data = server.fetch({"idle": "empty"}, {}, ctx=ctx(tmp_path))
    assert data["empty"]["title"] == "No print running"
    assert "image" not in data


def test_fetch_idle_with_nothing_is_an_empty_state(server, monkeypatch, tmp_path):
    off = {k: dict(v, state="unavailable") for k, v in p1s_states().items()}
    off["image.p1s_printer_cover_image"]["attributes"] = {}
    wire(server, monkeypatch, off)
    data = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert data["empty"]["title"] and "image" not in data


def test_fetch_ha_down_returns_error_state(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, {}, fail=urllib.error.URLError("connection refused"))
    data = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert "unreachable" in data["error"]


def test_fetch_ha_down_keeps_the_last_sheet(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    good = server.fetch({}, {}, ctx=ctx(tmp_path))
    wire(server, monkeypatch, {}, fail=urllib.error.URLError("connection refused"))
    data = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert data["image"] == good["image"] and "unreachable" in data["note"]


def test_fetch_without_ha_core(server, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_core", lambda: None)
    assert "Home Assistant" in server.fetch({}, {}, ctx=ctx(tmp_path))["error"]


def test_fetch_no_printer_is_an_empty_state(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, {"sensor.kitchen_temperature": {"state": "21", "attributes": {}}})
    data = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert "Bambu" in data["empty"]["title"]


def test_fetch_unknown_printer_option(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    data = server.fetch({"printer": "nope"}, {}, ctx=ctx(tmp_path))
    assert "nope" in data["empty"]["detail"]


def test_printer_option_accepts_prefix_or_status_entity(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    for value in ("p1s_printer", "sensor.p1s_printer_print_status"):
        assert "image" in server.fetch({"printer": value}, {}, ctx=ctx(tmp_path, settled=False))


def test_settings_reach_the_runner(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    seen = {}

    def ensure(self, job, size, opts, render_fn, **kw):
        seen.update(kw, opts=opts, size=size)
        return server.RenderState(None, True, "")
    monkeypatch.setattr(server.Runner, "ensure", ensure)
    server.fetch({"method": "ai", "background": "white", "panel": "grey4", "show_grid": False}, {
        "kie_api_key_secret": "sk-1", "ai_daily_cap": "3"}, ctx=ctx(tmp_path))
    assert seen["kie_key"] == "sk-1" and seen["ai_daily_cap"] == 3
    o = seen["opts"]
    assert (o.method, o.background, o.panel, o.show_grid) == ("ai", "white", "grey4", False)
    assert seen["size"] == SIZE


def test_bad_option_values_fall_back_to_defaults(server):
    o = server.sheet_options({"method": "x", "background": "pink", "panel": "auto", "show_icons": "false"}, "colour")
    assert (o.method, o.background, o.panel, o.show_icons) == ("toolpath", "blue", "colour", False)


def test_show_progress_false_draws_the_finished_part(server, monkeypatch):
    job = corpus.job_from_3mf(BLOCK, progress=0.3)
    seen = []
    monkeypatch.setattr(server, "toolpath_art", lambda facts, box, progress, u=None, walls=None: seen.append(progress) or
                        server.preview_art(corpus.SYNTHETIC_COVER.read_bytes(), box))
    server.draw(job, SIZE, server.sheet_options({"show_progress": False}, "colour"))
    server.draw(job, SIZE, server.sheet_options({}, "colour"))
    assert seen and seen[0] == 1.0 and seen[-1] == pytest.approx(0.3)


def test_toolpath_without_3mf_notes_the_fallback(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states(), with_3mf=False)
    server.fetch({}, {}, ctx=ctx(tmp_path))
    server._RUNNERS[str(tmp_path)].wait_idle(60)
    data = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert "preview" in data["note"].lower()


# ---------------------------------------------------------------- panel
@pytest.mark.parametrize("gamut,panel", [("spectra_6", "spectra"), ("waveshare_e6", "spectra"),
                                         ("gray_16", "grey16"), ("grey16", "grey16"), ("gray_4", "grey4"),
                                         ("mono", "bw"), ("bw", "bw"), ("rgb24", "colour"), ("", "colour"),
                                         ("something_new", "colour")])
def test_panel_for_gamut(server, gamut, panel):
    assert server.panel_for_gamut(gamut) == panel


def test_auto_panel_follows_the_target_device(server, monkeypatch):
    monkeypatch.setattr(server, "_device_gamut", lambda dev: {"door": "spectra_6", "frame": "gray_16"}.get(dev, ""))
    assert server.resolve_panel("auto", {"target_device_id": "door"}) == "spectra"
    assert server.resolve_panel("auto", {"target_device_id": "frame"}) == "grey16"
    assert server.resolve_panel("auto", {}) == "colour"
    assert server.resolve_panel("bw", {"target_device_id": "door"}) == "bw"


# ---------------------------------------------------------------- choices, routes
def test_choices_lists_every_printer(server, monkeypatch):
    s = p1s_states()
    s["sensor.a1_mini_print_status"] = {"state": "idle", "attributes": {"friendly_name": "A1 mini Print status"}}
    wire(server, monkeypatch, s)
    got = server.choices("printer")
    assert {c["value"] for c in got} == {"p1s_printer", "a1_mini"}
    assert server.choices("other") == []


def test_choices_without_printers_says_why(server, monkeypatch):
    wire(server, monkeypatch, {})
    (row,) = server.choices("printer")
    assert row["value"] == "" and row["note"] is True


def test_sheet_path_only_serves_sheet_names(server, tmp_path):
    (tmp_path / "sheets").mkdir()
    name = "a" * 40 + ".png"
    (tmp_path / "sheets" / name).write_bytes(b"x")
    (tmp_path / "secret.png").write_bytes(b"x")
    assert server.sheet_path(tmp_path, name) == tmp_path / "sheets" / name
    for bad in ("../secret.png", "index.json", "A" * 40 + ".png", "a" * 40 + ".png/..", "b" * 40 + ".png"):
        assert server.sheet_path(tmp_path, bad) is None


def test_compare_page_never_spends_credits(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    monkeypatch.setattr(server, "KieClient", lambda *a, **k: pytest.fail("compare must not call kie.ai"))
    html = server.compare_html(server._core(), tmp_path, "p1s_printer", 300, 400)
    assert html.count("<img") == 2  # toolpaths and line art; no AI picture cached
    assert "AI picture not made yet" in html
    (tmp_path / "ai").mkdir()
    job = server.current_job(server._core(), "p1s_printer", tmp_path)
    Image.new("RGB", (64, 64), "white").save(tmp_path / "ai" / f"{job.job_key}.png")
    assert server.compare_html(server._core(), tmp_path, "p1s_printer", 300, 400).count("<img") == 3


def test_ai_without_key_notes_the_fallback(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    data = server.fetch({"method": "ai"}, {}, ctx=ctx(tmp_path, preview=False, settled=False))
    assert "kie.ai" in data["note"] and "image" in data
    assert server.fetch({"method": "ai"}, {"kie_api_key": "k"}, ctx=ctx(tmp_path, preview=True))["example"] is True


def test_compare_downloads_with_a_short_timeout(server, monkeypatch, tmp_path):
    core = wire(server, monkeypatch, p1s_states())
    seen = []
    real = server._http_for
    monkeypatch.setattr(server, "_http_for", lambda c, **kw: seen.append(kw) or real(c, **kw))
    monkeypatch.setattr(server, "HaHttp", lambda *a, **k: corpus.fake_ha({}, serial=SERIAL))
    server.compare_html(core, tmp_path, "p1s_printer", 200, 260)
    assert seen and all(kw["timeout"] <= 10 for kw in seen)


def test_compare_says_why_the_cover_is_missing(server, monkeypatch, tmp_path):
    s = p1s_states()
    s["image.p1s_printer_cover_image"]["state"] = "unavailable"
    core = wire(server, monkeypatch, s, with_3mf=False)
    html = server.compare_html(core, tmp_path, "p1s_printer", 200, 260)
    assert "image.p1s_printer_cover_image" in html and "unavailable" in html


# ---------------------------------------------------------------- final review
def test_two_tone_quick_sheet_is_full_size(server, monkeypatch, tmp_path):
    """A quantised sheet is never drawn small and scaled up by the client (unreadable text)."""
    wire(server, monkeypatch, p1s_states())
    for panel in ("spectra", "bw", "grey4"):
        data = server.fetch({"panel": panel}, {}, ctx=ctx(tmp_path, settled=False, cell_w=1200, cell_h=1600))
        assert Image.open(image_file(server, tmp_path, data)).size == (1200, 1600), panel


def test_two_tone_without_budget_shows_drawing_not_a_small_sheet(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    monkeypatch.setattr(server, "QUICK_PX", 100_000)  # a big cell needs more budget than is left
    data = server.fetch({"panel": "spectra"}, {}, ctx=ctx(tmp_path, settled=False, cell_w=1200, cell_h=1600))
    assert data["empty"]["kind"] == "drawing" and "image" not in data and data["live_pending"] == 1
    assert not list(tmp_path.glob("sheets/*-q.png"))
    monkeypatch.setattr(server, "QUICK_PX", 2_000_000)
    colour = server.fetch({"panel": "colour"}, {}, ctx=ctx(tmp_path, settled=False, cell_w=1200, cell_h=1600))
    assert max(Image.open(image_file(server, tmp_path, colour)).size) <= server.INLINE_MAX  # colour: small is fine


def test_next_progress_step_keeps_the_toolpath_sheet_up(server, monkeypatch, tmp_path):
    """While the next 2 % step renders, the previous toolpath sheet stays up (no line-art flash)."""
    states = p1s_states()
    wire(server, monkeypatch, states)
    server.fetch({}, {}, ctx=ctx(tmp_path))
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    sheet = image_file(server, tmp_path, server.fetch({}, {}, ctx=ctx(tmp_path)))
    states["sensor.p1s_printer_current_layer"]["state"] = str(int(states["sensor.p1s_printer_current_layer"]["state"]) + 40)
    gate = __import__("threading").Event()
    real = server.draw
    monkeypatch.setattr(server, "draw", lambda *a, **k: gate.wait(30) and real(*a, **k))
    data = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert image_file(server, tmp_path, data) == sheet and data["live_pending"] == 1
    editor = server.fetch({}, {}, ctx=ctx(tmp_path, settled=False, preview=True))
    assert editor["example"] is True
    assert image_file(server, tmp_path, editor) != sheet
    gate.set()
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    assert image_file(server, tmp_path, server.fetch({}, {}, ctx=ctx(tmp_path))) != sheet


def test_walls_parsed_once_per_print(server, monkeypatch, tmp_path):
    art_mod = sys.modules["_tesserae_plugins.print_blueprint.blueprint.art"]
    calls = []
    real = art_mod.parse_walls
    monkeypatch.setattr(art_mod, "parse_walls", lambda *a: calls.append(1) or real(*a))
    art_mod._WALLS.clear()
    wire(server, monkeypatch, p1s_states())
    for size in ((480, 640), (600, 400), (640, 480)):
        server.fetch({}, {}, ctx=ctx(tmp_path, cell_w=size[0], cell_h=size[1]))
        assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    assert len(calls) == 1


def test_fallback_note_is_drawn_into_the_sheet(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states(), with_3mf=False)
    server.fetch({}, {}, ctx=ctx(tmp_path, cell_w=1200, cell_h=1600))
    server._RUNNERS[str(tmp_path)].wait_idle(60)
    data = server.fetch({}, {}, ctx=ctx(tmp_path, cell_w=1200, cell_h=1600))
    with Image.open(image_file(server, tmp_path, data)) as im:
        assert im.text["note"] == server.NOTE_NO_3MF
    assert data["note"] == server.NOTE_NO_3MF and data["printer"] and data["title"]
    js, css = (PLUGIN / "client.js").read_text(), (PLUGIN / "client.css").read_text()
    assert 'class="note"' not in js and ".note" not in css  # no overlay on top of the sheet


def test_note_slot_respects_the_layout(server):
    rs = sys.modules["_tesserae_plugins.print_blueprint.blueprint.render"]
    rp = sys.modules["_tesserae_plugins.print_blueprint.blueprint.report"]
    job = corpus.job_from_3mf(BLOCK, progress=0.4)
    for size in ((1200, 1600), (480, 800), (600, 400), (2560, 1440), (1000, 1000)):
        img, rep = rs.render_sheet(job, lambda b: server.preview_art(job.slice.preview_png, b), size,
                                   server.sheet_options({"method": "preview"}, "colour"), note=server.NOTE_NO_3MF)
        assert rp.check_layout(rep) == [], size
        assert "note" in rep.block_groups or "note" in rep.dropped, size


# ---------------------------------------------------------------- refresh button cycle
def _fake_button_actions(monkeypatch):
    import types
    mod = types.ModuleType("app.button_actions")
    mod.registry = {}

    class ActionResult:
        def __init__(self, new_step_index, target_page_id, force_refresh, description, force_download=False):
            self.new_step_index, self.target_page_id = new_step_index, target_page_id
            self.force_refresh, self.description = force_refresh, description

    mod.ActionResult = ActionResult
    mod.register = lambda name, fn: mod.registry.__setitem__(name, fn)
    monkeypatch.setitem(sys.modules, "app", types.ModuleType("app"))
    monkeypatch.setitem(sys.modules, "app.button_actions", mod)
    return mod


class Press:
    def __init__(self, device_id):
        self.device_id, self.current_step_index = device_id, 0


def test_missing_button_actions_does_not_break_import(server, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.button_actions", None)  # import raises ImportError
    server._register_action()


def test_cycle_action_registers_and_advances_per_device(server, monkeypatch, tmp_path):
    mod = _fake_button_actions(monkeypatch)
    server._register_action()
    act = mod.registry["print_blueprint_cycle"]
    server._DATA_DIRS[:] = [tmp_path]
    seen = []
    for _ in range(4):
        r = act(Press("d1"), None)
        assert r.force_refresh
        seen.append(json.loads((tmp_path / server.CYCLE_FILE).read_text())["d1"]["pressed"])
    assert seen == ["white", "blue", "black", "white"]
    act(Press("d2"), None)
    stored = json.loads((tmp_path / server.CYCLE_FILE).read_text())
    assert stored["d2"]["pressed"] == "white" and stored["d1"]["pressed"] == seen[-1]


def test_first_press_starts_from_the_cells_background(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    c = ctx(tmp_path, settled=False, target_device_id="d1")
    server.fetch({"background": "blue"}, {}, ctx=c)
    assert server.cycle_background(tmp_path, "d1") == "black"  # blue -> black
    assert server.cycle_background(tmp_path, "d1") == "white"


def test_fetch_uses_the_pressed_background_in_a_separate_sheet(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    c = ctx(tmp_path, settled=False, target_device_id="d1")
    base = server.fetch({"background": "blue"}, {}, ctx=c)
    assert base["background"] == "blue"
    server.cycle_background(tmp_path, "d1")  # -> black
    pressed = server.fetch({"background": "blue"}, {}, ctx=c)
    assert pressed["background"] == "black" and pressed["colors"] != base["colors"]
    assert server.fetch({"background": "blue"}, {}, ctx=ctx(tmp_path, settled=False, target_device_id="d2"))[
        "background"] == "blue"  # another device is untouched
    from blueprint.jobs import cache_key
    job = server.build_job("p1s_printer", server._states(FakeCore(p1s_states()).get_states(), "p1s_printer"),
                           lambda p: b"", tmp_path, allow_download=False)
    k = lambda bg: cache_key(job, SIZE, server.sheet_options({"background": bg}, "colour"))[:24]  # noqa: E731
    assert k("black") != k("blue")


def test_option_off_ignores_the_pressed_background(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    server.cycle_background(tmp_path, "d1")
    c = ctx(tmp_path, settled=False, target_device_id="d1")
    assert server.fetch({"background": "blue", "button_cycles_background": False}, {}, ctx=c)["background"] == "blue"
    assert server.fetch({"background": "blue"}, {}, ctx=c)["background"] == "white"



def test_option_off_makes_the_button_a_plain_refresh(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states())
    mod = _fake_button_actions(monkeypatch)
    server._register_action()
    act = mod.registry["print_blueprint_cycle"]
    c = ctx(tmp_path, settled=False, target_device_id="d1")
    server.fetch({"background": "blue", "button_cycles_background": False}, {}, ctx=c)
    r = act(Press("d1"), None)
    assert r.force_refresh and r.target_page_id is None
    assert "pressed" not in json.loads((tmp_path / server.CYCLE_FILE).read_text()).get("d1", {})
    # Back on: the next render clears the flag and the press cycles again.
    server.fetch({"background": "blue"}, {}, ctx=c)
    act(Press("d1"), None)
    assert json.loads((tmp_path / server.CYCLE_FILE).read_text())["d1"]["pressed"] == "black"


def test_cycle_repaints_the_page_the_device_last_downloaded(server, monkeypatch, tmp_path):
    """No target page means Tesserae re-pushes the lineup step and holds it; a warm doesn't count."""
    import flask
    mod = _fake_button_actions(monkeypatch)
    server._register_action()
    server._DATA_DIRS[:] = [tmp_path]

    class Pusher:
        def latest_render_for(self, device_id):
            return {"page_id": "warmed_print_page", "last_served_page_id": "photos"}

    app = flask.Flask("t")
    app.config["PUSH_MANAGER"] = Pusher()
    with app.app_context():
        r = mod.registry["print_blueprint_cycle"](Press("d1"), None)
    assert r.force_refresh and r.target_page_id == "photos" and r.new_step_index == 0


def test_a_slow_home_assistant_answer_stays_within_the_budget(server, monkeypatch, tmp_path):
    """A states read that hangs returns within the budget (last sheet or a plain message)."""
    import time as _time
    core = wire(server, monkeypatch, p1s_states())
    real = core.get_states
    monkeypatch.setattr(core, "get_states", lambda: (_time.sleep(2), real())[1])
    monkeypatch.setattr(server, "STATES_BUDGET_S", 0.3)
    started = _time.monotonic()
    out = server.fetch({"printer": "slowprinter_unseen"}, {}, ctx=ctx(tmp_path))
    assert _time.monotonic() - started < 1.5
    assert "slow" in (out.get("error") or out.get("note") or "")


def test_offline_fallback_only_shows_a_sheet_drawn_for_this_cell(server, monkeypatch, tmp_path):
    """Another cell's background (or panel, or size) is not this cell's last print."""
    wire(server, monkeypatch, p1s_states(status="finish"))
    server.fetch({"background": "white"}, {}, ctx=ctx(tmp_path))
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    runner = server._RUNNERS[str(tmp_path)]
    white = server.sheet_options({"background": "white"}, "colour")
    blue = server.sheet_options({"background": "blue"}, "colour")
    assert server._last_sheet(runner, "p1s_printer", SIZE, tmp_path, white) is not None
    assert server._last_sheet(runner, "p1s_printer", SIZE, tmp_path, blue) is None


def test_the_editor_preview_shows_the_last_print_while_idle(server, monkeypatch, tmp_path):
    """Setting the cell up while the printer is idle: the preview shows a print even when the cell
    is set to show nothing, or when the printer reports no job and no sheet matches these options."""
    wire(server, monkeypatch, p1s_states(status="finish"))
    server.fetch({"background": "white"}, {}, ctx=ctx(tmp_path))
    assert server._RUNNERS[str(tmp_path)].wait_idle(60)
    shown = server.fetch({"idle": "empty"}, {}, ctx=ctx(tmp_path, preview=True, settled=False))
    assert "empty" not in shown or shown["empty"]["kind"] == "drawing"
    assert server.fetch({"idle": "empty"}, {}, ctx=ctx(tmp_path))["empty"]["kind"] == "no_print"
    states = p1s_states(status="idle")
    states["sensor.p1s_printer_task_name"]["state"] = ""
    states["sensor.p1s_printer_gcode_filename"]["state"] = ""
    wire(server, monkeypatch, states)
    server._LAST.clear()
    preview = server.fetch({"background": "black"}, {}, ctx=ctx(tmp_path, preview=True, settled=False))
    assert "image" in preview, preview
    screen = server.fetch({"background": "black"}, {}, ctx=ctx(tmp_path))
    assert screen.get("empty", {}).get("kind") == "no_history"


def test_the_bounded_states_read_keeps_the_app_context(server, monkeypatch, tmp_path):
    """ha_core reads its settings through Flask's current_app: the pool thread must see it."""
    import flask
    core = wire(server, monkeypatch, p1s_states())
    real = core.get_states

    def needs_app():
        assert flask.current_app.name == "ctxcheck"
        return real()

    monkeypatch.setattr(core, "get_states", needs_app)
    app = flask.Flask("ctxcheck")
    with app.app_context():
        out = server.fetch({}, {}, ctx=ctx(tmp_path))
    assert "error" not in out, out


def test_the_editor_shows_the_cells_paper_and_an_edit_drops_the_pressed_colour(server, monkeypatch, tmp_path):
    """A refresh-button press leaves a colour for the display; the editor still previews the cell's
    own Paper, and changing Paper there makes the display follow the new setting."""
    wire(server, monkeypatch, p1s_states(status="finish"))
    dev = {"target_device_id": "door", "placement_id": "page/c1@door"}
    server.fetch({"background": "white"}, {}, ctx=ctx(tmp_path, **dev))
    server.cycle_background(tmp_path, "door")  # white -> next colour, pressed on the display
    saved = (tmp_path / server.CYCLE_FILE).read_bytes()
    pressed = server._read_map(tmp_path / server.CYCLE_FILE)["door"]["pressed"]
    assert pressed != "white"
    assert server.fetch({"background": "white"}, {}, ctx=ctx(tmp_path, **dev))["background"] == pressed
    prev = dict(dev, preview=True, settled=False)
    assert server.fetch({"background": "white"}, {}, ctx=ctx(tmp_path, **prev))["background"] == "white"
    assert server.fetch({"background": "black"}, {}, ctx=ctx(tmp_path, **prev))["background"] == "black"
    assert (tmp_path / server.CYCLE_FILE).read_bytes() == saved  # previews never write colour state
    assert server.fetch({"background": "black"}, {}, ctx=ctx(tmp_path, **dev))["background"] == "black"
    assert server.cycle_background(tmp_path, "door") == "white"


def test_saved_paper_change_drops_override_without_an_editor_preview(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states(status="idle"))
    c = ctx(tmp_path, target_device_id="door")
    server.fetch({"background": "white"}, {}, ctx=c)
    assert server.cycle_background(tmp_path, "door") == "blue"
    assert server.fetch({"background": "black"}, {}, ctx=c)["background"] == "black"
    assert server.cycle_background(tmp_path, "door") == "white"


def test_legacy_colour_files_are_ignored_and_preview_creates_no_state(server, monkeypatch, tmp_path):
    wire(server, monkeypatch, p1s_states(status="idle"))
    for name in ("cycle.json", "shown.json", "edited.json"):
        (tmp_path / name).write_text(json.dumps({"door": "black"}))
    c = ctx(tmp_path, target_device_id="door", preview=True)
    assert server.fetch({"background": "white"}, {}, ctx=c)["background"] == "white"
    assert not (tmp_path / server.CYCLE_FILE).exists()
    c["preview"] = False
    assert server.fetch({"background": "white"}, {}, ctx=c)["background"] == "white"
    assert server.cycle_background(tmp_path, "door") == "blue"


# ---------------------------------------------------------------- picture methods per printer
H3C_SERIAL = "DEMO_H3C_001"


def _wire_two(server, monkeypatch, *, h3c_files=False, fail=None, key=""):
    s = {**corpus.load_states("h3c_states.json"), **p1s_states()}
    core = FakeCore(s, fail)
    calls = []

    def http(path):
        calls.append(path)
        if "print_history" in path:
            files = [{"path": f"{SERIAL}/prints/cache/x.3mf", "printer_serial": SERIAL}]
            if h3c_files:
                files.append({"path": f"{H3C_SERIAL}/prints/cache/y.3mf", "printer_serial": H3C_SERIAL})
            return json.dumps({"files": files}).encode()
        raise OSError("404")

    monkeypatch.setattr(server, "_core", lambda: core)
    monkeypatch.setattr(server, "_http_for", lambda core, **kw: http)
    monkeypatch.setattr(server, "_own_settings", lambda: {"kie_api_key": key})
    return calls


def _rows(server, **options):
    return {r["value"]: r for r in server.choices("method", options=options)}


def test_toolpaths_is_disabled_for_a_printer_with_no_sliced_files(server, monkeypatch):
    calls = _wire_two(server, monkeypatch, key="k")
    rows = _rows(server, printer="h3c_printer")
    assert rows["toolpath"]["disabled"] is True
    assert rows["toolpath"]["reason"] == ("Not available for H3C Printer: Home Assistant gets no sliced files "
                                          "from it. It needs LAN mode and a storage card. "
                                          "Availability depends on the printer. Save and reopen to refresh.")
    assert "disabled" not in rows["preview"] and "disabled" not in rows["ai"]
    assert "disabled" not in _rows(server, printer="p1s_printer")["toolpath"]
    # cached: the editor opening again doesn't ask Home Assistant's history again
    n = len(calls)
    _rows(server, printer="h3c_printer")
    assert len(calls) == n


def test_toolpaths_stays_open_when_the_printer_has_files_or_ha_is_away(server, monkeypatch):
    _wire_two(server, monkeypatch, h3c_files=True, key="k")
    assert "disabled" not in _rows(server, printer="h3c_printer")["toolpath"]
    _wire_two(server, monkeypatch, fail=OSError("down"), key="k")
    server._AVAILABILITY.clear()
    assert not any(r.get("disabled") for r in _rows(server, printer="h3c_printer").values())


def test_ai_engraving_needs_a_key(server, monkeypatch):
    _wire_two(server, monkeypatch, key="")
    ai = _rows(server, printer="p1s_printer")["ai"]
    assert ai["disabled"] is True and "kie.ai API key" in ai["reason"]


def test_an_older_tesserae_gets_the_plain_list(server, monkeypatch):
    """Tesserae without cell options calls choices(name): all three open, with pictures."""
    _wire_two(server, monkeypatch, key="")
    rows = server.choices("method")
    m = json.loads((PLUGIN / "plugin.json").read_text())
    static = next(o for o in m["cell_options"] if o["name"] == "method")["choices"]
    assert [(r["value"], r["label"]) for r in rows] == [(r["value"], r["label"]) for r in static]
    assert all("/example/" in r["image"] for r in rows)


def test_blank_printer_means_the_first_one(server, monkeypatch):
    _wire_two(server, monkeypatch, key="k")
    first = server.printer_prefixes(server._core().get_states())[0][0]
    assert ("disabled" in _rows(server)["toolpath"]) == (first == "h3c_printer")
