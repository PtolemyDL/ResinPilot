"""Geometry heuristics for Ultra tilt printers (when little history exists)."""

from __future__ import annotations

import copy

from .models import GeometryClass, PrintSettings, Resin


def normalize_cross_section(cross_section_mm2: float, bed_area_mm2: float) -> float:
    """Map footprint to 0–1 relative to bed area."""
    if bed_area_mm2 <= 0:
        return 0.0
    return max(0.0, min(1.0, float(cross_section_mm2) / bed_area_mm2))


def bottom_adhesion_factor(
    geometry: GeometryClass,
    cross_section_mm2: float,
    bed_area_mm2: float,
) -> float:
    """Extra bottom exposure multiplier for bulky / high-suction parts."""
    footprint = normalize_cross_section(cross_section_mm2, bed_area_mm2)
    if geometry in (GeometryClass.LARGE_SOLID, GeometryClass.HIGH_SUCTION) or footprint >= 0.4:
        return 1.0 + 0.08 * max(geometry.suction_score, footprint)
    return 1.0


def apply_geometry_heuristics(
    settings: PrintSettings,
    geometry: GeometryClass,
    cross_section_mm2: float,
    bed_area_mm2: float,
    resin: Resin,
) -> PrintSettings:
    """
    Adjust rest times and bottom adhesion for suction / bulk.

    Ultras don't expose lift speed; rest-after-retract is the main control.
    """
    out = copy.deepcopy(settings)
    suction = geometry.suction_score
    footprint = normalize_cross_section(cross_section_mm2, bed_area_mm2)
    combined = max(suction, footprint)

    # Rest after retract: settle resin after vat tilt return
    extra_rest = 0.0
    if combined >= 0.85:
        extra_rest = 1.0
    elif combined >= 0.55:
        extra_rest = 0.5
    elif geometry == GeometryClass.FINE_DETAIL:
        extra_rest = -0.2  # slightly snappier for small parts
    out.rest_time_after_retract = max(0.5, settings.rest_time_after_retract + extra_rest)
    out.bottom_rest_time_after_retract = out.rest_time_after_retract

    # Bottom layers / exposure for adhesion on bulky or high-suction parts
    if geometry in (GeometryClass.LARGE_SOLID, GeometryClass.HIGH_SUCTION) or footprint >= 0.4:
        out.bottom_layer_count = max(settings.bottom_layer_count, 6)
        out.bottom_exposure_time = max(
            resin.min_bottom_exposure,
            settings.bottom_exposure_time * (1.0 + 0.08 * combined),
        )
        out.transition_layer_count = max(settings.transition_layer_count, 6)

    if geometry == GeometryClass.FINE_DETAIL:
        # Prefer slightly less over-cure for detail retention
        out.normal_exposure_time = max(
            resin.min_normal_exposure, settings.normal_exposure_time * 0.97
        )

    out.normal_exposure_time = round(out.normal_exposure_time, 2)
    out.bottom_exposure_time = round(out.bottom_exposure_time, 2)
    out.rest_time_after_retract = round(out.rest_time_after_retract, 2)
    out.bottom_rest_time_after_retract = round(out.bottom_rest_time_after_retract, 2)
    return out
