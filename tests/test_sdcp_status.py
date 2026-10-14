"""SDCP status classification and parse tests."""

import pytest

from resinpilot.sdcp import (
    RELEASE_FILM_MAX_DEFAULT,
    _classify,
    _parse_status_message,
    film_usage,
)


def test_film_usage_ok() -> None:
    f = film_usage(10_572)
    assert f["level"] == "ok"
    assert f["max"] == RELEASE_FILM_MAX_DEFAULT
    assert f["warning"] == ""
    assert "10,572" in f["label"]


def test_film_usage_warn_near_limit() -> None:
    f = film_usage(54_000)
    assert f["level"] == "warn"
    assert "near limit" in f["warning"]


def test_film_usage_critical_at_60k() -> None:
    f = film_usage(60_000)
    assert f["level"] == "critical"
    assert "Replace PTFE film" in f["warning"]
    assert f["pct"] == 100.0


def test_film_usage_over_max() -> None:
    f = film_usage(95_245)
    assert f["level"] == "critical"
    assert f["pct"] == 100.0
    assert f["remaining"] == 0


def test_classify_idle() -> None:
    kind, _, _ = _classify(0, 0, 0)
    assert kind == "idle"


def test_classify_printing() -> None:
    kind, _, _ = _classify(1, 3, 0)
    assert kind == "printing"


def test_classify_failed_error() -> None:
    kind, _, err = _classify(0, 0, 2)
    assert kind == "failed"
    assert err


def test_classify_stopped_failed() -> None:
    kind, _, _ = _classify(0, 8, 0)
    assert kind == "failed"


def test_classify_finished_complete() -> None:
    kind, label, _ = _classify(0, 9, 0)
    assert kind == "finished"
    assert label == "Finished"


def test_parse_elegoo_top_level_status_printing() -> None:
    """ELEGOO Ultras push Status at top level while printing (not under Data)."""
    msg = {
        "Status": {
            "CurrentStatus": [1],
            "TempOfUVLED": 43.2,
            "TempOfTank": 34,
            "PrintInfo": {
                "Status": 3,
                "CurrentLayer": 355,
                "TotalLayer": 799,
                "ErrorNumber": 0,
                "Filename": "part.ctb",
            },
        },
        "MainboardID": "0123456789abcdef",
        "Topic": "sdcp/status/0123456789abcdef",
    }
    st = _parse_status_message(msg, "192.0.2.49")
    assert st.connected
    assert st.status_kind == "printing"
    assert st.current_layer == 355
    assert st.total_layers == 799
    assert st.filename.endswith(".ctb")
    assert st.enclosure_temp_c == 34
    assert st.uv_led_temp_c == 43.2
    assert st.print_progress_pct is not None
    assert 40 < st.print_progress_pct < 50
    # richer fields still parse when present
    msg2 = {
        "Status": {
            "CurrentStatus": [1],
            "TempOfUVLED": 40.0,
            "TempOfTank": 32,
            "TempTargetTank": 30,
            "ReleaseFilm": 100,
            "HeatStatus": 1,
            "TimeLapseStatus": 0,
            "PrintInfo": {
                "Status": 3,
                "CurrentLayer": 10,
                "TotalLayer": 100,
                "CurrentTicks": 60000,
                "TotalTicks": 600000,
                "ErrorNumber": 0,
                "Filename": "x.stl_0.025_1.700_a.ctb",
                "TaskId": "abc-123",
            },
        },
        "MainboardID": "mid",
        "Topic": "sdcp/status/mid",
    }
    st2 = _parse_status_message(msg2, "1.2.3.4")
    assert st2.task_id == "abc-123"
    assert st2.release_film == 100
    assert st2.film["level"] == "ok"
    assert st2.film["max"] == RELEASE_FILM_MAX_DEFAULT
    assert st2.target_tank_temp_c == 30
    assert st2.duration_s == pytest.approx(60.0)
    assert st2.eta_total_s == pytest.approx(600.0)
