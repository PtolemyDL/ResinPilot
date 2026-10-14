"""Late / corrected outcome reports from the History tab."""

from __future__ import annotations

from resinpilot.charts import filter_history_rows


def test_history_filter_by_status_printer_resin_and_search() -> None:
    rows = [
        {"id": "#3", "outcome": "SUCCESS", "printer": "Mars L", "resin": "8K Grey", "file": "Valve_Base.ctb"},
        {"id": "#2", "outcome": "FAIL", "printer": "Mars L", "resin": "ABS Translucent", "file": "Manifold.ctb"},
        {"id": "#1", "outcome": "PARTIAL", "printer": "16K", "resin": "8K Grey", "file": "valve_cap.ctb"},
        {"id": "#4", "outcome": "PRINTING", "printer": "16K", "resin": "8K Grey", "file": "Jig.ctb"},
    ]
    ids = lambda rs: [r["id"] for r in rs]  # noqa: E731
    assert ids(filter_history_rows(rows)) == ["#3", "#2", "#1", "#4"]
    assert ids(filter_history_rows(rows, status="FAIL")) == ["#2", "#1"]
    assert ids(filter_history_rows(rows, printer="Mars L", resin="8K Grey")) == ["#3"]
    assert ids(filter_history_rows(rows, search="VALVE")) == ["#3", "#1"]
    assert ids(filter_history_rows(rows, search="valve 16k")) == ["#1"]
    assert ids(filter_history_rows(rows, status="PRINTING")) == ["#4"]

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from resinpilot.charts import build_history_log
from resinpilot.history import HistoryStore, OpenJob, is_new_print
from resinpilot.models import PrintOutcome, PrintSettings


@pytest.fixture
def history(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.db")


def _settings() -> PrintSettings:
    return PrintSettings(layer_height=0.025, normal_exposure_time=2.78, bottom_exposure_time=35.0)


def _awaiting_job(history: HistoryStore, filename: str = "part.ctb", task_id: str = "t1") -> None:
    history.upsert_open_job(
        OpenJob(
            printer_id="p1",
            filename=filename,
            resin_id="r1",
            layer_height=0.025,
            settings_json=json.dumps(_settings().to_dict()),
            started_at="2026-09-27T10:00:00+00:00",
            duration_s=3600.0,
            awaiting_outcome=True,
            task_id=task_id,
        )
    )


def test_amend_run_changes_outcome_and_syncs_to_team(history: HistoryStore) -> None:
    run_id = history.log_run(
        "p1", "r1", 0.025, 25.0, _settings(), PrintOutcome.SUCCESS, notes="SDCP success — part.ctb"
    )
    rec = history.amend_run(
        run_id,
        PrintOutcome.FAIL,
        notes="lifted off plate",
        failure_issue="no_adhesion",
        quality={"warped": True},
    )
    assert rec.outcome == PrintOutcome.FAIL
    assert rec.failure_issue == "no_adhesion"
    assert rec.quality == {"warped": True}
    assert rec.notes == "SDCP fail — part.ctb — lifted off plate"

    other = HistoryStore(history.db_path.parent / "other.db", team_file=history.team_file)
    mirrored = other.list_for_printer("p1")
    assert len(mirrored) == 1
    assert mirrored[0].outcome == PrintOutcome.FAIL
    assert mirrored[0].quality == {"warped": True}


def test_amend_to_success_clears_failure_issue(history: HistoryStore) -> None:
    run_id = history.log_run(
        "p1", "r1", 0.025, 25.0, _settings(), PrintOutcome.FAIL, failure_issue="detached"
    )
    rec = history.amend_run(run_id, PrintOutcome.SUCCESS, failure_issue="detached")
    assert rec.outcome == PrintOutcome.SUCCESS
    assert rec.failure_issue == ""


def test_parked_job_stays_reportable(history: HistoryStore) -> None:
    _awaiting_job(history)
    pending_id = history.park_open_job("p1")
    assert pending_id is not None
    assert history.get_open_job("p1") is None
    pending = history.list_unreported("p1")
    assert [p.job.filename for p in pending] == ["part.ctb"]
    assert pending[0].finished_at.startswith("2026-09-27T11:00")

    run_id = history.resolve_unreported(
        pending_id, PrintOutcome.SUCCESS, quality={"dimensions_ok": True}
    )
    rec = history.get_run(run_id)
    assert rec is not None
    assert rec.outcome == PrintOutcome.SUCCESS
    assert rec.created_at.startswith("2026-09-27T11:00")
    assert rec.quality == {"dimensions_ok": True}
    assert history.list_unreported("p1") == []


def test_park_ignores_jobs_still_printing(history: HistoryStore) -> None:
    history.upsert_open_job(OpenJob(printer_id="p1", filename="x.ctb", awaiting_outcome=False))
    assert history.park_open_job("p1") is None
    assert history.get_open_job("p1") is not None


def test_dismiss_unreported(history: HistoryStore) -> None:
    _awaiting_job(history)
    pending_id = history.park_open_job("p1")
    assert pending_id is not None
    history.dismiss_unreported(pending_id)
    assert history.list_unreported() == []


def test_history_log_merges_printers_with_printer_column(history: HistoryStore) -> None:
    history.log_run("p1", "r1", 0.025, 25.0, _settings(), PrintOutcome.SUCCESS)
    history.log_run("p2", "r1", 0.05, 25.0, _settings(), PrintOutcome.FAIL, failure_issue="detached")
    _awaiting_job(history)
    history.upsert_open_job(OpenJob(printer_id="p2", filename="now.ctb", awaiting_outcome=False))
    names = {"p1": "Saturn 4 Ultra 16K", "p2": "Saturn 3 Ultra"}

    log = build_history_log(history, names, {"r1": "Resin"})
    assert log["stats"]["total"] == 2
    assert log["stats"]["unreported"] == 1
    rows = log["rows"]
    # The unreported job started 2026-09-27, before the two runs logged "now".
    assert [(r["id"], r["printer"], r["outcome"]) for r in rows] == [
        ("#4", "Saturn 3 Ultra", "PRINTING"),
        ("#3", "Saturn 3 Ultra", "FAIL"),
        ("#2", "Saturn 4 Ultra 16K", "SUCCESS"),
        ("#1", "Saturn 4 Ultra 16K", "UNREPORTED"),
    ]
    assert log["stats"]["printing"] == 1


def test_reported_while_printing_is_not_tracked_again(history: HistoryStore) -> None:
    history.upsert_open_job(
        OpenJob(
            printer_id="p1",
            filename="part.ctb",
            resin_id="r1",
            settings_json=json.dumps(_settings().to_dict()),
            task_id="t9",
            started_at="2026-09-28T10:00:00+00:00",
        )
    )
    history.resolve_open_job("p1", PrintOutcome.FAIL, failure_issue="detached")
    assert history.has_run_for_task("p1", "t9")
    assert not history.has_run_for_task("p1", "t10")
    log = build_history_log(history, {}, {})
    assert [r["outcome"] for r in log["rows"]] == ["FAIL"]


def test_deleted_runs_stay_deleted_across_team_sync(history: HistoryStore) -> None:
    keep = history.log_run("p1", "r1", 0.025, 25.0, _settings(), PrintOutcome.SUCCESS, notes="real")
    fake = history.log_run("p1", "r1", 0.025, 25.0, _settings(), PrintOutcome.FAIL, notes="test")
    teammate = HistoryStore(history.db_path.parent / "mate.db", team_file=history.team_file)
    assert len(teammate.list_for_printer("p1")) == 2

    assert history.delete_runs([fake], reason="test print") == 1
    history.sync_team_data()
    assert [r.id for r in history.list_for_printer("p1")] == [keep]

    teammate.sync_team_data()
    assert [r.notes for r in teammate.list_for_printer("p1")] == ["real"]
    log = build_history_log(history, {}, {})
    assert [r["id"] for r in log["rows"]] == ["#1"]
    fresh = HistoryStore(history.db_path.parent / "fresh.db", team_file=history.team_file)
    assert [r.notes for r in fresh.list_for_printer("p1")] == ["real"]


def test_same_file_reprint_counts_as_new_print() -> None:
    job = OpenJob(printer_id="p1", filename="part.ctb", task_id="t1")
    assert is_new_print(job, SimpleNamespace(filename="other.ctb", task_id="t1"))
    assert is_new_print(job, SimpleNamespace(filename="part.ctb", task_id="t2"))
    assert not is_new_print(job, SimpleNamespace(filename="part.ctb", task_id="t1"))
    assert not is_new_print(job, SimpleNamespace(filename="", task_id=""))
