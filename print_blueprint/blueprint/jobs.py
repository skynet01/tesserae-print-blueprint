"""Background runner: renders sheets (and the optional AI picture) off the request path.

`ensure()` never blocks: it returns what is on disk and queues the rest for one daemon worker.

A sheet's name is 40 hex digits: 24 for its family (print, cell size, options) and 16 for its
variant (progress step or time step, print phase). While a new variant is drawn the newest sheet of
the same family stands in for it."""
from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import io
import json
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image

from .files import png_bytes, prune, write_atomic
from .kie import MODEL, PROMPT, KieClient, KieError, KieTaskFailed
from .layout import phase
from .model import PrintJob, SheetOptions

log = logging.getLogger(__name__)

KEEP_PNGS = 40
TASK_KEEP_S = 24 * 3600  # an ambiguous submission expires to manual recovery, never automatic retry
TASK_POLL_EVERY_S = 15 * 60  # ...looked at again at most this often (a stuck task never blocks a render long)
AI_RETRY_S = 6 * 3600
FAIL_RETRY_S = 300
TIME_STEP_S = 600  # without the progress option a live sheet is redrawn this often (times, ETA)
FAMILY_LEN = 24
DRAWING_REV = 5  # preview art also appears on fallback sheets

# render_fn(job, size, opts, ai_png, note) -> sheet; note is the footnote to draw into it
RenderFn = Callable[[PrintJob, tuple, SheetOptions, "bytes | None", str], Image.Image]


@dataclass
class RenderState:
    ready_path: Path | None
    pending: bool
    note: str = ""
    key: str = ""  # the cache key asked for (ready_path may be an older sheet of the same family)


def _sha(blob) -> str:
    return hashlib.sha1(json.dumps(blob, sort_keys=True).encode()).hexdigest()


def family_of(job_key: str, size: tuple[int, int], opts: SheetOptions) -> str:
    """The part of a cache key shared by every sheet of one print at one size and option set."""
    return _sha([DRAWING_REV, job_key, list(size), dataclasses.asdict(opts)])[:FAMILY_LEN]


def cache_key(job: PrintJob, size: tuple[int, int], opts: SheetOptions, now: float | None = None,
              *, ai_present: bool = False) -> str:
    """Family (print, size, options) + variant (2 % progress step, or a 10-minute step while live
    when progress is off, and the print phase: running, paused, done, failed, last)."""
    ph = phase(job)
    tick = f"t{int((time.time() if now is None else now) // TIME_STEP_S)}"
    if opts.show_progress:  # live: ETA and temperatures move on even when progress doesn't
        step: int | str = f"{math.floor(job.progress * 50)}{tick if ph == 'live' else ''}"
    elif ph in ("live", "paused"):
        step = tick
    else:
        step = 0
    # a sheet drawn before the 3MF or the cover arrived is drawn again once it does
    preview = (job.slice.preview_png if job.slice else None) or job.cover_png
    src = ["slice", job.slice.source] if job.slice is not None else ["cover" if preview else "none", job.has_3mf]
    src.append(hashlib.sha1(preview).hexdigest() if preview else "")
    family = family_of(job.job_key, size, opts)
    return family + _sha([step, ph, src, ai_present])[: 40 - FAMILY_LEN]


def ai_source_png(job: PrintJob) -> bytes | None:
    """The slicer preview (or cover) flattened onto white and padded to a 1024 px square, as the AI
    model expects (padded, never stretched)."""
    raw = (job.slice.preview_png if job.slice else None) or job.cover_png
    if not raw:
        return None
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    side = max(im.size)
    bg = Image.new("RGBA", (side, side), "white")
    bg.alpha_composite(im, ((side - im.width) // 2, (side - im.height) // 2))
    out = io.BytesIO()
    bg.convert("RGB").resize((1024, 1024), Image.LANCZOS).save(out, "PNG")
    return out.getvalue()


class Runner:
    def __init__(self, data_dir: Path, kie_factory: Callable[[str], KieClient] = KieClient,
                 now: Callable[[], float] = time.time):
        self.data_dir = Path(data_dir)
        self.sheets = self.data_dir / "sheets"
        self.ai_dir = self.data_dir / "ai"
        self._kie, self._now = kie_factory, now
        self._lock = threading.Lock()
        self._finished = threading.Condition(self._lock)
        self._q: queue.Queue = queue.Queue()
        self._inflight: set[str] = set()
        self._failed: dict[str, tuple[float, str]] = {}
        self._notes: dict[str, str] = {}
        self._thread: threading.Thread | None = None

    # ---- public -------------------------------------------------------------------------------
    def ensure(self, job: PrintJob, size: tuple[int, int], opts: SheetOptions, render_fn: RenderFn, *,
               kie_key: str = "", ai_daily_cap: int = 12,
               prepare_fn: Callable[[PrintJob], PrintJob] | None = None) -> RenderState:
        ai_present = opts.method == "ai" and (self.ai_dir / f"{job.job_key}.png").exists()
        key = cache_key(job, size, opts, self._now(), ai_present=ai_present)
        path = self.sheets / f"{key}.png"
        want_ai = opts.method == "ai" and bool(kie_key)
        ai_ok, ai_note = self._ai_allowed(job, ai_daily_cap) if want_ai and not ai_present else (False, "")
        ai_missing = want_ai and not ai_present and ai_ok
        have = path.exists()
        shown = path if have else self.newest(key)
        with self._lock:
            if key in self._inflight:
                return RenderState(shown, True, self._notes.get(key, ""), key)
            if have and not ai_missing:
                return RenderState(path, False, ai_note or self._notes.get(key, ""), key)
            failed = self._failed.get(key)
            if failed and self._now() - failed[0] < FAIL_RETRY_S:
                log.info("print_blueprint: not retrying %s for %ds after: %s", key,
                         int(FAIL_RETRY_S - (self._now() - failed[0])), failed[1])
                return RenderState(shown, False, failed[1], key)
            self._inflight.add(key)
            self._notes[key] = ""
            log.info("print_blueprint: queued %s (%s, %dx%d, %s)", key, job.prefix, size[0], size[1], opts.method)
            self._q.put((key, job, size, opts, render_fn, kie_key if want_ai else "", ai_daily_cap, prepare_fn))
            self._start_worker()
        return RenderState(shown, True, "", key)

    def newest(self, key: str) -> Path | None:
        """The sheet for `key`, else the newest sheet of its family (same print, size and options)."""
        path = self.sheets / f"{key}.png"
        if path.exists():
            return path
        best, best_t = None, -1.0
        for p in self.sheets.glob(key[:FAMILY_LEN] + "?" * (40 - FAMILY_LEN) + ".png"):
            try:
                t = p.stat().st_mtime
            except OSError:
                continue
            if t > best_t:
                best, best_t = p, t
        return best

    def wait_idle(self, timeout: float = 30) -> bool:
        """Block until the queue is drained (tests; callers never need this)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if not self._inflight:
                    return True
            time.sleep(0.01)
        return False

    def wait_for_sheet(self, key: str, timeout: float) -> Path | None:
        """Collect queued work without starting anything; failed AI keeps its fallback."""
        with self._finished:
            if key not in self._notes:
                return None
            done = self._finished.wait_for(lambda: key not in self._inflight, timeout=timeout)
            if not done or self._notes[key]:
                return None
        return self.newest(key)

    def last_finished(self, prefix: str) -> dict | None:
        """{job_key, title, ts, files: {cache_key: png name}} of the newest finished print, or None."""
        entry = self._read(self.sheets / "index.json").get(prefix)
        return entry if isinstance(entry, dict) else None

    # ---- worker -------------------------------------------------------------------------------
    def _start_worker(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._work, name="print-blueprint-runner", daemon=True)
            self._thread.start()

    def _work(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=60)
            except queue.Empty:
                with self._lock:
                    if self._q.empty():
                        self._thread = None
                        return
                continue
            key = item[0]
            started = time.monotonic()
            log.info("print_blueprint: render start %s", key)
            try:
                note = self._run(*item)
                err = None
                log.info("print_blueprint: render finish %s in %.1fs", key, time.monotonic() - started)
            except BaseException as e:  # never let a bad render kill the worker
                log.warning("print_blueprint: render failed %s after %.1fs", key, time.monotonic() - started,
                            exc_info=True)
                note, err = f"render failed: {e}", True
            with self._lock:
                self._inflight.discard(key)
                self._notes[key] = note
                if err:
                    self._failed[key] = (self._now(), note)
                else:
                    self._failed.pop(key, None)
                self._finished.notify_all()

    def _run(self, key, job: PrintJob, size, opts, render_fn, kie_key: str, cap: int, prepare_fn=None) -> str:
        if prepare_fn is not None:
            job = prepare_fn(job)
        note = ""
        ai_png = None
        if opts.method == "ai":
            if kie_key:
                try:
                    ai_png, note = self._ai(job, kie_key, cap)
                except Exception as e:  # the AI step never blocks the plain sheet
                    ai_png, note = None, f"AI picture failed: {e}"
            else:
                cached = self.ai_dir / f"{job.job_key}.png"
                if cached.exists():  # already paid for: a preview may use it, just not make one
                    ai_png = cached.read_bytes()
                else:
                    note = "AI picture needs a kie.ai API key"
        img = render_fn(job, size, opts, ai_png, note)
        key = cache_key(job, size, opts, self._now(), ai_present=ai_png is not None)
        write_atomic(self.sheets / f"{key}.png", png_bytes(img))
        self._index(job, key)
        self._prune()
        return note

    # ---- AI -----------------------------------------------------------------------------------
    def _ai_state(self) -> dict:
        path = self.ai_dir / "state.json"
        try:
            s = json.loads(path.read_text())
            if not isinstance(s, dict) or not isinstance(s.get("tried", {}), dict) \
                    or not isinstance(s.get("tasks", {}), dict):
                raise ValueError("invalid state")
        except FileNotFoundError:
            s = {}
        except (OSError, ValueError) as e:
            raise KieError("AI state.json is unreadable; check the kie.ai account before retrying") from e
        today = datetime.fromtimestamp(self._now()).strftime("%Y-%m-%d")
        if s.get("day") != today:
            s["day"], s["count"] = today, 0
        s.setdefault("tried", {})
        s.setdefault("tasks", {})
        if s.get("version") != 2:
            # Older code discarded some paid IDs. An old attempt without an ID is ambiguous.
            for key, at in s["tried"].items():
                s["tasks"].setdefault(key, {"state": "submitting", "at": at})
            s["version"] = 2
        for png in self.ai_dir.glob("*.png"):
            s["tasks"].setdefault(png.stem, {"state": "ready", "at": self._now()})
        return s

    def _pending_task(self, s: dict, job_key: str) -> str | None:
        """Paid IDs never expire. Only the interval between free collections is limited."""
        t = (s.get("tasks") or {}).get(job_key)
        now = self._now()
        if (isinstance(t, dict) and t.get("id")
                and (t.get("state") == "ready" or now - float(t.get("polled") or 0) >= TASK_POLL_EVERY_S)):
            return str(t["id"])
        return None

    def _ai_allowed(self, job: PrintJob, cap: int) -> tuple[bool, str]:
        job_key = job.job_key
        try:
            s = self._ai_state()
        except KieError as e:
            return False, str(e)
        if self._pending_task(s, job_key):
            return True, ""
        if job_key in s["tasks"]:
            task = s["tasks"][job_key]
            if isinstance(task, dict) and task.get("id"):
                return False, "AI picture not ready yet; paid task saved for collection"
            expired = self._now() - float(task.get("at") or 0) >= TASK_KEEP_S if isinstance(task, dict) else True
            return False, ("AI submission expired; " if expired else "AI submission status unknown; ") + \
                "check kie.ai, then remove this print's tasks entry in ai/state.json to retry"
        if self._now() - s["tried"].get(job_key, 0) < AI_RETRY_S:
            return False, "AI picture failed earlier; trying again in a few hours"
        if s["count"] >= cap:
            return False, "AI picture skipped: daily limit reached"
        raw = (job.slice.preview_png if job.slice else None) or job.cover_png
        if not raw:
            return False, "AI picture needs a preview image"
        try:
            Image.open(io.BytesIO(raw)).verify()
        except Exception as e:
            return False, f"AI picture failed: unreadable preview image ({e})"
        return True, ""

    def _ai(self, job: PrintJob, key: str, cap: int) -> tuple[bytes | None, str]:
        # One lock covers every state read/write, including the budget and callback. It also
        # serializes separate plugin processes; ensure() itself never waits on this lock.
        self.ai_dir.mkdir(parents=True, exist_ok=True)
        with (self.ai_dir / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._ai_locked(job, key, cap)

    def _ai_locked(self, job: PrintJob, key: str, cap: int) -> tuple[bytes | None, str]:
        cached = self.ai_dir / f"{job.job_key}.png"
        if cached.exists():
            self._write(self.ai_dir / "state.json", self._ai_state())
            return cached.read_bytes(), ""
        ok, why = self._ai_allowed(job, cap)
        if not ok:
            return None, why
        s = self._ai_state()
        pending = self._pending_task(s, job.job_key)
        if pending:  # paid for already: collect it instead of paying again
            s["tasks"][job.job_key].update(polled=self._now(), state="pending")
            self._write(self.ai_dir / "state.json", s)
        else:
            try:
                src = ai_source_png(job)
            except Exception as e:
                return None, f"AI picture failed: unreadable preview image ({e})"
            if src is None:
                return None, "AI picture needs a preview image"

            def debit():
                s["count"] += 1
                s["tried"][job.job_key] = self._now()
                s["tasks"][job.job_key] = {"state": "submitting", "at": self._now(), "polled": self._now()}
                self._write(self.ai_dir / "state.json", s)  # fail closed even if create loses its reply

            try:
                try:
                    from app import artwork

                    charge = getattr(artwork, "charge", None)  # absent before core's shared ceiling
                except ImportError:
                    charge = None
                if charge is None:  # older Tesserae: keep the widget's local cap
                    debit()
                else:
                    charge("print_blueprint", "engraving", 2, MODEL, debit)
            except Exception as e:
                # No provider call was made. A partially reserved charge stays conservative.
                return None, f"AI picture skipped: {e}"

        def keep_task(tid: str) -> None:
            s["tasks"][job.job_key].update(id=tid, state="pending")
            self._write(self.ai_dir / "state.json", s)

        try:
            client = self._kie(key)
            raw = client.collect(pending) if pending else client.repaint(src, PROMPT, on_task=keep_task)
            png = io.BytesIO()
            Image.open(io.BytesIO(raw)).convert("RGB").save(png, "PNG")  # must decode as a picture
        except KieTaskFailed as e:
            s["tasks"].pop(job.job_key, None)  # only the provider's terminal fail permits another purchase
            s["tried"][job.job_key] = self._now()
            self._write(self.ai_dir / "state.json", s)
            return None, f"AI picture failed: {e}"
        except Exception as e:
            return None, f"AI picture failed: {e}"
        return self._keep_ai(job, png.getvalue()), ""

    def _keep_ai(self, job: PrintJob, png: bytes) -> bytes:
        write_atomic(self.ai_dir / f"{job.job_key}.png", png)
        s = self._ai_state()
        s["tried"].pop(job.job_key, None)
        s["tasks"][job.job_key]["state"] = "ready"  # keep the ID even after its PNG is pruned
        self._write(self.ai_dir / "state.json", s)
        prune(self.ai_dir, "*.png")
        return png

    # ---- index + housekeeping -----------------------------------------------------------------
    def _index(self, job: PrintJob, key: str) -> None:
        if job.status != "finish":
            return
        p = self.sheets / "index.json"
        idx = self._read(p)
        e = idx.get(job.prefix) or {}
        if e.get("job_key") != job.job_key:
            e = {"job_key": job.job_key, "title": job.title, "files": {}}
        e["ts"] = self._now()
        e["files"][key] = f"{key}.png"
        idx[job.prefix] = e
        self._write(p, idx)

    def _prune(self) -> None:
        keep = {n for e in self._read(self.sheets / "index.json").values() if isinstance(e, dict)
                for n in (e.get("files") or {}).values()}
        pngs = sorted(self.sheets.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in pngs[KEEP_PNGS:]:
            if p.name not in keep:
                p.unlink(missing_ok=True)

    @staticmethod
    def _read(p: Path) -> dict:
        try:
            v = json.loads(p.read_text())
            return v if isinstance(v, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write(p: Path, data: dict) -> None:
        write_atomic(p, json.dumps(data).encode(), durable=p.name == "state.json")
