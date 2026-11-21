# Pytest imports these fixtures for discovery; parameter names intentionally match.
# ruff: noqa: F811
"""PrusaLink rendering must work without the Home Assistant widget."""

import time

from test_server import server  # noqa: F401
from test_prusalink import printer  # noqa: F401


def test_prusa_fetch_without_ha(server, printer, tmp_path, monkeypatch):
    settings, calls = printer(binary=True)
    monkeypatch.setattr(
        server,
        "_core",
        lambda: (_ for _ in ()).throw(AssertionError("PrusaLink must not call HA")),
    )
    monkeypatch.setattr(
        server, "_url", lambda path: "/plugins/print_blueprint/image/" + path.name
    )
    ctx = {"data_dir": str(tmp_path), "cell_w": 360, "cell_h": 240}
    start = time.monotonic()
    data = server.fetch({"source": "prusalink"}, settings, ctx=ctx)
    assert time.monotonic() - start < server.FETCH_BUDGET_S
    assert data["title"] == "Cube" and data["printer"] == "Workshop Prusa"
    assert data.get("image") or data.get("live_pending")
    assert server._runner(tmp_path).wait_idle(15)
    data = server.fetch({"source": "prusalink"}, settings, ctx=ctx)
    assert data["image"]
    assert calls["/usb/Cube.bgcode"] == 1


def test_prusa_live_preview_uses_source(server, printer, tmp_path, monkeypatch):
    settings, calls = printer()
    monkeypatch.setattr(
        server, "_core", lambda: (_ for _ in ()).throw(AssertionError("No HA"))
    )
    data = server.fetch(
        {"source": "prusalink", "editor_preview": "live"},
        settings,
        ctx={"data_dir": str(tmp_path), "preview": True, "cell_w": 240, "cell_h": 320},
    )
    assert calls["/api/v1/status"] > 0
    assert data.get("image")


def test_prusa_method_choices_do_not_query_ha(server, monkeypatch):
    monkeypatch.setattr(
        server, "_core", lambda: (_ for _ in ()).throw(AssertionError("No HA"))
    )
    rows = server.choices("method", {"source": "prusalink"})
    assert not rows[0].get("disabled")
