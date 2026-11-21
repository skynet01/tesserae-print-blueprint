"""print_blueprint: what a Bambu Lab printer is printing, drawn as a blueprint sheet.

The slow work stays outside fetch():

* ``fetch()`` reads Home Assistant through Tesserae's ha_core (states are cached
  there), builds the print job without downloading anything, and returns the
  newest sheet on disk. On every render (push, warm-ahead, editor or deck preview)
  it asks the background runner for the sheet this cell needs (only settled,
  non-preview renders may spend kie.ai credits). Until that lands it shows the newest sheet of the same print,
  size and options, else draws the cheap line-art sheet inline, and says ``live_pending``.
* For a real screen whose AI picture is pending, the browser gives the queued sheet
  15 extra seconds during composition before using line art. Previews never wait.
* ``blueprint.jobs.Runner`` downloads the sliced 3MF, renders the toolpaths and
  (optionally) the kie.ai engraving on one daemon thread.
* ``/plugins/print_blueprint/image/<name>.png`` serves the sheets (render safe,
  immutable); ``/plugins/print_blueprint/compare`` shows the three picture
  methods side by side for the current print.

Printers are discovered by the ``sensor.*_print_status`` suffix; nothing here
names a printer, an entity or a device.

The renderer package beside this file is imported relatively: the plugin folder
is registered as a package under the name the host gave this module, since the
host loads ``server.py`` by path (Tesserae docs/widgets.md, "Helper modules
beside server.py").
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import html
import importlib.machinery
import importlib.util
import io
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

_HERE = Path(__file__).resolve().parent


def _register_package() -> None:
    """Make ``from .blueprint import x`` work when the host loaded this file by path as
    ``_tesserae_plugins.<id>.server``: the plugin folder becomes that package. Helpers cached
    from an earlier load are dropped so an in-place reload picks up their new code too."""
    package = __name__.rpartition(".")[0]
    root = package.partition(".")[0]
    if root != "_tesserae_plugins":
        return
    for name in [m for m in sys.modules if m.startswith(package + ".") and m != __name__]:
        del sys.modules[name]
    for name, search in ((root, []), (package, [str(_HERE)])):
        if name == root and name in sys.modules:
            continue
        spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
        spec.submodule_search_locations = search
        sys.modules[name] = importlib.util.module_from_spec(spec)


_register_package()

from .blueprint.preview_progress import apply_progress  # noqa: E402
from .blueprint.art import Art, ai_art, preview_art, toolpath_art, walls_for  # noqa: E402
from .blueprint.files import png_bytes, prune, write_atomic  # noqa: E402
from .blueprint.example import EXAMPLE_NOTE, EXAMPLE_REV, render_example  # noqa: E402
from .blueprint.ha_source import build_job, no_sliced_files, printer_prefixes  # noqa: E402
from .blueprint.jobs import RenderState, Runner, cache_key, family_of  # noqa: E402,F401  (RenderState: tests)
from .blueprint.kie import KieClient  # noqa: E402,F401  (named so a test can prove compare never uses it)
from .blueprint.layout import LIVE  # noqa: E402
from .blueprint.model import PrintJob, SheetOptions  # noqa: E402
from .blueprint.render import TWO_TONE, render_sheet  # noqa: E402
from .blueprint.report import sheet_unit  # noqa: E402
from .blueprint.theme import glass, theme_for  # noqa: E402
from .blueprint import cell_look  # noqa: E402

log = logging.getLogger(__name__)

PLUGIN_ID = "print_blueprint"
RENDER_SAFE_ENDPOINTS: tuple[str, ...] = ("print_blueprint_admin.image", "print_blueprint_admin.example",
                                        "print_blueprint_admin.ready", "print_blueprint_admin.look",)

METHODS = ("toolpath", "preview", "ai")
BACKGROUNDS = ("blue", "white", "black")
PANELS = ("colour", "grey16", "grey4", "spectra", "bw")
LIVE_REFRESH_S = 20  # how often editors and push follow-ups look again while a sheet is being drawn
AI_WAIT_S = 15.0  # extra browser-compose time before line art; fetch() still has only six seconds
FETCH_BUDGET_S = 6.0  # the host's budget for fetch(), counted from its first line
HTTP_BUDGET_S = 2.0  # all Home Assistant GETs inside fetch() (print history, cover)
STATES_BUDGET_S = 3.0  # the Home Assistant states read; a slower answer finishes in the background
QUICK_MIN_S = 2.5  # the inline line-art sheet is only drawn with at least this much budget left
QUICK_PX = 2_000_000  # ...per this many pixels: a two-tone panel's quick sheet is drawn at full size
BACKGROUND_TIMEOUT_S = 120.0  # a 3MF download on the runner thread
COMPARE_TIMEOUT_S = 10.0  # the compare page holds a web worker while it downloads
INLINE_MAX = 1000  # the inline line-art sheet is drawn at most this big, then scaled by the client
# (not on two-tone panels: a quantised sheet scaled up has unreadable text, so it is drawn at full size)
DEFAULT_SIZE = (1200, 800)
SHEET_NAME = re.compile(r"[0-9a-f]{40}(?:-q)?\.png")
TOOLPATH_SETUP = ("In the Bambu Lab integration, give the printer its IP address and access code and keep models"
                  " to cache above 0. H2 and H3 printers also need Disable SSL verification (Advanced options).")
NOTE_NO_3MF = "No sliced file in Home Assistant yet: drawn from the preview picture"
NOTE_NO_KEY = "AI engraving needs a kie.ai API key: drawn from the preview picture"


# ---------------------------------------------------------------- Home Assistant access
def _core() -> Any:
    """Tesserae's ha_core server module, or None when it isn't installed."""
    try:
        from flask import current_app

        plugin = current_app.config["PLUGIN_REGISTRY"].get("ha_core")
    except Exception:
        return None
    return plugin.server_module if plugin is not None and plugin.server_module is not None else None


_MEMO: dict[str, tuple[float, bytes]] = {}
_MEMO_LOCK = threading.Lock()
_MEMO_TTL = (("/api/bambu_lab/print_history", 30.0), ("/api/image_proxy/", 120.0))


class HaHttp:
    """Authenticated GETs against Home Assistant that work off the request thread too: the
    URL, headers and TLS context are read from ha_core once, up front. Print history and
    cover pictures are memoised briefly so a polling editor doesn't ask HA every few seconds."""

    def __init__(self, base: str, headers: dict, ssl_context: Any, timeout: float, deadline: float | None):
        self.base, self.headers, self.ssl = base, dict(headers), ssl_context
        self.timeout, self.deadline = timeout, deadline

    def __call__(self, path: str) -> bytes:
        url = self.base + path
        ttl = next((t for p, t in _MEMO_TTL if path.startswith(p)), 0.0)
        if ttl:
            with _MEMO_LOCK:
                hit = _MEMO.get(url)
            if hit and time.monotonic() - hit[0] < ttl:
                return hit[1]
        timeout = self.timeout
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout < 0.3:
                raise TimeoutError("no time left in this render")
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ssl) as resp:
                data = resp.read()
        except urllib.error.HTTPError as err:
            err.close()  # an error response is an open socket (see ha_core._urlopen)
            raise
        if ttl:
            with _MEMO_LOCK:
                if len(_MEMO) > 32:
                    _MEMO.clear()
                _MEMO[url] = (time.monotonic(), data)
        return data


def _unreachable(path: str) -> bytes:
    raise OSError("Home Assistant is not configured")


def _http_for(core: Any, *, timeout: float, deadline: float | None = None):
    """An authenticated GET for Home Assistant, or one that always fails when ha_core can't say
    where Home Assistant is (no URL or token, an unreadable secret)."""
    ssl_ctx = None
    with_ssl = getattr(core, "_ssl_context", None)
    if callable(with_ssl):
        try:
            ssl_ctx = with_ssl()
        except Exception:
            ssl_ctx = None
    try:
        return HaHttp(core.base_url(), core._headers(), ssl_ctx, timeout, deadline)
    except Exception:
        log.warning("print_blueprint: no Home Assistant address from ha_core", exc_info=True)
        return _unreachable


_STATES_POOL = None
_SLOW = "Home Assistant is slow to answer right now."


def _states_within(core: Any, budget: float) -> list:
    """core.get_states(), or TimeoutError after `budget` seconds (the read goes on in the
    background and warms the core's cache for the next render)."""
    global _STATES_POOL
    import concurrent.futures

    if _STATES_POOL is None:
        _STATES_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="pb-states")
    import contextvars

    # the read runs on a pool thread: carry this render's context over (Flask's app context and
    # the widget's capability scope are context variables), or ha_core can't reach its settings
    future = _STATES_POOL.submit(contextvars.copy_context().run, core.get_states)
    try:
        return future.result(timeout=max(0.1, budget))
    except concurrent.futures.TimeoutError:
        raise TimeoutError(_SLOW) from None


def _friendly(core: Any, err: Exception) -> str:
    if isinstance(err, TimeoutError) and str(err) == _SLOW:
        return _SLOW
    try:
        return str(core.coerce_error(err))
    except Exception:
        return "Couldn't reach Home Assistant right now."


def _wanted(option: Any) -> str:
    """The printer option as a prefix; the print_status entity id is accepted too."""
    v = str(option or "").strip()
    m = re.fullmatch(r"sensor\.(.+)_print_status", v)
    return m.group(1) if m else v


def _states(states: list[dict], prefix: str) -> dict[str, dict]:
    """States by entity id, with this printer's print_status in lower case (HA may send FINISH)."""
    out = {s.get("entity_id"): s for s in states if isinstance(s, dict) and s.get("entity_id")}
    key = f"sensor.{prefix}_print_status"
    if key in out and isinstance(out[key].get("state"), str):
        out[key] = dict(out[key], state=out[key]["state"].strip().lower())
    return out


def current_job(core: Any, prefix: str, data_dir: Path, *, allow_download: bool = True) -> PrintJob:
    """The printer's job now (the compare page; fetch() builds its own without downloads)."""
    states = _states(core.get_states(), prefix)
    http = _http_for(core, timeout=COMPARE_TIMEOUT_S, deadline=time.monotonic() + 2 * COMPARE_TIMEOUT_S)
    return build_job(prefix, states, http, Path(data_dir), allow_download=allow_download)


# ---------------------------------------------------------------- refresh button: background cycle
CYCLE_ACTION = "print_blueprint_cycle"
CYCLE = ("white", "blue", "black")  # a press moves to the next one
CYCLE_FILE = "paper.json"  # {device_id: {base: cell Paper, pressed: button colour}}; old files are ignored
_CYCLE_LOCK = threading.Lock()
_DATA_DIRS: list[Path] = []  # the data dir fetch() last saw (the action has no ctx)


def _read_map(path: Path) -> dict:
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _next_background(current: str) -> str:
    return CYCLE[(CYCLE.index(current) + 1) % len(CYCLE)] if current in CYCLE else CYCLE[0]


def cycle_background(data_dir: Path, device_id: str) -> str:
    """Move `device_id` to the next background (from what it last showed on the first press); returns it."""
    data_dir = Path(data_dir)
    with _CYCLE_LOCK:
        cycle = _read_map(data_dir / CYCLE_FILE)
        record = cycle.get(device_id) or {"base": None, "pressed": ""}
        record["pressed"] = new = _next_background(record["pressed"])
        cycle[device_id] = record
        write_atomic(data_dir / CYCLE_FILE, json.dumps(cycle).encode())
    return new


def _paper_background(data_dir: Path, device_id: str, background: str) -> str:
    """A real render resets a button override when the cell's saved Paper changes."""
    path = Path(data_dir) / CYCLE_FILE
    with _CYCLE_LOCK:
        records = _read_map(path)
        record = records.get(device_id)
        if not isinstance(record, dict) or record.get("pressed") not in BACKGROUNDS \
                or record.get("base") not in (None, background):
            record = {"base": background, "pressed": background}
        else:
            record = {**record, "base": background}
        if records.get(device_id) != record:
            records[device_id] = record
            write_atomic(path, json.dumps(records).encode())
        return record["pressed"]


def _note_cycling(data_dir: Path, device_id: str, enabled: bool) -> None:
    """Remember, per device, whether its cell lets the refresh button cycle the background:
    the button action has no cell options, so each real render leaves the answer here."""
    path = Path(data_dir) / CYCLE_FILE
    with _CYCLE_LOCK:
        records = _read_map(path)
        record = records.get(device_id)
        record = dict(record) if isinstance(record, dict) else {}
        if enabled:
            if "cycle" not in record:
                return
            record.pop("cycle")
        elif record.get("cycle") is False:
            return
        else:
            record["cycle"] = False
        records[device_id] = record
        write_atomic(path, json.dumps(records).encode())


def _cycling_enabled(data_dir: Path, device_id: str) -> bool:
    record = _read_map(Path(data_dir) / CYCLE_FILE).get(device_id)
    return not (isinstance(record, dict) and record.get("cycle") is False)


def _cycle_dir() -> Path | None:
    if _DATA_DIRS:
        return _DATA_DIRS[-1]
    try:
        return _data_dir()
    except Exception:
        return None


def _cycle_action(ctx: Any, _arg: str | None) -> Any:
    from app.button_actions import ActionResult

    data_dir = _cycle_dir()
    if data_dir is None or not ctx.device_id:
        raise ValueError("print_blueprint_cycle: no data dir or device")
    if not _cycling_enabled(data_dir, ctx.device_id):
        # The cell turned the cycle off: a press just redraws the frame with the latest sensor data.
        return ActionResult(new_step_index=ctx.current_step_index, target_page_id=None, force_refresh=True,
                            description="print_blueprint_cycle off -> refresh")
    new = cycle_background(data_dir, ctx.device_id)
    # Repaint the page on the glass. Without a target page Tesserae re-pushes the lineup's step
    # and holds it until the next morning, so the display stayed on a print page after the print.
    return ActionResult(new_step_index=ctx.current_step_index, target_page_id=_page_on_glass(ctx.device_id),
                        force_refresh=True, description=f"print_blueprint_cycle -> {new}")


def _page_on_glass(device_id: str) -> str | None:
    """The page the device last downloaded (a background warm doesn't count), else None."""
    try:
        from flask import current_app

        pusher = current_app.config.get("PUSH_MANAGER")
        latest = getattr(pusher, "latest_render_for", None)
        rec = latest(device_id) if callable(latest) else None
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None
    page = rec.get("last_served_page_id") or rec.get("page_id")
    return page if isinstance(page, str) and page else None


def _register_action() -> None:
    try:
        from app.button_actions import register
    except Exception:  # not running inside Tesserae, or a Tesserae without button actions
        return
    try:
        register(CYCLE_ACTION, _cycle_action)
    except Exception:
        log.warning("print_blueprint: couldn't register %s", CYCLE_ACTION, exc_info=True)


# ---------------------------------------------------------------- options and panel
def _bool(v: Any, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default


def _grid_option(value: Any) -> bool | None:
    # The old True flag was ignored on white/black paper, so it means paper default.
    raw = str(value).strip().lower()
    if raw == "on":
        return True
    if raw in ("false", "off", "0", "no"):
        return False
    return None


def sheet_options(options: dict, panel: str) -> SheetOptions:
    method = str(options.get("method") or "toolpath")
    bg = str(options.get("background") or "blue")
    return SheetOptions(
        method=method if method in METHODS else "toolpath",
        background=bg if bg in BACKGROUNDS else "blue",
        panel=panel if panel in PANELS else "colour",
        show_icons=_bool(options.get("show_icons"), True),
        callout_notes=_bool(options.get("callout_notes"), True),
        fill_space=_bool(options.get("fill_space"), True),
        show_grid=_grid_option(options.get("show_grid")),
        show_subtitle=_bool(options.get("show_subtitle"), True),
        show_progress=_bool(options.get("show_progress"), True),
    )


def panel_for_gamut(gamut: str) -> str:
    """A device panel gamut (Tesserae's panel["gamut"] or display_color_mode) -> sheet panel."""
    g = str(gamut or "").strip().lower().replace("-", "_")
    if g in ("spectra_6", "spectra6", "spectra", "waveshare_e6", "inky_7colour", "acep_7colour"):
        return "spectra"
    if g in ("gray_16", "grey_16", "gray16", "grey16", "gray_8", "grey_8", "gray8", "grey8"):
        return "grey16"
    if g in ("gray_4", "grey_4", "gray4", "grey4"):
        return "grey4"
    if g in ("mono", "mono_dither", "bw", "bwr_3", "bwry_4"):
        return "bw"
    return "colour"


def _device_gamut(device_id: str) -> str:
    """The panel gamut of a Tesserae device: its panel block, else its renderer's only gamut,
    else what display_color_mode makes of it."""
    from flask import current_app

    device = current_app.config["DEVICE_REGISTRY"].get(device_id)
    if device is None:
        return ""
    gamut = str((getattr(device, "panel", None) or {}).get("gamut") or "")
    if gamut:
        return gamut
    renderers = current_app.config.get("RENDERER_REGISTRY")
    ids = getattr(device, "renderer_ids", None) or []
    renderer = renderers.get(ids[0]) if renderers is not None and ids else None
    gamuts = (renderer.manifest.get("gamuts") or []) if renderer is not None else []
    if len(gamuts) == 1:
        return str(gamuts[0])
    from app.display_color import display_color_mode

    return display_color_mode(device, renderers, current_app.config.get("SETTINGS_STORE")).gamut


def resolve_panel(option: Any, ctx: dict) -> str:
    """The panel option; "auto" follows the device this render is for (colour without one)."""
    option = str(option or "auto")
    if option in PANELS:
        return option
    device_id = ctx.get("target_device_id")
    if not device_id:
        return "colour"
    try:
        return panel_for_gamut(_device_gamut(str(device_id)))
    except Exception:
        log.debug("print_blueprint: no panel gamut for %s", device_id, exc_info=True)
        return "colour"


def target_safe_options(opts: SheetOptions, ctx: dict) -> SheetOptions:
    """Keep white mono paper in the physical panel's available grey inks."""
    if opts.background != "white" or opts.palette or opts.panel not in ("grey16", "grey4", "bw"):
        return opts
    device = ctx.get("target_device_id")
    if not device:
        return opts
    try:
        target = panel_for_gamut(_device_gamut(str(device)))
    except Exception:
        log.debug("print_blueprint: no target gamut for %s", device, exc_info=True)
        return opts
    if target in ("spectra", "bw"):
        return dataclasses.replace(opts, panel="bw")
    if target == "grey4" and opts.panel == "grey16":
        return dataclasses.replace(opts, panel="grey4")
    return opts


def resolve_tz() -> str:
    """Tesserae's own timezone setting (its renders use it too), "" when unset or "system"."""
    try:
        from flask import current_app

        store = current_app.config.get("SETTINGS_STORE")
        raw = str(((store.get_section("app") if store is not None else None) or {}).get("timezone") or "")
    except Exception:
        return ""
    return "" if raw.strip().lower() in ("", "system") else raw.strip()


def resolve_clock(ctx: dict) -> str:
    """"h12" or "h23": the clock style of the locale this render is for (the device's own
    locale, else the app's). A host without these helpers gets 24-hour."""
    try:
        from app.locale_resolve import resolve_locale, time_cycle
        from flask import current_app

        locale = str(ctx.get("locale") or "")
        if not locale:
            store = current_app.config.get("SETTINGS_STORE")
            app_section = (store.get_section("app") if store is not None else None) or {}
            devices = current_app.config.get("DEVICE_REGISTRY")
            device_id = str(ctx.get("target_device_id") or "")
            device = devices.get(device_id) if devices is not None and device_id else None
            locale = resolve_locale(app_section, device)
        return time_cycle(locale)
    except Exception:
        log.debug("print_blueprint: no locale for the clock", exc_info=True)
        return "h23"


def _hex(c) -> str:
    return "#%02x%02x%02x" % tuple(int(v) for v in c[:3])


def _colors(opts: SheetOptions) -> dict[str, str]:
    th = theme_for(opts)
    return {"bg": _hex(th["bg"]), "ink": _hex(th["ink"])}


# ---------------------------------------------------------------- drawing
def _preview_source(job: PrintJob) -> bytes | None:
    return job.cover_png or (job.slice.preview_png if job.slice else None)


def _empty_art() -> Art:
    return Art(np.zeros((1, 1), np.uint8), np.zeros((1, 1), np.uint8))


def draw(job: PrintJob, size: tuple[int, int], opts: SheetOptions, ai_png: bytes | None = None, note: str = ""):
    """The sheet for `job` at `size`, with `note` drawn as its footnote. Methods fall back
    toolpath -> preview, ai -> preview. The toolpaths are parsed once per print (see walls_for)."""
    progress = job.progress if opts.show_progress else 1.0
    u = sheet_unit(size)
    src = _preview_source(job)
    walls: list = []

    def art_fn(box):
        if opts.method == "ai" and ai_png:
            try:
                return ai_art(ai_png, box)
            except Exception:
                log.warning("print_blueprint: unreadable AI picture", exc_info=True)
        if opts.method == "toolpath" and job.slice is not None and job.slice.gcode:
            if not walls:
                walls.append(walls_for(job.slice, job.job_key))
            return toolpath_art(job.slice, box, progress, az=-55, u=u, walls=walls[0])
        if src:
            try:
                return apply_progress(preview_art(src, box), job, opts)
            except Exception:
                log.warning("print_blueprint: unreadable preview picture", exc_info=True)
        return _empty_art()

    # All three methods use the same callouts, so the picture gets the same room on the glass:
    # the toolpath-only callouts would otherwise push its hero aside and make it smaller.
    layout_opts = dataclasses.replace(opts, method="preview") if opts.method == "toolpath" else opts
    img, _ = render_sheet(job, art_fn, size, layout_opts, note=note)
    img.info["note"] = note  # kept in the PNG by png_bytes, read back by _sheet_note
    return img


_NOTES: dict[tuple, str] = {}  # (sheet name, mtime) -> its footnote, read from the PNG
_NOTES_LOCK = threading.Lock()


def _sheet_note(path: Path) -> str:
    """The footnote drawn into a sheet ("" when it has none)."""
    try:
        memo = (path.name, path.stat().st_mtime_ns)
    except OSError:
        return ""
    with _NOTES_LOCK:
        if memo in _NOTES:
            return _NOTES[memo]
    from PIL import Image

    try:
        with Image.open(path) as im:
            note = str(getattr(im, "text", {}).get("note") or "")
    except Exception:
        note = ""
    with _NOTES_LOCK:
        if len(_NOTES) > 500:
            _NOTES.clear()
        _NOTES[memo] = note
    return note


def _drawn_note(job: PrintJob, opts: SheetOptions, runner_note: str, has_key: bool) -> str:
    """The footnote a sheet carries: why its picture is not the one the cell asked for."""
    if opts.method == "toolpath":
        return NOTE_NO_3MF if job.slice is None else ""
    if "needs a kie.ai" in runner_note:
        return "" if has_key else NOTE_NO_KEY  # with a key this is only a preview: it never pays
    if runner_note.startswith("AI picture failed:"):
        return "AI picture failed: drawn from the preview picture"
    return runner_note


def _render_fn(has_key: bool = False):
    """Draw the prepared job, with the fallback note in the sheet."""

    def fn(job: PrintJob, size, opts: SheetOptions, ai_png, note: str = ""):
        return draw(job, size, opts, ai_png, _drawn_note(job, opts, note, has_key))

    return fn


def _prepare_fn(prefix: str, states: dict, http: HaHttp, data_dir: Path):
    """Load the actual source before the worker asks for AI or chooses the sheet's cache key."""

    def fn(job: PrintJob):
        try:
            full = build_job(prefix, states, http, data_dir, allow_download=True)
            full = dataclasses.replace(full, job_key=job.job_key, status=job.status,
                                       cover_png=full.cover_png or job.cover_png)
        except Exception:
            log.warning("print_blueprint: couldn't rebuild the job with downloads", exc_info=True)
            full = job
        return full

    return fn


def _save(img, path: Path) -> None:
    write_atomic(path, png_bytes(img))


def _inline_size(size: tuple[int, int], opts: SheetOptions | None = None) -> tuple[int, int]:
    """The quick sheet's size: the cell's on two-tone panels, else at most INLINE_MAX."""
    w, h = size
    if opts is not None and opts.panel in TWO_TONE:
        return w, h
    k = min(1.0, INLINE_MAX / max(w, h))
    return max(1, round(w * k)), max(1, round(h * k))


def _quick_need(size: tuple[int, int], opts: SheetOptions) -> float:
    """Seconds of budget the quick sheet needs (a full-size two-tone sheet needs more on big cells)."""
    w, h = _inline_size(size, opts)
    return QUICK_MIN_S * max(1.0, w * h / QUICK_PX)


def _quick(job: PrintJob, size: tuple[int, int], opts: SheetOptions, data_dir: Path) -> Path | None:
    """The cheap line-art sheet, drawn inline (once per job/progress bucket) so there is something
    to show before the first real sheet of this print exists. Never drawn small and scaled up on
    a two-tone panel."""
    if not _preview_source(job):
        return None
    qs = _inline_size(size, opts)
    qopts = dataclasses.replace(opts, method="preview")
    path = Path(data_dir) / "sheets" / f"{cache_key(job, qs, qopts)}-q.png"
    if path.exists():
        return path
    try:
        _save(draw(job, qs, qopts), path)
    except Exception:
        log.warning("print_blueprint: inline sheet failed", exc_info=True)
        return None
    return path


def sheet_path(data_dir: Path, name: str) -> Path | None:
    """The sheet called `name`, if that is a name this widget writes and it exists."""
    if not SHEET_NAME.fullmatch(name or ""):
        return None
    for folder in ("sheets", "examples"):
        path = Path(data_dir) / folder / name
        if path.is_file():
            return path
    return None


def _url(path: Path) -> str | None:
    """The sheet's URL with a version of its content, or None when it was pruned meanwhile."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    v = hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:12]
    return f"/plugins/{PLUGIN_ID}/image/{path.name}?v={v}"


def _last_sheet(runner: Runner, prefix: str, size: tuple[int, int], data_dir: Path,
                opts: SheetOptions) -> Path | None:
    """The newest finished print's sheet for this printer drawn for this cell (size, panel,
    background, picture), or None: another cell's palette or size would be wrong here."""
    entry = runner.last_finished(prefix) or {}
    family = family_of(str(entry.get("job_key") or ""), size, opts)
    paths = [p for p in (sheet_path(data_dir, str(n)) for k, n in (entry.get("files") or {}).items()
                         if str(k).startswith(family)) if p and p.exists()]
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def _example_path(data_dir: Path, size: tuple[int, int], opts: SheetOptions) -> Path:
    key = hashlib.sha1(json.dumps(["example", EXAMPLE_REV, size, dataclasses.asdict(opts)],
                                 sort_keys=True).encode()).hexdigest()
    path = data_dir / "examples" / f"{key}.png"
    if not path.exists():
        image, _ = render_example(size, opts)
        _save(image, path)
        prune(path.parent, "*.png", keep=40)
    return path


def _example_result(base: dict, data_dir: Path, size: tuple[int, int], opts: SheetOptions,
                    pending: bool = False) -> dict:
    out = {**base, "image": _url(_example_path(data_dir, size, opts)), "example": True,
           "title": "T800", "printer": "Example", "note": EXAMPLE_NOTE}
    if pending:
        out.update(live_pending=1, live_refresh_s=LIVE_REFRESH_S)
    return out


def _preview_draw(job, size, opts, ai_png, note=""):
    """The live editor must never cache a substitute as the chosen method."""
    from PIL import Image

    if opts.method == "toolpath":
        if job.slice is None or not job.slice.gcode or not walls_for(job.slice, job.job_key):
            return draw(job, size, opts, note=NOTE_NO_3MF)
    else:
        source = ai_png if opts.method == "ai" else _preview_source(job)
        try:
            if not source:
                raise ValueError("No picture for this method yet")
            Image.open(io.BytesIO(source)).verify()
        except Exception:
            return draw(job, size, opts, note="Chosen picture method is not ready")
    return draw(job, size, opts, ai_png)


def _editor_fetch(options: dict, base: dict, size: tuple[int, int],
                  opts: SheetOptions, data_dir: Path) -> dict:
    def example(pending=False):
        return _example_result(base, data_dir, size, opts, pending)

    if options.get("editor_preview") != "live":
        return example()
    core = _core()
    if core is None:
        return example()
    try:
        all_states = _states_within(core, STATES_BUDGET_S)
        printers = dict(printer_prefixes(all_states))
        prefix = _wanted(options.get("printer")) or next(iter(printers), "")
        if prefix not in printers:
            return example()
        states = _states(all_states, prefix)
        job = build_job(prefix, states, _http_for(core, timeout=HTTP_BUDGET_S),
                        data_dir, allow_download=False)
        # An old failure or idle sheet doesn't help choose how a running print will look.
        if job.status not in LIVE or not job.title:
            return example()
        if opts.method == "ai" and not (data_dir / "ai" / f"{job.job_key}.png").is_file():
            return example()
        runner = _runner(data_dir)
        st = runner.ensure(job, size, opts, _preview_draw, kie_key="",
                           prepare_fn=_prepare_fn(prefix, states,
                                                  _http_for(core, timeout=BACKGROUND_TIMEOUT_S), data_dir))
        ready = st.ready_path
        if ready is not None and not _sheet_note(ready):
            # newest() can include an old failed phase in this family. Require
            # the current variant so it cannot stand in for the selected method.
            expected = cache_key(job, size, opts, ai_present=opts.method == "ai")
            if ready.stem == expected:
                url = _url(ready)
                if url:
                    out = {**base, "image": url, "title": job.title, "printer": printers[prefix]}
                    if st.pending:
                        out.update(live_pending=1, live_refresh_s=LIVE_REFRESH_S)
                    return out
        return example(st.pending)
    except Exception:
        log.debug("print_blueprint: live editor preview unavailable", exc_info=True)
        return example()


# ---------------------------------------------------------------- fetch
_RUNNERS: dict[str, Runner] = {}
_RUNNERS_LOCK = threading.Lock()
_LAST: dict[tuple, dict] = {}  # last image result per cell, shown with a note while HA is away


def _runner(data_dir: Path) -> Runner:
    with _RUNNERS_LOCK:
        r = _RUNNERS.get(str(data_dir))
        if r is None:
            r = _RUNNERS[str(data_dir)] = Runner(Path(data_dir))
        return r


def _size(ctx: dict) -> tuple[int, int]:
    try:
        w, h = int(ctx.get("cell_w") or 0), int(ctx.get("cell_h") or 0)
    except (TypeError, ValueError):
        w = h = 0
    if w <= 0 or h <= 0:
        return DEFAULT_SIZE
    return min(max(w, 64), 4096), min(max(h, 64), 4096)


def _empty(base: dict, kind: str, title: str, detail: str, printer: str = "") -> dict:
    return {**base, "empty": {"kind": kind, "title": title, "detail": detail, "printer": printer}}


def _cap(settings: dict) -> int:
    try:
        return max(0, int(float(settings.get("ai_daily_cap", 12))))
    except (TypeError, ValueError):
        return 12


def fetch(options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
    """The sheet this cell shows now. Never downloads, parses a 3MF or draws heavy art itself.

    Every render queues the free work (3MF download, toolpaths) for the sheet it needs; only
    settled renders that are not previews (a push, a warm-ahead) may spend kie.ai credits."""
    start = time.monotonic()
    data_dir = Path(ctx["data_dir"])
    preview = bool(ctx.get("preview"))
    settled = bool(ctx.get("settled", not preview))
    size = _size(ctx)
    opts = sheet_options(options, resolve_panel(options.get("panel"), ctx))
    opts = dataclasses.replace(opts, clock=resolve_clock(ctx), tz=resolve_tz())
    look = cell_look.normalise(ctx.get("cell_look")) if "cell_look" in ctx else cell_look.saved(ctx, options)
    opts = cell_look.apply(opts, look)
    device = str(ctx.get("target_device_id") or "")
    _DATA_DIRS[:] = [data_dir]
    if device and not preview:
        cycles = _bool(options.get("button_cycles_background"), True)
        if cycles and not look:
            opts = dataclasses.replace(opts, background=_paper_background(data_dir, device, opts.background))
        _note_cycling(data_dir, device, cycles)
    opts = target_safe_options(opts, ctx)
    idle = str(options.get("idle") or "last")
    base = {"w": size[0], "h": size[1], "background": opts.background, "colors": _colors(opts),
            "cell_look": look, "look_preview": preview, "look_panel": opts.panel, "look_device": device}
    if preview:
        return _editor_fetch(options, base, size, opts, data_dir)
    memo = (str(data_dir), str(options.get("printer") or ""), size, json.dumps(dataclasses.asdict(opts)), idle)

    core = _core()
    if core is None:
        return {"error": "Install the Home Assistant Core widget and connect it to Home Assistant."}
    try:
        all_states = _states_within(core, min(STATES_BUDGET_S, FETCH_BUDGET_S - (time.monotonic() - start) - 1))
    except Exception as err:
        msg = _friendly(core, err)
        last = _LAST.get(memo)
        return {**last, "note": msg} if last else {"error": msg}

    printers = dict(printer_prefixes(all_states))
    if not printers:
        return _empty(base, "no_printer", "No Bambu Lab printer found",
                      "Add the Bambu Lab integration (ha-bambulab) to Home Assistant.")
    want = _wanted(options.get("printer"))
    if want and want not in printers:
        return _empty(base, "not_found", "Printer not found",
                      f"Home Assistant has no printer called {want}. Pick one in this widget's options.", want)
    prefix = want or next(iter(printers))
    label = printers[prefix]
    states = _states(all_states, prefix)
    http = _http_for(core, timeout=HTTP_BUDGET_S, deadline=start + HTTP_BUDGET_S)
    try:
        job = build_job(prefix, states, http, data_dir, allow_download=False)
    except Exception:
        log.warning("print_blueprint: couldn't read %s", prefix, exc_info=True)
        return {"error": f"Couldn't read {label} from Home Assistant right now."}

    runner = _runner(data_dir)
    if job.status not in LIVE:
        # The editor's preview always shows a print, so the cell can be set up while the printer
        # is idle; a screen gets what the cell is set to.
        if idle == "empty" and not preview:
            return _empty(base, "no_print", "No print running", f"{label} is not printing.", label)
        if not job.title:  # offline or never printed: the newest finished sheet, if any
            last = _last_sheet(runner, prefix, size, data_dir, opts)
            if last is None:
                return _empty(base, "no_history", "No print running", f"{label} has no print to show yet.", label)
            url = _url(last)
            if url is None:
                return _empty(base, "no_history", "No print running", f"{label} has no print to show yet.", label)
            title = str((runner.last_finished(prefix) or {}).get("title") or "")
            out = {**base, "image": url, "printer": label, "title": title}
            _LAST[memo] = out
            return out

    kie_key = str(settings.get("kie_api_key_secret") or settings.get("kie_api_key") or "").strip()
    note = NOTE_NO_KEY if opts.method == "ai" and not kie_key else ""
    # Every render queues the free drawing for the exact sheet this cell needs. Only a settled,
    # non-preview render may spend kie.ai credits. (Editor and deck-preview renders used to start
    # nothing, so after an option change no sheet was drawn until a panel push came along.)
    paid_key = "" if preview or not settled else kie_key
    render_fn = _render_fn(bool(kie_key))
    st = runner.ensure(job, size, opts, render_fn, kie_key=paid_key, ai_daily_cap=_cap(settings),
                       prepare_fn=_prepare_fn(prefix, states, _http_for(core, timeout=BACKGROUND_TIMEOUT_S), data_dir))
    ready, pending = st.ready_path, st.pending  # ready may be an older step of this sheet
    if st.note and not (preview and kie_key and "needs a kie.ai" in st.note):
        note = note or st.note

    out = {**base, "printer": label, "title": job.title}
    url = _url(ready) if ready is not None else None
    if url:
        out["image"] = url
        note = note or _sheet_note(ready)
    else:
        note = note if note == NOTE_NO_KEY else ""
        left = FETCH_BUDGET_S - (time.monotonic() - start)
        need = _quick_need(size, opts)
        quick = _quick(job, size, opts, data_dir) if left >= need else None
        url = _url(quick) if quick is not None else None
        if url:
            out["image"] = url
        elif pending or left < need:
            pending = True  # come back: the drawing (or at least the line-art sheet) will be there
            out["empty"] = {"kind": "drawing", "title": "Drawing the blueprint",
                            "detail": f"{label}: {job.title}".strip(": "), "printer": label}
        else:
            out["empty"] = {"kind": "nothing", "title": "Nothing to draw yet",
                            "detail": "The drawing starts when a screen shows this widget.", "printer": label}
    if pending:
        out["live_refresh_s"] = LIVE_REFRESH_S
        out["live_pending"] = 1
        if opts.method == "ai" and paid_key and st.key and not (runner.ai_dir / f"{job.job_key}.png").exists():
            out["ai_wait"] = f"/plugins/{PLUGIN_ID}/ready/{st.key}"
    if note:
        out["note"] = note
    if "image" in out:
        _LAST[memo] = {k: v for k, v in out.items() if k not in ("live_refresh_s", "live_pending", "note")}
    return out


CHOICES_BUDGET_S = 2.0  # the editor waits on choices(): Home Assistant gets this long, then all stay open
AVAILABILITY_TTL_S = 60.0
_AVAILABILITY: dict[str, tuple[float, str]] = {}  # printer prefix -> (when, why Toolpaths can't work or "")
_AVAILABILITY_LOCK = threading.Lock()
NO_KEY_REASON = "Needs a kie.ai API key: add it under Settings → Widgets → Print Blueprint."


def _own_settings() -> dict[str, Any]:
    """This widget's settings from the host's store, or {} when they can't be read."""
    try:
        from flask import current_app

        store = current_app.config.get("SETTINGS_STORE")
        registry = current_app.config.get("PLUGIN_REGISTRY")
        plugin = registry.get(PLUGIN_ID) if registry is not None else None
        if store is None or plugin is None:
            return {}
        got = store.get_for_runtime("plugins", PLUGIN_ID, plugin.manifest.get("settings", []))
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def _has_kie_key(settings: dict[str, Any]) -> bool:
    return bool(str(settings.get("kie_api_key_secret") or settings.get("kie_api_key") or "").strip())


def _toolpath_blocker(core: Any, printer: Any) -> str:
    """Why Toolpaths can't work for this printer, or "" when it can or that isn't known.
    Cached a minute per printer, and bounded: the editor waits on it."""
    try:
        all_states = _states_within(core, CHOICES_BUDGET_S)
    except Exception:
        return ""
    printers = dict(printer_prefixes(all_states))
    prefix = _wanted(printer) or next(iter(printers), "")
    if prefix not in printers:
        return ""
    with _AVAILABILITY_LOCK:
        hit = _AVAILABILITY.get(prefix)
    if hit and time.monotonic() - hit[0] < AVAILABILITY_TTL_S:
        return hit[1]
    http = _http_for(core, timeout=CHOICES_BUDGET_S, deadline=time.monotonic() + CHOICES_BUDGET_S)
    blocked = no_sliced_files(prefix, _states(all_states, prefix), http)
    why = (f"Not available for {printers[prefix]} yet: Home Assistant has no sliced files from it. "
           f"{TOOLPATH_SETUP}") if blocked else ""
    with _AVAILABILITY_LOCK:
        if len(_AVAILABILITY) > 32:
            _AVAILABILITY.clear()
        _AVAILABILITY[prefix] = (time.monotonic(), why)
    return why


def _choice_context() -> dict:
    """choices() gets options but no render ctx. The canvas form POST carries the cell size."""
    try:
        from flask import request

        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else dict(request.args)
    except RuntimeError:  # a legacy caller or a test without a Flask request
        return {}


def _example_url(method: str, options: dict | None = None) -> str:
    from urllib.parse import urlencode

    values = options or {}
    context = _choice_context()
    size = _size(context)
    # The image is small, but is laid out at the real cell size before reduction.
    opts = sheet_options(values, resolve_panel(values.get("panel"), context))
    look = cell_look.saved(context, values)
    if not look:
        opts = target_safe_options(opts, context)
    query = {"w": size[0], "h": size[1], **dataclasses.asdict(opts)}
    query["show_grid"] = "auto" if opts.show_grid is None else "on" if opts.show_grid else "off"
    query.pop("palette")
    query.pop("font_path")
    if look:
        query["look"] = json.dumps(look, separators=(",", ":"))
    query.pop("method")
    query.update(thumb=1, rev=EXAMPLE_REV)
    return f"/plugins/{PLUGIN_ID}/example/{method}.png?{urlencode(query)}"


def _method_choices(options: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The three picture methods with their pictures. With the cell's options (Tesserae with
    choices_depends_on), Toolpaths is disabled for a printer Home Assistant gets no sliced files
    from, and AI engraving without a kie.ai key. An older Tesserae calls choices(name) and gets
    all three open, as the static list was."""
    rows = [{"value": v, "label": label, "image": _example_url(v, options)}
            for v, label in _METHOD_NAMES.items()]
    if options is None:
        return rows
    core = _core()
    why = _toolpath_blocker(core, options.get("printer")) if core is not None else ""
    if why:
        rows[0].update(disabled=True, reason=why)
    if not _has_kie_key(_own_settings()):
        rows[2].update(disabled=True, reason=NO_KEY_REASON)
    return rows


def choices(name: str, options: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """The printer picker (every `sensor.*_print_status` in Home Assistant) and the picture
    methods for the cell's saved printer, refreshed when the editor opens."""
    if name == "method":
        rows = _method_choices(options)
        if rows[0].get("disabled"):
            rows[0]["reason"] += " Availability depends on the printer. Save and reopen to refresh."
        return rows
    if name != "printer":
        return []
    core = _core()
    if core is None:
        return [{"value": "", "label": "Install the Home Assistant Core widget first.", "note": True}]
    try:
        found = printer_prefixes(core.get_states())
    except Exception as err:
        return [{"value": "", "label": _friendly(core, err), "note": True}]
    if not found:
        return [{"value": "", "label": "No Bambu Lab printer in Home Assistant. Add the Bambu Lab integration.",
                 "note": True}]
    return [{"value": p, "label": label} for p, label in found]


# ---------------------------------------------------------------- compare page
_METHOD_NAMES = {"toolpath": "Toolpaths", "preview": "Line art from the preview", "ai": "AI engraving"}


def _data_uri(img) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _no_cover_reason(core: Any, prefix: str) -> str:
    """Why there is no preview picture, as precisely as Home Assistant lets us say."""
    eid = f"image.{prefix}_cover_image"
    try:
        ent = next((x for x in core.get_states() if x.get("entity_id") == eid), None)
    except Exception:
        ent = None
    if ent is None:
        return f"No preview picture: Home Assistant has no {eid}."
    state = str(ent.get("state") or "").strip().lower()
    if state in ("unavailable", "unknown", ""):
        return (f"No preview picture: {eid} is {state or 'empty'} in Home Assistant. The Bambu Lab integration "
                "drops it when it reloads during a print; it comes back with the next print.")
    return f"No preview picture: Home Assistant would not send {eid}."


def compare_html(core: Any, data_dir: Path, prefix: str, w: int, h: int, background: str = "blue",
                 panel: str = "colour", printers: list[tuple[str, str]] | None = None) -> str:
    """The three picture methods side by side for the printer's current job. The AI picture is
    shown only when one was already made: this page never spends kie.ai credits."""
    data_dir = Path(data_dir)
    opts = dataclasses.replace(sheet_options({"background": background}, panel), clock=resolve_clock({}),
                               tz=resolve_tz())
    esc = html.escape
    figures = []
    try:
        job = current_job(core, prefix, data_dir)
    except Exception as err:
        job, problem = None, _friendly(core, err)
    else:
        problem = ""
    if job is not None:
        ai_file = data_dir / "ai" / f"{job.job_key}.png"
        for method in METHODS:
            why = ""
            if method == "toolpath" and job.slice is None:
                why = ("No sliced file in Home Assistant for this print (a cloud print, or an ha-bambulab "
                       "version without print history).")
            elif method == "preview" and not _preview_source(job):
                why = _no_cover_reason(core, prefix)
            elif method == "ai" and not ai_file.exists():
                why = ("AI picture not made yet. Pick AI engraving and the next screen refresh makes one "
                       "(about US$0.02, needs a kie.ai API key).")
            body = f'<p class="why">{esc(why)}</p>'
            if not why:
                try:
                    ai_png = ai_file.read_bytes() if method == "ai" else None
                    img = draw(job, (w, h), dataclasses.replace(opts, method=method), ai_png)
                    shown = glass(img) if panel == "spectra" else img  # as the panel paints it
                    body = f'<img src="{_data_uri(shown)}" width="{w}" height="{h}" alt="{esc(_METHOD_NAMES[method])}">'
                except Exception as err:
                    log.warning("print_blueprint: compare %s failed", method, exc_info=True)
                    body = f'<p class="why">Drawing failed: {esc(str(err))}</p>'
            figures.append(f'<figure><figcaption>{esc(_METHOD_NAMES[method])}</figcaption>{body}</figure>')
    picker = "".join(
        f'<option value="{esc(p)}"{" selected" if p == prefix else ""}>{esc(label)}</option>'
        for p, label in (printers or []))
    bgs = "".join(f'<option{" selected" if b == opts.background else ""}>{b}</option>' for b in BACKGROUNDS)
    pans = "".join(f'<option{" selected" if p == opts.panel else ""}>{p}</option>' for p in PANELS)
    title = esc(job.title) if job is not None and job.title else "No print"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Print Blueprint: compare picture methods</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font: 15px/1.45 system-ui, sans-serif; margin: 24px; color: #1d2433; background: #f4f2ec; }}
h1 {{ font-size: 20px; margin: 0 0 4px; }} form {{ margin: 12px 0 20px; display: flex; gap: 12px; flex-wrap: wrap; align-items: end; }}
label {{ display: grid; gap: 2px; font-size: 13px; }} input {{ width: 6em; }}
.row {{ display: flex; gap: 20px; flex-wrap: wrap; align-items: start; }}
figure {{ margin: 0; max-width: {max(w, 240)}px; }} figcaption {{ font-weight: 600; margin-bottom: 6px; }}
img {{ max-width: 100%; height: auto; display: block; box-shadow: 0 1px 4px #0003; }}
.why {{ width: min({w}px, 90vw); color: #555; }} .problem {{ color: #a12; }}
</style></head><body>
<h1>Compare picture methods</h1>
<p>{title}. The cell's Picture option picks one; Toolpaths falls back to line art when there is no sliced file.</p>
<form method="get">
<label>Printer <select name="printer">{picker}</select></label>
<label>Paper <select name="background">{bgs}</select></label>
<label>Panel <select name="panel">{pans}</select></label>
<label>Width <input name="w" type="number" value="{w}"></label>
<label>Height <input name="h" type="number" value="{h}"></label>
<button type="submit">Show</button>
</form>
{f'<p class="problem">{esc(problem)}</p>' if problem else ''}
<div class="row">{''.join(figures)}</div>
</body></html>"""


# ---------------------------------------------------------------- routes
def _data_dir() -> Path:
    from flask import abort, current_app

    plugin = current_app.config["PLUGIN_REGISTRY"].get(PLUGIN_ID)
    if plugin is None:
        abort(404)
    return Path(plugin.data_dir)


def _int_arg(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(value), lo), hi)
    except (TypeError, ValueError):
        return default


def blueprint():
    """`/plugins/print_blueprint/image/<name>.png` (sheets) and `/compare` (the three methods)."""
    from flask import Blueprint, Response, abort, request, send_file

    # No "/" index on purpose: Tesserae lists any widget with one under
    # Widgets > Admin pages, and the compare page is a tool, not settings.
    bp = Blueprint("print_blueprint_admin", __name__)

    @bp.get("/image/<name>")
    def image(name: str):
        path = sheet_path(_data_dir(), name)
        if path is None:
            abort(404)
        response = send_file(path, mimetype="image/png", conditional=True)
        # The URL carries a version of the file, so it never changes under a URL.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @bp.get("/ready/<key>")
    def ready(key: str):
        # Read only: no HA calls, task submissions or re-queues on the render-safe route.
        if not re.fullmatch(r"[0-9a-f]{40}", key):
            abort(404)
        runner = _RUNNERS.get(str(_data_dir()))
        path = runner.wait_for_sheet(key, AI_WAIT_S) if runner is not None else None
        url = _url(path) if path is not None else None
        response = Response(json.dumps({"image": url}) if url else "", status=200 if url else 204,
                            mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.get("/look")
    def look():
        # The browser supplies resolved CSS, never a font URL/path or permission to spend.
        try:
            options = json.loads(request.args.get("options", "{}"))
            values = json.loads(request.args.get("look", "null"))
        except ValueError:
            abort(400)
        if not isinstance(options, dict) or (values is not None and cell_look.normalise(values) is None):
            abort(400)
        if request.args.get("panel") in PANELS:
            options["panel"] = request.args["panel"]
        context = {"data_dir": str(_data_dir()), "cell_w": _int_arg(request.args.get("w"), 1200, 64, 4096),
                   "cell_h": _int_arg(request.args.get("h"), 800, 64, 4096),
                   "preview": request.args.get("preview") == "1", "settled": False, "cell_look": values,
                   "target_device_id": request.args.get("target", "")}
        response = Response(json.dumps(fetch(options, _own_settings(), ctx=context)), mimetype="application/json")
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.get("/example/<method>.png")
    def example(method: str):
        if method not in METHODS:
            abort(404)
        size = _size({"cell_w": request.args.get("w"), "cell_h": request.args.get("h")})
        opts = sheet_options({**request.args, "method": method}, request.args.get("panel", "colour"))
        try:
            opts = cell_look.apply(opts, json.loads(request.args.get("look", "null")))
        except ValueError:
            abort(400)
        full = _example_path(_data_dir(), size, opts)
        path = full
        if request.args.get("thumb") == "1":
            from PIL import Image

            # Preserve the cell's typography/layout at a modest download size. The
            # lightbox uses this same image, at up to 800 px on its longest edge.
            path = _data_dir() / "example-thumbs" / full.name
            if not path.exists():
                with Image.open(full) as image:
                    image.thumbnail((800, 800), Image.Resampling.LANCZOS)
                    _save(image, path)
                prune(path.parent, "*.png", keep=40)
        response = send_file(path, mimetype="image/png", conditional=True)
        response.headers["Cache-Control"] = "private, max-age=3600"
        return response

    @bp.get("/compare")
    def compare():
        core = _core()
        if core is None:
            return Response("Install the Home Assistant Core widget first.", status=503, mimetype="text/plain")
        try:
            printers = printer_prefixes(core.get_states())
        except Exception as err:
            return Response(_friendly(core, err), status=502, mimetype="text/plain")
        if not printers:
            return Response("No Bambu Lab printer in Home Assistant.", status=404, mimetype="text/plain")
        want = _wanted(request.args.get("printer"))
        prefix = want if want in dict(printers) else printers[0][0]
        w = _int_arg(request.args.get("w"), 600, 120, 2400)
        h = _int_arg(request.args.get("h"), 800, 120, 2400)
        bg = request.args.get("background") or "blue"
        panel = request.args.get("panel") or "colour"
        page = compare_html(core, _data_dir(), prefix, w, h, bg, panel, printers)
        return Response(page, mimetype="text/html")

    return bp


_register_action()
