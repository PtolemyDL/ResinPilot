"""Failure types and quality answers → layer exposure corrections."""

from __future__ import annotations

from pathlib import Path

import pytest

import resinpilot.shape_scan as shape_scan
from resinpilot.bottom_calibration import ISSUE_FEATURES_CLOSED, ISSUE_LAYER_SEPARATION
from resinpilot.catalog import Catalog
from resinpilot.engine import RecommendationEngine, scale_exposure
from resinpilot.history import HistoryStore
from resinpilot.models import PrintOutcome, PrintSettings
from resinpilot.outcome_signals import calibrate_layer_exposure, is_clean_success
from resinpilot.parts import PartFamilies

from resinpilot.paths import BUILTIN_DATA_DIR as DATA
PID = "saturn_4_ultra"
RID = "elegoo_8k_standard_space_grey"


@pytest.fixture(autouse=True)
def no_real_scans(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shape_scan, "SCANS_NAME", str(tmp_path / "scans.json"))


@pytest.fixture
def history(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.db")


def _log(history: HistoryStore, exposure: float, outcome=PrintOutcome.SUCCESS, filename="Part_v1.ctb", **kw) -> None:
    history.log_run(
        PID, RID, 0.025, 25.0,
        PrintSettings(layer_height=0.025, normal_exposure_time=exposure, bottom_exposure_time=40.0),
        outcome, filename=filename, sync_team=False, **kw,
    )


def _cal(history: HistoryStore, current: float = 2.0):
    runs = history.list_for_printer(PID)
    return calibrate_layer_exposure(runs, current, 0.025, 1.0, scale_exposure)


def test_layer_separation_raises_from_the_failed_value(history: HistoryStore) -> None:
    _log(history, 2.0, PrintOutcome.FAIL, failure_issue=ISSUE_LAYER_SEPARATION)
    cal = _cal(history)
    assert cal.value == pytest.approx(2.2)
    assert "layers split" in cal.detail


def test_newer_clean_success_resolves_the_signal(history: HistoryStore) -> None:
    _log(history, 2.0, PrintOutcome.FAIL, failure_issue=ISSUE_LAYER_SEPARATION)
    _log(history, 2.2)
    assert _cal(history).value == pytest.approx(2.0)


def test_closed_channels_lower_exposure(history: HistoryStore) -> None:
    _log(history, 2.0, PrintOutcome.FAIL, failure_issue=ISSUE_FEATURES_CLOSED)
    assert _cal(history).value == pytest.approx(1.84)


def test_quality_answers_are_signals(history: HistoryStore) -> None:
    _log(history, 2.0, quality={"missing_features": True, "dimensions_ok": True})
    rec = history.list_for_printer(PID)[0]
    assert not is_clean_success(rec)
    assert _cal(history).value == pytest.approx(2.12)


def test_under_cure_wins_over_over_cure(history: HistoryStore) -> None:
    _log(history, 2.0, quality={"dimensions_ok": False})
    _log(history, 2.0, PrintOutcome.FAIL, failure_issue=ISSUE_LAYER_SEPARATION)
    cal = _cal(history)
    assert cal.value == pytest.approx(2.2)
    assert "under-cure sign wins" in cal.detail


def test_engine_applies_part_signals_at_full_and_resin_wide_at_half(history: HistoryStore) -> None:
    for _ in range(3):
        _log(history, 2.0, filename="Valve_Lid_v1.ctb")
    _log(history, 2.0, PrintOutcome.FAIL, filename="Valve_Lid_v1.ctb", failure_issue=ISSUE_LAYER_SEPARATION)
    engine = RecommendationEngine(Catalog(DATA), history)
    fid = PartFamilies(history.list_recent(outcome=None, limit=100)).family_id("Valve_Lid_v1.ctb")
    part = engine.recommend(PID, RID, 0.025, 25.0, part_family=fid)
    assert part.settings.normal_exposure_time == pytest.approx(2.2)
    assert "layers split" in part.exposure_detail
    general = engine.recommend(PID, RID, 0.025, 25.0)
    assert "layers split" in general.exposure_detail or general.settings.normal_exposure_time >= 2.1
