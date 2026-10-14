"""Every test gets its own ResinPilot data folder with a small sample fleet."""

from __future__ import annotations

from pathlib import Path

import pytest

from resinpilot.fleet import FleetEntry, save_fleet

SAMPLE_FLEET = [
    FleetEntry(id="saturn_4_ultra_16k", name="Saturn 16K", model_id="elegoo_saturn_4_ultra_16k", host="192.0.2.10"),
    FleetEntry(id="saturn_4_ultra", name="Saturn", model_id="elegoo_saturn_4_ultra", host="192.0.2.11"),
    FleetEntry(id="mars_5_ultra_left", name="Mars A", model_id="elegoo_mars_5_ultra", host="192.0.2.12"),
    FleetEntry(id="mars_5_ultra_right", name="Mars B", model_id="elegoo_mars_5_ultra", host="192.0.2.13"),
]


@pytest.fixture(autouse=True)
def resinpilot_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "resinpilot_home"
    home.mkdir()
    monkeypatch.setenv("RESINPILOT_HOME", str(home))
    save_fleet(SAMPLE_FLEET)
    return home
