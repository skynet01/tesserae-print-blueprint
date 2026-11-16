"""Turn a raw 3MF/task name into a sheet title the drafting font can draw."""
from __future__ import annotations

import re


def pretty_title(raw: str, fallback: str) -> str:
    n = "".join(ch for ch in (raw or "") if ord(ch) < 0x250)  # osifont lacks CJK etc.
    n = re.sub(r"\(\s*\)", "", n)
    if not re.search(r"[A-Za-z0-9]", n):
        return fallback
    n = re.sub(r"_plate_\d+$", "", n)
    n = n.replace("_", " ")
    n = re.sub(r"\s+", " ", n).strip()
    return n.upper() or fallback
