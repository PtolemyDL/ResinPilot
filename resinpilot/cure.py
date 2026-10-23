"""Resin cure model for layer exposure.

Jacobs' working curve (Fundamentals of Stereolithography, 1992) is

    cure depth = Dp · ln(exposure energy / Ec)

so the exposure *time* that cures a layer, on one printer's lamp, is

    t(h) = t(h0) · exp((h − h0) / Dp)

Dp is how far 405 nm light travels in that resin. A pigmented resin has a
small Dp, so a thicker layer needs a much longer exposure. A clear resin has
a large Dp, so the same thickness change barely moves the time. A straight
line (or a single power of layer height) uses one slope for every resin and
misses both cases.

Temperature is not in that equation. The cure rate follows an Arrhenius
response: colder resin needs more time, and a flexible resin more so than a
standard one, because viscosity and oxygen inhibition change with heat.
The old flat 2.5 % per °C is only the local slope of that curve at 25 °C
for a standard resin (Ea ≈ 18.5 kJ/mol).

Dp and Ea start as family priors. ELEGOO publishes one layer height per
resin, and a different absolute time on each printer, so the prior only
shapes the curve. Successful prints on this printer replace it: two layer
heights fit Dp, and measured vat temperatures a few degrees apart fit Ea.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .history import HistoryRecord
from .models import Resin

R_GAS = 8.314462618  # J/mol·K
REF_TEMP_C = 25.0
RECENCY_DECAY = 0.85
# Two pseudo-runs of the family prior, so one odd pair cannot swing the curve.
PRIOR_WEIGHT = 2.0
DP_MIN_MM = 0.04
DP_MAX_MM = 0.45
EA_MIN = 8_000.0
EA_MAX = 40_000.0
HEIGHT_SPAN_MM = 0.008
TEMP_SPAN_C = 4.0

# 405 nm family priors. Dp in mm. Not a measured Ec for a bottle.
_DP_MM = {
    "standard-8k": 0.12,
    "standard": 0.10,
    "abs-like": 0.11,
    "clear": 0.24,
    "sculpt": 0.09,
    "flexible": 0.18,
}
# J/mol. 18500 matches ~2.5 %/°C at 25 °C, the previous linear rule, as a local slope.
_EA = {
    "standard-8k": 18_500.0,
    "standard": 18_500.0,
    "abs-like": 18_000.0,
    "clear": 21_000.0,
    "sculpt": 22_000.0,
    "flexible": 28_000.0,
}


@dataclass(frozen=True)
class CureModel:
    dp_mm: float
    ea_j_mol: float
    dp_source: str  # "prior" or "fit"
    ea_source: str  # "family" or "fit"
    n: int


def prior_dp(resin: Resin) -> float:
    dp = _DP_MM.get(resin.material_family, 0.12)
    color = (resin.color or "").lower()
    if any(word in color for word in ("clear", "translucent")):
        dp = max(dp, 0.22)
    elif "black" in color:
        dp *= 0.75
    elif "white" in color:
        dp *= 0.90
    return dp


def prior_ea(resin: Resin) -> float:
    ea = _EA.get(resin.material_family, 18_500.0)
    color = (resin.color or "").lower()
    if any(word in color for word in ("clear", "translucent")):
        ea = max(ea, 21_000.0)
    if resin.material_family == "flexible":
        ea = 28_000.0
    return ea


def arrhenius(temp_c: float, ref_c: float, ea_j_mol: float) -> float:
    """Time multiplier from `ref_c` to `temp_c`. Colder than the reference is > 1."""
    return math.exp(ea_j_mol / R_GAS * (1.0 / (temp_c + 273.15) - 1.0 / (ref_c + 273.15)))


def temperature_factor(vat_temp_c: float, ea_j_mol: float = 18_500.0) -> float:
    """Exposure multiplier relative to 25 °C for this activation energy."""
    return arrhenius(vat_temp_c, REF_TEMP_C, ea_j_mol)


def anchor_temp(rec: HistoryRecord) -> float:
    """Vat temperature worth fitting. Imported guesses are treated as 25 °C."""
    if (rec.telemetry or {}).get("vat_temp_estimated"):
        return REF_TEMP_C
    if rec.vat_temperature_c is None:
        return REF_TEMP_C
    return float(rec.vat_temperature_c)


def project_time(
    seconds: float,
    h_from: float,
    h_to: float,
    temp_from: float,
    temp_to: float,
    model: CureModel,
) -> float:
    """Move one measured exposure onto another layer height and vat temperature."""
    if seconds <= 0 or model.dp_mm <= 0:
        return seconds
    return seconds * math.exp((h_to - h_from) / model.dp_mm) * arrhenius(temp_to, temp_from, model.ea_j_mol)


def _wls_slope(xs: list[float], ys: list[float], ws: list[float]) -> float | None:
    weight = sum(ws)
    if weight <= 0:
        return None
    xbar = sum(w * x for w, x in zip(ws, xs)) / weight
    ybar = sum(w * y for w, y in zip(ws, ys)) / weight
    var = sum(w * (x - xbar) ** 2 for w, x in zip(ws, xs))
    if var < 1e-12:
        return None
    cov = sum(w * (x - xbar) * (y - ybar) for w, x, y in zip(ws, xs, ys))
    return cov / var


def fit_cure(resin: Resin, runs: list[HistoryRecord]) -> CureModel:
    """Family prior, tightened by this resin's successes (newest first)."""
    dp0 = prior_dp(resin)
    ea0 = prior_ea(resin)
    points: list[tuple[float, float, float, float]] = []
    for i, rec in enumerate(runs):
        seconds = rec.settings.normal_exposure_time
        if seconds <= 0:
            continue
        points.append((rec.layer_height, seconds, anchor_temp(rec), RECENCY_DECAY**i))
    if len(points) < 2:
        return CureModel(dp0, ea0, "prior", "family", len(points))

    heights = [h for h, _t, _temp, _w in points]
    dp = dp0
    dp_source = "prior"
    if max(heights) - min(heights) >= HEIGHT_SPAN_MM:
        ys = [math.log(t / arrhenius(temp, REF_TEMP_C, ea0)) for h, t, temp, _w in points]
        ws = [w for _h, _t, _temp, w in points]
        slope = _wls_slope(heights, ys, ws)
        if slope and slope > 0:
            dp_fit = min(DP_MAX_MM, max(DP_MIN_MM, 1.0 / slope))
            wsum = sum(ws)
            dp = (wsum * dp_fit + PRIOR_WEIGHT * dp0) / (wsum + PRIOR_WEIGHT)
            dp_source = "fit"

    ea = ea0
    ea_source = "family"
    temps = [temp for _h, _t, temp, _w in points]
    if max(temps) - min(temps) >= TEMP_SPAN_C:
        xs = [1.0 / (temp + 273.15) for _h, _t, temp, _w in points]
        ys = [math.log(t) - h / dp for h, t, _temp, _w in points]
        ws = [w for _h, _t, _temp, w in points]
        slope = _wls_slope(xs, ys, ws)
        if slope and slope > 0:
            ea_fit = min(EA_MAX, max(EA_MIN, slope * R_GAS))
            wsum = sum(ws)
            ea = (wsum * ea_fit + PRIOR_WEIGHT * ea0) / (wsum + PRIOR_WEIGHT)
            ea_source = "fit"
    return CureModel(dp, ea, dp_source, ea_source, len(points))


def project_runs(
    runs: list[HistoryRecord],
    target_h: float,
    target_temp: float,
    model: CureModel,
    field: str = "normal_exposure_time",
) -> float | None:
    """Recency-weighted exposure at the requested layer height and vat temperature."""
    num = den = 0.0
    for i, rec in enumerate(runs):
        seconds = float(getattr(rec.settings, field))
        if seconds <= 0:
            continue
        weight = RECENCY_DECAY**i
        num += weight * project_time(seconds, rec.layer_height, target_h, anchor_temp(rec), target_temp, model)
        den += weight
    if den <= 0:
        return None
    return num / den


def describe_curve(
    resin: Resin,
    model: CureModel,
    target_h: float,
    target_temp: float,
    same_height: bool,
) -> str:
    """One line for the recommendation card."""
    if model.dp_source == "fit":
        text = f"cure curve fit to {model.n} successes of this resin (Dp {model.dp_mm * 1000:.0f} µm)"
    elif same_height:
        text = f"{model.n} successes of this resin at {target_h:.3f} mm"
    else:
        text = f"{resin.material_family} cure curve (Dp {model.dp_mm * 1000:.0f} µm)"
    if abs(target_temp - REF_TEMP_C) >= 0.5:
        pct = (arrhenius(target_temp, REF_TEMP_C, model.ea_j_mol) - 1.0) * 100.0
        who = "measured vat temperatures" if model.ea_source == "fit" else f"{resin.material_family} temperature response"
        text += f"; {target_temp:.0f} °C is {pct:+.0f}% vs {REF_TEMP_C:.0f} °C ({who})"
    return text
