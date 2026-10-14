"""Tests for geometry heuristics and Ridge learning."""

from __future__ import annotations

from pathlib import Path

import pytest

from resinpilot.catalog import Catalog
from resinpilot.engine import RecommendationEngine
from resinpilot.geometry import apply_geometry_heuristics
from resinpilot.history import HistoryStore
from resinpilot.learning import MIN_SAMPLES, PrinterLearner
from resinpilot.models import ConfidenceSource, GeometryClass, PrintSettings, Resin

from resinpilot.paths import BUILTIN_DATA_DIR as DATA


@pytest.fixture
def catalog() -> Catalog:
    return Catalog(DATA)


@pytest.fixture
def history(tmp_path: Path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.db")


@pytest.fixture
def resin() -> Resin:
    return Resin(
        id="t",
        name="T",
        brand="T",
        color="Grey",
        material_family="standard",
        min_normal_exposure=1.2,
        min_bottom_exposure=12.0,
    )


def test_high_suction_increases_rest(resin: Resin) -> None:
    base = PrintSettings(
        layer_height=0.05,
        normal_exposure_time=2.5,
        bottom_exposure_time=30.0,
        rest_time_after_retract=1.2,
        bottom_rest_time_after_retract=1.2,
    )
    out = apply_geometry_heuristics(
        base, GeometryClass.HIGH_SUCTION, cross_section_mm2=15000, bed_area_mm2=20000, resin=resin
    )
    assert out.rest_time_after_retract > base.rest_time_after_retract
    assert out.bottom_layer_count >= 6


def test_learner_needs_min_samples(history: HistoryStore, resin: Resin) -> None:
    learner = PrinterLearner(history)
    template = PrintSettings(layer_height=0.05, normal_exposure_time=2.5, bottom_exposure_time=30.0)
    pred = learner.predict(
        "saturn_4_ultra",
        "elegoo_8k_standard_space_grey",
        0.05,
        25.0,
        GeometryClass.NORMAL,
        0.0,
        20000.0,
        resin,
        template,
    )
    assert pred is None


def test_learner_predicts_after_enough_successes(
    history: HistoryStore, resin: Resin, catalog: Catalog
) -> None:
    printer_id = "saturn_4_ultra"
    resin_id = "elegoo_8k_standard_space_grey"
    # Synthetic: colder → higher exposure
    for i in range(MIN_SAMPLES + 2):
        temp = 20.0 + i
        exp = 3.2 - 0.05 * i
        settings = PrintSettings(
            layer_height=0.05,
            normal_exposure_time=exp,
            bottom_exposure_time=30.0 + i,
            rest_time_after_retract=1.2,
            bottom_rest_time_after_retract=1.2,
            bottom_layer_count=5,
            transition_layer_count=5,
        )
        history.log_success(
            printer_id,
            resin_id,
            0.05,
            temp,
            settings,
            geometry_class=GeometryClass.NORMAL,
            cross_section_mm2=1000.0 * i,
        )
    learner = PrinterLearner(history)
    template = PrintSettings(layer_height=0.05, normal_exposure_time=2.5, bottom_exposure_time=30.0)
    pred = learner.predict(
        printer_id,
        resin_id,
        0.05,
        22.0,
        GeometryClass.NORMAL,
        2000.0,
        catalog.printers[printer_id].machine_width
        * catalog.printers[printer_id].machine_depth,
        resin,
        template,
    )
    assert pred is not None
    assert pred.n_samples >= MIN_SAMPLES
    assert pred.settings.normal_exposure_time > 0


def test_engine_uses_learned_when_fit_ok(
    history: HistoryStore, catalog: Catalog
) -> None:
    engine = RecommendationEngine(catalog, history)
    printer_id = "mars_5_ultra_left"
    resin_id = "anycubic_clear"
    for i in range(8):
        settings = PrintSettings(
            layer_height=0.05,
            normal_exposure_time=3.0 + 0.1 * i,
            bottom_exposure_time=35.0,
            rest_time_after_retract=1.2 + 0.05 * i,
            bottom_rest_time_after_retract=1.2,
            bottom_layer_count=5 + (i % 2),
            transition_layer_count=5,
        )
        history.log_success(
            printer_id,
            resin_id,
            0.05,
            24.0 + 0.5 * i,
            settings,
            geometry_class=GeometryClass.NORMAL if i % 2 == 0 else GeometryClass.LARGE_SOLID,
            cross_section_mm2=500.0 * i,
        )
    rec = engine.recommend(
        printer_id,
        resin_id,
        0.05,
        26.0,
        geometry_class=GeometryClass.LARGE_SOLID,
        cross_section_mm2=2000.0,
    )
    assert rec.confidence == ConfidenceSource.LEARNED


def test_history_separated_by_printer(history: HistoryStore) -> None:
    s = PrintSettings(layer_height=0.05, normal_exposure_time=2.5, bottom_exposure_time=30.0)
    history.log_success("saturn_4_ultra", "elegoo_8k_standard_space_grey", 0.05, 25.0, s)
    history.log_success("mars_5_ultra_left", "elegoo_8k_standard_space_grey", 0.05, 25.0, s)
    assert len(history.list_for_printer("saturn_4_ultra")) == 1
    assert len(history.list_for_printer("mars_5_ultra_left")) == 1
