"""Convert Home Assistant values (which follow the user's unit system) to metric.

An unit these functions do not know gives None (the value is then unknown), never an exception."""
from __future__ import annotations

_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8, "yd": 914.4}


def _norm(unit: str | None) -> str:
    return (unit or "").strip().lower().replace("°", "").replace("º", "")


def to_c(value: float, unit: str | None) -> float | None:
    u = _norm(unit)
    v = float(value)
    if u in ("", "c", "degc", "celsius"):
        return v
    if u in ("f", "degf", "fahrenheit"):
        return (v - 32.0) * 5.0 / 9.0
    if u in ("k", "kelvin"):
        return v - 273.15
    return None


def to_mm(value: float, unit: str | None) -> float | None:
    k = _MM.get(_norm(unit) or "mm")
    return float(value) * k if k is not None else None


def to_m(value: float, unit: str | None) -> float | None:
    mm = to_mm(value, unit or "m")
    return mm / 1000.0 if mm is not None else None
