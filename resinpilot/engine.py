"""Recommendation engine: working curve → history → baseline → geometry → clamp.

Layer exposure follows the resin working curve (time grows as exp(layer height / Dp)),
with an Arrhenius temperature term. Dp and the temperature response are fit
from this printer's successes when the prints cover more than one layer height
or vat temperature, and otherwise come from the resin family.
"""

from __future__ import annotations

import copy
import math
from dataclasses import replace
from typing import Any, Callable

from .bottom_calibration import calibrate_bottom_exposure
from .catalog import Catalog
from .cure import (
    REF_TEMP_C,
    anchor_temp,
    arrhenius,
    describe_curve,
    fit_cure,
    prior_dp,
    prior_ea,
    project_runs,
    project_time,
    temperature_factor,
)
from .geometry import apply_geometry_heuristics, bottom_adhesion_factor
from .history import HistoryRecord, HistoryStore
from .learning import PrinterLearner
from .models import (
    ConfidenceSource,
    GeometryClass,
    PrintOutcome,
    PrintSettings,
    Recommendation,
    Resin,
)
from .parts import OTHER_GROUP, SIZE_LABELS, PartFamilies
from .parts import size_class as parts_size_class
from .outcome_signals import RESIN_WIDE_WEIGHT, calibrate_layer_exposure, is_clean_success
from .shape_scan import TAG_CHANNELS, TAG_FINE, TAG_LABELS, THIN_FEATURE_MM

DEFAULT_DP_MM = 0.12
CURVE_HEIGHTS_MM = (0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10)
CURVE_TEMPS_C = (20.0, 25.0, 30.0)
TEMP_REST_DELTA_PER_C = 0.05
# Default for the "minimum bottom exposure" setting (0 = only each resin's own minimum applies).
GLOBAL_MIN_BOTTOM_EXPOSURE = 0.0
# Part-specific calibration kicks in once a part / size class has this many runs.
MIN_FAMILY_RUNS = 3
MIN_SIZE_CLASS_RUNS = 3

SAFE_RANGES = {
    "layer_height": (0.01, 0.20),
    "normal_exposure_time": (0.5, 30.0),
    "bottom_exposure_time": (5.0, 120.0),
    "bottom_layer_count": (1, 20),
    "transition_layer_count": (0, 20),
    "bottom_layer_lift_height": (0.0, 15.0),
    "normal_layer_lift_height": (0.0, 15.0),
    "bottom_layer_lift_speed": (0.0, 400.0),
    "normal_layer_lift_speed": (0.0, 400.0),
    "normal_drop_speed": (0.0, 400.0),
    "rest_time_after_retract": (0.0, 10.0),
    "rest_time_before_lift": (0.0, 10.0),
    "rest_time_after_lift": (0.0, 10.0),
}


def scale_exposure(t_ref: float, h: float, h_ref: float, dp_mm: float | None = None) -> float:
    """Exposure time at a new layer height: t scales as exp(delta h / Dp). Default Dp is 0.12 mm."""
    if h_ref <= 0:
        raise ValueError("h_ref must be positive")
    dp = DEFAULT_DP_MM if dp_mm is None else dp_mm
    if dp <= 0:
        raise ValueError("dp_mm must be positive")
    return t_ref * math.exp((h - h_ref) / dp)


def _curve_label(name: str) -> str:
    for prefix in ("ELEGOO ", "Anycubic ", "Siraya Tech ", "3DMaterials "):
        name = name.replace(prefix, "")
    return name.replace(" Resin", "").replace(" 3D Printer", "")


def _curve_exposure(basis, baseline, model, height: float, temp: float) -> float | None:
    if basis:
        value = project_runs(basis, height, temp, model)
        if value is not None:
            return round(value, 2)
    if baseline is None or baseline.settings.normal_exposure_time <= 0:
        return None
    scaled = scale_exposure(
        baseline.settings.normal_exposure_time,
        height,
        baseline.layer_height,
        model.dp_mm,
    )
    return round(scaled * arrhenius(temp, baseline.vat_temperature_c, model.ea_j_mol), 2)


def apply_layer_height_scaling(
    settings: PrintSettings,
    target_height: float,
    h_ref: float | None = None,
    dp_mm: float | None = None,
) -> PrintSettings:
    h_ref = h_ref if h_ref is not None else settings.layer_height
    if abs(target_height - h_ref) < 1e-9:
        out = copy.deepcopy(settings)
        out.layer_height = target_height
        return out
    out = copy.deepcopy(settings)
    out.layer_height = target_height
    out.normal_exposure_time = scale_exposure(settings.normal_exposure_time, target_height, h_ref, dp_mm)
    out.bottom_exposure_time = scale_exposure(settings.bottom_exposure_time, target_height, h_ref, dp_mm)
    return out


def apply_temperature_compensation(
    settings: PrintSettings,
    vat_temp_c: float,
    resin: Resin,
) -> PrintSettings:
    factor = temperature_factor(vat_temp_c, prior_ea(resin))
    out = copy.deepcopy(settings)
    out.normal_exposure_time = max(
        resin.min_normal_exposure, settings.normal_exposure_time * factor
    )
    out.bottom_exposure_time = max(
        resin.min_bottom_exposure, settings.bottom_exposure_time * factor
    )
    if vat_temp_c < REF_TEMP_C:
        extra_rest = (REF_TEMP_C - vat_temp_c) * TEMP_REST_DELTA_PER_C
        out.rest_time_after_retract = settings.rest_time_after_retract + extra_rest
        out.bottom_rest_time_after_retract = (
            settings.bottom_rest_time_after_retract + extra_rest
        )
    return out


def clamp_settings(settings: PrintSettings, resin: Resin, min_bottom: float = GLOBAL_MIN_BOTTOM_EXPOSURE) -> PrintSettings:
    out = copy.deepcopy(settings)
    for attr, (lo, hi) in SAFE_RANGES.items():
        val = getattr(out, attr)
        setattr(out, attr, type(val)(min(hi, max(lo, val))))
    out.normal_exposure_time = max(resin.min_normal_exposure, out.normal_exposure_time)
    out.bottom_exposure_time = max(
        min_bottom,
        resin.min_bottom_exposure,
        out.bottom_exposure_time,
    )
    out.normal_exposure_time = round(out.normal_exposure_time, 2)
    out.bottom_exposure_time = round(out.bottom_exposure_time, 2)
    out.rest_time_after_retract = round(out.rest_time_after_retract, 2)
    out.bottom_rest_time_after_retract = round(out.bottom_rest_time_after_retract, 2)
    out.layer_height = round(out.layer_height, 3)
    return out


def _merge_printer_motion(settings: PrintSettings, catalog: Catalog, printer_id: str) -> PrintSettings:
    printer = catalog.printers[printer_id]
    m = printer.motion
    return replace(
        settings,
        bottom_layer_lift_height=m.bottom_layer_lift_height,
        bottom_layer_lift_speed=m.bottom_layer_lift_speed,
        normal_layer_lift_height=m.normal_layer_lift_height,
        normal_layer_lift_speed=m.normal_layer_lift_speed,
        normal_drop_speed=m.normal_drop_speed,
        rest_time_after_retract=m.rest_time_after_retract,
        rest_time_before_lift=m.rest_time_before_lift,
        rest_time_after_lift=m.rest_time_after_lift,
        bottom_rest_time_after_retract=m.bottom_rest_time_after_retract,
        bottom_rest_time_before_lift=m.bottom_rest_time_before_lift,
        bottom_rest_time_after_lift=m.bottom_rest_time_after_lift,
    )


def _bed_area(catalog: Catalog, printer_id: str) -> float:
    p = catalog.printers[printer_id]
    return float(p.machine_width * p.machine_depth)


class RecommendationEngine:
    def __init__(self, catalog: Catalog, history: HistoryStore, min_bottom_exposure: float = GLOBAL_MIN_BOTTOM_EXPOSURE) -> None:
        self.catalog = catalog
        self.history = history
        self.learner = PrinterLearner(history)
        self.min_bottom_exposure = min_bottom_exposure

    def recommend(
        self,
        printer_id: str,
        resin_id: str,
        layer_height: float,
        vat_temperature_c: float,
        geometry_class: GeometryClass = GeometryClass.NORMAL,
        cross_section_mm2: float = 0.0,
        part_family: str = "",
        size_class: str = "",
    ) -> Recommendation:
        """`part_family` (family id) and `size_class` narrow calibration to prints of that part / size."""
        rec = self._recommend(
            printer_id, resin_id, layer_height, vat_temperature_c, geometry_class, cross_section_mm2,
            part_family, size_class,
        )
        if part_family:
            self._apply_family_exposure(rec, part_family, layer_height)
        self._apply_outcome_signals(rec, part_family, size_class, layer_height)
        return rec

    def _apply_outcome_signals(self, rec: Recommendation, part_family: str, size_class: str, layer_height: float) -> None:
        """Failure types and quality answers in the relevant reports → layer exposure correction."""
        run_filter, scope = self._part_scope(rec.printer_id, rec.resin_id, part_family, size_class)
        runs = self.history.list_for_printer(rec.printer_id, resin_id=rec.resin_id, limit=1000)
        if run_filter is not None:
            runs = [r for r in runs if run_filter(r)]
        families = PartFamilies(self.history.list_recent(printer_id=None, outcome=None, limit=100000))
        cal = calibrate_layer_exposure(
            runs,
            current=rec.settings.normal_exposure_time,
            layer_height=layer_height,
            floor=self.catalog.resins[rec.resin_id].min_normal_exposure,
            scale=scale_exposure,
            tags_of=lambda r: families.shape_tags(families.family_id(r.filename)),
            weight=1.0 if run_filter is not None else RESIN_WIDE_WEIGHT,
            scope=scope.replace("'s", "").replace("'", "").strip(),
        )
        if cal.detail:
            rec.exposure_detail = cal.detail
        if abs(cal.value - rec.settings.normal_exposure_time) >= 0.005:
            rec.settings.normal_exposure_time = cal.value
            rec.confidence = ConfidenceSource.CALIBRATED
            rec.source_detail = f"{cal.detail}; {rec.source_detail}"

    def _recommend(
        self,
        printer_id: str,
        resin_id: str,
        layer_height: float,
        vat_temperature_c: float,
        geometry_class: GeometryClass,
        cross_section_mm2: float,
        part_family: str,
        size_class: str,
    ) -> Recommendation:
        if printer_id not in self.catalog.printers:
            raise KeyError(f"Unknown printer: {printer_id}")
        if resin_id not in self.catalog.resins:
            raise KeyError(f"Unknown resin: {resin_id}")

        resin = self.catalog.resins[resin_id]
        bed = _bed_area(self.catalog, printer_id)
        warnings: list[str] = []
        if vat_temperature_c < 20:
            warnings.append(
                f"Vat temperature {vat_temperature_c:.1f} °C is below 20 °C — warm the resin."
            )
        if vat_temperature_c > 35:
            warnings.append(
                f"Vat temperature {vat_temperature_c:.1f} °C is above 35 °C — check resin limits."
            )
        if cross_section_mm2 > bed * 0.7:
            warnings.append(
                "Cross-section is a large fraction of the bed — high suction risk; prefer Slow mode."
            )

        # Template from baseline for learned copy / fallbacks
        baseline = self.catalog.get_baseline(printer_id, resin_id)
        if baseline is None:
            raise ValueError(f"No baseline for printer={printer_id}, resin={resin_id}")
        template = _merge_printer_motion(
            copy.deepcopy(baseline.settings), self.catalog, printer_id
        )

        # 1) Learned regression (per printer, prefers same resin)
        learned = self.learner.predict(
            printer_id=printer_id,
            resin_id=resin_id,
            layer_height=layer_height,
            vat_temperature_c=vat_temperature_c,
            geometry=geometry_class,
            cross_section_mm2=cross_section_mm2,
            bed_area_mm2=bed,
            resin=resin,
            template=template,
            baseline_ref=self._baseline_ref(printer_id),
        )
        if learned is not None and learned.r2_normal >= 0.15:
            curve = self._overlay_cure(learned.settings, printer_id, resin, layer_height, vat_temperature_c)
            if curve:
                learned = replace(learned, detail=f"{curve}; {learned.detail}")
            settings, bottom_cal = self._calibrate_bottom(
                learned.settings, printer_id, resin_id, resin, geometry_class, cross_section_mm2,
                part_family, size_class, vat_temperature_c,
            )
            settings = clamp_settings(settings, resin, self.min_bottom_exposure)
            return Recommendation(
                settings=settings,
                confidence=ConfidenceSource.LEARNED,
                source_detail=learned.detail,
                warnings=warnings,
                printer_id=printer_id,
                resin_id=resin_id,
                vat_temperature_c=vat_temperature_c,
                geometry_class=geometry_class,
                cross_section_mm2=cross_section_mm2,
                bottom_detail=bottom_cal.detail,
                bottom_calibrated=bottom_cal.calibrated,
            )

        confidence = ConfidenceSource.BASELINE
        source_detail = ""
        settings: PrintSettings | None = None
        temp_matched = False
        geom_matched = False

        # 2) Exact / nearest history (geometry-aware)
        exact = self.history.find_exact(
            printer_id,
            resin_id,
            layer_height,
            vat_temperature_c,
            geometry_class=geometry_class,
        )
        if exact is not None:
            settings = copy.deepcopy(exact.settings)
            settings.layer_height = layer_height
            confidence = ConfidenceSource.CALIBRATED
            source_detail = (
                f"Exact success match (id={exact.id}, {exact.geometry_class.label}, "
                f"{exact.vat_temperature_c:.1f} °C)"
            )
            temp_matched = True
            geom_matched = True
        else:
            nearest = self.history.find_nearest_layer(
                printer_id, resin_id, layer_height, geometry_class=geometry_class
            )
            if nearest is not None:
                basis = self._cure_basis(printer_id, resin_id)
                model = fit_cure(resin, basis)
                settings = copy.deepcopy(nearest.settings)
                settings.layer_height = layer_height
                normal = project_runs(basis, layer_height, vat_temperature_c, model)
                bottom = project_runs(basis, layer_height, vat_temperature_c, model, "bottom_exposure_time")
                if normal is not None:
                    settings.normal_exposure_time = normal
                if bottom is not None:
                    settings.bottom_exposure_time = bottom
                same_height = all(abs(r.layer_height - layer_height) < 1e-3 for r in basis)
                if abs(vat_temperature_c - REF_TEMP_C) >= 0.5:
                    confidence = ConfidenceSource.TEMP_ADJUSTED
                elif same_height:
                    confidence = ConfidenceSource.CALIBRATED
                else:
                    confidence = ConfidenceSource.INTERPOLATED
                source_detail = describe_curve(resin, model, layer_height, vat_temperature_c, same_height)
                temp_matched = True
                geom_matched = nearest.geometry_class == geometry_class

        # 3) YAML baseline
        if settings is None:
            settings = apply_layer_height_scaling(
                copy.deepcopy(template), layer_height, baseline.layer_height, prior_dp(resin)
            )
            if abs(layer_height - baseline.layer_height) > 1e-6:
                confidence = ConfidenceSource.INTERPOLATED
                source_detail = (
                    f"Baseline {baseline.layer_height:.3f} mm @ {baseline.vat_temperature_c:.0f} °C, "
                    f"{resin.material_family} cure curve (Dp {prior_dp(resin) * 1000:.0f} µm)"
                )
            else:
                confidence = ConfidenceSource.BASELINE
                source_detail = (
                    f"YAML baseline @ {baseline.layer_height:.3f} mm / "
                    f"{baseline.vat_temperature_c:.0f} °C"
                    + (f" — {baseline.notes}" if baseline.notes else "")
                )

        if not temp_matched:
            before = settings.normal_exposure_time
            settings = apply_temperature_compensation(settings, vat_temperature_c, resin)
            if abs(settings.normal_exposure_time - before) > 1e-6:
                if confidence in (ConfidenceSource.BASELINE, ConfidenceSource.INTERPOLATED):
                    confidence = ConfidenceSource.TEMP_ADJUSTED
                source_detail += f"; temp adjusted for {vat_temperature_c:.1f} °C"

        if not geom_matched:
            settings = apply_geometry_heuristics(
                settings, geometry_class, cross_section_mm2, bed, resin
            )
            source_detail += f"; geometry heuristic ({geometry_class.label})"

        if learned is not None:
            warnings.append(
                f"Regression had {learned.n_samples} samples but weak fit "
                f"(R²={learned.r2_normal:.2f}) — exposure follows the resin cure curve."
            )

        settings, bottom_cal = self._calibrate_bottom(
            settings, printer_id, resin_id, resin, geometry_class, cross_section_mm2,
            part_family, size_class, vat_temperature_c,
        )
        settings = clamp_settings(settings, resin, self.min_bottom_exposure)
        return Recommendation(
            settings=settings,
            confidence=confidence,
            source_detail=source_detail.strip("; "),
            warnings=warnings,
            printer_id=printer_id,
            resin_id=resin_id,
            vat_temperature_c=vat_temperature_c,
            geometry_class=geometry_class,
            cross_section_mm2=cross_section_mm2,
            bottom_detail=bottom_cal.detail,
            bottom_calibrated=bottom_cal.calibrated,
        )

    def _part_context(self, printer_id: str, resin_id: str, part_family: str) -> dict[str, Any]:
        """This part's runs on this printer + resin, its name and shape, and same-shape parts' runs."""
        all_runs = self.history.list_recent(printer_id=None, outcome=None, limit=100000)
        families = PartFamilies(all_runs)
        here = [r for r in all_runs if r.printer_id == printer_id and r.resin_id == resin_id]
        fid_of = {r.id: families.family_id(r.filename) for r in here}
        anywhere = [r for r in all_runs if families.family_id(r.filename) == part_family]
        tags = families.shape_tags(part_family)
        group = families.group(part_family)
        return {
            "name": families.resolve(anywhere[0].filename)[1] if anywhere else families.name(part_family),
            "runs": [r for r in here if fid_of[r.id] == part_family],
            "tags": tags,
            "shape": families.shape(part_family),
            # Other parts with the same detected shape (e.g. internal channels), only once scanned.
            "alike": [
                r for r in here
                if tags and fid_of[r.id] != part_family and families.shape_tags(fid_of[r.id]) == tags
            ],
            "group": group,
            # Other parts of the same generic group (e.g. all valve variants).
            "group_runs": [
                r for r in here
                if group != OTHER_GROUP and fid_of[r.id] != part_family and families.group(fid_of[r.id]) == group
            ],
        }

    @staticmethod
    def _shape_label(tags: tuple[str, ...]) -> str:
        return " + ".join(TAG_LABELS[t].lower() for t in tags)

    def _part_scope(
        self, printer_id: str, resin_id: str, part_family: str, size_class: str
    ) -> tuple[Callable[[HistoryRecord], bool] | None, str]:
        """Narrowest run set with enough data: this part, same-shape parts, this size class, the resin."""
        if part_family:
            ctx = self._part_context(printer_id, resin_id, part_family)
            if len(ctx["runs"]) >= MIN_FAMILY_RUNS:
                ids = {r.id for r in ctx["runs"]}
                return (lambda r: r.id in ids), f"{ctx['name']}'s"
            alike = ctx["runs"] + ctx["alike"]
            if size_class:
                alike = [r for r in alike if parts_size_class(r) in (size_class, "")]
            if len(alike) >= MIN_FAMILY_RUNS:
                ids = {r.id for r in alike}
                return (lambda r: r.id in ids), f"parts with {self._shape_label(ctx['tags'])}'"
            grouped = ctx["runs"] + ctx["group_runs"]
            if len(grouped) >= MIN_FAMILY_RUNS:
                ids = {r.id for r in grouped}
                return (lambda r: r.id in ids), f"{ctx['group']} group's"
        if size_class:
            runs = self.history.list_for_printer(printer_id, resin_id=resin_id, limit=1000)
            same = [r for r in runs if parts_size_class(r) == size_class]
            if len(same) >= MIN_SIZE_CLASS_RUNS:
                return (lambda r: parts_size_class(r) == size_class), f"{SIZE_LABELS[size_class].lower()} parts'"
        return None, "this resin's"

    def exposure_curves(self, printer_id: str, resin_id: str = "") -> dict[str, Any]:
        """Layer exposure against layer height, one line per resin and vat temperature.

        A resin with successful prints uses the curve fit from those prints.
        Otherwise the line is that printer's manufacturer baseline, shaped by
        the resin family's penetration and temperature response.
        """
        names = {rid: resin.name for rid, resin in self.catalog.primary_resins.items()}
        if resin_id:
            resin_ids = [resin_id] if resin_id in names else []
        else:
            resin_ids = list(names)
        used = {
            rid
            for rid in resin_ids
            if self.history.list_successes(printer_id, rid, limit=1)
        }
        resins: list[dict[str, Any]] = []
        series: list[dict[str, Any]] = []
        for rid in resin_ids:
            resin = self.catalog.resins[rid]
            basis = self._cure_basis(printer_id, rid)
            model = fit_cure(resin, basis)
            baseline = self.catalog.get_baseline(printer_id, rid)
            if not basis and baseline is None:
                continue
            source = "fit" if basis else "baseline"
            resins.append(
                {
                    "id": rid,
                    "name": resin.name,
                    "label": _curve_label(resin.name),
                    "used": rid in used,
                    "dp_um": round(model.dp_mm * 1000),
                    "source": source,
                    "n": model.n if basis else 0,
                }
            )
            for temp in CURVE_TEMPS_C:
                series.append(
                    {
                        "resin_id": rid,
                        "temp": temp,
                        "exposure": [
                            _curve_exposure(basis, baseline, model, height, temp)
                            for height in CURVE_HEIGHTS_MM
                        ],
                    }
                )
        prints = []
        for rec in self.history.list_successes(printer_id, resin_id or None, limit=400):
            if rec.resin_id not in names:
                continue
            if rec.settings.normal_exposure_time <= 0:
                continue
            prints.append(
                {
                    "resin_id": rec.resin_id,
                    "layer": rec.layer_height,
                    "exp": rec.settings.normal_exposure_time,
                    "temp": anchor_temp(rec),
                }
            )
        return {
            "heights": list(CURVE_HEIGHTS_MM),
            "temps": list(CURVE_TEMPS_C),
            "resins": resins,
            "series": series,
            "prints": prints,
        }

    def _cure_basis(self, printer_id: str, resin_id: str) -> list[HistoryRecord]:
        runs = self.history.list_successes(printer_id, resin_id, limit=200)
        clean = [r for r in runs if is_clean_success(r)]
        return clean or runs

    def _overlay_cure(
        self,
        settings: PrintSettings,
        printer_id: str,
        resin: Resin,
        layer_height: float,
        vat_temperature_c: float,
    ) -> str:
        """Replace a linear exposure guess with the resin working curve."""
        basis = self._cure_basis(printer_id, resin.id)
        if not basis:
            return ""
        model = fit_cure(resin, basis)
        normal = project_runs(basis, layer_height, vat_temperature_c, model)
        if normal is None:
            return ""
        settings.normal_exposure_time = normal
        settings.layer_height = layer_height
        same_height = all(abs(r.layer_height - layer_height) < 1e-3 for r in basis)
        return describe_curve(resin, model, layer_height, vat_temperature_c, same_height)

    def _weighted_exposure(
        self,
        ok: list[HistoryRecord],
        layer_height: float,
        resin: Resin,
        vat_temperature_c: float,
    ) -> tuple[float, int, bool, Any]:
        same_layer = [r for r in ok if abs(r.layer_height - layer_height) < 1e-3]
        basis = same_layer if len(same_layer) >= 2 else ok
        model = fit_cure(resin, basis)
        value = project_runs(basis, layer_height, vat_temperature_c, model)
        return (value if value is not None else 0.0), len(basis), basis is same_layer, model

    def _apply_family_exposure(self, rec: Recommendation, part_family: str, layer_height: float) -> None:
        """Use what actually worked for this part (or same-shape parts): recency-weighted layer exposure."""
        ctx = self._part_context(rec.printer_id, rec.resin_id, part_family)
        name = ctx["name"]
        self._shape_warnings(rec, ctx)
        ok = [r for r in ctx["runs"] if r.outcome == PrintOutcome.SUCCESS]
        clean = [r for r in ok if is_clean_success(r, ctx["tags"])]
        # Successes with exposure-related quality problems (missing features, closed channels…) aren't copied.
        if len(clean) >= MIN_FAMILY_RUNS:
            ok = clean
        source = f"of {name}"
        if len(ok) < MIN_FAMILY_RUNS:
            alike_ok = ok + [r for r in ctx["alike"] if r.outcome == PrintOutcome.SUCCESS]
            group_ok = ok + [r for r in ctx["group_runs"] if r.outcome == PrintOutcome.SUCCESS]
            if len(alike_ok) >= MIN_FAMILY_RUNS:
                ok = alike_ok
                source = f"of parts with {self._shape_label(ctx['tags'])} like {name}"
            elif len(group_ok) >= MIN_FAMILY_RUNS:
                ok = group_ok
                source = f"in the {ctx['group']} group (no {MIN_FAMILY_RUNS} successes of {name} itself yet)"
            else:
                have = f"{len(ok)} successful print{'s' if len(ok) != 1 else ''}" if ok else "no successful prints"
                rec.part_detail = (
                    f"{name}: {have} on this printer with this resin yet — layer exposure is the general "
                    f"recommendation until there are {MIN_FAMILY_RUNS}"
                )
                return
        resin = self.catalog.resins[rec.resin_id]
        value, n, same_layer, model = self._weighted_exposure(ok, layer_height, resin, rec.vat_temperature_c)
        rec.settings.normal_exposure_time = round(max(resin.min_normal_exposure, value), 2)
        rec.confidence = ConfidenceSource.CALIBRATED
        fitted = "fit" if model.dp_source == "fit" else "resin prior"
        at = (
            f"at {layer_height:.3f} mm"
            if same_layer
            else f"on the cure curve (Dp {model.dp_mm * 1000:.0f} µm, {fitted})"
        )
        rec.part_detail = f"Layer exposure from {n} successful prints {source} on this printer ({at})"
        rec.source_detail = f"{rec.part_detail}; {rec.source_detail}"

    @staticmethod
    def _shape_warnings(rec: Recommendation, ctx: dict[str, Any]) -> None:
        shape = ctx["shape"]
        if not shape:
            return
        if TAG_CHANNELS in ctx["tags"]:
            rec.warnings.append(
                f"{ctx['name']} has ~{shape['channel_mm']:.2f} mm internal channels — over-exposure narrows "
                "or closes them; if channels clog, lower layer exposure first and flush them right after printing"
            )
        if TAG_FINE in ctx["tags"]:
            rec.warnings.append(
                f"{ctx['name']} has fine features ({shape['thin_pct']:.0f}% of the cross-section under "
                f"{THIN_FEATURE_MM:g} mm) — don't drop layer exposure below what worked, thin walls break first"
            )

    def _baseline_ref(self, printer_id: str):
        cache: dict[tuple[str, float], tuple[float, float] | None] = {}

        def ref(resin_id: str, layer_height: float) -> tuple[float, float] | None:
            key = (resin_id, round(layer_height, 4))
            if key not in cache:
                b = self.catalog.get_baseline(printer_id, resin_id)
                if b is None or b.settings.normal_exposure_time <= 0 or b.settings.bottom_exposure_time <= 0:
                    cache[key] = None
                else:
                    resin = self.catalog.resins.get(resin_id)
                    dp = prior_dp(resin) if resin is not None else DEFAULT_DP_MM
                    cache[key] = (
                        scale_exposure(b.settings.normal_exposure_time, layer_height, b.layer_height, dp),
                        scale_exposure(b.settings.bottom_exposure_time, layer_height, b.layer_height, dp),
                    )
            return cache[key]

        return ref

    def _calibrate_bottom(
        self,
        settings: PrintSettings,
        printer_id: str,
        resin_id: str,
        resin: Resin,
        geometry_class: GeometryClass,
        cross_section_mm2: float,
        part_family: str = "",
        size_class: str = "",
        vat_temperature_c: float = REF_TEMP_C,
    ):
        floor = max(self.min_bottom_exposure, resin.min_bottom_exposure)
        run_filter, scope = self._part_scope(printer_id, resin_id, part_family, size_class)
        model = fit_cure(resin, self._cure_basis(printer_id, resin_id))

        def project(rec: HistoryRecord) -> float:
            return project_time(
                rec.settings.bottom_exposure_time,
                rec.layer_height,
                settings.layer_height,
                anchor_temp(rec),
                vat_temperature_c,
                model,
            )

        cal = calibrate_bottom_exposure(
            self.history,
            printer_id,
            resin_id,
            current=settings.bottom_exposure_time,
            floor=floor,
            baseline_ref=self._baseline_ref(printer_id),
            run_filter=run_filter,
            scope=scope,
            project=project,
            target_layer_height=settings.layer_height,
            target_temp_c=vat_temperature_c,
            temperature_ea=prior_ea(resin),
        )
        if not cal.calibrated:
            return settings, cal
        factor = bottom_adhesion_factor(
            geometry_class, cross_section_mm2, _bed_area(self.catalog, printer_id)
        )
        out = copy.deepcopy(settings)
        out.bottom_exposure_time = cal.value * factor
        if factor > 1.0:
            cal.detail += f"; ×{factor:.2f} for {geometry_class.label.lower()}"
        return out, cal
