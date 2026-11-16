"""Offline PrintJobs for layout tests: a 3MF (+ optional Home Assistant states) or a generated cover.

Jobs are built by `blueprint.ha_source.build_job` against a fake Home Assistant (`http_get`), so the
corpus exercises the real mapping: temperatures, fans, AMS trays, tool rack, 3MF lookup and cover."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote

from blueprint.ha_source import build_job
from blueprint.model import PrintJob

FIX = Path(__file__).parent / "fixtures"
CORPUS = Path(os.environ.get("PB_CORPUS", FIX))
SYNTHETIC_COVER = FIX / "synthetic-cover.png"


def corpus_files() -> list[Path]:
    return sorted(CORPUS.glob("*.3mf")) if CORPUS.is_dir() else []


def load_states(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def _prefix(states: dict) -> str:
    for k in states:
        m = re.fullmatch(r"sensor\.(.+)_print_status", k)
        if m:
            return m.group(1)
    raise ValueError("no print_status sensor")


def fake_ha(files: dict[str, bytes] | None = None, model: str = "", serial: str = "", cover: bytes | None = None):
    """An `http_get` serving print_history for `files` {filename: 3MF bytes}, their downloads and a cover."""
    entries = [{"filename": n, "path": f"{serial}/prints/cache/{n}", "size": len(b), "printer_serial": serial,
                "printer_model": model, "modified": f"2026-11-14T00:{i:02d}:00"}
               for i, (n, b) in enumerate((files or {}).items())]
    blobs = {e["path"]: files[e["filename"]] for e in entries}

    def get(path: str) -> bytes:
        if path.startswith("/api/bambu_lab/print_history"):
            return json.dumps({"files": entries}).encode()
        if path.startswith("/api/bambu_lab/file_cache/"):
            return blobs[unquote(path[len("/api/bambu_lab/file_cache/"):])]
        if path.startswith("/api/image_proxy/") and cover is not None:
            return cover
        raise OSError(f"404 {path}")
    return get


def _build(states: dict, http_get) -> PrintJob:
    return build_job(_prefix(states), states, http_get, Path(tempfile.mkdtemp(prefix="pb-corpus-")))


_LIVE = ("prefix", "printer_name", "serial", "status", "start_ts", "end_ts", "nozzle_d", "nozzle_type", "nozzle_temp",
         "bed_temp", "chamber_c", "bed_type", "material", "weight_g", "length_m", "fans", "speed", "usage_h",
         "objects", "skipped", "ams", "ams_humidity", "tools", "progress", "layer", "layers")


def ha_fields(states: dict) -> dict:
    """PrintJob keyword arguments that come from the live printer."""
    job = _build(states, fake_ha())
    return {k: getattr(job, k) for k in _LIVE}


_STATES = {"p1s": "p1s_states.json", "h3c": "h3c_states.json"}


def job_from_3mf(path: Path, states: str | None = "p1s", progress: float | None = None,
                 model: str = "P1S") -> PrintJob:
    """A job for a corpus 3MF; live fields from the named states fixture, slice-derived counts."""
    path = Path(path)
    stem = path.stem
    m = re.match(r"^(\d+)-(.*)$", stem)
    pid, task = (m.group(1), m.group(2)) if m else (stem, stem)
    st = json.loads(json.dumps(load_states(_STATES[states]))) if states else {
        "sensor.printer_print_status": {"state": "finish", "attributes": {}}}
    p = _prefix(st)
    serial = "CORPUS"
    for key, val in (("serial_number", serial), ("task_name", task), ("gcode_filename", task + ".3mf")):
        st[f"sensor.{p}_{key}"] = {"state": val, "attributes": {}}
    job = _build(st, fake_ha({path.name: path.read_bytes()}, model=model, serial=serial))
    facts = job.slice
    f: dict = {"job_key": pid}
    if progress is not None:
        f["progress"] = progress
        f["status"] = "finish" if progress >= 1 else "running"
    prog = f.get("progress", job.progress)
    layers = (facts.layers or None) if facts else None
    f["layers"] = layers
    f["layer"] = max(1, round(layers * prog)) if layers else None
    start = job.start_ts
    if start and facts and facts.prediction_s > 0:
        f["end_ts"] = start + facts.prediction_s
    if facts:
        f["weight_g"], f["length_m"] = facts.weight_g or job.weight_g, facts.length_m or job.length_m
    return dataclasses.replace(job, **f)


def h3c_job(progress: float | None = None) -> PrintJob:
    """A synthetic H3C print without a cached 3MF: generated cover only."""
    job = _build(load_states("h3c_states.json"), fake_ha(model="H3C", serial="H3C", cover=SYNTHETIC_COVER.read_bytes()))
    f: dict = {"model": "H3C", "title": "Synthetic_assembly_plate_1", "job_key": "h3c-live"}
    if progress is not None:
        f["progress"] = progress
    return dataclasses.replace(job, **f)
