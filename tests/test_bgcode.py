"""Small synthetic files, plus upstream libbgcode reference cross-checks."""

import struct
import zlib

import pytest

from blueprint.bgcode import decode_bgcode


def block(kind, data, compression=0, encoding=0):
    payload = zlib.compress(data) if compression == 1 else data
    header = struct.pack("<HHI", kind, compression, len(data))
    if compression:
        header += struct.pack("<I", len(payload))
    raw = header + struct.pack("<H", encoding) + payload
    return raw + struct.pack("<I", zlib.crc32(raw))


def fixture(compression=0, encoding=0, gcode=b"G1 X1 Y2 E3\n"):
    return (
        b"GCDE"
        + struct.pack("<IH", 1, 1)
        + block(3, b"nozzle_diameter=0.4\n", compression)
        + block(4, b"filament used [g]=1.2\n", compression)
        + block(1, gcode, compression, encoding)
    )


@pytest.mark.parametrize("compression", [0, 1])
def test_decode_metadata_and_gcode(compression):
    data = decode_bgcode(fixture(compression))
    assert b"; nozzle_diameter = 0.4" in data
    assert b"; filament used [g] = 1.2" in data
    assert data.endswith(b"G1 X1 Y2 E3\n")


def test_meatpack_both_encodings():
    # Enable packing, no spaces; G1 X1 E1 newline, low nibble first.
    packed = bytes([255, 255, 251, 255, 255, 247, 0x1D, 0x1E, 0x1B, 0x0C])
    for encoding in (1, 2):
        assert b"G1 X1 E1\n" in decode_bgcode(fixture(1, encoding, packed))


@pytest.mark.parametrize("mutation", ["crc", "truncated", "version", "size"])
def test_bad_binary_files_fail(mutation):
    data = bytearray(fixture())
    if mutation == "crc":
        data[-1] ^= 1
    elif mutation == "truncated":
        data = data[:-5]
    elif mutation == "version":
        data[4] = 2
    elif mutation == "size":
        data[14:18] = struct.pack("<I", 0xFFFFFFFF)
    with pytest.raises(ValueError):
        decode_bgcode(bytes(data))


def test_prusa_reference_decoder_output():
    # Paired libbgcode fixture and reference-decoded printer instructions.
    import hashlib
    import io
    from pathlib import Path
    from PIL import Image
    from blueprint.plain_gcode import read_gcode

    raw = (Path(__file__).parent / "fixtures/prusa-reference.bgcode").read_bytes()
    decoded = decode_bgcode(raw)
    commands = b"\n".join(
        line
        for line in decoded.splitlines()
        if line.strip() and not line.startswith(b";")
    )
    assert (
        hashlib.sha256(commands).hexdigest()
        == "2342f7c204d75ad84ebaaa74e0477c9a65f71c7f23f51d0bf969d851873eaf46"
    )
    facts = read_gcode(decoded)
    assert facts.layers == 31 and facts.material == "PLA"
    assert facts.weight_g == 0.75 and facts.nozzle_d == 0.4
    with Image.open(io.BytesIO(facts.preview_png)) as image:
        assert image.size == (313, 173)


@pytest.mark.parametrize("window,compression", [(11, 2), (12, 3)])
def test_heatshrink_literals_and_overlapping_backrefs(window, compression):
    # Literal G, literal 1, a distance=2 length=4 copy, then newline.
    bits = (
        "1"
        + f"{ord('G'):08b}"
        + "1"
        + f"{ord('1'):08b}"
        + "0"
        + f"{1:0{window}b}"
        + f"{3:04b}"
        + "1"
        + f"{10:08b}"
    )
    bits += "0" * (-len(bits) % 8)
    packed = int(bits, 2).to_bytes(len(bits) // 8, "big")
    body = struct.pack("<HHIIH", 1, compression, 7, len(packed), 0) + packed
    raw = (
        b"GCDE" + struct.pack("<IH", 1, 1) + body + struct.pack("<I", zlib.crc32(body))
    )
    assert decode_bgcode(raw) == b"G1G1G1\n"
