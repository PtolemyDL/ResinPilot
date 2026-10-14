"""Post-print quality checklist storage and summaries."""

from __future__ import annotations

from pathlib import Path

import pytest

from resinpilot.history import HistoryStore
from resinpilot.models import PrintOutcome, PrintSettings
from resinpilot.quality import clean_quality, quality_problems, quality_summary


def test_clean_quality_drops_unknown_and_non_bool() -> None:
    raw = {"dimensions_ok": True, "warped": "yes", "bogus": False, "resin_residue": False}
    assert clean_quality(raw) == {"dimensions_ok": True, "resin_residue": False}
    assert clean_quality('{"surface_ok": false}') == {"surface_ok": False}
    assert clean_quality("not json") == {}


def test_problems_respect_good_answer() -> None:
    q = {"dimensions_ok": False, "resin_residue": True, "surface_ok": True, "warped": False}
    assert quality_problems(q) == ["dimensions", "residue"]
    assert quality_summary(q) == "issues: dimensions, residue"
    assert quality_summary({"surface_ok": True, "missing_features": False}) == "all 2 checks OK"
    assert quality_summary({}) == ""


@pytest.fixture
def history(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.db")


def test_quality_round_trips_and_syncs(history: HistoryStore) -> None:
    s = PrintSettings(layer_height=0.025, normal_exposure_time=1.5, bottom_exposure_time=35.0)
    history.log_run(
        "p1",
        "r1",
        0.025,
        25.0,
        s,
        PrintOutcome.SUCCESS,
        quality={"dimensions_ok": True, "missing_features": True},
    )
    rec = history.list_for_printer("p1")[0]
    assert rec.quality == {"dimensions_ok": True, "missing_features": True}

    other = HistoryStore(history.db_path.parent / "other.db", team_file=history.team_file)
    assert other.list_for_printer("p1")[0].quality == rec.quality
