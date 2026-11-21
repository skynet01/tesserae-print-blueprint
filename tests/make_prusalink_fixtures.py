"""Regenerate the small PrusaLink fixtures (no slicer or native tools required)."""

import base64
import io
import json
import struct
import zlib
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).parent / "fixtures"


def block(kind, raw, params=b"\0\0"):
    packed = zlib.compress(raw)
    body = struct.pack("<HHII", kind, 1, len(raw), len(packed)) + params + packed
    return body + struct.pack("<I", zlib.crc32(body))


def make():
    im = Image.new("RGB", (128, 128), "white")
    d = ImageDraw.Draw(im)
    d.polygon(
        [(25, 42), (65, 20), (108, 43), (108, 90), (65, 112), (25, 88)],
        fill="#8a8a8a",
        outline="black",
        width=2,
    )
    d.line([(25, 42), (65, 65), (108, 43)], fill="black", width=2)
    d.line([(65, 65), (65, 112)], fill="black", width=2)
    out = io.BytesIO()
    im.save(out, "PNG")
    png = out.getvalue()
    meta = (
        "printer_model=MK4\nlayer_height=0.2\nnozzle_diameter=0.4\nfilament_type=PLA\n"
        "filament used [g]=1.2\nfilament used [mm]=400\nperimeters=2\n"
        "fill_density=15%\nfill_pattern=grid\nsupport_material=0\n"
        "estimated printing time (normal mode)=17m 26s\n"
    )
    gcode = "G90\nM83\n"
    for z in (0.2, 0.4, 0.6):
        gcode += f";LAYER_CHANGE\n;Z:{z}\nG1 Z{z}\n;TYPE:External perimeter\nG1 X0 Y0\nG1 X20 Y0 E1\nG1 X20 Y20 E1\nG1 X0 Y20 E1\nG1 X0 Y0 E1\n"
    encoded = base64.b64encode(png).decode()
    thumb = f"; thumbnail begin 128x128 {len(encoded)}\n; {encoded}\n; thumbnail end\n"
    plain = (
        "".join("; " + line.replace("=", " = ", 1) + "\n" for line in meta.splitlines())
        + thumb
        + gcode
    )
    (ROOT / "prusalink-cube.gcode").write_text(plain)
    binary = (
        b"GCDE"
        + struct.pack("<IH", 1, 1)
        + block(3, b"printer_model=MK4\n")
        + block(5, png, struct.pack("<HHH", 0, 128, 128))
        + block(4, meta.encode())
        + block(2, b"")
        + block(1, gcode.encode())
    )
    (ROOT / "prusalink-cube.bgcode").write_bytes(binary)
    docs = {
        "status": {
            "printer": {
                "state": "PRINTING",
                "temp_nozzle": 214.9,
                "target_nozzle": 215,
                "temp_bed": 59.5,
                "target_bed": 60,
                "axis_z": 0.4,
                "speed": 100,
            },
            "job": {
                "id": 42,
                "progress": 50,
                "time_remaining": 520,
                "time_printing": 526,
            },
        },
        "info": {"name": "Workshop Prusa", "nozzle_diameter": 0.4, "serial": "FIXTURE"},
        "job": {
            "id": 42,
            "state": "PRINTING",
            "file": {
                "name": "CUBE~1.BGC",
                "display_name": "Cube.bgcode",
                "path": "/usb/Cube.bgcode",
                "m_timestamp": 1648042843,
                "size": len(binary),
                "refs": {"download": "/usb/Cube.bgcode"},
            },
        },
    }
    for name, doc in docs.items():
        (ROOT / f"prusalink-{name}.json").write_text(json.dumps(doc, indent=2) + "\n")


if __name__ == "__main__":
    make()
