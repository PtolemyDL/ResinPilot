"""Finding, adding, renaming and removing printers through the service, with a fake driver."""

from __future__ import annotations

from typing import Any

import pytest

from resinpilot import drivers as drivers_mod
from resinpilot.drivers import FoundPrinter
from resinpilot.fleet import load_fleet, save_fleet
from resinpilot.sdcp import PrinterLiveStatus
from resinpilot.web_service import WebService


class FakeDriver:
    id = "sdcp"
    label = "Fake SDCP"

    def discover(self, timeout_s: float = 2.5, target: str = "") -> list[FoundPrinter]:
        return [
            FoundPrinter("sdcp", "192.0.2.20", "board-a", "Workshop", "Saturn 4 Ultra 16K", "ELEGOO", "V1.2"),
            FoundPrinter("sdcp", "192.0.2.21", "board-b", "", "Photon Mystery", "ANYCUBIC"),
        ]

    def describe(self, printer: FoundPrinter) -> dict[str, Any]:
        return {"resolution_x": 9024, "resolution_y": 5120, "machine_width": 153.9, "machine_depth": 77.4}

    def status(self, host: str, device_id: str) -> PrinterLiveStatus:
        return PrinterLiveStatus(connected=False, ip=host, raw_error="offline in tests")


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> WebService:
    save_fleet([])
    monkeypatch.setattr(drivers_mod, "_registry", {"sdcp": FakeDriver()})
    return WebService()


def test_discover_matches_library_models(service: WebService) -> None:
    rows = {r["host"]: r for r in service.discover()["printers"]}
    assert rows["192.0.2.20"]["model_id"] == "elegoo_saturn_4_ultra_16k"
    assert rows["192.0.2.20"]["known_model"] and not rows["192.0.2.20"]["added"]
    assert not rows["192.0.2.21"]["known_model"]


def test_sdcp_skips_filament_printers() -> None:
    from resinpilot.drivers.sdcp_driver import _is_fdm

    assert _is_fdm("Centauri Carbon")
    assert _is_fdm("Mystery 1", ["gcode", "3mf"])
    assert not _is_fdm("Saturn 4 Ultra")
    assert not _is_fdm("Mystery 2", ["ctb", "goo"])
    assert not _is_fdm("Mystery 3", [])


def test_add_rename_remove(service: WebService) -> None:
    found = service.discover()["printers"]
    res = service.add_printers(found)
    assert len(res["added"]) == 2
    assert all(r["added"] for r in service.discover()["printers"])
    assert service.add_printers(found)["added"] == []  # no duplicates

    printers = {p.name: p for p in service.catalog.printers.values()}
    unknown = printers["Photon Mystery"]
    assert unknown.model_name == "Photon Mystery"
    assert unknown.resolution_x == 9024
    assert service.today_profiles()["cards"], "new printers get profiles straight away"

    service.update_printer(unknown.id, name="Bench")
    assert any(e.name == "Bench" for e in load_fleet())
    service.remove_printer(unknown.id)
    assert unknown.id not in service.catalog.printers
    assert len(load_fleet()) == 1


def test_settings_validate_and_apply(service: WebService) -> None:
    out = service.update_settings({"today_layer_heights": [0.05], "min_bottom_exposure_s": 20})
    assert out["today_layer_heights"] == [0.05]
    assert service.engine.min_bottom_exposure == 20
    with pytest.raises(ValueError):
        service.update_settings({"today_layer_heights": [5]})
