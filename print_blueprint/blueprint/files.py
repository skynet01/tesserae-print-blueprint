"""Small file helpers: atomic writes that never share a temp name, and keep-the-newest pruning."""
from __future__ import annotations

import io
import os
import tempfile
import time
from pathlib import Path

from PIL import Image, PngImagePlugin

KEEP_PRINTS = 12  # 3MFs, covers and AI pictures kept on disk, newest first
_STALE_PART_S = 3600  # a temp file this old belongs to a write that died


def write_atomic(path: Path, data: bytes, *, durable: bool = False) -> None:
    """Write `data` to `path` through a temp file of its own, so concurrent writers never clash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
        if durable:
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def prune(folder: Path, pattern: str, keep: int = KEEP_PRINTS) -> None:
    """Delete all but the `keep` newest files matching `pattern` in `folder`, and stale temp files."""
    folder = Path(folder)
    try:
        files = sorted(folder.glob(pattern), key=_mtime, reverse=True)
        parts = list(folder.glob("*.part"))
    except OSError:
        return
    old = time.time() - _STALE_PART_S
    for p in files[keep:] + [p for p in parts if _mtime(p) < old]:
        try:
            p.unlink()
        except OSError:
            pass


def png_bytes(img: Image.Image) -> bytes:
    """`img` as PNG; a footnote in img.info["note"] is kept as a text chunk (read back with
    Image.open(path).text["note"])."""
    info = PngImagePlugin.PngInfo()
    if img.info.get("note"):
        info.add_text("note", str(img.info["note"]))
    buf = io.BytesIO()
    img.save(buf, "PNG", pnginfo=info)
    return buf.getvalue()
