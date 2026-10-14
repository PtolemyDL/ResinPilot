"""Bottom-exposure calibration from logged outcomes."""

from __future__ import annotations

from pathlib import Path

import pytest

from resinpilot.bottom_calibration import (
    ISSUE_NO_ADHESION,
    ISSUE_OTHER,
    ISSUE_OVER_CURE,
    ISSUE_SUPPORTS,
    calibrate_bottom_exposure,
    infer_issue,
)
from resinpilot.catalog import Catalog
from resinpilot.engine import RecommendationEngine
from resinpilot.history import HistoryStore
from resinpilot.models import PrintOutcome, PrintSettings

from resinpilot.paths import BUILTIN_DATA_DIR as DATA
PID = "saturn_4_ultra"
RID = "elegoo_8k_standard_space_grey"


@pytest.fixture
def history(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.db")


def _settings(bottom: float, layer: float = 0.025) -> PrintSettings:
    return PrintSettings(
        layer_height=layer,
        normal_exposure_time=1.5,
        bottom_exposure_time=bottom,
        bottom_layer_count=5,
        transition_layer_count=5,
    )


def _log(history: HistoryStore, bottom: float, outcome: PrintOutcome, **kw) -> None:
    history.log_run(
        PID,
        kw.pop("resin_id", RID),
        kw.pop("layer", 0.025),
        25.0,
        _settings(bottom),
        outcome,
        sync_team=False,
        **kw,
    )


def test_no_history_stays_at_floor(history: HistoryStore) -> None:
    cal = calibrate_bottom_exposure(history, PID, RID, current=30.0, floor=35.0)
    assert cal.value == 35.0
    assert not cal.calibrated


def test_successes_anchor_value(history: HistoryStore) -> None:
    _log(history, 40.0, PrintOutcome.SUCCESS)
    _log(history, 42.0, PrintOutcome.SUCCESS)
    cal = calibrate_bottom_exposure(history, PID, RID, current=30.0, floor=35.0)
    assert cal.calibrated
    assert 40.0 <= cal.value <= 42.0


def test_adhesion_fail_raises_bottom(history: HistoryStore) -> None:
    _log(history, 35.0, PrintOutcome.SUCCESS)
    _log(history, 38.0, PrintOutcome.FAIL, failure_issue=ISSUE_NO_ADHESION)
    cal = calibrate_bottom_exposure(history, PID, RID, current=30.0, floor=35.0)
    assert cal.value >= 38.0 * 1.15 - 0.5
    assert "adhesion" in cal.detail


def test_adhesion_fail_resolved_by_newer_success(history: HistoryStore) -> None:
    _log(history, 38.0, PrintOutcome.FAIL, failure_issue=ISSUE_NO_ADHESION)
    _log(history, 44.0, PrintOutcome.SUCCESS)
    cal = calibrate_bottom_exposure(history, PID, RID, current=30.0, floor=35.0)
    assert cal.value == pytest.approx(44.0)
    assert "adhesion" not in cal.detail


def test_over_cure_lowers_but_not_below_floor(history: HistoryStore) -> None:
    _log(history, 50.0, PrintOutcome.SUCCESS)
    _log(history, 50.0, PrintOutcome.FAIL, failure_issue=ISSUE_OVER_CURE)
    cal = calibrate_bottom_exposure(history, PID, RID, current=30.0, floor=35.0)
    assert cal.value == pytest.approx(45.0)

    _log(history, 36.0, PrintOutcome.FAIL, failure_issue=ISSUE_OVER_CURE)
    cal = calibrate_bottom_exposure(history, PID, RID, current=30.0, floor=35.0)
    assert cal.value == 35.0


def test_other_failures_do_not_move_bottom(history: HistoryStore) -> None:
    _log(history, 40.0, PrintOutcome.SUCCESS)
    _log(history, 40.0, PrintOutcome.FAIL, failure_issue=ISSUE_OTHER)
    cal = calibrate_bottom_exposure(history, PID, RID, current=30.0, floor=35.0)
    assert cal.value == pytest.approx(40.0)


def test_infer_issue_from_notes_and_early_layer(history: HistoryStore) -> None:
    _log(history, 35.0, PrintOutcome.FAIL, notes="part stuck on the film")
    _log(history, 35.0, PrintOutcome.FAIL, notes="bad", end_layer=8, total_layers=700)
    _log(history, 35.0, PrintOutcome.FAIL, notes="support broke", end_layer=699, total_layers=700)
    _log(history, 35.0, PrintOutcome.FAIL, notes="resin ran out", end_layer=699, total_layers=700)
    recs = history.list_for_printer(PID)
    issues = sorted(infer_issue(r) for r in recs)
    assert issues == sorted([ISSUE_NO_ADHESION, ISSUE_NO_ADHESION, ISSUE_SUPPORTS, ISSUE_OTHER])


def test_engine_uses_calibrated_bottom(history: HistoryStore) -> None:
    engine = RecommendationEngine(Catalog(DATA), history)
    before = engine.recommend(PID, RID, 0.025, 25.0)
    assert before.settings.bottom_exposure_time == 35.0
    assert not before.bottom_calibrated

    _log(history, 36.0, PrintOutcome.FAIL, failure_issue=ISSUE_NO_ADHESION)
    after = engine.recommend(PID, RID, 0.05, 25.0)
    assert after.bottom_calibrated
    assert after.settings.bottom_exposure_time > 40.0
    # Same bottom at both layer heights — it's about the plate, not Z
    other = engine.recommend(PID, RID, 0.025, 25.0)
    assert other.settings.bottom_exposure_time == after.settings.bottom_exposure_time


def test_each_resin_calibrates_from_its_own_runs(history: HistoryStore) -> None:
    clear = "anycubic_clear"
    _log(history, 40.0, PrintOutcome.SUCCESS)
    _log(history, 40.0, PrintOutcome.FAIL, failure_issue=ISSUE_NO_ADHESION)
    _log(history, 37.0, PrintOutcome.SUCCESS, resin_id=clear)
    grey = calibrate_bottom_exposure(history, PID, RID, current=30.0, floor=35.0)
    own = calibrate_bottom_exposure(history, PID, clear, current=30.0, floor=35.0)
    assert grey.value >= 46.0
    assert own.value == pytest.approx(37.0)
    assert "this resin's 1 success" in own.detail


def test_new_resin_starts_from_other_resin_successes_without_their_fails(history: HistoryStore) -> None:
    _log(history, 40.0, PrintOutcome.SUCCESS)
    _log(history, 40.0, PrintOutcome.FAIL, failure_issue=ISSUE_NO_ADHESION)
    cal = calibrate_bottom_exposure(
        history, PID, "anycubic_clear", current=30.0, floor=35.0, baseline_ref=lambda rid, h: (2.0, 30.0)
    )
    assert cal.value == pytest.approx(40.0)
    assert "no runs with this resin yet" in cal.detail


def test_new_resin_layer_exposure_follows_its_own_baseline(history: HistoryStore) -> None:
    for _ in range(6):
        history.log_run(
            PID, RID, 0.05, 25.0,
            PrintSettings(layer_height=0.05, normal_exposure_time=2.0, bottom_exposure_time=35.0),
            PrintOutcome.SUCCESS, sync_team=False,
        )
    for _ in range(6):
        history.log_run(
            PID, RID, 0.025, 25.0,
            PrintSettings(layer_height=0.025, normal_exposure_time=1.6, bottom_exposure_time=35.0),
            PrintOutcome.SUCCESS, sync_team=False,
        )
    catalog = Catalog(DATA)
    engine = RecommendationEngine(catalog, history)
    grey = engine.recommend(PID, RID, 0.05, 25.0)
    high_clear = engine.recommend(PID, "anycubic_high_clear", 0.05, 25.0)
    ratio = (
        catalog.get_baseline(PID, "anycubic_high_clear").settings.normal_exposure_time
        / catalog.get_baseline(PID, RID).settings.normal_exposure_time
    )
    assert "other resins" in high_clear.source_detail
    assert high_clear.settings.normal_exposure_time == pytest.approx(
        grey.settings.normal_exposure_time * ratio, rel=0.05
    )


def test_failure_issue_round_trips(history: HistoryStore) -> None:
    _log(history, 35.0, PrintOutcome.FAIL, failure_issue=ISSUE_OVER_CURE)
    rec = history.list_for_printer(PID)[0]
    assert rec.failure_issue == ISSUE_OVER_CURE
