"""Working-curve calibration: resin, layer height, and temperature."""

from __future__ import annotations

from pathlib import Path

import pytest

from resinpilot.catalog import Catalog
from resinpilot.cure import fit_cure, prior_dp, prior_ea, project_runs, temperature_factor
from resinpilot.engine import RecommendationEngine, scale_exposure
from resinpilot.history import HistoryStore
from resinpilot.models import PrintSettings
from resinpilot.paths import BUILTIN_DATA_DIR as DATA


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(DATA)


@pytest.fixture
def history(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.db")


def test_clear_resin_changes_less_with_layer_height_than_black(catalog: Catalog) -> None:
    clear = catalog.resins["anycubic_clear"]
    black = catalog.resins["elegoo_abs_like_3_black"]
    assert prior_dp(clear) > prior_dp(black)
    clear_t = scale_exposure(2.5, 0.025, 0.05, prior_dp(clear))
    black_t = scale_exposure(3.0, 0.025, 0.05, prior_dp(black))
    assert clear_t / 2.5 > black_t / 3.0


def test_flexible_resin_is_more_sensitive_to_cold(catalog: Catalog) -> None:
    flex = catalog.resins["superflex"]
    standard = catalog.resins["elegoo_8k_standard_space_grey"]
    assert prior_ea(flex) > prior_ea(standard)
    assert temperature_factor(18.0, prior_ea(flex)) > temperature_factor(18.0, prior_ea(standard)) > 1.0


def test_two_layer_heights_fit_a_curve_steeper_than_a_straight_line(
    catalog: Catalog, history: HistoryStore
) -> None:
    resin = catalog.resins["elegoo_8k_standard_space_grey"]
    for height, seconds in ((0.025, 1.8), (0.05, 2.5)):
        history.log_success(
            "saturn_4_ultra",
            resin.id,
            height,
            25.0,
            PrintSettings(layer_height=height, normal_exposure_time=seconds, bottom_exposure_time=40.0),
        )
    runs = history.list_successes("saturn_4_ultra", resin.id)
    model = fit_cure(resin, runs)
    assert model.dp_source == "fit"
    assert model.dp_mm < prior_dp(resin)
    predicted = project_runs(runs, 0.10, 25.0, model)
    straight = 1.8 + (2.5 - 1.8) * (0.10 - 0.025) / (0.05 - 0.025)
    assert predicted is not None
    assert predicted > straight


def test_exposure_curves_follow_resin_and_temperature(catalog: Catalog, history: HistoryStore) -> None:
    curves = RecommendationEngine(catalog, history).exposure_curves("saturn_4_ultra")
    heights = curves["heights"]
    thin = heights.index(0.025)
    mid = heights.index(0.05)
    by = {(row["resin_id"], row["temp"]): row["exposure"] for row in curves["series"]}
    clear_25 = by[("anycubic_clear", 25.0)]
    black_25 = by[("elegoo_abs_like_3_black", 25.0)]
    assert clear_25[mid] == pytest.approx(2.5, abs=0.05)
    assert clear_25[thin] / clear_25[mid] > black_25[thin] / black_25[mid]
    assert by[("anycubic_clear", 20.0)][mid] > clear_25[mid] > by[("anycubic_clear", 30.0)][mid]
    assert clear_25[-1] > clear_25[mid] > clear_25[thin]
