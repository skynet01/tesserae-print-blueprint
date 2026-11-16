"""Build the original geometric test fixtures. No downloads or model inputs."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image, ImageDraw

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def cover() -> bytes:
    """A post on a base plate, drawn from six shaded polygons."""
    image = Image.new("RGBA", (320, 320))
    draw = ImageDraw.Draw(image)
    draw.polygon([(30, 205), (220, 160), (290, 210), (100, 265)], fill=(185, 190, 200))
    draw.polygon([(30, 205), (100, 265), (100, 285), (30, 225)], fill=(95, 105, 120))
    draw.polygon([(100, 265), (290, 210), (290, 230), (100, 285)], fill=(125, 135, 150))
    draw.polygon([(120, 45), (175, 30), (215, 60), (160, 75)], fill=(225, 230, 235))
    draw.polygon([(120, 45), (160, 75), (160, 230), (120, 200)], fill=(105, 115, 130))
    draw.polygon([(160, 75), (215, 60), (215, 210), (160, 230)], fill=(155, 165, 180))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def sliced_model(name: str, parts: list[tuple], layers: int, weight: float, length: float) -> bytes:
    """A valid 3MF envelope with closed box meshes and Bambu-style sliced metadata."""
    model = ET.Element("model", xmlns=NS, unit="millimeter")
    ET.SubElement(model, "metadata", name="Title").text = name
    resources, build = ET.SubElement(model, "resources"), ET.SubElement(model, "build")
    config = ET.Element("config")
    plate = ET.SubElement(config, "plate")
    facts = {"prediction": "120", "weight": str(weight), "first_layer_time": "5",
             "nozzle_diameters": "0.4", "support_used": "false"}
    for key, value in facts.items():
        ET.SubElement(plate, "metadata", key=key, value=value)
    ET.SubElement(plate, "filament", type="PLA", color="#FFFFFF", used_g=str(weight), used_m=str(length))
    lines = [f"; total layer number: {layers}", f"; max_z_height: {layers * .2:.1f}"]
    settings = {"layer_height": "0.2", "wall_loops": "2", "sparse_infill_density": "15%",
                "sparse_infill_pattern": "grid", "filament_density": "1.24", "line_width": "0.42",
                "filament_settings_id": "Generic PLA @ Test printer", "nozzle_temperature": "220",
                "curr_bed_type": "Textured PEI Plate", "textured_plate_temp": "55",
                "support_threshold_angle": "30", "nozzle_diameter": "0.4"}
    lines += [f"; {key} = {value}" for key, value in settings.items()]
    lines += ["G90", "M83"]
    triangles = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                 (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    for ident, (label, x, y, width, depth) in enumerate(parts, 1):
        obj = ET.SubElement(resources, "object", id=str(ident), type="model", name=label)
        mesh = ET.SubElement(obj, "mesh")
        vertices, faces = ET.SubElement(mesh, "vertices"), ET.SubElement(mesh, "triangles")
        for z in (0, layers * .2):
            for px, py in [(x, y), (x + width, y), (x + width, y + depth), (x, y + depth)]:
                ET.SubElement(vertices, "vertex", x=str(px), y=str(py), z=f"{z:.1f}")
        for a, b, c in triangles:
            ET.SubElement(faces, "triangle", v1=str(a), v2=str(b), v3=str(c))
        ET.SubElement(build, "item", objectid=str(ident))
        ET.SubElement(plate, "object", name=label, identify_id=str(ident))
    for layer in range(1, layers + 1):
        lines += ["; CHANGE_LAYER", f"; Z_HEIGHT: {layer * .2:.1f}"]
        for _, x, y, width, depth in parts:
            for offset, feature in [(0, "Outer wall"), (.4, "Inner wall")]:
                lines += [f";TYPE:{feature}", f"; FEATURE: {feature}",
                          f"G0 X{x + offset} Y{y + offset} Z{layer * .2:.1f}"]
                points = [(x + width - offset, y + offset), (x + width - offset, y + depth - offset),
                          (x + offset, y + depth - offset), (x + offset, y + offset)]
                lines += [f"G1 X{px} Y{py} E1 F1800" for px, py in points]
    bbox = [min(p[1] for p in parts), min(p[2] for p in parts),
            max(p[1] + p[3] for p in parts), max(p[2] + p[4] for p in parts)]
    members = {
        "[Content_Types].xml": b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="config" ContentType="application/octet-stream"/><Default Extension="json" ContentType="application/json"/><Default Extension="gcode" ContentType="text/plain"/><Default Extension="png" ContentType="image/png"/></Types>',
        "_rels/.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="model" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>',
        "3D/3dmodel.model": ET.tostring(model),
        "Metadata/slice_info.config": ET.tostring(config),
        "Metadata/project_settings.config": json.dumps(settings).encode(),
        "Metadata/model_settings.config": ET.tostring(config),
        "Metadata/plate_1.json": json.dumps({"bbox_all": bbox}).encode(),
        "Metadata/plate_1.gcode": ("\n".join(lines) + "\n").encode(),
        "Metadata/plate_1.png": cover(),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for filename, data in members.items():
            info = zipfile.ZipInfo(filename, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return output.getvalue()


def write_fixtures(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "synthetic-plate.3mf").write_bytes(sliced_model(
        "Synthetic Plate", [("Test plate", 80, 80, 20, 12), ("Test tab", 104, 80, 6, 6)], 12, 1.2, .4))
    (directory / "synthetic-block.3mf").write_bytes(sliced_model(
        "Synthetic Block", [("Test block", 80, 80, 8, 6)], 16, 2.4, .8))
    (directory / "synthetic-cover.png").write_bytes(cover())
    files = []
    for index, name in enumerate(("synthetic-block.3mf", "synthetic-plate.3mf")):
        files.append({"filename": name, "path": f"DEMO_P1S_001/prints/cache/{name}",
                      "size": (directory / name).stat().st_size, "printer_serial": "DEMO_P1S_001",
                      "printer_name": "Demo P1S", "printer_model": "P1S",
                      "modified": f"2026-01-01T12:0{index}:00"})
    (directory / "print_history.json").write_text(json.dumps({"files": files}, indent=2) + "\n")


if __name__ == "__main__":
    write_fixtures(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "fixtures")
