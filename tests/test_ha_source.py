import copy
import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from blueprint.ha_source import build_job, printer_prefixes
from blueprint.model import PrintJob

FIX = Path(__file__).parent / "fixtures"
BLOCK = FIX / "synthetic-block.3mf"


def states(name):
    return json.loads((FIX / name).read_text())


class Http:
    """Fake HA: print_history JSON and file_cache downloads, recording every path asked for."""

    def __init__(self, history=None, files=None, fail=()):
        self.history, self.files, self.fail, self.calls = history, files or {}, fail, []

    def __call__(self, path):
        self.calls.append(path)
        if any(path.startswith(f) for f in self.fail):
            raise OSError("404")
        if path.startswith("/api/bambu_lab/print_history"):
            if self.history is None:
                raise OSError("404")
            return json.dumps(self.history).encode()
        if path.startswith("/api/bambu_lab/file_cache/"):
            return self.files[unquote(path[len("/api/bambu_lab/file_cache/"):])]
        if path.startswith("/api/image_proxy/"):
            return b"COVER"
        raise OSError("404")


def p1s_block_states():
    s = states("p1s_states.json")
    s["sensor.p1s_printer_task_name"]["state"] = "Synthetic Block"
    s["sensor.p1s_printer_gcode_filename"]["state"] = "synthetic-block.3mf"
    return s


def test_build_job_p1s_from_states(tmp_path):
    hist = json.loads((FIX / "print_history.json").read_text())
    path = next(f["path"] for f in hist["files"] if "synthetic-block" in f["filename"])
    http = Http(hist, {path: BLOCK.read_bytes()})
    job = build_job("p1s_printer", p1s_block_states(), http, tmp_path)
    assert job.model == "P1S"
    assert job.nozzle_d == pytest.approx(0.4)
    assert job.length_m == pytest.approx(24.13, abs=0.01)
    assert len(job.ams) == 4 and job.ams[0].type == "PETG" and job.ams[0].color == "#FFD00B"
    assert job.slice is not None and job.slice.layers > 0
    assert job.title == "Synthetic Block" and job.serial == "DEMO_P1S_001"
    assert job.progress == pytest.approx(501 / 856)
    assert job.layer == 501 and job.layers == 856
    assert job.ams_humidity == "INDEX 3 / 5"
    assert job.nozzle_temp == (245.0, 245.0) and job.fans["chamber"] == 70
    assert "heatbreak" not in job.fans
    assert len(job.job_key) == 40
    assert job.cover_png is None  # the 3MF has its own preview


def test_3mf_downloaded_once(tmp_path):
    hist = json.loads((FIX / "print_history.json").read_text())
    path = next(f["path"] for f in hist["files"] if "synthetic-block" in f["filename"])
    http = Http(hist, {path: BLOCK.read_bytes()})
    for _ in range(3):
        build_job("p1s_printer", p1s_block_states(), http, tmp_path)
    assert sum(c.startswith("/api/bambu_lab/file_cache/") for c in http.calls) == 1
    assert len(list((tmp_path / "3mf").glob("*.3mf"))) == 1


def test_job_without_3mf_has_no_slicer_facts(tmp_path):
    job = build_job("h3c_printer", states("h3c_states.json"), Http({"files": []}), tmp_path)
    assert job.slice is None
    assert job.cover_png == b"COVER"
    assert len(job.tools) == 9
    assert job.tools[3].temp_c == pytest.approx(245.0)  # 473 F
    assert all(t.temp_c < 400 for t in job.tools)
    assert len(job.ams) == 10 and job.ams[-1].empty  # 9 AMS 3H slots + empty external spool
    assert job.ams_humidity == "24 %"
    assert job.printer_name == "H3C Printer"


@pytest.mark.parametrize("fail", [("/api/bambu_lab/print_history",), ("/api/bambu_lab/file_cache",)])
def test_http_errors_mean_no_slice(tmp_path, fail):
    hist = json.loads((FIX / "print_history.json").read_text())
    path = next(f["path"] for f in hist["files"] if "synthetic-block" in f["filename"])
    http = Http(hist, {path: BLOCK.read_bytes()}, fail=fail)
    job = build_job("p1s_printer", p1s_block_states(), http, tmp_path)
    assert job.slice is None


def test_unit_conversion_from_fahrenheit(tmp_path):
    s = states("p1s_states.json")
    for k in ("nozzle_temperature", "nozzle_target_temperature"):
        e = s[f"sensor.p1s_printer_{k}"]
        e["state"], e["attributes"]["unit_of_measurement"] = "473", "°F"
    job = build_job("p1s_printer", s, Http(None), tmp_path)
    assert job.nozzle_temp == (pytest.approx(245.0), pytest.approx(245.0))
    assert job.length_m == pytest.approx(24.13, abs=0.01)


def test_finished_status_is_full_progress(tmp_path):
    s = states("p1s_states.json")
    s["sensor.p1s_printer_print_status"]["state"] = "finish"
    assert build_job("p1s_printer", s, Http(None), tmp_path).progress == 1.0


def test_progress_falls_back_to_percent(tmp_path):
    s = states("p1s_states.json")
    del s["sensor.p1s_printer_total_layer_count"]
    assert build_job("p1s_printer", s, Http(None), tmp_path).progress == pytest.approx(0.72)


def test_printer_prefixes_any_model():
    def ent(eid, state="idle", **a):
        return {"entity_id": eid, "state": state, "attributes": a}
    lst = [ent("sensor.a1_mini_print_status", friendly_name="A1 Mini Print status"),
           ent("sensor.x1c_shop_print_status"), ent("sensor.x1c_shop_printer_name", "Shop X1C"),
           ent("sensor.unrelated", "1")]
    assert printer_prefixes(lst) == [("a1_mini", "A1 Mini"), ("x1c_shop", "Shop X1C")]


def test_missing_entities_are_none(tmp_path):
    s = {"sensor.lone_print_status": {"entity_id": "sensor.lone_print_status", "state": "running", "attributes": {}}}
    j = build_job("lone", s, Http(None), tmp_path)
    assert isinstance(j, PrintJob)
    assert j.status == "running" and j.slice is None and j.cover_png is None
    assert j.nozzle_d is None and j.nozzle_temp is None and j.bed_temp is None and j.chamber_c is None
    assert j.layer is None and j.layers is None and j.weight_g is None and j.length_m is None
    assert j.ams == [] and j.tools == [] and j.fans == {} and j.ams_humidity is None
    assert j.progress == 0.0 and j.model == ""


def test_model_from_prefix_when_no_history(tmp_path):
    s = {"sensor.a1_mini_print_status": {"entity_id": "x", "state": "idle", "attributes": {}}}
    assert build_job("a1_mini", s, Http(None), tmp_path).model == "A1 MINI"


def test_ams_units_sorted_and_legacy_names(tmp_path):
    s = {"sensor.q_print_status": {"state": "idle", "attributes": {}}}
    for k, t in (("ams_2_tray_1", "B1"), ("ams_1_tray_2", "A2"), ("ams_1_tray_1", "A1"),
                 ("ams_128_tray_1", "HT"), ("ams_tray_3", "LEG")):
        s[f"sensor.q_{k}"] = {"state": t, "attributes": {"type": t, "color": "#112233FF"}}
    j = build_job("q", s, Http(None), tmp_path)
    assert [t.type for t in j.ams] == ["LEG", "A1", "A2", "B1", "HT"]


# ---- fix round 1 ----------------------------------------------------------------------------------

def _st(prefix, **extra):
    s = {f"sensor.{prefix}_print_status": {"state": "running", "attributes": {}},
         f"sensor.{prefix}_serial_number": {"state": "SER_" + prefix, "attributes": {}}}
    for k, v in extra.items():
        s[f"sensor.{prefix}_{k}"] = {"state": v, "attributes": {"unit_of_measurement": "%"}}
    return s


def test_tool_without_sensors_is_none(tmp_path):
    s = _st("r")
    s["sensor.r_hotendrack_printtool_1"] = {"state": "mounted", "attributes": {}}
    t = build_job("r", s, Http(None), tmp_path).tools[0]
    assert t.temp_c is None and t.hours is None and t.state == "mounted"


def test_neighbouring_prefixes_do_not_share_entities(tmp_path):
    s = {}
    s.update(_st("x1c", aux_fan_speed="10"))
    s.update(_st("x1c_shop", aux_fan_speed="90", chamber_fan_speed="55"))
    s.update(_st("a1", cooling_fan_speed="1"))
    s.update(_st("a1_mini", cooling_fan_speed="2"))
    s["sensor.x1c_shop_ams_tray_1"] = {"state": "PLA", "attributes": {"type": "PLA"}}
    s["sensor.x1c_shop_hotendrack_printtool_1"] = {"state": "docked", "attributes": {}}
    assert build_job("x1c", s, Http(None), tmp_path).fans == {"aux": 10.0}
    assert build_job("x1c", s, Http(None), tmp_path).ams == []
    assert build_job("x1c", s, Http(None), tmp_path).tools == []
    shop = build_job("x1c_shop", s, Http(None), tmp_path)
    assert shop.fans == {"aux": 90.0, "chamber": 55.0} and len(shop.ams) == 1
    assert build_job("a1", s, Http(None), tmp_path).fans == {"cooling": 1.0}
    assert build_job("a1_mini", s, Http(None), tmp_path).fans == {"cooling": 2.0}


def _hist(*files):
    return {"files": [{"filename": n, "path": f"SER/{n}", "size": 1, "modified": m, "printer_serial": "SER"}
                      for n, m in files]}


def _match_states(task="", gcode=""):
    s = _st("m")
    s["sensor.m_serial_number"]["state"] = "SER"
    for k, v in (("task_name", task), ("gcode_filename", gcode)):
        if v:
            s[f"sensor.m_{k}"] = {"state": v, "attributes": {}}
    return s


def test_match_by_task_name_and_strips_size_prefix(tmp_path):
    block = BLOCK.read_bytes()
    http = Http(_hist(("999-Other.3mf", "2026-01-02T00:00:00"), ("123456-Block.3mf", "2026-01-01T00:00:00")),
                {"SER/123456-Block.3mf": block})
    # gcode_filename is a cloud path that names nothing; task_name carries the match
    job = build_job("m", _match_states("Block", "/data/Metadata/plate_1.gcode"), http, tmp_path)
    assert job.slice is not None


def test_no_match_means_no_slice(tmp_path):
    http = Http(_hist(("1-Other.3mf", "2026-01-02T00:00:00")), {})
    job = build_job("m", _match_states("Block", "Block.3mf"), http, tmp_path)
    assert job.slice is None and not any("file_cache" in c for c in http.calls)


def test_same_name_newest_wins_by_timestamp_not_string(tmp_path):
    # "2026-9-..." sorts after "2026-10-..." as a string but is older
    http = Http(_hist(("1-Block.3mf", "2026-09-01T00:00:00"), ("2-Block.3mf", "2026-10-01T00:00:00")),
                {"SER/1-Block.3mf": BLOCK.read_bytes(), "SER/2-Block.3mf": BLOCK.read_bytes()})
    build_job("m", _match_states("Block"), http, tmp_path)
    assert [c for c in http.calls if "file_cache" in c] == ["/api/bambu_lab/file_cache/SER/2-Block.3mf"]


def test_corrupt_cached_3mf_is_deleted_and_refetched(tmp_path):
    good = {"SER/1-Block.3mf": b"not a zip"}
    http = Http(_hist(("1-Block.3mf", "2026-01-01T00:00:00")), good)
    assert build_job("m", _match_states("Block"), http, tmp_path).slice is None
    assert list((tmp_path / "3mf").glob("*.3mf")) == []
    good["SER/1-Block.3mf"] = BLOCK.read_bytes()
    assert build_job("m", _match_states("Block"), http, tmp_path).slice is not None
    assert sum("file_cache" in c for c in http.calls) == 2


def test_allow_download_false_never_downloads(tmp_path):
    http = Http(_hist(("1-Block.3mf", "2026-01-01T00:00:00")), {"SER/1-Block.3mf": BLOCK.read_bytes()})
    job = build_job("m", _match_states("Block"), http, tmp_path, allow_download=False)
    assert job.slice is None and not any("file_cache" in c for c in http.calls)
    assert any("print_history" in c for c in http.calls)
    build_job("m", _match_states("Block"), http, tmp_path)  # download for real
    n = len(http.calls)
    job = build_job("m", _match_states("Block"), http, tmp_path, allow_download=False)
    assert job.slice is not None and not any("file_cache" in c for c in http.calls[n:])


def test_parsed_3mf_is_memoised(tmp_path, monkeypatch):
    import blueprint.ha_source as hs
    calls = []
    real = hs.read_3mf
    monkeypatch.setattr(hs, "read_3mf", lambda b: calls.append(1) or real(b))
    http = Http(_hist(("1-Block.3mf", "2026-01-01T00:00:00")), {"SER/1-Block.3mf": BLOCK.read_bytes()})
    for _ in range(3):
        assert build_job("m", _match_states("Block"), http, tmp_path).slice is not None
    assert len(calls) == 1


def test_allow_download_false_never_parses_on_the_request_thread(tmp_path, monkeypatch):
    import blueprint.ha_source as hs
    http = Http(_hist(("1-Block.3mf", "2026-01-01T00:00:00")), {"SER/1-Block.3mf": BLOCK.read_bytes()})
    build_job("m", _match_states("Block"), http, tmp_path)  # downloaded and parsed (the runner thread)
    monkeypatch.setattr(hs, "_PARSED", {})  # a restart: the file is on disk, the memo is empty
    calls = []
    monkeypatch.setattr(hs, "read_3mf", lambda b: calls.append(1))
    assert build_job("m", _match_states("Block"), http, tmp_path, allow_download=False).slice is None
    assert calls == []


def test_unavailable_cover_uses_the_saved_copy(tmp_path):
    s = states("h3c_states.json")
    http = Http({"files": []})
    assert build_job("h3c_printer", s, http, tmp_path).cover_png == b"COVER"
    s["image.h3c_printer_cover_image"]["state"] = "unavailable"  # e.g. after an integration reload
    n = len(http.calls)
    job = build_job("h3c_printer", s, http, tmp_path)
    assert job.cover_png == b"COVER"
    assert not any("image_proxy" in c for c in http.calls[n:])  # HA answers 500 for it: don't ask


def test_unavailable_cover_without_a_copy_is_none(tmp_path):
    s = states("h3c_states.json")
    s["image.h3c_printer_cover_image"]["state"] = "unavailable"
    http = Http({"files": []})
    assert build_job("h3c_printer", s, http, tmp_path).cover_png is None
    assert not any("image_proxy" in c for c in http.calls)


def test_failed_cover_request_falls_back_to_the_saved_copy(tmp_path):
    s = states("h3c_states.json")
    assert build_job("h3c_printer", s, Http({"files": []}), tmp_path).cover_png == b"COVER"
    job = build_job("h3c_printer", s, Http({"files": []}, fail=("/api/image_proxy/",)), tmp_path)
    assert job.cover_png == b"COVER"


# ---- final review ----------------------------------------------------------------------------------

def test_3mfs_on_disk_keep_the_newest_twelve(tmp_path):
    names = [(f"{i}-Block{i}.3mf", f"2026-01-01T00:{i:02d}:00") for i in range(14)]
    http = Http(_hist(*names), {f"SER/{n}": BLOCK.read_bytes() for n, _ in names})
    for i in range(14):
        assert build_job("m", _match_states(f"Block{i}"), http, tmp_path).slice is not None
    left = list((tmp_path / "3mf").glob("*.3mf"))
    assert len(left) == 12 and not list((tmp_path / "3mf").glob("*.part"))


def test_covers_on_disk_keep_the_newest_twelve(tmp_path):
    s = states("h3c_states.json")
    for i in range(14):
        s["sensor.h3c_printer_start_time"]["state"] = f"2026-11-{i + 1:02d}T05:35:47+00:00"
        assert build_job("h3c_printer", s, Http({"files": []}), tmp_path).cover_png == b"COVER"
    assert len(list((tmp_path / "covers").glob("*.png"))) == 12


def test_slice_facts_name_their_source(tmp_path):
    http = Http(_hist(("1-Block.3mf", "2026-01-01T00:00:00")), {"SER/1-Block.3mf": BLOCK.read_bytes()})
    sl = build_job("m", _match_states("Block"), http, tmp_path).slice
    assert sl.source.startswith(str(tmp_path / "3mf")) and sl.layer_z


def test_unknown_units_are_unknown_values(tmp_path):
    s = _st("u", nozzle_size="0.4", print_length="24", nozzle_temperature="200", nozzle_target_temperature="200")
    s["sensor.u_nozzle_size"]["attributes"]["unit_of_measurement"] = "furlong"
    s["sensor.u_print_length"]["attributes"]["unit_of_measurement"] = "chain"
    s["sensor.u_nozzle_temperature"]["attributes"]["unit_of_measurement"] = "W"
    j = build_job("u", s, Http(None), tmp_path)
    assert j.nozzle_d is None and j.length_m is None and j.nozzle_temp is None


def test_job_key_without_a_serial_keeps_printers_apart():
    from blueprint.ha_source import build_job
    import tempfile
    def states(prefix):
        return {f"sensor.{prefix}_task_name": {"state": "Block"}, f"sensor.{prefix}_print_status": {"state": "running"}}
    http = lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))
    with tempfile.TemporaryDirectory() as d:
        a = build_job("a", states("a"), http, d, allow_download=False)
        b = build_job("b", states("b"), http, d, allow_download=False)
    assert a.job_key != b.job_key


# ---------------------------------------------------------------- can Toolpaths work here
H3C_SERIAL = "DEMO_H3C_001"


def _history(*serials):
    return json.dumps({"files": [{"filename": f"{i}-x.3mf", "path": f"{s}/prints/cache/{i}-x.3mf",
                                  "printer_serial": s} for i, s in enumerate(serials)]}).encode()


def test_no_sliced_files_from_a_cloud_printer_without_storage():
    from blueprint.ha_source import no_sliced_files

    h3c = states("h3c_states.json")  # print_type cloud, no SD card, LAN mode off
    assert no_sliced_files("h3c_printer", h3c, lambda path: _history())
    assert no_sliced_files("h3c_printer", h3c, lambda path: _history("OTHER")), "another printer's files"
    assert not no_sliced_files("h3c_printer", h3c, lambda path: _history(H3C_SERIAL))


@pytest.mark.parametrize("key,state", [
    ("sensor.h3c_printer_print_type", "local"),
    ("sensor.h3c_printer_sd_card_status", "normal"),
    ("binary_sensor.h3c_printer_developer_lan_mode", "on"),
])
def test_each_sign_alone_is_enough(key, state):
    from blueprint.ha_source import no_sliced_files

    s = copy.deepcopy(states("h3c_states.json"))
    others = {"sensor.h3c_printer_print_type": "local", "sensor.h3c_printer_sd_card_status": "normal",
              "binary_sensor.h3c_printer_developer_lan_mode": "on"}
    for k, v in others.items():
        s[k]["state"] = v
    assert not no_sliced_files("h3c_printer", s, lambda path: _history()), "no sign, no verdict"
    s[key]["state"] = states("h3c_states.json")[key]["state"]
    assert no_sliced_files("h3c_printer", s, lambda path: _history())


def test_anything_unknown_keeps_toolpaths_open():
    from blueprint.ha_source import no_sliced_files

    h3c = states("h3c_states.json")

    def down(path):
        raise OSError("unreachable")

    assert not no_sliced_files("h3c_printer", h3c, down), "history endpoint failed"
    assert not no_sliced_files("h3c_printer", h3c, lambda path: b'{"error": "x"}'), "no file list"
    assert not no_sliced_files("h3c_printer", h3c, lambda path: b"not json")
    s = copy.deepcopy(h3c)
    s["sensor.h3c_printer_serial_number"]["state"] = "unavailable"
    assert not no_sliced_files("h3c_printer", s, lambda path: _history()), "no serial"
    s = copy.deepcopy(h3c)
    for k in ("sensor.h3c_printer_print_type", "sensor.h3c_printer_sd_card_status",
              "binary_sensor.h3c_printer_developer_lan_mode"):
        s[k]["state"] = "unknown"
    assert not no_sliced_files("h3c_printer", s, lambda path: _history()), "unknown states are not signs"


def test_a_printer_with_sliced_files_stays_open():
    from blueprint.ha_source import no_sliced_files

    p1s = states("p1s_states.json")
    serial = p1s["sensor.p1s_printer_serial_number"]["state"]
    assert not no_sliced_files("p1s_printer", p1s, lambda path: (FIX / "print_history.json").read_bytes())
    assert not no_sliced_files("p1s_printer", p1s, lambda path: _history(serial))


def test_tool_filament_and_colour_come_from_each_nozzle(tmp_path):
    s = states('h3c_states.json')
    s['sensor.h3c_printer_hotendrack_printtool_2']['attributes'].update(
        filament_name='Bambu PLA Basic', filament_type='PLA', color='#FFFFFFFF')
    s['sensor.h3c_printer_hotendrack_printtool_3']['attributes'].update(
        filament_name=None, filament_type='PETG', color='#123456FF')
    job = build_job('h3c_printer', s, Http({'files': []}), tmp_path)
    assert job.tools[1].filament == 'Bambu PLA Basic'
    assert job.tools[1].color == '#FFFFFF'
    assert job.tools[2].filament == 'PETG'
    assert job.tools[2].color == '#123456'
