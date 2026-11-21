"""OctoPrint REST source. Telemetry is bounded; sliced files are prepared by the worker.

Uses the same printer/job endpoints, X-Api-Key and offline/state mapping as
https://github.com/dmellok/tesserae-widget-octoprint (octoprint_status).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from urllib.parse import quote, urlsplit

from .files import prune, write_atomic
from .model import PrintJob, SliceFacts
from .plain_gcode import read_gcode

log = logging.getLogger(__name__)

TELEMETRY_TTL_S = 10.0
TELEMETRY_BUDGET_S = 2.0
FILE_RETRY_S = 60.0
_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="blueprint-octoprint")
_LOCK = threading.Lock()
_TELEMETRY: dict = {}
_SLICES: dict[str, SliceFacts] = {}
_RETRY: dict[str, float] = {}
_RUNS: dict[str, dict] = {}


class _LocalRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward an API key to another host through a file/plugin redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if (
            urlsplit(req.full_url).netloc != urlsplit(newurl).netloc
            or urlsplit(req.full_url).scheme != urlsplit(newurl).scheme
        ):
            raise ValueError("OctoPrint redirected outside its server")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, base: str, key: str, budget: float):
        self.base, self.key = base.rstrip("/"), key
        self.deadline = time.monotonic() + budget

    def get(self, path: str, *, limit: int | None = None) -> bytes:
        # Plugin thumbnail and file refs can be absolute. Resolve only same-origin URLs;
        # files served behind a reverse proxy often report a different internal origin.
        target = urlsplit(path)
        if target.netloc:
            if (target.scheme, target.netloc) != (
                urlsplit(self.base).scheme,
                urlsplit(self.base).netloc,
            ):
                raise ValueError("OctoPrint file URL is outside its server")
            url = path
        else:
            url = self.base + "/" + path.lstrip("/")
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("OctoPrint request budget exhausted")
        request = urllib.request.Request(
            url,
            headers={
                "X-Api-Key": self.key,
                "User-Agent": "tesserae/0.1 (+print_blueprint)",
            },
        )
        with urllib.request.build_opener(_LocalRedirect()).open(
            request, timeout=min(left, 10.0)
        ) as response:
            chunks = []
            size = 0
            while True:
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("OctoPrint request budget exhausted")
                chunk = response.read1(
                    min(1024 * 1024, limit + 1 - size) if limit else 1024 * 1024
                )
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if limit and size > limit:
                    raise ValueError("OctoPrint response is too large")

    def json(self, path: str) -> dict:
        value = json.loads(self.get(path, limit=2_000_000))
        if not isinstance(value, dict):
            raise TypeError("OctoPrint returned invalid data")
        return value


def _config(settings: dict) -> tuple[str, str]:
    base = str(settings.get("octoprint_url") or "").strip().rstrip("/")
    key = str(
        settings.get("octoprint_api_key_secret")
        or settings.get("octoprint_api_key")
        or ""
    ).strip()
    if not base:
        raise ValueError("Set the OctoPrint URL in widget settings.")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Set a valid http or https OctoPrint URL in widget settings.")
    if not key:
        raise ValueError("Set the OctoPrint API key in widget settings.")
    return base, key


def _poll(base, key):
    client = Client(base, key, TELEMETRY_BUDGET_S)
    try:
        printer = client.json("/api/printer?exclude=sd")
    except (OSError, ValueError, TypeError):
        return {}, {}  # 409 is normal when the serial link is disconnected
    try:
        job = client.json("/api/job")
    except (OSError, ValueError, TypeError):
        job = {}
    return printer, job


def _telemetry(base, key) -> tuple[dict, dict]:
    identity = hashlib.sha256((base + "|" + key).encode()).hexdigest()
    with _LOCK:
        entry = _TELEMETRY.get(identity)
        if entry is None:
            if len(_TELEMETRY) >= 16:
                _TELEMETRY.pop(next(iter(_TELEMETRY)))
            entry = {"when": 0.0, "data": ({}, {}), "future": None}
            _TELEMETRY[identity] = entry
        if (
            entry["future"] is None
            and time.monotonic() - entry["when"] >= TELEMETRY_TTL_S
        ):
            entry["future"] = _WORKERS.submit(_poll, base, key)
        future = entry["future"]
    if future is not None:
        try:
            data = future.result(timeout=TELEMETRY_BUDGET_S)
        except TimeoutError:
            return entry["data"]
        with _LOCK:
            # Keep the last file identity across a disconnect, but never its old temperatures.
            if not data[0] and entry["data"][1]:
                data = ({}, entry["data"][1])
            entry.update(when=time.monotonic(), data=data, future=None)
    return entry["data"]


def _num(value) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def _status(flags: dict, completion: float | None) -> str:
    if flags.get("error") or flags.get("closedOrError"):
        return "failed"
    if flags.get("printing") or flags.get("resuming"):
        return "running"
    if flags.get("paused") or flags.get("pausing"):
        return "pause"
    if flags.get("operational") and completion is not None and completion >= 100:
        return "finish"
    return "idle"


def _slice(
    client: Client, file: dict, identity: str, data_dir: Path, download: bool
) -> SliceFacts | None:
    cached = Path(data_dir) / "octoprint" / f"{identity}.gcode"
    memo = str(cached)
    with _LOCK:
        hit = _SLICES.get(memo)
        retry = _RETRY.get(memo, 0.0)
    if hit is not None or not download or time.monotonic() < retry:
        return hit
    try:
        metadata = {}
        if not cached.exists():
            origin = str(file.get("origin") or "local")
            path = quote(str(file.get("path") or file.get("name") or ""), safe="/")
            metadata = client.json(f"/api/files/{quote(origin, safe='')}/{path}")
            # The files API's download ref is the source of truth (SD files may have none).
            ref = (metadata.get("refs") or {}).get("download")
            if not ref:
                with _LOCK:
                    _RETRY[memo] = time.monotonic() + FILE_RETRY_S
                return None
            write_atomic(cached, client.get(str(ref)))
            prune(cached.parent, "*.gcode")
        facts = dataclasses.replace(read_gcode(cached.read_bytes()), source=memo)
        cover = cached.with_suffix(".png")
        if facts.preview_png is None:
            if cover.exists():
                facts = dataclasses.replace(facts, preview_png=cover.read_bytes())
            else:
                if not metadata:
                    origin = quote(str(file.get("origin") or "local"), safe="")
                    path = quote(
                        str(file.get("path") or file.get("name") or ""), safe="/"
                    )
                    try:
                        metadata = client.json(f"/api/files/{origin}/{path}")
                    except (OSError, ValueError, TypeError):
                        metadata = {}
                thumb = metadata.get("thumbnail") or (metadata.get("refs") or {}).get(
                    "thumbnail"
                )
                if isinstance(thumb, str):
                    try:
                        picture = client.get(thumb, limit=20_000_000)
                        write_atomic(cover, picture)
                        prune(cover.parent, "*.png")
                        facts = dataclasses.replace(facts, preview_png=picture)
                    except (OSError, ValueError, TypeError):
                        log.debug("print_blueprint: OctoPrint thumbnail unavailable")
        with _LOCK:
            if len(_SLICES) >= 4:
                _SLICES.pop(next(iter(_SLICES)))
            _SLICES[memo] = facts
        return facts
    except (OSError, ValueError, TypeError):
        with _LOCK:
            if len(_RETRY) >= 32:
                _RETRY.pop(next(iter(_RETRY)))
            _RETRY[memo] = time.monotonic() + FILE_RETRY_S
        return None


def _run_identity(
    data_dir: Path, prefix: str, file_key: str, status: str, elapsed: float | None
):
    """Keep a print's start stable and distinguish reprints of the same uploaded file."""
    path = Path(data_dir) / "octoprint" / f"{prefix}-run.json"
    live = status in ("running", "pause")
    with _LOCK:
        previous = _RUNS.get(str(path))
        if previous is None:
            try:
                previous = json.loads(path.read_text())
                if not isinstance(previous, dict):
                    previous = {}
            except (OSError, ValueError):
                previous = {}
        if status == "offline":
            live = previous.get("live", False)
        reset = (
            previous.get("file") != file_key
            or live
            and (
                not previous.get("live")
                or previous.get("start") is None
                and elapsed is not None
                or elapsed is not None
                and elapsed + 2 < previous.get("elapsed", 0)
            )
        )
        start = time.time() - elapsed if live and elapsed is not None else None
        current = {
            "file": file_key,
            "live": live,
            "elapsed": elapsed or 0,
            "start": start if reset else previous.get("start"),
        }
        changed = reset or previous.get("live") != live
        _RUNS[str(path)] = current
        if len(_RUNS) > 32:
            _RUNS.pop(next(iter(_RUNS)))
        if changed:
            write_atomic(path, json.dumps(current).encode())
    key = hashlib.sha1(f"{file_key}|{current['start']}".encode()).hexdigest()
    return key, current["start"]


def build_job_octoprint(settings, options, ctx, data_dir) -> PrintJob:
    """Build the shared job; ctx['allow_download'] is only set by Runner's prepare callback."""
    base, key = _config(settings)
    printer, raw = _telemetry(base, key)
    meta, progress = raw.get("job") or {}, raw.get("progress") or {}
    file = meta.get("file") or {}
    completion = _num(progress.get("completion"))
    status = (
        _status((printer.get("state") or {}).get("flags") or {}, completion)
        if printer
        else "offline"
    )
    prefix = "octoprint_" + hashlib.sha1(base.encode()).hexdigest()[:16]
    file_identity = hashlib.sha1(
        json.dumps(
            [
                base,
                file.get("origin"),
                file.get("path") or file.get("name"),
                file.get("date"),
                file.get("size"),
            ]
        ).encode()
    ).hexdigest()
    sl = (
        _slice(
            Client(base, key, min(30.0, ctx.get("_download_budget_s", 30.0))),
            file,
            file_identity,
            Path(data_dir),
            bool(ctx.get("allow_download")),
        )
        if file.get("name")
        else None
    )
    temps = printer.get("temperature") or {}

    def pair(name):
        temp = temps.get(name) or {}
        a, t = _num(temp.get("actual")), _num(temp.get("target"))
        return (a, t) if a is not None and t is not None else None

    elapsed, remaining = (
        _num(progress.get("printTime")),
        _num(progress.get("printTimeLeft")),
    )
    now = time.time()
    job_key, start = (
        _run_identity(Path(data_dir), prefix, file_identity, status, elapsed)
        if file.get("name")
        else (file_identity, None)
    )
    title = str(file.get("display") or file.get("name") or "")
    title = _stem(title)
    return PrintJob(
        prefix=prefix,
        brand="OCTOPRINT",
        model="",
        printer_name=urlsplit(base).hostname or "OctoPrint",
        serial="",
        title=title,
        status=status,
        progress=min(1.0, max(0.0, (completion or 0.0) / 100)),
        layer=None,
        layers=sl.layers if sl and sl.layers else None,
        start_ts=start,
        end_ts=now + remaining
        if remaining is not None and status in ("running", "pause")
        else None,
        nozzle_d=sl.nozzle_d if sl else None,
        nozzle_type=None,
        nozzle_temp=pair("tool0"),
        bed_temp=pair("bed"),
        chamber_c=_num((temps.get("chamber") or {}).get("actual")),
        bed_type=None,
        material=sl.material if sl else None,
        weight_g=sl.weight_g or None if sl else None,
        length_m=sl.length_m or None if sl else None,
        fans={},
        speed=None,
        usage_h=None,
        objects=None,
        skipped=None,
        ams=[],
        ams_humidity=None,
        tools=[],
        slice=sl,
        cover_png=None,
        job_key=job_key,
        has_3mf=bool(file.get("name") and file.get("origin") == "local"),
    )


def _stem(name: str) -> str:
    for suffix in (".gcode", ".gco", ".g"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name
