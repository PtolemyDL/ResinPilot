"""Printer-reported slice settings (SDCP Cmd 321 SliceInformation) and file-name cross-check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resinpilot.charts import format_run_rows
from resinpilot.history import HistoryStore, OpenJob, apply_slice_to_job
from resinpilot.models import PrintOutcome, PrintSettings
from resinpilot.sdcp import TaskDetail, filename_mismatches, parse_ctb_filename, settings_from_slice_info

SLICE_16K = {
    "layer_height": 0.02500000037252903,
    "bottom_layer_numbers": 5,
    "transition_layer_numbers": 10,
    "bottom_layer_exposure_time": 34000,
    "normal_layer_exposure_time": 1400,
    "bottom_layer_pwm": 255,
    "normal_layer_pwm": 255,
    "bottom_layer_rest_time_after_drop": 1200,
    "normal_layer_rest_time_after_drop": 1200,
    "normal_layer_rest_time_before_lift": 0,
}


def test_slice_info_converts_ms_to_seconds() -> None:
    s = settings_from_slice_info(SLICE_16K)
    assert s is not None
    assert s.layer_height == 0.025
    assert s.normal_exposure_time == 1.4
    assert s.bottom_exposure_time == 34.0
    assert s.bottom_layer_count == 5
    assert s.transition_layer_count == 10
    assert s.rest_time_after_retract == 1.2
    assert settings_from_slice_info({}) is None


def test_filename_parse_handles_tags_and_checks_against_printer() -> None:
    name = "/media/mmcblk0p3/3in1_Manifold_v7.stl_#45_0.025_1.600_2026_09_24_12_03.ctb"
    assert parse_ctb_filename(name)["normal_exposure_s"] == 1.6
    s = settings_from_slice_info(SLICE_16K)
    assert s is not None
    assert filename_mismatches("Base,_Valve.stl_0.025_1.400_2026_09_25_12_16.ctb", s) == []
    assert filename_mismatches(name, s) == ["file name says layer exposure 1.6s, printer says 1.4s"]


def test_printer_settings_beat_app_estimate_when_logging(tmp_path: Path) -> None:
    history = HistoryStore(tmp_path / "history.db")
    estimate = PrintSettings(layer_height=0.025, normal_exposure_time=1.5, bottom_exposure_time=15.0)
    job = OpenJob(
        printer_id="p1",
        filename="Base,_Valve.stl_0.025_1.400_2026_09_25_12_16.ctb",
        resin_id="r1",
        settings_json=json.dumps(estimate.to_dict()),
        awaiting_outcome=True,
    )
    assert apply_slice_to_job(job, SLICE_16K)
    assert not apply_slice_to_job(job, SLICE_16K)
    history.upsert_open_job(job)

    run_id = history.resolve_open_job("p1", PrintOutcome.SUCCESS, settings=estimate)
    rec = history.get_run(run_id)
    assert rec is not None
    assert rec.settings.bottom_exposure_time == 34.0
    assert rec.settings.normal_exposure_time == 1.4
    assert rec.telemetry["settings_source"] == "printer"


def test_run_settings_backfill_syncs_to_team(tmp_path: Path) -> None:
    history = HistoryStore(tmp_path / "history.db")
    estimate = PrintSettings(layer_height=0.025, normal_exposure_time=1.5, bottom_exposure_time=15.0)
    run_id = history.log_run("p1", "r1", 0.025, 25.0, estimate, PrintOutcome.SUCCESS)
    actual = settings_from_slice_info(SLICE_16K)
    assert actual is not None
    history.set_run_printer_settings(run_id, actual, SLICE_16K, [])
    assert history.get_run(run_id).settings.bottom_exposure_time == 34.0  # type: ignore[union-attr]

    mate = HistoryStore(tmp_path / "mate.db", team_file=history.team_file)
    rec = mate.list_for_printer("p1")[0]
    assert rec.settings.bottom_exposure_time == 34.0
    assert (rec.telemetry or {}).get("settings_source") == "printer"


def test_amended_runs_do_not_duplicate_on_resync(tmp_path: Path) -> None:
    history = HistoryStore(tmp_path / "history.db")
    estimate = PrintSettings(layer_height=0.025, normal_exposure_time=1.5, bottom_exposure_time=15.0)
    run_id = history.log_run("p1", "r1", 0.025, 25.0, estimate, PrintOutcome.SUCCESS)
    actual = settings_from_slice_info(SLICE_16K)
    assert actual is not None
    history.set_run_printer_settings(run_id, actual, SLICE_16K, [])
    history.amend_run(run_id, PrintOutcome.FAIL, failure_issue="detached")
    for _ in range(3):
        history.sync_team_data()
    assert len(history.list_for_printer("p1")) == 1
    mate = HistoryStore(tmp_path / "mate.db", team_file=history.team_file)
    mate.sync_team_data()
    runs = mate.list_for_printer("p1")
    assert len(runs) == 1
    assert runs[0].outcome == PrintOutcome.FAIL
    assert runs[0].settings.normal_exposure_time == 1.4


def _detail(task_id: str, begin: int, status: int = 1, done: int = 750) -> TaskDetail:
    return TaskDetail(
        task_id=task_id,
        task_name=f"/media/mmcblk0p3/{task_id}.stl_0.025_1.400_2026_09_25_12_16.ctb",
        begin_time=begin,
        end_time=begin + 3600,
        task_status=status,
        slice_information={**SLICE_16K, "total_layer_numbers": 750},
        raw={"AlreadyPrintLayer": done, "ErrorStatusReason": 0},
    )


def test_printer_history_import_logs_completed_prints_once(tmp_path: Path) -> None:
    history = HistoryStore(tmp_path / "history.db")
    estimate = PrintSettings(layer_height=0.025, normal_exposure_time=1.5, bottom_exposure_time=15.0)
    history.log_run("p1", "r1", 0.025, 25.0, estimate, PrintOutcome.FAIL, task_id="logged")
    details = [
        _detail("a", 1_790_000_000),
        _detail("logged", 1_790_100_000),
        _detail("stopped", 1_790_200_000, status=3, done=100),
        _detail("b", 1_790_300_000),
    ]
    preview = history.import_printer_prints("p1", details, "r1", 24.0, dry_run=True)
    assert preview["imported"] == 2 and len(history.list_for_printer("p1")) == 1

    res = history.import_printer_prints("p1", details, "r1", 24.0)
    assert (res["imported"], res["already"], res["incomplete"]) == (2, 1, 1)
    runs = {r.task_id: r for r in history.list_for_printer("p1")}
    assert runs["logged"].outcome == PrintOutcome.FAIL
    assert runs["a"].outcome == PrintOutcome.SUCCESS
    assert runs["a"].settings.bottom_exposure_time == 34.0
    assert runs["a"].started_at.startswith("2026-09-21T14:13")
    assert (runs["a"].telemetry or {}).get("settings_source") == "printer"
    assert history.import_printer_prints("p1", details, "r1", 24.0)["imported"] == 0


def test_imported_and_running_prints_keep_vat_resin(tmp_path: Path) -> None:
    slow = _detail("slow", 1_790_000_000)
    slow.slice_information["normal_layer_exposure_time"] = 4000
    history = HistoryStore(tmp_path / "history.db")
    history.import_printer_prints("p1", [slow], "grey", 24.0)
    assert [r.resin_id for r in history.list_for_printer("p1")] == ["grey"]

    job = OpenJob(printer_id="p2", filename="x.ctb", resin_id="grey")
    assert apply_slice_to_job(job, {**SLICE_16K, "normal_layer_exposure_time": 3000})
    assert job.resin_id == "grey"


def test_vat_resin_tags_running_print_and_run_resin_can_be_fixed(tmp_path: Path) -> None:
    history = HistoryStore(tmp_path / "history.db")
    assert history.vat_resin("p1") == ""
    history.upsert_open_job(OpenJob(printer_id="p1", filename="a.ctb", resin_id="old"))
    history.set_vat_resin("p1", "clear")
    assert history.vat_resin("p1") == "clear"
    assert history.get_open_job("p1").resin_id == "clear"  # type: ignore[union-attr]
    assert HistoryStore(tmp_path / "mate.db", team_file=history.team_file).vat_resin("p1") == "clear"

    estimate = PrintSettings(layer_height=0.025, normal_exposure_time=1.5, bottom_exposure_time=15.0)
    run_id = history.log_run("p1", "old", 0.025, 25.0, estimate, PrintOutcome.SUCCESS)
    rec = history.edit_run_settings(run_id, {"resin_id": "clear"})
    assert rec.resin_id == "clear"
    assert (rec.telemetry or {}).get("settings_source") is None
    mate = HistoryStore(tmp_path / "mate2.db", team_file=history.team_file)
    assert mate.list_for_printer("p1")[0].resin_id == "clear"


def test_hand_edited_settings_sync_and_show_in_history(tmp_path: Path) -> None:
    history = HistoryStore(tmp_path / "history.db")
    actual = settings_from_slice_info(SLICE_16K)
    assert actual is not None
    run_id = history.log_run("p1", "r1", 0.025, 25.0, actual, PrintOutcome.SUCCESS)
    history.set_run_printer_settings(run_id, actual, SLICE_16K, [])

    with pytest.raises(ValueError, match="Bottom exposure"):
        history.edit_run_settings(run_id, {"bottom_exposure_time": 999})
    rec = history.edit_run_settings(run_id, {"bottom_exposure_time": "40", "bottom_layer_count": 6.0})
    assert rec.settings.bottom_exposure_time == 40.0
    assert rec.settings.bottom_layer_count == 6
    assert rec.settings.normal_exposure_time == 1.4
    assert rec.telemetry["settings_source"] == "manual"
    assert rec.telemetry["manual_edits"] == {"bottom_exposure_time": 40.0, "bottom_layer_count": 6}

    row = format_run_rows([rec], {})[0]
    assert row["bottom_exp"] == "40.0s"
    assert row["settings"]["bottom_exposure_time"] == 40.0
    assert "printer reported 34s" in row["exp_note"]

    history.sync_team_data()
    assert len(history.list_for_printer("p1")) == 1
    mate = HistoryStore(tmp_path / "mate.db", team_file=history.team_file)
    runs = mate.list_for_printer("p1")
    assert len(runs) == 1
    assert runs[0].settings.bottom_exposure_time == 40.0
    assert (runs[0].telemetry or {}).get("settings_source") == "manual"
