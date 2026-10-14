"""Part families from file names, size classes, and part-aware recommendations."""

from __future__ import annotations

from pathlib import Path

import pytest

from resinpilot.bottom_calibration import ISSUE_NO_ADHESION
from resinpilot.catalog import Catalog
from resinpilot.engine import RecommendationEngine
from resinpilot.history import HistoryStore
from resinpilot.models import PrintOutcome, PrintSettings
from resinpilot.parts import (
    OTHER_GROUP,
    SIZE_BULKY,
    SIZE_MEDIUM,
    SIZE_SMALL,
    PartFamilies,
    auto_family,
    auto_groups,
    load_group_overrides,
    load_overrides,
    save_group_override,
    save_override,
    size_class,
)

from resinpilot.paths import BUILTIN_DATA_DIR as DATA
PID = "saturn_4_ultra"
RID = "elegoo_8k_standard_space_grey"


@pytest.fixture
def history(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.db")


def _log(
    history: HistoryStore,
    filename: str,
    outcome: PrintOutcome = PrintOutcome.SUCCESS,
    exposure: float = 1.5,
    bottom: float = 35.0,
    volume_ml: float = 10.0,
    layers: int = 2000,
    **kw,
) -> None:
    history.log_run(
        PID,
        RID,
        0.025,
        25.0,
        PrintSettings(layer_height=0.025, normal_exposure_time=exposure, bottom_exposure_time=bottom),
        outcome,
        filename=filename,
        telemetry={"slice_info": {"volume": volume_ml, "total_layer_numbers": layers, "layer_height": 0.025}},
        sync_team=False,
        **kw,
    )


def test_versions_and_slice_tails_share_a_family() -> None:
    a, _ = auto_family("3in1_Manifold_v2.stl_0.025_2025-09-24.ctb")
    b, _ = auto_family("3in1_Manifold_v3.ctb")
    c, _ = auto_family("Valve_Base_Ver.Alex.ctb")
    assert a == b
    assert a != c


def test_rename_merges_families(tmp_path: Path, history: HistoryStore) -> None:
    path = tmp_path / "families.json"
    _log(history, "Gear_A.ctb")
    _log(history, "Gear_B.ctb")
    recs = history.list_for_printer(PID)
    key_a, _ = auto_family("Gear_A.ctb")
    key_b, _ = auto_family("Gear_B.ctb")
    save_override(key_a, "Gears", path)
    save_override(key_b, "Gears", path)
    fam = PartFamilies(recs, load_overrides(path))
    assert fam.family_id("Gear_A.ctb") == fam.family_id("Gear_B.ctb")
    save_override(key_b, "", path)
    assert key_b not in load_overrides(path)


def test_auto_groups_put_variants_together() -> None:
    names = {
        "valvelid": "Valve Lid",
        "basevalve": "Base, Valve",
        "valvev88base": "ValveV88Base",
        "newvalves": "New Valves",
        "3in1manifold": "3in1 Manifold",
        "manifoldgluechanneltest": "ManifoldGlueChannelTest",
        "assemblyjigtop": "Assembly Jig Top",
        "valvejig": "Valve jig",
        "pumpcam": "PumpCam",
        "lid": "Lid",
    }
    groups = auto_groups(names)
    assert {groups[k] for k in ("valvelid", "basevalve", "valvev88base", "newvalves")} == {"Valve"}
    assert groups["3in1manifold"] == groups["manifoldgluechanneltest"] == "Manifold"
    assert groups["assemblyjigtop"] == groups["valvejig"] == "Jigs"
    assert groups["pumpcam"] == groups["lid"] == OTHER_GROUP


def test_group_override_survives_renames(tmp_path: Path, history: HistoryStore) -> None:
    path = tmp_path / "families.json"
    _log(history, "PumpCam.ctb")
    save_group_override("pumpcam", "Pump", path)
    save_override("pumpcam", "Pump Cam", path)
    assert load_group_overrides(path) == {"pumpcam": "Pump"}
    fam = PartFamilies(history.list_for_printer(PID), load_overrides(path), {}, load_group_overrides(path))
    assert fam.group("pumpcam") == "Pump"


def test_size_class_from_volume_and_height(history: HistoryStore) -> None:
    # 2000 layers × 0.025 mm = 50 mm tall
    _log(history, "tiny.ctb", volume_ml=5.0)    # 100 mm²/layer
    _log(history, "mid.ctb", volume_ml=25.0)    # 500 mm²/layer
    _log(history, "block.ctb", volume_ml=50.0)  # 1000 mm²/layer
    by_name = {r.filename: size_class(r) for r in history.list_for_printer(PID)}
    assert by_name == {"tiny.ctb": SIZE_SMALL, "mid.ctb": SIZE_MEDIUM, "block.ctb": SIZE_BULKY}


def test_family_successes_set_layer_exposure(history: HistoryStore) -> None:
    for _ in range(3):
        _log(history, "Manifold_v1.ctb", exposure=2.4)
    for _ in range(3):
        _log(history, "Other.ctb", exposure=1.5)
    engine = RecommendationEngine(Catalog(DATA), history)
    fid = PartFamilies(history.list_recent(outcome=None, limit=100)).family_id("Manifold_v1.ctb")
    part = engine.recommend(PID, RID, 0.025, 25.0, part_family=fid)
    general = engine.recommend(PID, RID, 0.025, 25.0)
    assert part.settings.normal_exposure_time == pytest.approx(2.4)
    assert "Manifold" in part.part_detail
    assert general.settings.normal_exposure_time != pytest.approx(2.4)


def test_new_part_falls_back_with_note(history: HistoryStore) -> None:
    _log(history, "Bracket.ctb", exposure=2.4)
    engine = RecommendationEngine(Catalog(DATA), history)
    fid = PartFamilies(history.list_recent(outcome=None, limit=100)).family_id("Bracket.ctb")
    rec = engine.recommend(PID, RID, 0.025, 25.0, part_family=fid)
    assert "1 successful print" in rec.part_detail
    assert rec.settings.normal_exposure_time == pytest.approx(
        engine.recommend(PID, RID, 0.025, 25.0).settings.normal_exposure_time
    )


def test_bottom_follows_size_class(history: HistoryStore) -> None:
    for _ in range(3):
        _log(history, "Block.ctb", outcome=PrintOutcome.FAIL, bottom=40.0, volume_ml=50.0,
             failure_issue=ISSUE_NO_ADHESION)
    for _ in range(3):
        _log(history, "Pin.ctb", bottom=36.0, volume_ml=5.0)
    engine = RecommendationEngine(Catalog(DATA), history)
    bulky = engine.recommend(PID, RID, 0.025, 25.0, size_class=SIZE_BULKY)
    small = engine.recommend(PID, RID, 0.025, 25.0, size_class=SIZE_SMALL)
    assert bulky.settings.bottom_exposure_time > 45.0
    assert small.settings.bottom_exposure_time == pytest.approx(36.0)
    assert "bulky" in bulky.bottom_detail.lower()
