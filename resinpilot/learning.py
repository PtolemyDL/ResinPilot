"""Per-printer regression learning from successful print runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .geometry import normalize_cross_section
from .history import HistoryRecord, HistoryStore
from .models import GeometryClass, PrintSettings, Resin

MIN_SAMPLES = 5

BaselineRef = Callable[[str, float], "tuple[float, float] | None"]
"""(resin_id, layer_height) -> official (layer exposure, bottom exposure) on this printer."""


@dataclass
class LearnedPrediction:
    settings: PrintSettings
    n_samples: int
    r2_normal: float
    detail: str


def _feature_row(
    layer_height: float,
    geometry: GeometryClass,
    cross_section_mm2: float,
    bed_area_mm2: float,
) -> list[float]:
    """Temperature is not a feature: most logged temps are estimates in a narrow band, so a
    regression on them extrapolates wildly. It's compensated with the rule-based factor instead."""
    footprint = normalize_cross_section(cross_section_mm2, bed_area_mm2)
    return [
        layer_height,
        geometry.suction_score,
        footprint,
    ]


def _training_temp(records: list[HistoryRecord]) -> float | None:
    temps = [
        r.vat_temperature_c
        for r in records
        if r.vat_temperature_c is not None and not (r.telemetry or {}).get("vat_temp_estimated")
    ]
    return float(np.median(temps)) if temps else None


def _build_matrix(
    records: list[HistoryRecord],
    bed_area_mm2: float,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    x_rows = []
    targets: dict[str, list[float]] = {
        "normal_exposure_time": [],
        "bottom_exposure_time": [],
        "rest_time_after_retract": [],
        "bottom_layer_count": [],
        "transition_layer_count": [],
    }
    for rec in records:
        x_rows.append(
            _feature_row(
                rec.layer_height,
                rec.geometry_class,
                rec.cross_section_mm2,
                bed_area_mm2,
            )
        )
        s = rec.settings
        targets["normal_exposure_time"].append(s.normal_exposure_time)
        targets["bottom_exposure_time"].append(s.bottom_exposure_time)
        targets["rest_time_after_retract"].append(s.rest_time_after_retract)
        targets["bottom_layer_count"].append(float(s.bottom_layer_count))
        targets["transition_layer_count"].append(float(s.transition_layer_count))
    x = np.asarray(x_rows, dtype=float)
    y = {k: np.asarray(v, dtype=float) for k, v in targets.items()}
    return x, y


def _fit_ridge(x: np.ndarray, y: np.ndarray) -> tuple[Pipeline, float]:
    model = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("ridge", Ridge(alpha=1.0)),
        ]
    )
    model.fit(x, y)
    r2 = float(model.score(x, y)) if len(y) >= 2 else 0.0
    return model, r2


class PrinterLearner:
    """Fit exposure/rest models from one printer's successful runs (optionally resin-scoped)."""

    def __init__(self, history: HistoryStore) -> None:
        self.history = history

    def predict(
        self,
        printer_id: str,
        resin_id: str,
        layer_height: float,
        vat_temperature_c: float,
        geometry: GeometryClass,
        cross_section_mm2: float,
        bed_area_mm2: float,
        resin: Resin,
        template: PrintSettings,
        baseline_ref: BaselineRef | None = None,
    ) -> LearnedPrediction | None:
        """Same-resin regression when there's enough data; otherwise transfer this printer's
        correction (actual / official baseline) learned on other resins onto this resin's baseline."""
        from .outcome_signals import is_clean_success

        records = self.history.list_successes(printer_id, resin_id=resin_id, limit=500)
        # Successes flagged with exposure problems (missing features, dimensions off) aren't "what worked".
        clean = [r for r in records if is_clean_success(r)]
        if len(clean) >= MIN_SAMPLES:
            records = clean
        same_n = len(records)
        scope = f"{same_n} runs with this resin on this printer"
        target_ref: tuple[float, float] | None = None
        if same_n < MIN_SAMPLES:
            if baseline_ref is None:
                return None
            target_ref = baseline_ref(resin_id, layer_height)
            if target_ref is None:
                return None
            others = [
                r
                for r in self.history.list_successes(printer_id, resin_id=None, limit=500)
                if r.resin_id != resin_id and baseline_ref(r.resin_id, r.layer_height) is not None
            ]
            if len(others) < MIN_SAMPLES:
                return None
            records = others
            scope = (
                f"this resin's official baseline × this printer's correction from "
                f"{len(records)} runs with other resins (only {same_n} with this resin)"
            )

        x, y = _build_matrix(records, bed_area_mm2)
        if target_ref is not None:
            refs = np.asarray([baseline_ref(r.resin_id, r.layer_height) for r in records], dtype=float)
            y["normal_exposure_time"] = y["normal_exposure_time"] / refs[:, 0]
            y["bottom_exposure_time"] = y["bottom_exposure_time"] / refs[:, 1]
        models: dict[str, Pipeline] = {}
        scores: dict[str, float] = {}
        for key, target in y.items():
            model, r2 = _fit_ridge(x, target)
            models[key] = model
            scores[key] = r2

        query = np.asarray(
            [
                _feature_row(
                    layer_height,
                    geometry,
                    cross_section_mm2,
                    bed_area_mm2,
                )
            ],
            dtype=float,
        )

        out = copy_settings(template)
        out.layer_height = layer_height
        out.normal_exposure_time = float(models["normal_exposure_time"].predict(query)[0])
        out.bottom_exposure_time = float(models["bottom_exposure_time"].predict(query)[0])
        if target_ref is not None:
            out.normal_exposure_time *= target_ref[0]
            out.bottom_exposure_time *= target_ref[1]
        train_temp = _training_temp(records)
        if train_temp is not None and abs(train_temp - vat_temperature_c) > 0.5:
            from .engine import temperature_factor

            out.normal_exposure_time *= temperature_factor(vat_temperature_c) / temperature_factor(train_temp)
            scope += f"; temp-corrected {train_temp:.0f}→{vat_temperature_c:.0f} °C"
        out.rest_time_after_retract = float(
            models["rest_time_after_retract"].predict(query)[0]
        )
        out.bottom_rest_time_after_retract = out.rest_time_after_retract
        out.bottom_layer_count = int(
            round(float(models["bottom_layer_count"].predict(query)[0]))
        )
        out.transition_layer_count = int(
            round(float(models["transition_layer_count"].predict(query)[0]))
        )

        # Floors
        out.normal_exposure_time = max(resin.min_normal_exposure, out.normal_exposure_time)
        out.bottom_exposure_time = max(resin.min_bottom_exposure, out.bottom_exposure_time)
        out.bottom_exposure_time = max(35.0, out.bottom_exposure_time)
        out.rest_time_after_retract = max(0.0, out.rest_time_after_retract)
        out.bottom_layer_count = max(1, min(20, out.bottom_layer_count))
        out.transition_layer_count = max(0, min(20, out.transition_layer_count))

        out.normal_exposure_time = round(out.normal_exposure_time, 2)
        out.bottom_exposure_time = round(out.bottom_exposure_time, 2)
        out.rest_time_after_retract = round(out.rest_time_after_retract, 2)
        out.bottom_rest_time_after_retract = out.rest_time_after_retract

        r2n = scores["normal_exposure_time"]
        detail = f"Learned from {scope}; fit R²={r2n:.2f}"
        return LearnedPrediction(
            settings=out,
            n_samples=len(records),
            r2_normal=r2n,
            detail=detail,
        )


def copy_settings(settings: PrintSettings) -> PrintSettings:
    return PrintSettings.from_dict(settings.to_dict())
