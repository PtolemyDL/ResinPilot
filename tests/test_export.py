"""Unit tests for Chitubox .cfg export."""

from __future__ import annotations

from pathlib import Path

from resinpilot.catalog import Catalog
from resinpilot.chitubox_export import (
    build_cfg_lines,
    default_cfg_filename,
    settings_clipboard_text,
    write_cfg,
)
from resinpilot.models import PrintSettings

from resinpilot.paths import BUILTIN_DATA_DIR as DATA

REQUIRED_KEYS = {
    "__resin",
    "layerHeight",
    "normalExposureTime",
    "bottomLayerExposureTime",
    "bottomLayerCount",
    "transitionLayerCount",
    "normalLayerLiftHeight",
    "normalLayerLiftSpeed",
    "bottomLayerLiftHeight",
    "bottomLayerLiftSpeed",
    "normalRestTimeAfterRetract",
    "machineWidth",
    "machineDepth",
    "machineHeight",
    "resolutionX",
    "resolutionY",
    "machineType",
    "currProfile",
}


def test_build_cfg_contains_required_keys() -> None:
    catalog = Catalog(DATA)
    printer = catalog.printers["saturn_4_ultra"]
    resin = catalog.resins["elegoo_8k_standard_space_grey"]
    settings = PrintSettings(
        layer_height=0.05,
        normal_exposure_time=2.6,
        bottom_exposure_time=30.0,
        bottom_layer_count=5,
        transition_layer_count=5,
    )
    lines = build_cfg_lines(printer, resin, settings)
    keys = {line.split(":", 1)[0] for line in lines}
    missing = REQUIRED_KEYS - keys
    assert not missing, f"Missing keys: {missing}"


def test_write_cfg_roundtrip(tmp_path: Path) -> None:
    catalog = Catalog(DATA)
    printer = catalog.printers["mars_5_ultra_left"]
    resin = catalog.resins["anycubic_clear"]
    settings = PrintSettings(
        layer_height=0.03,
        normal_exposure_time=2.9,
        bottom_exposure_time=34.0,
    )
    path = tmp_path / "test.cfg"
    write_cfg(path, printer, resin, settings)
    text = path.read_text(encoding="utf-8")
    assert "layerHeight:0.03" in text
    assert "normalExposureTime:2.9" in text
    assert "machineType:ELEGOO Mars 5 Ultra" in text


def test_default_filename() -> None:
    catalog = Catalog(DATA)
    printer = catalog.printers["saturn_4_ultra_16k"]
    resin = catalog.resins["elegoo_abs_like_3_black"]
    name = default_cfg_filename(printer, resin, 0.05, 25)
    assert name.endswith(".cfg")
    assert "0.050mm" in name
    assert "25C" in name


def test_clipboard_text_contains_exposures() -> None:
    settings = PrintSettings(
        layer_height=0.05,
        normal_exposure_time=2.5,
        bottom_exposure_time=30.0,
    )
    text = settings_clipboard_text(settings)
    assert "2.50" in text
    assert "30.00" in text
    assert "Lift/retract unused" in text
    assert "Bottom lift height" not in text
