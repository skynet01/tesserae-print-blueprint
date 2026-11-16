"""Data model shared by the readers, renderer and Home Assistant source."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Tray:
    type: str
    color: str
    active: bool
    empty: bool


@dataclass
class Tool:
    id: int
    state: str
    filament: str
    temp_c: float | None
    hours: float | None


@dataclass
class SliceFacts:
    layer_h: float
    layers: int
    walls: int | None
    infill_pct: float | None
    pattern: str | None
    support: bool | None  # None when the slice metadata does not say
    support_type: str | None
    support_angle: float | None
    envelope_mm: tuple[float, float, float]
    prediction_s: float
    weight_g: float
    length_m: float
    density: float | None
    line_w: float | None
    first_layer_s: float | None
    nozzle_d: float | None
    nozzle_temp: float | None
    bed_temp: float | None
    bed_type: str | None
    material: str | None
    filament_name: str | None
    colors: list[str]
    objects: list[str]
    bbox_all: tuple[float, float, float, float] | None
    gcode: str
    preview_png: bytes | None
    layer_z: list[float] = field(default_factory=list)  # Z of every layer, from the G-code's Z_HEIGHT comments
    source: str = ""  # the cached 3MF this came from (with its size and mtime); keys the parsed-walls cache


@dataclass
class PrintJob:
    prefix: str
    model: str
    printer_name: str
    serial: str
    title: str
    status: str  # HA print_status (running, pause, finish, idle, failed, ...)
    progress: float  # 0..1; layer/layers when both known, else print_progress/100
    layer: int | None
    layers: int | None
    start_ts: float | None
    end_ts: float | None
    nozzle_d: float | None
    nozzle_type: str | None
    nozzle_temp: tuple[float, float] | None
    bed_temp: tuple[float, float] | None
    chamber_c: float | None
    bed_type: str | None
    material: str | None
    weight_g: float | None
    length_m: float | None
    fans: dict[str, float]
    speed: str | None
    usage_h: float | None
    objects: int | None
    skipped: int | None
    ams: list[Tray]
    ams_humidity: str | None
    tools: list[Tool]
    slice: SliceFacts | None
    cover_png: bytes | None
    job_key: str  # stable id of this print
    has_3mf: bool = False  # the sliced file is in the printer's history (downloaded or not yet)


@dataclass
class SheetOptions:
    method: str = "toolpath"  # toolpath | preview | ai
    background: str = "blue"  # blue | white | black
    panel: str = "colour"  # colour | grey16 | grey4 | spectra | bw (resolved, never "auto")
    show_icons: bool = True
    callout_notes: bool = True
    fill_space: bool = True
    show_grid: bool = True
    show_subtitle: bool = True
    show_progress: bool = True  # False: finished part, no current-layer marker or nozzle-layer callout
    clock: str = "h23"  # h12 (2:47 PM) | h23 (14:47): the device locale's clock style
    tz: str = ""  # IANA zone the times are shown in (Tesserae's setting); "" = the process's own
