"""OctoPrint REST snapshots and sliced G-code through the public source boundary."""

import json
from pathlib import Path

import pytest
from blueprint import source_octoprint as source
from blueprint.gcode import parse_walls
from blueprint.plain_gcode import read_gcode

FIX = Path(__file__).parent / "fixtures" / "octoprint"


def test_plain_gcode_slicer_facts_and_toolpaths():
    facts = read_gcode((FIX / "block.gcode").read_bytes())
    assert facts.layers == 3
    assert facts.layer_h == 0.2
    assert facts.material == "PLA"
    assert facts.weight_g == 1.2
    assert facts.length_m == 0.4
    assert facts.nozzle_d == 0.4
    assert facts.preview_png is None
    assert len(parse_walls(facts.gcode, facts.bbox_all)) == 3
    assert facts.envelope_mm == (20.0, 20.0, 0.6)


def test_embedded_thumbnail():
    facts = read_gcode((FIX / "block-thumbnail.gcode").read_bytes())
    assert facts.preview_png.startswith(b"\x89PNG")


@pytest.fixture
def api(monkeypatch):
    calls = []

    def get(self, path, **kwargs):
        calls.append(path)
        if path == "/api/printer?exclude=sd":
            return (FIX / "printer.json").read_bytes()
        if path == "/api/job":
            return (FIX / "job.json").read_bytes()
        if path == "/api/files/local/block.gcode":
            return (FIX / "file.json").read_bytes()
        if path in (
            "/downloads/files/local/block.gcode",
            "http://octo.test/downloads/files/local/block.gcode",
        ):
            return (FIX / "block.gcode").read_bytes()
        raise AssertionError(path)

    monkeypatch.setattr(source.Client, "get", get)
    source._TELEMETRY.clear()
    source._SLICES.clear()
    return calls


SETTINGS = {"octoprint_url": "http://octo.test", "octoprint_api_key_secret": "test"}


def test_job_read_and_background_file_cache(api, tmp_path):
    job = source.build_job_octoprint(SETTINGS, {}, {}, tmp_path)
    assert job.title == "block"
    assert job.status == "running"
    assert job.progress == 0.5
    assert job.nozzle_temp == (205.0, 210.0)
    assert job.bed_temp == (59.0, 60.0)
    assert job.slice is None
    assert len(api) == 2  # request thread never downloads or parses G-code
    full = source.build_job_octoprint(SETTINGS, {}, {"allow_download": True}, tmp_path)
    assert full.slice and full.material == "PLA"
    assert full.layer is None  # core OctoPrint does not report the current layer
    assert not full.tools and not full.ams
    assert len(api) == 4
    again = source.build_job_octoprint(SETTINGS, {}, {}, tmp_path)
    assert again.slice == full.slice
    assert again.job_key == job.job_key
    assert len(api) == 4


def test_missing_settings(tmp_path):
    with pytest.raises(ValueError, match="URL"):
        source.build_job_octoprint({}, {}, {}, tmp_path)
    with pytest.raises(ValueError, match="API key"):
        source.build_job_octoprint(
            {"octoprint_url": "http://octo.test"}, {}, {}, tmp_path
        )


@pytest.mark.parametrize(
    "flags,progress,status",
    [
        ({"error": True, "printing": True}, 50, "failed"),
        ({"paused": True}, 50, "pause"),
        ({"resuming": True}, 50, "running"),
        ({"operational": True}, 100, "finish"),
        ({"operational": True}, 0, "idle"),
    ],
)
def test_status_mapping(flags, progress, status):
    assert source._status(flags, progress) == status


def test_disconnected_printer_keeps_file_without_old_temperatures(
    api, monkeypatch, tmp_path
):
    source.build_job_octoprint(SETTINGS, {}, {}, tmp_path)
    for entry in source._TELEMETRY.values():
        entry["when"] = 0

    def offline(*args, **kwargs):
        raise OSError("409 not operational")

    monkeypatch.setattr(source.Client, "get", offline)
    job = source.build_job_octoprint(SETTINGS, {}, {}, tmp_path)
    assert job.status == "offline" and job.title == "block"
    assert job.nozzle_temp is None


def test_file_fetch_failure_has_a_cooldown(api, monkeypatch, tmp_path):
    original = source.Client.get

    def fail_file(self, path, **kwargs):
        if "/api/files/" in path:
            api.append(path)
            raise OSError("timeout")
        return original(self, path, **kwargs)

    monkeypatch.setattr(source.Client, "get", fail_file)
    source.build_job_octoprint(SETTINGS, {}, {"allow_download": True}, tmp_path)
    source.build_job_octoprint(SETTINGS, {}, {"allow_download": True}, tmp_path)
    assert len(api) == 3


def test_plugin_thumbnail(api, monkeypatch, tmp_path):
    original = source.Client.get

    def get(self, path, **kwargs):
        if path == "/api/files/local/block.gcode":
            data = json.loads((FIX / "file.json").read_bytes())
            data["thumbnail"] = "/plugin/thumbnail/block.png"
            return json.dumps(data).encode()
        if path == "/plugin/thumbnail/block.png":
            return (FIX.parent / "synthetic-cover.png").read_bytes()
        return original(self, path, **kwargs)

    monkeypatch.setattr(source.Client, "get", get)
    job = source.build_job_octoprint(SETTINGS, {}, {"allow_download": True}, tmp_path)
    assert job.slice.preview_png.startswith(b"\x89PNG")


def test_telemetry_budget_does_not_wait_for_slow_server(monkeypatch, tmp_path):
    import threading
    import time

    release = threading.Event()
    source._TELEMETRY.clear()
    monkeypatch.setattr(source, "TELEMETRY_BUDGET_S", 0.05)

    def slow(*args):
        release.wait(2)
        return {}, {}

    monkeypatch.setattr(source, "_poll", slow)
    start = time.monotonic()
    try:
        source.build_job_octoprint(SETTINGS, {}, {}, tmp_path)
        assert time.monotonic() - start < 0.2
        # Renders share an in-flight request, even when a socket has not returned yet.
        assert len(source._TELEMETRY) == 1
    finally:
        release.set()


def test_api_key_never_leaves_configured_host(monkeypatch):
    with pytest.raises(ValueError, match="outside"):
        source.Client("http://octo.test", "secret", 1).get(
            "https://other.test/cover.png"
        )


def test_reprint_identity_is_stable_and_survives_restart(api, tmp_path):
    first = source.build_job_octoprint(SETTINGS, {}, {}, tmp_path)
    source._RUNS.clear()
    again = source.build_job_octoprint(SETTINGS, {}, {}, tmp_path)
    assert again.job_key == first.job_key and again.start_ts == first.start_ts
    source._run_identity(
        tmp_path, first.prefix, next(iter(source._RUNS.values()))["file"], "finish", 120
    )
    second = source.build_job_octoprint(SETTINGS, {}, {}, tmp_path)
    assert second.job_key != first.job_key


def test_recorded_virtual_printer_completion(monkeypatch, tmp_path):
    source._TELEMETRY.clear()

    def get(self, path, **kwargs):
        name = (
            "virtual-printer.json"
            if path.startswith("/api/printer")
            else "virtual-job.json"
        )
        return (FIX / name).read_bytes()

    monkeypatch.setattr(source.Client, "get", get)
    job = source.build_job_octoprint(SETTINGS, {}, {}, tmp_path)
    assert job.status == "finish"
    assert job.progress == 1.0 and job.title == "block"
    assert job.nozzle_temp == (21.3, 0.0)
    assert job.brand == "OCTOPRINT"
