"""PrusaLink v1 contract exercised through a real local HTTP server."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from blueprint.source_prusalink import build_job_prusalink, prepare_job_prusalink

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def printer():
    servers = []

    def start(
        auth="key",
        algorithm="MD5-sess",
        binary=False,
        state="PRINTING",
        progress=50,
        delay=0,
        thumbnail_only=False,
        job_id=42,
        axis_z=0.4,
        short_file=False,
    ):
        counts = Counter()

        def digest_ok(header, path):
            fields = dict(
                (k, q or b)
                for k, q, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', header)
            )

            def h(s):
                return (hashlib.sha256 if algorithm == "SHA-256" else hashlib.md5)(
                    s.encode()
                ).hexdigest()

            a1 = h("maker:PrusaLink:secret")
            if algorithm.endswith("-sess"):
                a1 = h(f"{a1}:nonce:{fields.get('cnonce')}")
            expected = h(
                f"{a1}:nonce:{fields.get('nc')}:{fields.get('cnonce')}:auth:{h('GET:' + path)}"
            )
            return fields.get("response") == expected

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                counts[self.path] += 1
                valid = (
                    self.headers.get("X-Api-Key") == "secret"
                    if auth == "key"
                    else digest_ok(self.headers.get("Authorization", ""), self.path)
                )
                if not valid:
                    self.send_response(401)
                    self.send_header(
                        "WWW-Authenticate",
                        f'Digest realm="PrusaLink",nonce="nonce",qop="auth",algorithm="{algorithm}"',
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                docs = {
                    f"/api/v1/{name}": json.loads(
                        (FIX / f"prusalink-{name}.json").read_text()
                    )
                    for name in ("status", "job", "info")
                }
                if not binary:
                    docs["/api/v1/job"]["file"].update(
                        display_name="Cube.gcode", refs={"download": "/usb/Cube.gcode"}
                    )
                docs["/api/v1/status"]["printer"]["state"] = state
                docs["/api/v1/status"]["printer"]["axis_z"] = axis_z
                docs["/api/v1/status"]["job"]["progress"] = progress
                docs["/api/v1/job"]["id"] = job_id
                if thumbnail_only:
                    docs["/api/v1/job"]["file"]["refs"] = {"thumbnail": "/thumb.png"}
                if delay and self.path == "/api/v1/status":
                    import time

                    time.sleep(delay)
                if self.path == "/thumb.png":
                    body = (FIX / "synthetic-cover.png").read_bytes()
                elif self.path in docs:
                    body = json.dumps(docs[self.path]).encode()
                elif self.path.startswith("/usb/"):
                    body = (
                        FIX
                        / (
                            "prusalink-cube.bgcode"
                            if binary
                            else "prusalink-cube.gcode"
                        )
                    ).read_bytes()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Length",
                    str(
                        len(body)
                        + (10 if short_file and self.path.startswith("/usb/") else 0)
                    ),
                )
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        settings = {
            "prusalink_url": f"http://127.0.0.1:{server.server_port}",
            "prusalink_api_key_secret"
            if auth == "key"
            else "prusalink_password_secret": "secret",
        }
        return settings, counts

    yield start
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "auth,algorithm",
    [("key", "MD5"), ("digest", "MD5"), ("digest", "MD5-sess"), ("digest", "SHA-256")],
)
@pytest.mark.parametrize("binary", [False, True])
def test_prusalink_job_and_file(printer, tmp_path, auth, algorithm, binary):
    settings, counts = printer(auth, algorithm, binary)
    job = build_job_prusalink(settings, {}, {}, tmp_path)
    assert job.title == "Cube" and job.printer_name == "Workshop Prusa"
    assert job.status == "running" and job.progress == 0.5
    assert job.nozzle_temp == (214.9, 215) and job.bed_temp == (59.5, 60)
    assert job.end_ts - job.start_ts == pytest.approx(1046, abs=1)
    assert job.slice is None and not any(k.startswith("/usb/") for k in counts)
    full = prepare_job_prusalink(job, settings, tmp_path)
    assert full.slice and full.slice.layers == 3 and full.layer == 2
    assert full.material == "PLA" and full.nozzle_d == 0.4
    assert full.weight_g == 1.2 and full.length_m == 0.4
    assert full.slice.preview_png.startswith(b"\x89PNG")
    assert not full.tools and not full.ams
    before = dict(counts)
    again = build_job_prusalink(settings, {}, {}, tmp_path)
    prepare_job_prusalink(again, settings, tmp_path)
    assert dict(counts) == before  # no polling or file requests inside the TTL
    assert again.job_key == job.job_key and again.slice is not None


def test_missing_address_and_sign_in(tmp_path):
    with pytest.raises(ValueError, match="address"):
        build_job_prusalink({}, {}, {}, tmp_path)
    with pytest.raises(ValueError, match="password or API key"):
        build_job_prusalink({"prusalink_url": "printer.local"}, {}, {}, tmp_path)


@pytest.mark.parametrize(
    "state,status,progress",
    [
        ("PRINTING", "running", 0),
        ("PAUSED", "pause", 50),
        ("ATTENTION", "pause", 50),
        ("FINISHED", "finish", 100),
        ("STOPPED", "failed", 50),
    ],
)
def test_states_and_progress(printer, tmp_path, state, status, progress):
    settings, _ = printer(state=state, progress=progress)
    job = build_job_prusalink(settings, {}, {}, tmp_path)
    assert job.status == status and job.progress == progress / 100
    if status == "pause":
        assert job.end_ts is None


def test_bad_credentials_are_actionable(printer, tmp_path):
    settings, _ = printer()
    settings["prusalink_api_key_secret"] = "wrong"
    with pytest.raises(ValueError, match="refused"):
        build_job_prusalink(settings, {}, {}, tmp_path)


def test_slow_telemetry_cannot_hold_render(printer, tmp_path):
    import time

    settings, _ = printer(delay=0.3)
    start = time.monotonic()
    with pytest.raises(OSError, match="slow"):
        build_job_prusalink(settings, {}, {"_prusalink_budget_s": 0.02}, tmp_path)
    assert time.monotonic() - start < 0.2


def test_changed_job_id_does_not_download_previous_file(printer, tmp_path):
    settings, counts = printer(job_id=43)
    job = build_job_prusalink(settings, {}, {}, tmp_path)
    full = prepare_job_prusalink(job, settings, tmp_path)
    assert full.slice is None and not job.has_3mf
    assert not any(k.startswith("/usb/") for k in counts)


def test_thumbnail_endpoint_is_cached_and_reused(printer, tmp_path):
    settings, counts = printer(thumbnail_only=True)
    job = build_job_prusalink(settings, {}, {}, tmp_path)
    full = prepare_job_prusalink(job, settings, tmp_path)
    assert full.cover_png and full.slice is None
    full = prepare_job_prusalink(job, settings, tmp_path)
    assert full.cover_png and counts["/thumb.png"] == 1
    again = build_job_prusalink(settings, {}, {}, tmp_path)
    assert again.cover_png == full.cover_png


def test_incomplete_http_file_is_not_cached(printer, tmp_path):
    settings, _ = printer(short_file=True)
    job = build_job_prusalink(settings, {}, {}, tmp_path)
    assert prepare_job_prusalink(job, settings, tmp_path).slice is None
    assert not list((tmp_path / "prusalink").glob("*.gcode"))


@pytest.mark.parametrize(
    "path",
    [
        "https://example.invalid/file",
        "//example.invalid/file",
        "/\\example.invalid/file",
    ],
)
def test_file_references_cannot_send_credentials_to_other_hosts(path):
    from blueprint.prusalink_http import Client

    client = Client(
        {"prusalink_url": "http://127.0.0.1:1", "prusalink_api_key": "secret"}
    )
    with pytest.raises(ValueError, match="outside this printer"):
        client.get(path)


def test_password_can_be_used_as_api_key(printer, tmp_path):
    settings, _ = printer()
    settings["prusalink_password_secret"] = settings.pop("prusalink_api_key_secret")
    assert build_job_prusalink(settings, {}, {}, tmp_path).title == "Cube"


def test_zero_progress_uses_zero_z(printer, tmp_path):
    settings, _ = printer(progress=0, axis_z=0)
    job = build_job_prusalink(settings, {}, {}, tmp_path)
    full = prepare_job_prusalink(job, settings, tmp_path)
    assert full.layer == 0 and full.progress == 0
