"""Paid submissions are counted at the fake provider boundary; collection is free."""
import dataclasses
import json
import multiprocessing
import sys
import threading
import types

import pytest

from blueprint import kie as kie_module
from blueprint import files
from blueprint.jobs import Runner, cache_key
from blueprint.kie import KieClient, KieError
from test_kie import Http
from test_jobs import AI, OPTS, SIZE, Kie, Render, _png, job


@pytest.fixture(autouse=True)
def older_core(monkeypatch):
    monkeypatch.setitem(sys.modules, "app", types.ModuleType("app"))
    monkeypatch.delitem(sys.modules, "app.artwork", raising=False)


class Provider(Kie):
    def repaint(self, png, prompt, timeout_s=240, on_task=None):
        self.n += 1
        if self.fail == "crash":
            raise SystemExit("lost response after create")
        if self.fail == "ambiguous":
            raise KieError("lost response after create")
        on_task(f"T{self.n}")
        if self.fail == "decode":
            return b"not a picture"
        if self.fail == "terminal":
            raise getattr(kie_module, "KieTaskFailed", KieError)("generation failed")
        if self.fail:
            raise KieError(str(self.fail))
        return _png("black")


def draw(runner, j, render=None, size=SIZE, cap=12):
    runner.ensure(j, size, AI, render or Render(), kie_key="K", ai_daily_cap=cap)
    assert runner.wait_idle()


@pytest.mark.parametrize("failure", ["timed out after 240 s", "download failed (HTTP 503)", "decode"])
def test_paid_task_survives_errors_restart_and_days(tmp_path, failure):
    clock = [1_000_000.0]
    provider, render = Provider(failure), Render()
    r = Runner(tmp_path, kie_factory=provider, now=lambda: clock[0])
    draw(r, job(), render)
    assert provider.n == 1 and render.calls[-1] == ("k1", False)
    clock[0] += 3 * 86400
    r = Runner(tmp_path, kie_factory=provider, now=lambda: clock[0])
    draw(r, job(), render, cap=0)  # collection ignores both caps and the old retry expiry
    assert provider.n == 1 and provider.collected == ["T1"]
    assert render.calls[-1] == ("k1", True)


def test_ambiguous_submission_expires_closed_until_explicit_reset(tmp_path):
    clock = [1_000_000.0]
    provider = Provider("crash")
    r = Runner(tmp_path, kie_factory=provider, now=lambda: clock[0])
    draw(r, job())
    clock[0] += 3 * 86400
    r = Runner(tmp_path, kie_factory=provider, now=lambda: clock[0])
    render = Render()
    draw(r, job(), render, size=(30, 20))
    assert provider.n == 1
    assert "expired" in render.notes[-1] and "state.json" in render.notes[-1]
    path = tmp_path / "ai/state.json"
    state = json.loads(path.read_text())
    del state["tasks"]["k1"]  # documented operator reset after checking the provider account
    path.write_text(json.dumps(state))
    provider.fail = False
    draw(r, job(), size=(31, 20))
    assert provider.n == 2


def test_terminal_failure_is_the_only_automatic_retry(tmp_path):
    clock = [1_000_000.0]
    provider = Provider("terminal")
    r = Runner(tmp_path, kie_factory=provider, now=lambda: clock[0])
    draw(r, job())
    draw(r, job(), size=(30, 20))
    assert provider.n == 1
    clock[0] += 6 * 3600 + 1
    provider.fail = False
    draw(r, job())
    assert provider.n == 2


def test_missing_or_bad_source_does_not_block_a_later_cover(tmp_path):
    provider = Provider()
    r = Runner(tmp_path, kie_factory=provider)
    for key, source in [("missing", None), ("bad", b"bad")]:
        j = dataclasses.replace(job(key), cover_png=source)
        draw(r, j)
        assert provider.n == (0 if key == "missing" else 1)
        j.cover_png = _png()
        draw(r, j)
    assert provider.n == 2


def test_finished_fallback_is_redrawn_after_other_size_gets_ai(tmp_path):
    clock = [1_000_000.0]
    provider, render = Provider("timed out"), Render()
    r = Runner(tmp_path, kie_factory=provider, now=lambda: clock[0])
    j = job(status="finish", progress=1)
    draw(r, j, render)
    clock[0] += 16 * 60
    draw(r, j, render, size=(30, 20))
    before = len(render.calls)
    draw(r, j, render)
    assert provider.n == 1 and len(render.calls) == before + 1
    assert render.calls[-1] == ("k1", True)


def test_listed_3mf_does_not_share_parsed_cache_key():
    from types import SimpleNamespace
    j = job(status="finish")
    j.has_3mf = True
    before = cache_key(j, SIZE, OPTS)
    j.slice = SimpleNamespace(source="file.3mf", preview_png=None)
    assert cache_key(j, SIZE, OPTS) != before


def test_pruned_completed_picture_is_never_bought_again(tmp_path):
    provider = Provider()
    r = Runner(tmp_path, kie_factory=provider)
    draw(r, job())
    (tmp_path / "ai/k1.png").unlink()
    draw(r, job(), size=(30, 20))
    assert provider.n == 1 and provider.collected == ["T1"]


def test_two_runners_ensure_only_submit_once(tmp_path):
    provider = Provider()
    runners = [Runner(tmp_path, kie_factory=provider) for _ in range(2)]
    gate = threading.Barrier(2)

    def start(r):
        gate.wait()
        draw(r, job())

    threads = [threading.Thread(target=start, args=(r,)) for r in runners]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
        assert not t.is_alive()
    assert provider.n == 1


def _process_ai(data_dir, gate, paid):
    class Paid(Provider):
        def repaint(self, *args, **kwargs):
            with paid.get_lock():
                paid.value += 1
            return super().repaint(*args, **kwargs)

    gate.wait()
    Runner(data_dir, kie_factory=Paid())._ai(job(), "K", 12)


def test_two_processes_only_submit_once(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    gate, paid = ctx.Barrier(2), ctx.Value("i", 0)
    processes = [ctx.Process(target=_process_ai, args=(tmp_path, gate, paid)) for _ in range(2)]
    for p in processes:
        p.start()
    for p in processes:
        p.join(15)
        assert p.exitcode == 0
    assert paid.value == 1


@pytest.mark.parametrize("cap,ceiling,expected", [(0, False, 0), (12, True, 0), (12, False, 1)])
def test_shared_ceiling_and_local_cap_reserve_before_provider(tmp_path, monkeypatch, cap, ceiling, expected):
    charges = []

    def charge(plugin_id, collection, cents, model, debit):
        if ceiling:
            raise RuntimeError("server monthly ceiling reached")
        debit()
        charges.append((plugin_id, collection, cents, model))

    monkeypatch.setattr(sys.modules["app"], "artwork", types.SimpleNamespace(charge=charge), raising=False)

    class Charged(Provider):
        def repaint(self, *args, **kwargs):
            assert charges == [("print_blueprint", "engraving", 2, "google/nano-banana-edit")]
            state = json.loads((tmp_path / "ai/state.json").read_text())
            assert state["count"] == 1 and state["tasks"]["k1"]["state"] == "submitting"
            return super().repaint(*args, **kwargs)

    provider, render = Charged(), Render()
    r = Runner(tmp_path, kie_factory=provider)
    draw(r, job(), render, cap=cap)
    assert provider.n == expected
    if ceiling and cap:
        assert "ceiling" in render.notes[-1]


def test_a_core_with_artwork_but_no_shared_charge_still_buys_once(tmp_path, monkeypatch):
    """A legacy HA core has app.artwork without charge(): the local cap alone applies, one purchase."""
    monkeypatch.setattr(sys.modules["app"], "artwork", types.SimpleNamespace(), raising=False)
    provider, render = Provider(), Render()
    draw(Runner(tmp_path, kie_factory=provider), job(), render)
    assert provider.n == 1
    state = json.loads((tmp_path / "ai/state.json").read_text())
    assert state["count"] == 1


def test_unreadable_state_fails_closed(tmp_path):
    (tmp_path / "ai").mkdir()
    (tmp_path / "ai/state.json").write_text("{")
    provider = Provider()
    draw(Runner(tmp_path, kie_factory=provider), job())
    assert provider.n == 0


def test_task_id_save_failure_keeps_submission_guard(tmp_path, monkeypatch):
    provider = Provider()
    r = Runner(tmp_path, kie_factory=provider)
    write = r._write

    def fail_id_write(path, state):
        if path.name == "state.json" and state.get("tasks", {}).get("k1", {}).get("id"):
            raise OSError("disk error saving task id")
        write(path, state)

    monkeypatch.setattr(r, "_write", fail_id_write)
    draw(r, job())
    assert provider.n == 1
    draw(Runner(tmp_path, kie_factory=provider), job(), size=(30, 20))
    assert provider.n == 1


def test_paid_collection_does_not_need_the_original_cover(tmp_path):
    clock = [1_000_000.0]
    provider = Provider("timed out")
    r = Runner(tmp_path, kie_factory=provider, now=lambda: clock[0])
    draw(r, job(status="finish"))
    bare = dataclasses.replace(job(status="finish"), cover_png=None)
    draw(r, bare)
    clock[0] += 16 * 60
    render = Render()
    draw(r, bare, render)
    assert provider.n == 1 and provider.collected == ["T1"]
    assert render.calls[-1] == ("k1", True)


def test_expired_submission_message_reaches_an_existing_sheet(tmp_path):
    clock = [1_000_000.0]
    provider = Provider("ambiguous")
    r = Runner(tmp_path, kie_factory=provider, now=lambda: clock[0])
    draw(r, job(status="finish"))
    clock[0] += 3 * 86400
    st = r.ensure(job(status="finish"), SIZE, AI, Render(), kie_key="K")
    assert not st.pending and st.ready_path
    assert "expired" in st.note and "state.json" in st.note
    assert provider.n == 1


def test_worker_keys_sheet_from_prepared_source(tmp_path):
    from types import SimpleNamespace
    j = job(status="finish")
    full = dataclasses.replace(j, slice=SimpleNamespace(source="parsed.3mf", preview_png=None))
    r = Runner(tmp_path)
    render = Render()
    r.ensure(j, SIZE, OPTS, render, prepare_fn=lambda _: full)
    assert r.wait_idle()
    assert (tmp_path / "sheets" / f"{cache_key(full, SIZE, OPTS)}.png").exists()
    st = r.ensure(full, SIZE, OPTS, render)
    assert not st.pending and len(render.calls) == 1


def test_failed_state_flush_prevents_paid_submission(tmp_path, monkeypatch):
    def disk_failure(fd):
        raise OSError("state could not be flushed to disk")

    monkeypatch.setattr(files.os, "fsync", disk_failure)
    provider = Provider()
    draw(Runner(tmp_path, kie_factory=provider), job())
    assert provider.n == 0


@pytest.mark.parametrize("download", [(503, b"busy"), (200, b"bad image")])
def test_real_client_keeps_task_through_failed_downloads_and_collections(tmp_path, download):
    clock = [1_000_000.0]
    success = (200, {"code": 200, "data": {"state": "success", "resultJson": {"resultUrls": ["image"]}}})
    http = Http([
        (200, {"code": 200, "data": {"downloadUrl": "source"}}),
        (200, {"code": 200, "data": {"taskId": "T1"}}),
        success, download, success, download, success, (200, _png("black")),
    ])
    client = KieClient("K", http=http, sleep=lambda _: None)
    r = Runner(tmp_path, kie_factory=lambda _: client, now=lambda: clock[0])
    j = job(status="finish")
    render = Render()
    for _ in range(3):
        draw(r, j, render)
        clock[0] += 16 * 60
    assert render.calls == [("k1", False), ("k1", False), ("k1", True)]
    assert sum("createTask" in url for _, url, _ in http.calls) == 1
    assert sum("recordInfo" in url for _, url, _ in http.calls) == 3


def test_legacy_attempt_without_task_id_needs_manual_recovery(tmp_path):
    provider = Provider()
    (tmp_path / "ai").mkdir()
    (tmp_path / "ai/state.json").write_text(json.dumps({"day": "old", "count": 1, "tried": {"k1": 100}}))
    render = Render()
    draw(Runner(tmp_path, kie_factory=provider), job(), render)
    assert provider.n == 0 and "state.json" in render.notes[-1]
