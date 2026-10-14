"""History open-job + SDCP filename helpers."""

import json
from pathlib import Path

import pytest

from resinpilot.history import HistoryStore, OpenJob
from resinpilot.models import GeometryClass, PrintOutcome, PrintSettings
from resinpilot.sdcp import _parse_status_message, layer_height_from_filename


def _settings(**kwargs: float) -> PrintSettings:
    base = dict(
        layer_height=0.05,
        normal_exposure_time=2.0,
        bottom_exposure_time=25.0,
    )
    base.update(kwargs)
    return PrintSettings(**base)


def test_layer_height_from_filename() -> None:
    name = "Base,_Valve.stl_0.025_1.600_2026_09_24.ctb"
    assert layer_height_from_filename(name) == pytest.approx(0.025)


def test_parse_ctb_filename_exposure() -> None:
    from resinpilot.sdcp import parse_ctb_filename

    parsed = parse_ctb_filename("part.stl_0.050_4.000_2026_09_23.ctb")
    assert parsed["layer_height"] == pytest.approx(0.05)
    assert parsed["normal_exposure_s"] == pytest.approx(4.0)


def test_parse_uv_only_temp() -> None:
    """Mars Ultras report UV LED but not TempOfTank."""
    msg = {
        "Status": {
            "CurrentStatus": [0],
            "TempOfUVLED": 30.5,
            "PrintInfo": {"Status": 0, "CurrentLayer": 0, "TotalLayer": 0, "ErrorNumber": 0},
        },
        "MainboardID": "abc",
        "Topic": "sdcp/status/abc",
    }
    st = _parse_status_message(msg, "192.0.2.47")
    assert st.enclosure_temp_c is None
    assert st.uv_led_temp_c == pytest.approx(30.5)
    assert "30° UV" in st.temp_display()
    assert st.best_temp_c == pytest.approx(30.5)


def test_open_job_resolve_records_temps(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "h.db")
    store.upsert_open_job(
        OpenJob(
            printer_id="mars_left",
            filename="part.stl_0.050_2.000.ctb",
            resin_id="abs_like",
            layer_height=0.05,
            vat_temperature_c=31.0,
            uv_led_temp_c=44.0,
            total_layers=800,
            current_layer=800,
            settings_json=json.dumps(_settings().to_dict()),
            geometry_class=GeometryClass.NORMAL.value,
            awaiting_outcome=True,
            last_status="idle",
            started_at="2026-09-24T12:00:00+00:00",
        )
    )
    row_id = store.resolve_open_job("mars_left", PrintOutcome.SUCCESS, notes="looks good")
    assert row_id is not None
    rec = store.list_for_printer("mars_left", limit=1)[0]
    assert rec.vat_temperature_c == pytest.approx(31.0)
    assert rec.uv_led_temp_c == pytest.approx(44.0)
    assert rec.filename.startswith("part")
    assert rec.source == "sdcp"
    assert store.get_open_job("mars_left") is None
