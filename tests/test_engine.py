"""Unit tests for recommendation engine scaling and clamps."""

from __future__ import annotations

from pathlib import Path

import pytest

from resinpilot.catalog import Catalog
from resinpilot.engine import (
    RecommendationEngine,
    apply_layer_height_scaling,
    apply_temperature_compensation,
    clamp_settings,
    scale_exposure,
    temperature_factor,
)
from resinpilot.history import HistoryStore
from resinpilot.models import ConfidenceSource, PrintSettings, Resin

from resinpilot.paths import BUILTIN_DATA_DIR as DATA


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(DATA)


@pytest.fixture
def history(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.db")


@pytest.fixture
def engine(catalog: Catalog, history: HistoryStore) -> RecommendationEngine:
    return RecommendationEngine(catalog, history)


@pytest.fixture
def sample_settings() -> PrintSettings:
    return PrintSettings(
        layer_height=0.05,
        normal_exposure_time=2.5,
        bottom_exposure_time=30.0,
        bottom_layer_count=5,
        transition_layer_count=5,
        rest_time_after_retract=0.5,
        bottom_rest_time_after_retract=0.5,
    )


@pytest.fixture
def sample_resin() -> Resin:
    return Resin(
        id="test",
        name="Test",
        brand="Test",
        color="Grey",
        material_family="standard",
        min_normal_exposure=1.2,
        min_bottom_exposure=12.0,
    )


def test_scale_exposure_thicker_needs_more() -> None:
    t = scale_exposure(2.5, 0.10, 0.05)
    assert t > 2.5
    assert t == pytest.approx(2.5 * (2.0**0.85), rel=1e-6)


def test_scale_exposure_thinner_needs_less() -> None:
    t = scale_exposure(2.5, 0.025, 0.05)
    assert t < 2.5


def test_temperature_factor_colder_increases() -> None:
    assert temperature_factor(20.0) > 1.0
    assert temperature_factor(30.0) < 1.0
    assert temperature_factor(25.0) == pytest.approx(1.0)


def test_apply_layer_height_scaling(sample_settings: PrintSettings) -> None:
    out = apply_layer_height_scaling(sample_settings, 0.10, 0.05)
    assert out.layer_height == 0.10
    assert out.normal_exposure_time > sample_settings.normal_exposure_time
    assert out.bottom_exposure_time > sample_settings.bottom_exposure_time


def test_apply_temperature_compensation_cold(
    sample_settings: PrintSettings, sample_resin: Resin
) -> None:
    out = apply_temperature_compensation(sample_settings, 20.0, sample_resin)
    assert out.normal_exposure_time > sample_settings.normal_exposure_time
    assert out.rest_time_after_retract > sample_settings.rest_time_after_retract


def test_clamp_respects_resin_minimum(sample_resin: Resin) -> None:
    settings = PrintSettings(
        layer_height=0.05,
        normal_exposure_time=0.1,
        bottom_exposure_time=1.0,
    )
    out = clamp_settings(settings, sample_resin)
    assert out.normal_exposure_time >= sample_resin.min_normal_exposure
    assert out.bottom_exposure_time >= sample_resin.min_bottom_exposure


def test_clamp_global_min_bottom_exposure() -> None:
    resin = Resin(
        id="test",
        name="Test",
        brand="Test",
        color="Grey",
        material_family="standard",
        min_normal_exposure=1.2,
        min_bottom_exposure=20.0,
    )
    settings = PrintSettings(
        layer_height=0.05,
        normal_exposure_time=2.0,
        bottom_exposure_time=28.0,
    )
    assert clamp_settings(settings, resin).bottom_exposure_time == 28.0
    assert clamp_settings(settings, resin, min_bottom=35.0).bottom_exposure_time == 35.0


def test_recommend_baseline(engine: RecommendationEngine) -> None:
    rec = engine.recommend(
        "saturn_4_ultra",
        "elegoo_8k_standard_space_grey",
        0.05,
        25.0,
    )
    assert rec.confidence in {ConfidenceSource.BASELINE, ConfidenceSource.TEMP_ADJUSTED}
    assert rec.settings.normal_exposure_time > 0
    assert rec.settings.bottom_exposure_time >= 35.0
    assert abs(rec.settings.layer_height - 0.05) < 1e-6


def test_recommend_interpolates_layer_height(engine: RecommendationEngine) -> None:
    rec = engine.recommend(
        "saturn_4_ultra",
        "elegoo_8k_standard_space_grey",
        0.10,
        25.0,
    )
    assert rec.confidence in {
        ConfidenceSource.INTERPOLATED,
        ConfidenceSource.TEMP_ADJUSTED,
    }
    assert rec.settings.normal_exposure_time > 2.6
    assert rec.settings.bottom_exposure_time >= 35.0


def test_recommend_warns_cold_vat(engine: RecommendationEngine) -> None:
    from resinpilot.models import GeometryClass

    rec = engine.recommend(
        "mars_5_ultra_left",
        "anycubic_clear",
        0.05,
        18.0,
        geometry_class=GeometryClass.NORMAL,
    )
    assert any("20" in w for w in rec.warnings)
    assert rec.settings.bottom_exposure_time >= 35.0


def test_history_overrides_baseline(
    engine: RecommendationEngine, history: HistoryStore, catalog: Catalog
) -> None:
    calibrated = PrintSettings(
        layer_height=0.05,
        normal_exposure_time=2.1,
        bottom_exposure_time=40.0,
        bottom_layer_count=5,
        transition_layer_count=5,
        rest_time_after_retract=0.5,
        bottom_rest_time_after_retract=0.5,
    )
    history.log_success(
        "saturn_4_ultra",
        "elegoo_8k_standard_space_grey",
        0.05,
        25.0,
        calibrated,
    )
    rec = engine.recommend(
        "saturn_4_ultra",
        "elegoo_8k_standard_space_grey",
        0.05,
        25.0,
    )
    assert rec.confidence == ConfidenceSource.CALIBRATED
    assert rec.settings.normal_exposure_time == pytest.approx(2.1)
    assert rec.settings.bottom_exposure_time >= 35.0
