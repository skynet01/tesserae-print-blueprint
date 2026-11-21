"""Prusa binary G-code v1 to plain G-code, without native dependencies.

Format: https://github.com/prusa3d/libbgcode/blob/main/doc/specifications.md
MeatPack command semantics follow libbgcode (AGPL-3.0, Prusa Research).
Block CRCs and declared sizes are checked before decoding. Thumbnail blocks become
standard thumbnail comments so the plain G-code reader owns image/metadata parsing.
"""

from __future__ import annotations

import base64
import io
import re
import struct
import zlib

from PIL import Image

MAX_DECODED_BYTES = 128 * 1024 * 1024
MAX_BLOCK_BYTES = 32 * 1024 * 1024


def _heatshrink(data: bytes, size: int, window: int) -> bytes:
    """Heatshrink's MSB-first literal/back-reference stream (lookahead 4)."""
    bit = 0
    out = bytearray()

    def bits(n: int) -> int:
        nonlocal bit
        if bit + n > len(data) * 8:
            raise ValueError("truncated Heatshrink stream")
        value = 0
        for _ in range(n):
            value = (value << 1) | ((data[bit // 8] >> (7 - bit % 8)) & 1)
            bit += 1
        return value

    while len(out) < size:
        if bits(1):
            out.append(bits(8))
        else:
            distance, count = bits(window) + 1, bits(4) + 1
            if len(out) + count > size:
                raise ValueError("Heatshrink output exceeds block size")
            for _ in range(count):
                # Heatshrink's history window is initially zero-filled.
                out.append(out[-distance] if distance <= len(out) else 0)
    if len(data) * 8 - bit >= 8 or any(bits(1) for _ in range(len(data) * 8 - bit)):
        raise ValueError("extra Heatshrink data")
    return bytes(out)


def _meatpack(data: bytes) -> bytes:
    packing = nospace = False
    pending = 0
    buffered = None
    out = bytearray()
    i = 0
    while i < len(data):
        c = data[i]
        i += 1
        if c == 255 and i < len(data) and data[i] == 255:
            i += 1
            if i >= len(data):
                raise ValueError("truncated MeatPack command")
            cmd = data[i]
            i += 1
            if cmd == 251:
                packing = True
            elif cmd in (250, 249):
                packing = False
            elif cmd == 247:
                nospace = True
            elif cmd == 246:
                nospace = False
            continue
        if not packing:
            out.append(c)
        elif pending:
            out.append(c)
            if buffered is not None:
                out.append(buffered)
                buffered = None
            pending -= 1
        else:
            table = b"0123456789.E\nGX" if nospace else b"0123456789. \nGX"
            lo, hi = c & 15, c >> 4
            if lo == 15:
                pending = 1 + (hi == 15)
                buffered = table[hi] if hi != 15 else None
            else:
                out.append(table[lo])
                if lo != 12:
                    if hi == 15:
                        pending = 1
                    else:
                        out.append(table[hi])
        if len(out) > MAX_DECODED_BYTES:
            raise ValueError("MeatPack output too large")
    if pending:
        raise ValueError("truncated MeatPack literal")
    # libbgcode restores spaces in compact G lines; required by the wall parser.
    lines = []
    for line in bytes(out).splitlines(keepends=True):
        if line.startswith(b"G") and b" " not in line.rstrip():
            line = re.sub(rb"(?<!^)([XYZEFIJRSGPWHCA])", rb" \1", line)
        lines.append(line)
    return b"".join(lines)


def decode_bgcode(data: bytes) -> bytes:
    """Decode v1 blocks (None/Deflate/Heatshrink, raw/MeatPack G-code)."""
    if len(data) < 10 or data[:4] != b"GCDE":
        raise ValueError("not a binary G-code file")
    version, checksum = struct.unpack_from("<IH", data, 4)
    if version != 1 or checksum not in (0, 1):
        raise ValueError("unsupported binary G-code version or checksum")
    pos = 10
    chunks = []
    total = 0
    thumbnail = b""
    thumbnail_area = 0
    while pos < len(data):
        start = pos
        if len(data) - pos < 8:
            raise ValueError("truncated block header")
        kind, compression, size = struct.unpack_from("<HHI", data, pos)
        pos += 8
        if kind > 5 or compression > 3 or size > MAX_BLOCK_BYTES:
            raise ValueError("unsupported or oversized block")
        stored = size
        if compression:
            if len(data) - pos < 4:
                raise ValueError("truncated compressed block")
            stored = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        params_size = 6 if kind == 5 else 2
        end = pos + params_size + stored
        if stored > MAX_BLOCK_BYTES or end + (4 if checksum else 0) > len(data):
            raise ValueError("truncated or oversized block data")
        params = data[pos : pos + params_size]
        raw = data[pos + params_size : end]
        if (
            checksum
            and zlib.crc32(data[start:end]) != struct.unpack_from("<I", data, end)[0]
        ):
            raise ValueError("binary G-code checksum mismatch")
        pos = end + (4 if checksum else 0)
        if compression == 1:
            decoder = zlib.decompressobj()
            try:
                raw = decoder.decompress(raw, size + 1)
            except zlib.error as err:
                raise ValueError("invalid Deflate stream") from err
            if not decoder.eof or decoder.unused_data:
                raise ValueError("incomplete or trailing Deflate stream")
        elif compression in (2, 3):
            raw = _heatshrink(raw, size, 11 if compression == 2 else 12)
        if len(raw) != size:
            raise ValueError("binary G-code block size mismatch")
        encoding = struct.unpack_from("<H", params)[0]
        if kind == 1:
            if encoding not in (0, 1, 2):
                raise ValueError("unsupported G-code encoding")
            chunk = _meatpack(raw) if encoding else raw
        elif kind == 5:
            if encoding not in (0, 1, 2):
                raise ValueError("unsupported thumbnail format")
            width, height = struct.unpack_from("<HH", params, 2)
            chunk = b""
            if width and height and width * height <= 4_000_000:
                try:
                    with Image.open(io.BytesIO(raw)) as im:
                        if im.size != (width, height):
                            raise ValueError("thumbnail size mismatch")
                        png = io.BytesIO()
                        im.save(png, "PNG")
                    encoded = base64.b64encode(png.getvalue())
                    picture = (
                        f"\n; thumbnail begin {width}x{height} {len(encoded)}\n; ".encode()
                        + b"\n; ".join(
                            encoded[i : i + 78] for i in range(0, len(encoded), 78)
                        )
                        + b"\n; thumbnail end\n"
                    )
                    if width * height > thumbnail_area:
                        thumbnail, thumbnail_area = picture, width * height
                except (OSError, ValueError):
                    pass  # a bad optional preview must not discard usable toolpaths
        else:
            if encoding != 0:
                raise ValueError("unsupported metadata encoding")
            lines = []
            for line in raw.splitlines():
                if b"=" in line:
                    key, value = line.split(b"=", 1)
                    lines.append(b"; " + key.strip() + b" = " + value.strip() + b"\n")
            chunk = b"".join(lines)
        total += len(chunk)
        if total > MAX_DECODED_BYTES:
            raise ValueError("decoded binary G-code too large")
        chunks.append(chunk)
    return thumbnail + b"".join(chunks)
