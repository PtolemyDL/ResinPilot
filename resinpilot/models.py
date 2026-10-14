"""Shared data models for ResinPilot."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ConfidenceSource(str, Enum):
    BASELINE = "baseline"
    INTERPOLATED = "interpolated"
    CALIBRATED = "calibrated"
    TEMP_ADJUSTED = "temp-adjusted"
    LEARNED = "learned"


class PrintOutcome(str, Enum):
    SUCCESS = "success"
    FAIL = "fail"
    PARTIAL = "partial"


class GeometryClass(str, Enum):
    """Part geometry categories that affect suction / cure needs."""

    FINE_DETAIL = "fine_detail"  # small / delicate features
    NORMAL = "normal"
    LARGE_SOLID = "large_solid"  # bulky body, more bottom adhesion demand
    HIGH_SUCTION = "high_suction"  # large XY footprint / raft-like area

    @property
    def label(self) -> str:
        return {
            GeometryClass.FINE_DETAIL: "Fine detail",
            GeometryClass.NORMAL: "Normal",
            GeometryClass.LARGE_SOLID: "Large solid",
            GeometryClass.HIGH_SUCTION: "High suction (large XY)",
        }[self]

    @property
    def suction_score(self) -> float:
        """0–1 feature for regression / heuristics."""
        return {
            GeometryClass.FINE_DETAIL: 0.15,
            GeometryClass.NORMAL: 0.40,
            GeometryClass.LARGE_SOLID: 0.70,
            GeometryClass.HIGH_SUCTION: 1.0,
        }[self]


GEOMETRY_CHOICES: list[GeometryClass] = list(GeometryClass)


@dataclass(frozen=True)
class MotionTemplate:
    # Tilt-release printers (e.g. ELEGOO Ultra series) ignore lift/retract distance & speed (0).
    bottom_layer_lift_height: float = 0.0
    bottom_layer_lift_speed: float = 0.0
    normal_layer_lift_height: float = 0.0
    normal_layer_lift_speed: float = 0.0
    normal_drop_speed: float = 0.0
    rest_time_after_retract: float = 1.2
    rest_time_before_lift: float = 0.0
    rest_time_after_lift: float = 0.0
    bottom_rest_time_after_retract: float = 1.2
    bottom_rest_time_before_lift: float = 0.0
    bottom_rest_time_after_lift: float = 0.0


@dataclass(frozen=True)
class Printer:
    id: str
    name: str
    machine_type: str
    machine_width: float
    machine_depth: float
    machine_height: float
    resolution_x: int
    resolution_y: int
    motion: MotionTemplate = field(default_factory=MotionTemplate)
    project_type: str = "LCD_mirror"
    photo: str = ""  # relative to project root, e.g. assets/printers/foo.png
    specs: tuple[str, ...] = ()
    # Network address and device id used by the printer's driver (SDCP: IP + mainboard id).
    sdcp_ip: str = ""
    sdcp_mainboard_id: str = ""
    model_id: str = ""
    driver: str = "sdcp"
    brand: str = ""
    model_name: str = ""

    @property
    def xy_label(self) -> str:
        return f"{self.resolution_x} × {self.resolution_y}"

    @property
    def volume_label(self) -> str:
        return f"{self.machine_width:g} × {self.machine_depth:g} × {self.machine_height:g} mm"

    @property
    def pixel_um(self) -> float:
        """XY pixel size in microns (0 when unknown)."""
        if not self.resolution_x or not self.machine_width:
            return 0.0
        return 1000.0 * self.machine_width / self.resolution_x

    @property
    def photo_path(self) -> Path | None:
        return _package_file(self.photo)


@dataclass(frozen=True)
class PrinterModel:
    """A printer model from the library (or described by the printer itself on discovery)."""

    id: str
    brand: str
    name: str
    driver: str = "sdcp"
    match_names: tuple[str, ...] = ()
    machine_type: str = ""
    machine_width: float = 0.0
    machine_depth: float = 0.0
    machine_height: float = 0.0
    resolution_x: int = 0
    resolution_y: int = 0
    project_type: str = "LCD_mirror"
    photo: str = ""
    specs: tuple[str, ...] = ()
    motion: MotionTemplate = field(default_factory=MotionTemplate)

    @property
    def full_name(self) -> str:
        return f"{self.brand} {self.name}".strip()


def _package_file(rel: str) -> Path | None:
    if not rel:
        return None
    path = Path(__file__).resolve().parent / rel
    return path if path.is_file() else None


@dataclass(frozen=True)
class Resin:
    id: str
    name: str
    brand: str
    color: str
    material_family: str
    density: float = 1.1
    min_normal_exposure: float = 1.2
    min_bottom_exposure: float = 35.0
    photo: str = ""
    swatch: str = "#8E8E93"
    notes: str = ""
    typical_uses: str = ""
    wavelength_nm: int = 405
    store_url: str = ""
    amazon_url: str = ""
    # Maker's suggested exposure at 0.05 mm, used when no printer-model baseline exists.
    starting_exposure: float = 0.0
    starting_bottom_exposure: float = 0.0
    translucent: bool = False

    @property
    def purchase_url(self) -> str:
        return self.store_url or self.amazon_url

    @property
    def photo_path(self) -> Path | None:
        return _package_file(self.photo)

    @property
    def short_label(self) -> str:
        return f"{self.brand} · {self.color}"


@dataclass
class PrintSettings:
    layer_height: float
    normal_exposure_time: float
    bottom_exposure_time: float
    bottom_layer_count: int = 5
    transition_layer_count: int = 5
    bottom_layer_lift_height: float = 0.0
    bottom_layer_lift_speed: float = 0.0
    normal_layer_lift_height: float = 0.0
    normal_layer_lift_speed: float = 0.0
    normal_drop_speed: float = 0.0
    rest_time_after_retract: float = 1.2
    rest_time_before_lift: float = 0.0
    rest_time_after_lift: float = 0.0
    bottom_rest_time_after_retract: float = 1.2
    bottom_rest_time_before_lift: float = 0.0
    bottom_rest_time_after_lift: float = 0.0
    light_off_time: float = 0.0
    bottom_light_off_time: float = 0.0
    normal_light_intensity_pwm: int = 255
    bottom_light_intensity_pwm: int = 255

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PrintSettings:
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class BaselineEntry:
    printer_id: str
    resin_id: str
    layer_height: float
    vat_temperature_c: float
    settings: PrintSettings
    notes: str = ""


@dataclass
class Recommendation:
    settings: PrintSettings
    confidence: ConfidenceSource
    source_detail: str
    warnings: list[str] = field(default_factory=list)
    printer_id: str = ""
    resin_id: str = ""
    vat_temperature_c: float = 25.0
    geometry_class: GeometryClass = GeometryClass.NORMAL
    cross_section_mm2: float = 0.0
    bottom_detail: str = ""
    bottom_calibrated: bool = False
    part_detail: str = ""
    exposure_detail: str = ""


# Default layer heights on the Today cards, left → right (changeable in Settings).
TODAY_LAYER_HEIGHTS: tuple[float, ...] = (0.025, 0.05)

# Fields shown in the slicer's print-settings tab
EDITABLE_FIELDS: list[tuple[str, str, type]] = [
    ("layer_height", "Layer height", float),
    ("normal_exposure_time", "Layer exposure", float),
    ("bottom_exposure_time", "Bottom exposure", float),
    ("bottom_layer_count", "Bottom layers", int),
    ("transition_layer_count", "Transition layers", int),
    ("rest_time_before_lift", "Rest before lift", float),
    ("rest_time_after_lift", "Rest after lift", float),
    ("rest_time_after_retract", "Rest after retract", float),
]

EDITABLE_UNITS: dict[str, str] = {
    "layer_height": "mm",
    "normal_exposure_time": "s",
    "bottom_exposure_time": "s",
    "bottom_layer_count": "",
    "transition_layer_count": "",
    "rest_time_before_lift": "s",
    "rest_time_after_lift": "s",
    "rest_time_after_retract": "s",
}
