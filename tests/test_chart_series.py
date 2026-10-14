"""Chart data series for the web Charts tab."""

from __future__ import annotations

from datetime import datetime, timezone

from resinpilot.charts import (
    bottom_distribution,
    failure_breakdown,
    fleet_comparison,
    printer_kpis,
    recent_rate,
    recipe_clusters,
    settings_timeline,
    temp_measured,
    weekly_usage,
)
from resinpilot.history import HistoryRecord
from resinpilot.models import PrintOutcome, PrintSettings


def _rec(
    rid: int,
    when: str,
    exp: float = 2.0,
    bottom: float = 35.0,
    layer: float = 0.05,
    outcome: PrintOutcome = PrintOutcome.SUCCESS,
    issue: str = "",
    hours: float = 2.0,
    estimated: bool = False,
    temp: float = 30.0,
) -> HistoryRecord:
    return HistoryRecord(
        id=rid,
        printer_id="p1",
        resin_id="r1",
        layer_height=layer,
        vat_temperature_c=temp,
        settings=PrintSettings(layer_height=layer, normal_exposure_time=exp, bottom_exposure_time=bottom),
        created_at=when,
        outcome=outcome,
        failure_issue=issue,
        duration_s=hours * 3600,
        telemetry={"vat_temp_estimated": True} if estimated else {},
    )


RECORDS = [
    _rec(3, "2026-09-30T12:00:00+00:00", exp=1.7, bottom=55, outcome=PrintOutcome.FAIL, issue="detached"),
    _rec(2, "2026-09-22T12:00:00+00:00", exp=2.0, bottom=36, outcome=PrintOutcome.FAIL, hours=1.0),
    _rec(1, "2026-09-08T12:00:00+00:00", exp=2.0, bottom=34, estimated=True, temp=32.0),
]


def test_timeline_is_oldest_first_with_fail_reason() -> None:
    pts = settings_timeline(RECORDS)
    assert [p["bottom"] for p in pts] == [34, 36, 55]
    assert pts[-1]["outcome"] == "fail" and pts[-1]["issue"] == "Came off the plate mid-print"
    assert pts[1]["issue"] == ""


def test_recipe_clusters_count_runs_per_setting() -> None:
    clusters = recipe_clusters(RECORDS)
    assert {(c["exp"], c["n"], c["ok"], c["fail"]) for c in clusters} == {(1.7, 1, 0, 1), (2.0, 2, 1, 1)}


def test_bottom_distribution_fills_empty_buckets() -> None:
    bins = bottom_distribution(RECORDS)
    assert [b["lo"] for b in bins] == [30, 35, 40, 45, 50, 55]
    assert (bins[0]["ok"], bins[1]["fail"], bins[2]["ok"] + bins[2]["fail"], bins[-1]["fail"]) == (1, 1, 0, 1)


def test_weekly_usage_fills_gap_weeks() -> None:
    weeks = weekly_usage(RECORDS)
    assert [w["prints"] for w in weeks] == [1, 0, 1, 1]
    assert weeks[2] == {"week": weeks[2]["week"], "prints": 1, "fails": 1, "hours": 1.0}


def test_failure_breakdown_labels_unspecified() -> None:
    assert [(f["issue"], f["n"]) for f in failure_breakdown(RECORDS)] == [("detached", 1), ("unspecified", 1)]


def test_estimated_temps_are_ignored() -> None:
    assert [temp_measured(r) for r in RECORDS] == [True, True, False]
    assert printer_kpis(RECORDS)["avg_vat_c"] == 30.0


def test_recent_rate_and_fleet_comparison() -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert recent_rate(RECORDS, days=28, now=now) == {"prints_per_week": 0.75, "hours_per_week": 1.25}
    fleet = fleet_comparison({"p1": RECORDS, "p2": []}, {"p1": "Printer 1"}, now=now)
    p1, p2 = fleet["printers"]
    assert (p1["name"], p1["total"], p1["typical_exp"], p1["typical_bottom"]) == ("Printer 1", 3, 2.0, 36.0)
    assert p2["total"] == 0 and p2["typical_exp"] is None
    assert len(fleet["weeks"]) == 4 and fleet["weekly_prints"]["p2"] == [0, 0, 0, 0]
