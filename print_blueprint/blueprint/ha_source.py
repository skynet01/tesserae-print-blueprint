"""Build a PrintJob from Home Assistant: entity states, the ha-bambulab print history and cover image.

Everything is discovered by entity suffix from the printer's prefix; every entity is optional and a
missing one leaves its field None / empty. No printer names, models or entity ids are assumed."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlsplit

from .files import prune, write_atomic
from .model import PrintJob, SliceFacts, Tool, Tray
from .threemf import read_3mf
from .units import to_c, to_m, to_mm

HistoryGet = Callable[[str], bytes]

_BAD = {"", "unknown", "unavailable", "none"}
_MODELS = re.compile(r"(?<![a-z0-9])(a1[ _-]?mini|a1|p1p|p1s|p2s|x1e|x1c|x1|h2d|h2s|h2c|h3c)(?![a-z0-9])", re.I)
_G = {"g": 1.0, "kg": 1000.0, "mg": 0.001, "oz": 28.3495, "lb": 453.592}


def printer_prefixes(states: list[dict]) -> list[tuple[str, str]]:
    """(prefix, label) for every `sensor.<prefix>_print_status`, sorted by label."""
    by_id = {s.get("entity_id"): s for s in states}
    out = []
    for eid in by_id:
        m = re.fullmatch(r"sensor\.(.+)_print_status", eid or "")
        if not m:
            continue
        p = m.group(1)
        name = (by_id.get(f"sensor.{p}_printer_name") or {}).get("state")
        label = name if name not in _BAD else None
        if not label:
            fn = (by_id[eid].get("attributes") or {}).get("friendly_name") or ""
            label = re.sub(r"\s*print status$", "", fn, flags=re.I).strip() or p
        out.append((p, label))
    return sorted(out, key=lambda t: t[1].lower())


def _natural(s: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def _ts(s) -> float | None:
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except (TypeError, ValueError):
        return None


def _stem(name: str) -> str:
    name = name.rsplit("/", 1)[-1]
    for ext in (".gcode.3mf", ".3mf", ".gcode"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
    return name


class _Ent:
    """Typed reads of one printer's entities."""

    def __init__(self, prefix: str, states: dict[str, dict]):
        self.p, self.states = prefix, states
        # entities of another printer whose prefix extends ours (x1c vs x1c_shop) are not ours
        found = [re.fullmatch(r"sensor\.(.+)_print_status", k) for k in states]
        self.others = tuple(f"sensor.{m.group(1)}_" for m in found
                            if m and m.group(1) != prefix and m.group(1).startswith(prefix + "_"))

    def get(self, key: str, dom: str = "sensor") -> dict | None:
        return self.states.get(f"{dom}.{self.p}_{key}")

    def text(self, key: str, dom: str = "sensor") -> str | None:
        e = self.get(key, dom)
        s = str(e.get("state", "")).strip() if e else ""
        return None if s.lower() in _BAD else s

    def attrs(self, key: str) -> dict:
        return (self.get(key) or {}).get("attributes") or {}

    def num(self, key: str) -> float | None:
        s = self.text(key)
        try:
            return float(s) if s is not None else None
        except ValueError:
            return None

    def unit(self, key: str) -> str | None:
        return self.attrs(key).get("unit_of_measurement")

    def temp(self, key: str) -> float | None:
        v = self.num(key)
        c = to_c(v, self.unit(key)) if v is not None else None
        return round(c, 2) if c is not None else None

    def pair(self, key: str) -> tuple[float, float] | None:
        a, t = self.temp(f"{key}_temperature"), self.temp(f"{key}_target_temperature")
        return (a, t) if a is not None and t is not None else None

    def match(self, pattern: str):
        """(match, entity) for every sensor of this printer whose key fullmatches `pattern`."""
        rx = re.compile(rf"sensor\.{re.escape(self.p)}_{pattern}")
        for k, e in self.states.items():
            m = rx.fullmatch(k)
            if m and not k.startswith(self.others):
                yield m, e


def _truthy(v) -> bool:
    return str(v).strip().lower() == "true"


def _trays(h: _Ent) -> list[Tray]:
    found: list[tuple[tuple, Tray]] = []
    for m, e in h.match(r"ams(?:_(\w+?))?_tray_(\d+)"):
        a = e.get("attributes") or {}
        empty = _truthy(a.get("empty"))
        found.append(((_natural(m.group(1) or ""), int(m.group(2))),
                      Tray("" if empty else str(a.get("type") or ""), str(a.get("color") or "")[:7],
                           _truthy(a.get("active")), empty)))
    found.sort(key=lambda t: t[0])
    trays = [t for _, t in found]
    for _, e in h.match(r"externalspool_external_spool"):
        a = e.get("attributes") or {}
        empty = _truthy(a.get("empty")) or str(e.get("state")).lower() == "empty"
        trays.append(Tray("" if empty else str(a.get("type") or ""), str(a.get("color") or "")[:7],
                          _truthy(a.get("active")), empty))
    return trays


def _humidity(h: _Ent) -> str | None:
    for m, e in sorted(h.match(r"ams(?:_\w+?)?_humidity_index"), key=lambda t: t[0].group(0)):
        try:
            return f"INDEX {float(e['state']):.0f} / 5"
        except (TypeError, ValueError):
            pass
    for m, e in sorted(h.match(r"ams(?:_\w+?)?(?:_zone_\d+)?_humidity"), key=lambda t: t[0].group(0)):
        try:
            return f"{float(e['state']):.0f} %"
        except (TypeError, ValueError):
            pass
    return None


def _tools(h: _Ent) -> list[Tool]:
    active = h.attrs("active_tray")
    tools = []
    for m, e in h.match(r"hotendrack_printtool_(\d+)"):
        n = int(m.group(1))
        a = e.get("attributes") or {}
        fil = str(a.get("filament_name") or "")
        color = str(a.get("color") or "")[:7]
        if fil.lower() in _BAD:
            fil = str(a.get("filament_type") or "")
        if fil.lower() in _BAD:
            fil = str(active.get("name") or "") if e.get("state") == "mounted" else ""
            color = str(active.get("color") or "")[:7] if fil else ""
        tools.append(Tool(n, str(e.get("state")), fil, h.temp(f"hotendrack_printtool_{n}_temperature"),
                          h.num(f"hotendrack_printtool_{n}_print_time"), color))
    return sorted(tools, key=lambda t: t.id)


def _fans(h: _Ent) -> dict[str, float]:
    fans = {}
    for m, e in h.match(r"(\w+?)_fan_speed"):
        if m.group(1) == "heatbreak":
            continue
        try:
            fans[m.group(1).replace("_", " ")] = float(e["state"])
        except (TypeError, ValueError):
            pass
    return fans


def _model(h: _Ent, history_model: str | None) -> str:
    if history_model:
        return history_model.upper()
    for src in (h.p, h.text("printer_name") or "", (h.get("print_status") or {}).get("attributes", {}).get("friendly_name", "")):
        m = _MODELS.search(str(src))
        if m:
            return re.sub(r"[ _-]+", " ", m.group(1)).upper()
    return ""


def _load_history(http_get: HistoryGet, serial: str) -> list[dict]:
    if not serial:
        return []
    try:
        data = json.loads(http_get(f"/api/bambu_lab/print_history?serial={quote(serial)}"))
        files = data.get("files") if isinstance(data, dict) else None
        files = [f for f in files or [] if isinstance(f, dict) and f.get("path")]
    except Exception:
        return []
    files = [f for f in files if not f.get("printer_serial") or f.get("printer_serial") == serial]
    return sorted(files, key=lambda f: _ts(f.get("modified")) or 0.0, reverse=True)


def _no_storage_signs(h: _Ent) -> bool:
    """The printer says it prints from the cloud or has no storage: any of these, read live.
    An unknown or unavailable state is not a sign."""
    return (
        (h.text("print_type") or "").lower() == "cloud"
        or (h.text("sd_card_status") or "").lower() in ("not_applicable", "missing")
        or (h.text("developer_lan_mode", "binary_sensor") or "").lower() == "off"
    )


def no_sliced_files(prefix: str, states: dict[str, dict], http_get: HistoryGet) -> bool:
    """True only when it is known that Home Assistant has no sliced file from this printer: the
    print history answered with none for its serial, and the printer shows a sign it never will
    (cloud prints, no storage card, LAN mode off). Anything unknown answers False."""
    h = _Ent(prefix, states)
    serial = h.text("serial_number") or ""
    if not serial or not _no_storage_signs(h):
        return False
    try:
        data = json.loads(http_get(f"/api/bambu_lab/print_history?serial={quote(serial)}"))
    except Exception:
        return False
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return False
    return not any(
        isinstance(f, dict) and f.get("path") and (not f.get("printer_serial") or f.get("printer_serial") == serial)
        for f in files
    )


def _find_file(files: list[dict], wanted: set[str]) -> dict | None:
    wanted = {w for w in wanted if w}
    for f in files:  # newest first
        stem = re.sub(r"^\d+-", "", _stem(str(f.get("filename") or f["path"])))
        if stem in wanted:
            return f
    return None


_PARSED: dict[tuple, SliceFacts] = {}  # parsed 3MFs by (path, mtime, size); a G-code header is slow to re-read
_PARSED_LOCK = threading.Lock()


def _slice_for(entry: dict, http_get: HistoryGet, cache_dir: Path, allow_download: bool = True) -> SliceFacts | None:
    """The 3MF's facts. With allow_download=False (the request thread) nothing is downloaded or
    parsed: only a 3MF the background thread already parsed is returned."""
    path = str(entry["path"])
    key = hashlib.sha1(f"{path}{entry.get('size', '')}".encode()).hexdigest()
    cached = Path(cache_dir) / "3mf" / f"{key}.3mf"
    try:
        if not cached.exists():
            if not allow_download:
                return None
            data = http_get("/api/bambu_lab/file_cache/" + quote(path, safe="/"))
            write_atomic(cached, data)
            prune(cached.parent, "*.3mf")
        st = cached.stat()
        memo = (str(cached), st.st_mtime_ns, st.st_size)
        with _PARSED_LOCK:
            hit = _PARSED.get(memo)
        if hit is not None or not allow_download:
            return hit
        try:
            facts = dataclasses.replace(read_3mf(cached.read_bytes()), source=":".join(str(v) for v in memo))
        except Exception:
            cached.unlink(missing_ok=True)  # corrupt or partial: fetch it again next time
            return None
        with _PARSED_LOCK:
            if len(_PARSED) >= 4:
                _PARSED.pop(next(iter(_PARSED)))
            _PARSED[memo] = facts
        return facts
    except Exception:
        return None


def _cover(h: _Ent, http_get: HistoryGet, cache_dir: Path, job_key: str | None) -> bytes | None:
    """The cover picture, kept per print on disk: Home Assistant drops it (state unavailable,
    image_proxy answers 500) when the integration reloads mid-print, and never gets it back.
    Without a print identity (job_key None) nothing is saved or reused."""
    saved = Path(cache_dir) / "covers" / f"{job_key or 'none'}.png"
    ent = h.get("cover_image", "image") or {}
    pic = (ent.get("attributes") or {}).get("entity_picture")
    data = None
    if pic and str(ent.get("state", "")).strip().lower() not in _BAD:
        u = urlsplit(str(pic))
        try:
            data = http_get(u.path + (f"?{u.query}" if u.query else "")) or None
        except Exception:
            data = None
    if job_key is None:
        return data
    if data:
        try:
            if not saved.exists() or saved.read_bytes() != data:
                write_atomic(saved, data)
                prune(saved.parent, "*.png")
        except OSError:
            pass
        return data
    try:
        return saved.read_bytes()
    except OSError:
        return None


def _weight_g(h: _Ent) -> float | None:
    v = h.num("print_weight")
    return round(v * _G.get((h.unit("print_weight") or "g").lower(), 1.0), 2) if v else None


def _rounded(v: float | None, nd: int) -> float | None:
    return round(v, nd) if v is not None else None


def build_job(prefix: str, states: dict[str, dict], http_get: HistoryGet, cache_dir: Path,
              allow_download: bool = True) -> PrintJob:
    h = _Ent(prefix, states)
    serial = h.text("serial_number") or ""
    task = h.text("task_name") or ""
    gname = h.text("gcode_filename") or ""
    status = h.text("print_status") or ""
    start = h.text("start_time") or ""

    files = _load_history(http_get, serial)
    entry = _find_file(files, {_stem(gname), task})
    sl = _slice_for(entry, http_get, cache_dir, allow_download) if entry else None
    hist_model = next((f.get("printer_model") for f in files if f.get("printer_model")), None)
    # with a serial the key is unchanged (cached sheets and AI art keep matching); without one the
    # printer prefix keeps two printers printing the same name apart
    job_key = hashlib.sha1((serial + task + start if serial else f"{prefix}|{task}|{start}").encode()).hexdigest()
    cover = _cover(h, http_get, cache_dir, job_key if serial + task + start else None) if sl is None or sl.preview_png is None else None

    layer, layers = h.num("current_layer"), h.num("total_layer_count")
    if layer is not None and layers and layers > 0:
        progress = layer / layers
    else:
        progress = (h.num("print_progress") or 0.0) / 100
    progress = 1.0 if status == "finish" else min(1.0, max(0.0, progress))

    nd, length, objs, skipped = h.num("nozzle_size"), h.num("print_length"), h.num("printable_objects"), h.num("skipped_objects")
    active = h.attrs("active_tray")
    return PrintJob(
        prefix=prefix, model=_model(h, hist_model), printer_name=h.text("printer_name") or "", serial=serial,
        title=task or _stem(gname), status=status, progress=progress,
        layer=int(layer) or None if layer is not None else None,
        layers=int(layers) or None if layers is not None else None,
        start_ts=_ts(start), end_ts=_ts(h.text("end_time")),
        nozzle_d=_rounded(to_mm(nd, h.unit("nozzle_size")), 3) if nd else None, nozzle_type=h.text("nozzle_type"),
        nozzle_temp=h.pair("nozzle"), bed_temp=h.pair("bed"), chamber_c=h.temp("chamber_temperature"),
        bed_type=h.text("print_bed_type"), material=active.get("name") or None,
        weight_g=_weight_g(h), length_m=_rounded(to_m(length, h.unit("print_length")), 3) if length else None,
        fans=_fans(h), speed=h.text("speed_profile"), usage_h=h.num("total_usage"),
        objects=int(objs) if objs is not None else None, skipped=int(skipped) if skipped is not None else None,
        ams=_trays(h), ams_humidity=_humidity(h), tools=_tools(h), slice=sl, cover_png=cover,
        job_key=job_key, has_3mf=entry is not None,
    )
