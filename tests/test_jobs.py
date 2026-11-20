import dataclasses
import io
import json
import logging
import threading

from PIL import Image

from blueprint.jobs import Runner, ai_source_png, cache_key
from blueprint.kie import KieError, KieTaskFailed
from blueprint.model import PrintJob, SheetOptions


def job(key="k1", progress=0.5, status="running", prefix="p"):
    return PrintJob(prefix=prefix, model="P1S", printer_name="", serial="S", title="T", status=status,
                    progress=progress, layer=None, layers=None, start_ts=None, end_ts=None, nozzle_d=None,
                    nozzle_type=None, nozzle_temp=None, bed_temp=None, chamber_c=None, bed_type=None,
                    material=None, weight_g=None, length_m=None, fans={}, speed=None, usage_h=None,
                    objects=None, skipped=None, ams=[], ams_humidity=None, tools=[], slice=None,
                    cover_png=_png(), job_key=key)


def _png(color="white"):
    b = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(b, "PNG")
    return b.getvalue()


class Render:
    def __init__(self):
        self.calls = []
        self.notes = []
        self.gate = None

    def __call__(self, j, size, opts, ai, note=""):
        if self.gate:
            self.gate.wait(5)
        self.calls.append((j.job_key, ai is not None))
        self.notes.append(note)
        return Image.new("RGB", size, "white")


class Kie:
    def __init__(self, fail=False):
        self.n, self.fail = 0, fail

    def __call__(self, key):
        return self

    def repaint(self, png, prompt, timeout_s=240, on_task=None):
        self.n += 1
        if on_task is not None:
            on_task(f"T{self.n}")
        if self.fail == "timeout":
            raise KieError("timed out after 240 s")
        if self.fail:
            raise KieTaskFailed("refused")
        return _png("black")

    def collect(self, tid, timeout_s=240):
        self.collected = getattr(self, "collected", []) + [tid]
        if getattr(self, "still_running", False):
            raise KieError("timed out after 240 s")
        return _png("black")


OPTS = SheetOptions()
AI = SheetOptions(method="ai")
SIZE = (60, 40)


def test_runner_dedupes_and_caps_ai(tmp_path):
    kie, render = Kie(), Render()
    r = Runner(tmp_path, kie_factory=kie)
    render.gate = threading.Event()
    first = r.ensure(job("a"), SIZE, AI, render, kie_key="K", ai_daily_cap=2)
    again = r.ensure(job("a"), SIZE, AI, render, kie_key="K", ai_daily_cap=2)
    assert first.pending and again.pending and first.ready_path is None  # returned without blocking
    render.gate.set()
    assert r.wait_idle()
    assert render.calls == [("a", True)] and kie.n == 1
    done = r.ensure(job("a"), SIZE, AI, render, kie_key="K", ai_daily_cap=2)
    assert done.ready_path and done.ready_path.exists() and not done.pending
    # a second size reuses the cached AI picture: no new kie call
    r.ensure(job("a"), (30, 20), AI, render, kie_key="K", ai_daily_cap=2)
    r.wait_idle()
    assert kie.n == 1 and len(render.calls) == 2 and render.calls[1][1] is True
    # cap of 2 per day: b is the second paid image, c is refused
    r.ensure(job("b"), SIZE, AI, render, kie_key="K", ai_daily_cap=2)
    r.wait_idle()
    st = r.ensure(job("c"), SIZE, AI, render, kie_key="K", ai_daily_cap=2)
    r.wait_idle()
    assert kie.n == 2
    st = r.ensure(job("c"), SIZE, AI, render, kie_key="K", ai_daily_cap=2)
    assert st.ready_path and "limit" in st.note and render.calls[-1] == ("c", False)


def test_failed_ai_not_retried_for_six_hours(tmp_path):
    t = [1_000_000.0]
    kie, render = Kie(fail=True), Render()
    r = Runner(tmp_path, kie_factory=kie, now=lambda: t[0])
    r.ensure(job("x"), SIZE, AI, render, kie_key="K")
    r.wait_idle()
    assert kie.n == 1 and render.calls == [("x", False)]
    st = r.ensure(job("x"), (30, 20), AI, render, kie_key="K")  # other size, same print
    r.wait_idle()
    assert kie.n == 1
    t[0] += 6 * 3600 + 1
    r.ensure(job("x"), SIZE, AI, render, kie_key="K")  # stale fallback sheet is re-queued
    r.wait_idle()
    assert kie.n == 2


def test_no_ai_without_key_or_for_other_methods(tmp_path):
    kie, render = Kie(), Render()
    r = Runner(tmp_path, kie_factory=kie)
    r.ensure(job("a"), SIZE, AI, render)
    r.ensure(job("b"), SIZE, OPTS, render, kie_key="K")
    r.wait_idle()
    assert kie.n == 0


def test_progress_bucket_and_show_progress():
    assert cache_key(job(progress=0.50), SIZE, OPTS) == cache_key(job(progress=0.51), SIZE, OPTS)
    assert cache_key(job(progress=0.50), SIZE, OPTS) != cache_key(job(progress=0.60), SIZE, OPTS)
    still = SheetOptions(show_progress=False)
    t = 1_000_000 * 600.0
    assert cache_key(job(progress=0.1), SIZE, still, t) == cache_key(job(progress=0.9), SIZE, still, t + 30)


def test_key_follows_the_print_phase():
    """running -> paused / failed / finished each get their own sheet (status line, ETA wording)."""
    for opts in (OPTS, SheetOptions(show_progress=False)):
        keys = {cache_key(job(progress=0.4, status=st), SIZE, opts, 6000.0) for st in ("running", "pause", "failed")}
        assert len(keys) == 3
        # same print, size and options: one family, so an older step can stand in for a new one
        assert len({k[:24] for k in keys}) == 1


def test_key_steps_by_time_without_progress():
    still = SheetOptions(show_progress=False)
    t = 1_000_000 * 600.0
    j = job(progress=0.4)
    assert cache_key(j, SIZE, still, t) == cache_key(j, SIZE, still, t + 599)
    assert cache_key(j, SIZE, still, t) != cache_key(j, SIZE, still, t + 600)  # times and ETA move on
    done = job(progress=1.0, status="finish")
    assert cache_key(done, SIZE, still, t) == cache_key(done, SIZE, still, t + 6000)  # a finished sheet stays


def test_new_step_shows_the_newest_older_step_meanwhile(tmp_path):
    render = Render()
    r = Runner(tmp_path)
    r.ensure(job("a", progress=0.30), SIZE, OPTS, render)
    assert r.wait_idle()
    first = r.ensure(job("a", progress=0.30), SIZE, OPTS, render).ready_path
    render.gate = threading.Event()
    st = r.ensure(job("a", progress=0.34), SIZE, OPTS, render)  # the next 2 % step
    assert st.pending and st.ready_path == first and st.key != first.stem
    other = r.ensure(job("a", progress=0.34), (30, 20), OPTS, render)  # another size: nothing to stand in
    assert other.ready_path is None
    render.gate.set()
    assert r.wait_idle()
    assert r.ensure(job("a", progress=0.34), SIZE, OPTS, render).ready_path.stem == st.key


def test_render_error_is_reported_not_raised(tmp_path):
    def boom(*a):
        raise RuntimeError("bad art")
    r = Runner(tmp_path)
    r.ensure(job(), SIZE, OPTS, boom)
    r.wait_idle()
    st = r.ensure(job(), SIZE, OPTS, boom)
    assert st.ready_path is None and not st.pending and "bad art" in st.note


def test_keeps_newest_40_and_indexes_last_finished(tmp_path):
    render = Render()
    r = Runner(tmp_path)
    r.ensure(job("done", progress=1.0, status="finish", prefix="prn"), SIZE, OPTS, render)
    for i in range(45):
        r.ensure(job(f"j{i}", prefix="other"), SIZE, OPTS, render)
        r.wait_idle()
    pngs = list((tmp_path / "sheets").glob("*.png"))
    assert len(pngs) <= 41  # 40 newest + the indexed finished sheet
    last = r.last_finished("prn")
    assert last["job_key"] == "done" and (tmp_path / "sheets" / list(last["files"].values())[0]).exists()
    assert r.last_finished("other") is None
    assert json.loads((tmp_path / "sheets" / "index.json").read_text())["prn"]["title"] == "T"


def test_ai_source_flattens_to_1024():
    im = Image.open(io.BytesIO(ai_source_png(job())))
    assert im.size == (1024, 1024)


def test_ai_source_is_padded_not_stretched():
    j = job()
    b = io.BytesIO()
    Image.new("RGB", (200, 100), "black").save(b, "PNG")
    j.cover_png = b.getvalue()
    im = Image.open(io.BytesIO(ai_source_png(j))).convert("L")
    assert im.getpixel((512, 512)) < 20  # the picture, centred
    assert im.getpixel((512, 40)) > 235 and im.getpixel((512, 1000)) > 235  # white bands above and below


def test_ai_pictures_keep_the_newest_twelve(tmp_path):
    kie, render = Kie(), Render()
    r = Runner(tmp_path, kie_factory=kie)
    for i in range(15):
        r.ensure(job(f"p{i}"), SIZE, AI, render, kie_key="K", ai_daily_cap=99)
        assert r.wait_idle()
    assert kie.n == 15 and len(list((tmp_path / "ai").glob("*.png"))) == 12
    assert (tmp_path / "ai" / "p14.png").exists() and (tmp_path / "ai" / "state.json").exists()


def test_runner_passes_its_note_to_the_render(tmp_path):
    kie, render = Kie(fail=True), Render()
    r = Runner(tmp_path, kie_factory=kie)
    r.ensure(job("f"), SIZE, AI, render, kie_key="K")
    assert r.wait_idle()
    assert render.notes == ["AI picture failed: refused"]


def test_ai_without_source_image_renders_once(tmp_path):
    kie, render = Kie(), Render()
    r = Runner(tmp_path, kie_factory=kie)
    j = job("n")
    j.cover_png = None
    for _ in range(3):
        r.ensure(j, SIZE, AI, render, kie_key="K")
        r.wait_idle()
    assert len(render.calls) == 1 and kie.n == 0


def test_corrupt_cover_falls_back_to_plain_sheet(tmp_path):
    kie, render = Kie(), Render()
    r = Runner(tmp_path, kie_factory=kie)
    j = job("c")
    j.cover_png = b"garbage"
    r.ensure(j, SIZE, AI, render, kie_key="K")
    r.wait_idle()
    st = r.ensure(j, SIZE, AI, render, kie_key="K")
    r.wait_idle()
    assert st.ready_path and st.ready_path.exists() and not st.pending
    assert render.calls == [("c", False)] and kie.n == 0


def test_changed_options_never_show_a_sheet_drawn_with_other_options(tmp_path):
    """Blue -> white (or another panel or size): the old blue sheet must not stand in for the white one."""
    render = Render()
    r = Runner(tmp_path)
    r.ensure(job("a"), SIZE, OPTS, render)
    assert r.wait_idle()
    blue = r.ensure(job("a"), SIZE, OPTS, render).ready_path
    assert blue is not None
    for changed in (dataclasses.replace(OPTS, background="white"), dataclasses.replace(OPTS, panel="spectra"),
                    dataclasses.replace(OPTS, method="preview"), dataclasses.replace(OPTS, show_grid=False)):
        render.gate = threading.Event()
        st = r.ensure(job("a"), SIZE, changed, render)
        assert st.pending and st.ready_path is None, changed
        render.gate.set()
        assert r.wait_idle()
        assert r.ensure(job("a"), SIZE, changed, render).ready_path.stem == st.key != blue.stem
    assert r.newest(cache_key(job("a"), (30, 20), OPTS)) is None


def test_every_sheet_option_is_in_the_key():
    base = cache_key(job("a"), SIZE, OPTS)
    for f in dataclasses.fields(SheetOptions):
        v = getattr(OPTS, f.name)
        other = True if v is None else (not v) if isinstance(v, bool) else (
            ("#000000", "#ffffff", "#555555", "#ff0000") if isinstance(v, tuple) else v + "x")
        assert cache_key(job("a"), SIZE, dataclasses.replace(OPTS, **{f.name: other}))[:24] != base[:24], f.name


def test_runner_logs_start_finish_and_failures(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="blueprint.jobs")
    r = Runner(tmp_path)
    r.ensure(job("a"), SIZE, OPTS, Render())
    assert r.wait_idle()
    msgs = [x.getMessage() for x in caplog.records]
    key = cache_key(job("a"), SIZE, OPTS)
    assert any("queued" in m and key in m for m in msgs)
    assert any("render start" in m and key in m for m in msgs)
    assert any("render finish" in m and key in m for m in msgs)

    def boom(*a):
        raise RuntimeError("bad art")
    caplog.clear()
    r.ensure(job("b"), SIZE, OPTS, boom)
    assert r.wait_idle()
    fails = [x for x in caplog.records if x.levelno == logging.WARNING]
    assert fails and "render failed" in fails[0].getMessage() and fails[0].exc_info
    caplog.clear()
    r.ensure(job("b"), SIZE, OPTS, boom)  # inside the backoff
    assert any("not retrying" in x.getMessage() for x in caplog.records)


def test_live_sheet_moves_on_with_time_and_source():
    """While printing, a sheet is redrawn every TIME_STEP_S even if progress holds, and a sheet drawn
    before the sliced file (or cover) was known is drawn again once it is."""
    from blueprint.jobs import TIME_STEP_S
    j, t = job(progress=0.5), 6000.0
    assert cache_key(j, SIZE, OPTS, t) != cache_key(j, SIZE, OPTS, t + TIME_STEP_S)
    done = job(progress=1.0, status="finish")
    assert cache_key(done, SIZE, OPTS, t) == cache_key(done, SIZE, OPTS, t + 10 * TIME_STEP_S)
    bare = dataclasses.replace(j, cover_png=None)
    assert cache_key(bare, SIZE, OPTS, t)[24:] != cache_key(j, SIZE, OPTS, t)[24:]
    assert cache_key(dataclasses.replace(bare, has_3mf=True), SIZE, OPTS, t) != cache_key(bare, SIZE, OPTS, t)


def test_a_preview_uses_ai_art_already_paid_for(tmp_path):
    """No key on this render (an editor preview) still draws the AI picture that exists."""
    r = Runner(tmp_path)
    (tmp_path / "ai").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ai" / "k1.png").write_bytes(_png("red"))
    seen = []

    def fn(j, size, opts, ai_png, note):
        seen.append((ai_png, note))
        return Image.new("RGB", size, "white")

    r.ensure(job(), SIZE, dataclasses.replace(OPTS, method="ai"), fn)
    assert r.wait_idle(30)
    assert seen and seen[0][0] is not None and seen[0][1] == ""


def test_a_timed_out_paid_picture_is_collected_not_bought_again(tmp_path):
    """The kie.ai task is paid when it starts; a local timeout keeps its id, and a later render
    (a pause catch-up hours later) collects that picture instead of paying for another."""
    t = [1_000_000.0]
    kie, render = Kie(fail="timeout"), Render()
    r = Runner(tmp_path, kie_factory=kie, now=lambda: t[0])
    r.ensure(job("x"), SIZE, AI, render, kie_key="K")
    assert r.wait_idle()
    assert kie.n == 1 and render.calls == [("x", False)]
    kie.still_running = True
    t[0] += 16 * 60  # the task is re-checked at most every 15 min, never re-bought
    r.ensure(job("x"), (30, 20), AI, render, kie_key="K")
    assert r.wait_idle()
    assert kie.n == 1 and kie.collected == ["T1"]
    kie.still_running = False
    t[0] += 16 * 60
    r.ensure(job("x"), (31, 20), AI, render, kie_key="K")
    assert r.wait_idle()
    assert kie.n == 1 and kie.collected == ["T1", "T1"] and render.calls[-1] == ("x", True)
    assert (tmp_path / "ai" / "x.png").exists()


def test_wait_for_sheet_returns_the_finished_ai_sheet(tmp_path):
    """The browser can collect the sheet queued before the AI picture arrived."""
    from concurrent.futures import ThreadPoolExecutor

    kie, render = Kie(), Render()
    render.gate = threading.Event()
    r = Runner(tmp_path, kie_factory=kie)
    st = r.ensure(job(), SIZE, AI, render, kie_key="K")
    assert r.wait_for_sheet(st.key, timeout=0) is None
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(r.wait_for_sheet, st.key, timeout=2)
        render.gate.set()
        path = waiting.result(timeout=3)
    assert path and path.is_file()
    assert render.calls == [("k1", True)] and kie.n == 1
    assert path == r.newest(st.key)  # the final key includes ai_present=True
    assert r.wait_for_sheet("0" * 40, timeout=0) is None


def test_wait_for_sheet_does_not_collect_a_failed_ai_fallback(tmp_path):
    r = Runner(tmp_path, kie_factory=Kie(fail=True))
    st = r.ensure(job(), SIZE, AI, Render(), kie_key="K")
    assert r.wait_idle()
    assert r.newest(st.key).is_file()
    assert r.wait_for_sheet(st.key, timeout=0) is None
