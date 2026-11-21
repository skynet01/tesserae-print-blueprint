"""PrusaLink v1 telemetry and current sliced file, adapted to the existing PrintJob.

Telemetry is polled at most every 20 seconds. The caller waits at most two
seconds; slow reads finish on a bounded worker pool. File download and parsing
run only in the sheet Runner's prepare function. No Home Assistant is needed.
"""

from __future__ import annotations

import bisect
import concurrent.futures
import dataclasses
import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path

from .bgcode import decode_bgcode
from .files import prune, write_atomic
from .model import PrintJob, SliceFacts
from .plain_gcode import read_gcode
from .prusalink_http import AuthError, Client

POLL_S = 20.0
MAX_FILE_BYTES = 64 * 1024 * 1024
_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="pb-prusa"
)
_LOCK = threading.Lock()
_SOURCES: dict[str, dict] = {}
_PARSED: dict[str, SliceFacts] = {}


def _identity(settings):
    client = Client(settings)
    identity = hashlib.sha256(
        json.dumps([client.base, client.username, client.password, client.key]).encode()
    ).hexdigest()
    return identity, client


def _num(value):
    try:
        n = float(value)
        return n if not isinstance(value, bool) and math.isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _pair(printer, name):
    a, b = _num(printer.get("temp_" + name)), _num(printer.get("target_" + name))
    return (a, b) if a is not None and b is not None else None


def _poll(client, previous):
    status = client.json("/api/v1/status")
    if not isinstance(status.get("printer"), dict):
        raise ValueError("That address answered, but not as a PrusaLink printer.")
    telemetry = status.get("job")
    doc = {}
    if isinstance(telemetry, dict) and telemetry:
        try:
            doc = client.json("/api/v1/job")
        except AuthError:
            raise
        except (OSError, ValueError):
            # Firmware may remove /job after a print finishes. Never attach another job's file.
            old = previous.get("doc", {})
            if old.get("id") == telemetry.get("id"):
                doc = old
    info = previous.get("info", {})
    info_at = previous.get("info_at", 0)
    if not info or time.monotonic() - info_at > 3600:
        try:
            info = client.json("/api/v1/info")
            info_at = time.monotonic()
        except (OSError, ValueError):
            pass
    return {
        "status": status,
        "doc": doc,
        "info": info,
        "info_at": info_at,
        "now": time.time(),
    }


def _snapshot(identity, client, budget):
    with _LOCK:
        state = _SOURCES.get(identity)
        if state is None:
            # Bound both retained credentials/telemetry and queued work, including on settings edits.
            if len(_SOURCES) >= 8:
                victim = next(
                    (
                        k
                        for k, s in _SOURCES.items()
                        if s.get("future") is None or s["future"].done()
                    ),
                    None,
                )
                if victim is None:
                    raise OSError("PrusaLink is busy; try again shortly.")
                del _SOURCES[victim]
            state = _SOURCES[identity] = {"at": 0, "snapshot": {}}
        future = state.get("future")
        if future is None or (
            future.done() and time.monotonic() - state["at"] >= POLL_S
        ):
            state["at"] = time.monotonic()
            future = state["future"] = _POOL.submit(_poll, client, state["snapshot"])
    try:
        snapshot = future.result(timeout=max(0, min(2, budget)))
    except concurrent.futures.TimeoutError:
        raise OSError("PrusaLink is offline or slow to answer right now.") from None
    except AuthError:
        raise
    except (OSError, TimeoutError):
        raise OSError("PrusaLink is offline or unreachable right now.") from None
    with _LOCK:
        state["snapshot"] = snapshot
    return snapshot


def _file(doc):
    value = doc.get("file")
    return value if isinstance(value, dict) else {}


def _file_key(identity, meta):
    refs = meta.get("refs") or {}
    return hashlib.sha256(
        json.dumps(
            [
                identity,
                meta.get("path"),
                refs.get("download"),
                meta.get("size"),
                meta.get("m_timestamp"),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _enrich(job, facts, printer):
    z = _num(printer.get("axis_z"))
    layer = None
    if z is not None and facts.layer_z:
        layer = max(0, min(facts.layers, bisect.bisect_right(facts.layer_z, z + 0.001)))
    model = re.search(r"^;\s*printer_model\s*=\s*(.+)", facts.gcode, re.M)
    return dataclasses.replace(
        job,
        slice=facts,
        layers=facts.layers or None,
        model=model[1].strip() if model else job.model,
        layer=facts.layers if job.status == "finish" else layer,
        nozzle_d=facts.nozzle_d or job.nozzle_d,
        material=facts.material or job.material,
        weight_g=facts.weight_g or None,
        length_m=facts.length_m or None,
    )


def build_job_prusalink(settings, options, ctx, data_dir) -> PrintJob:
    identity, client = _identity(settings)
    snapshot = _snapshot(identity, client, (ctx or {}).get("_prusalink_budget_s", 2.0))
    status, doc, info = snapshot["status"], snapshot["doc"], snapshot["info"]
    printer, telemetry = status["printer"], status.get("job") or {}
    meta = _file(doc)
    # /job and /status are separate reads: a changed id must never cross-wire files and telemetry.
    if (
        doc.get("id") is not None
        and telemetry.get("id") is not None
        and doc["id"] != telemetry["id"]
    ):
        meta = {}
    state = str(printer.get("state") or "").upper()
    phase = {
        "PRINTING": "running",
        "PAUSED": "pause",
        "ATTENTION": "pause",
        "FINISHED": "finish",
        "STOPPED": "failed",
        "ERROR": "failed",
    }.get(state, "idle")
    progress = _num(telemetry.get("progress", doc.get("progress"))) or 0
    elapsed = _num(telemetry.get("time_printing", doc.get("time_printing")))
    remaining = _num(telemetry.get("time_remaining", doc.get("time_remaining")))
    now = snapshot["now"]
    name = str(meta.get("display_name") or meta.get("name") or "")
    title = name.rsplit("/", 1)[-1]
    for ext in (".bgcode", ".gcode", ".gco", ".g", ".bgc"):
        if title.lower().endswith(ext):
            title = title[: -len(ext)]
            break
    file_key = _file_key(identity, meta)
    job_key = hashlib.sha1(
        f"{identity}|{telemetry.get('id', doc.get('id'))}|{file_key}".encode()
    ).hexdigest()
    prefix = "prusalink_" + hashlib.sha1(client.base.encode()).hexdigest()[:16]
    job = PrintJob(
        prefix=prefix,
        brand="PRUSA",
        model=str(
            (meta.get("meta") or {}).get("printer_model")
            or info.get("printer_model")
            or ""
        ),
        printer_name=str(info.get("name") or "Prusa"),
        serial=str(info.get("serial") or ""),
        title=title,
        status=phase,
        progress=1.0 if phase == "finish" else min(1.0, max(0.0, progress / 100)),
        layer=None,
        layers=None,
        start_ts=now - elapsed if elapsed is not None and elapsed >= 0 else None,
        end_ts=(now if phase == "finish" else now + remaining)
        if remaining is not None and phase in ("running", "finish")
        else None,
        nozzle_d=_num(info.get("nozzle_diameter")),
        nozzle_type=None,
        nozzle_temp=_pair(printer, "nozzle"),
        bed_temp=_pair(printer, "bed"),
        chamber_c=None,
        bed_type=None,
        material=None,
        weight_g=None,
        length_m=None,
        fans={},
        speed=f"{printer['speed']} %"
        if _num(printer.get("speed")) is not None
        else None,
        usage_h=None,
        objects=None,
        skipped=None,
        ams=[],
        ams_humidity=None,
        tools=[],
        slice=None,
        cover_png=None,
        job_key=job_key,
        has_3mf=bool((meta.get("refs") or {}).get("download")),
    )
    with _LOCK:
        _SOURCES[identity].setdefault("jobs", {})[job_key] = (meta, printer)
        jobs = _SOURCES[identity]["jobs"]
        if len(jobs) > 12:
            del jobs[next(iter(jobs))]
        facts = _PARSED.get(file_key)
    try:
        job.cover_png = (Path(data_dir) / "prusalink" / f"{file_key}.png").read_bytes()
    except OSError:
        pass
    return _enrich(job, facts, printer) if facts else job


def prepare_job_prusalink(job, settings, data_dir) -> PrintJob:
    """Runner prepare_fn: downloads once per file revision and never changes print identity."""
    identity, client = _identity(settings)
    client.deadline = time.monotonic() + 120
    with _LOCK:
        state = _SOURCES.get(identity, {})
        meta, printer = state.get("jobs", {}).get(job.job_key, ({}, {}))
        key = _file_key(identity, meta)
        facts = _PARSED.get(key)
        retry_at = state.get("retry", {}).get(key, 0)

    def with_cover(current):
        if (
            (current.slice and current.slice.preview_png)
            or current.cover_png
            or not refs.get("thumbnail")
        ):
            return current
        cover_path = Path(data_dir) / "prusalink" / f"{key}.png"
        try:
            cover = (
                cover_path.read_bytes()
                if cover_path.exists()
                else client.get(str(refs["thumbnail"]), 1_000_000)
            )
            if cover.startswith((b"\x89PNG", b"\xff\xd8\xff")):
                if not cover_path.exists():
                    write_atomic(cover_path, cover)
                    prune(cover_path.parent, "*.png")
                return dataclasses.replace(current, cover_png=cover)
        except (OSError, ValueError):
            pass
        return current

    refs = meta.get("refs") or {}
    if facts:
        return with_cover(_enrich(job, facts, printer))
    if time.monotonic() < retry_at:
        return job
    cache = Path(data_dir) / "prusalink" / f"{key}.gcode"
    try:
        if cache.exists():
            data = cache.read_bytes()
        elif refs.get("download"):
            raw = client.get(str(refs["download"]), MAX_FILE_BYTES)
            data = decode_bgcode(raw) if raw.startswith(b"GCDE") else raw
        else:
            data = None
        if data:
            facts = dataclasses.replace(read_gcode(data), source=key)
            if not cache.exists():
                write_atomic(cache, data)
                prune(cache.parent, "*.gcode")
            with _LOCK:
                if len(_PARSED) >= 4:
                    _PARSED.pop(next(iter(_PARSED)))
                _PARSED[key] = facts
            return with_cover(_enrich(job, facts, printer))
    except (OSError, ValueError):
        cache.unlink(missing_ok=True)
        with _LOCK:
            state.setdefault("retry", {})[key] = time.monotonic() + 60
    # Firmware may offer a preview without a downloadable sliced file.
    return with_cover(job)
