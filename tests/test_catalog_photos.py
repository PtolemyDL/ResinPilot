"""Model library, fleet and photo smoke tests."""

from resinpilot.catalog import Catalog


def test_models_have_photos_and_specs() -> None:
    catalog = Catalog()
    assert catalog.models
    for m in catalog.models.values():
        assert m.match_names, f"{m.id} has no match names"
        assert m.resolution_x and m.machine_width, f"{m.id} missing size"


def test_fleet_printers_take_model_details() -> None:
    catalog = Catalog()
    assert len(catalog.printers) == 4
    mars = catalog.printers["mars_5_ultra_left"]
    assert mars.name == "Mars A"
    assert mars.sdcp_ip == "192.0.2.12"
    assert mars.model_id == "elegoo_mars_5_ultra"
    for p in catalog.printers.values():
        assert p.specs, f"{p.id} missing specs"
        assert p.photo_path is not None and p.photo_path.is_file(), f"missing file for {p.photo}"


def test_match_model_by_reported_machine_name() -> None:
    catalog = Catalog()
    assert catalog.match_model("ELEGOO", "Saturn 4 Ultra 16K", "sdcp").id == "elegoo_saturn_4_ultra_16k"
    assert catalog.match_model("ELEGOO", "Mars 5 Ultra", "sdcp").id == "elegoo_mars_5_ultra"
    assert catalog.match_model("Acme", "Unknown 9000", "sdcp") is None


def test_unknown_model_borrows_closest_baseline() -> None:
    from resinpilot.fleet import FleetEntry

    entry = FleetEntry(
        id="mystery",
        name="Mystery",
        model_id="acme_mystery",
        host="192.0.2.50",
        model={"brand": "Acme", "name": "Mystery", "resolution_x": 9024, "resolution_y": 5120,
               "machine_width": 153.96, "machine_depth": 77.4, "machine_height": 165},
    )
    catalog = Catalog(fleet=[entry])
    resin_id = next(iter(catalog.resins))
    base = catalog.get_baseline("mystery", resin_id)
    assert base is not None and base.settings.normal_exposure_time > 0
