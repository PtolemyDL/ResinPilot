"""Shape detection from layer masks, and same-shape fallback in the engine."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import resinpilot.shape_scan as shape_scan
from resinpilot.catalog import Catalog
from resinpilot.engine import RecommendationEngine
from resinpilot.history import HistoryStore
from resinpilot.models import PrintOutcome, PrintSettings
from resinpilot.parts import PartFamilies
from resinpilot.shape_scan import TAG_CHANNELS, TAG_FINE, analyze_mask, save_scan, summarize

from resinpilot.paths import BUILTIN_DATA_DIR as DATA
PID = "saturn_4_ultra"
RID = "elegoo_8k_standard_space_grey"
PX = 0.025


def _disc(mask: np.ndarray, cy: int, cx: int, r_px: float, value: bool) -> None:
    yy, xx = np.ogrid[: mask.shape[0], : mask.shape[1]]
    mask[(yy - cy) ** 2 + (xx - cx) ** 2 <= r_px**2] = value


@pytest.fixture(autouse=True)
def scans_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "shape_scans.json"
    monkeypatch.setattr(shape_scan, "SCANS_NAME", str(path))
    return path


def test_block_with_channels() -> None:
    mask = np.zeros((1000, 1000), dtype=bool)
    mask[100:900, 100:900] = True  # 20 × 20 mm block
    for cx in (300, 500, 700):
        _disc(mask, 500, cx, 10, False)  # 0.5 mm channels
    layer = analyze_mask(mask, PX)
    assert layer["channels"] == 3
    assert layer["channel_diameters_mm"][0] == pytest.approx(0.5, abs=0.05)
    assert layer["thin_pct"] < 2


def test_large_cutout_is_not_a_channel() -> None:
    mask = np.zeros((1000, 1000), dtype=bool)
    mask[100:900, 100:900] = True
    _disc(mask, 500, 500, 200, False)  # 10 mm bore
    assert analyze_mask(mask, PX)["channels"] == 0


def test_thin_ribs_are_fine_features() -> None:
    mask = np.zeros((600, 600), dtype=bool)
    for x in range(100, 500, 40):
        mask[100:500, x : x + 12] = True  # 0.3 mm ribs
    layer = analyze_mask(mask, PX)
    assert layer["thin_pct"] > 80
    tags = summarize([layer] * 5)["tags"]
    assert TAG_FINE in tags and TAG_CHANNELS not in tags


def _log(history: HistoryStore, filename: str, exposure: float) -> None:
    history.log_run(
        PID, RID, 0.025, 25.0,
        PrintSettings(layer_height=0.025, normal_exposure_time=exposure, bottom_exposure_time=35.0),
        PrintOutcome.SUCCESS, filename=filename, sync_team=False,
    )


def test_new_part_uses_same_shape_parts(tmp_path: Path) -> None:
    history = HistoryStore(tmp_path / "history.db")
    channel_scan = {"layers_sampled": 40, "tags": [TAG_CHANNELS], "channel_mm": 0.5, "min_channel_mm": 0.45,
                    "channel_layer_pct": 60.0, "thin_pct": 2.0, "scanned_at": "2026-10-13T00:00:00+00:00"}
    for name in ("ManifoldA_v1.ctb", "ManifoldB_v1.ctb", "NewManifold_v1.ctb"):
        save_scan(name, channel_scan)
    for _ in range(2):
        _log(history, "ManifoldA_v1.ctb", 1.8)
        _log(history, "ManifoldB_v1.ctb", 1.8)
    _log(history, "NewManifold_v1.ctb", 1.8)
    for _ in range(4):
        _log(history, "Block_v1.ctb", 2.5)
    engine = RecommendationEngine(Catalog(DATA), history)
    fid = PartFamilies(history.list_recent(outcome=None, limit=100)).family_id("NewManifold_v1.ctb")
    rec = engine.recommend(PID, RID, 0.025, 25.0, part_family=fid)
    assert rec.settings.normal_exposure_time == pytest.approx(1.8)
    assert "internal channels" in rec.part_detail
    assert any("channels" in w for w in rec.warnings)
