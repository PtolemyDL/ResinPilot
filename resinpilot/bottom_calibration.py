"""Bottom-exposure calibration from logged outcomes.

Bottom exposure governs build-plate adhesion, so it is calibrated from
outcomes rather than scaled from layer exposure:

* successes anchor the value (recency-weighted mean of what worked),
* adhesion failures (part did not stick / came off the plate) push it up,
* over-cure reports (elephant foot, part welded to the plate) pull it down,
* the floor (resin minimum, or the user's minimum bottom exposure) is never crossed.

Runs at every layer height count — bottom layers are about the plate, not
the Z resolution of the rest of the part. Each resin calibrates from its own
runs on that printer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .history import HistoryRecord, HistoryStore
from .models import PrintOutcome

ISSUE_NONE = ""
ISSUE_NO_ADHESION = "no_adhesion"
ISSUE_DETACHED = "detached"
ISSUE_OVER_CURE = "over_cure"
ISSUE_LAYER_SEPARATION = "layer_separation"
ISSUE_SUPPORTS = "supports_failed"
ISSUE_FEATURES_CLOSED = "features_closed"
ISSUE_OTHER = "other"

FAILURE_ISSUES: dict[str, str] = {
    ISSUE_NO_ADHESION: "Didn't stick to plate (stuck on film)",
    ISSUE_DETACHED: "Came off the plate mid-print",
    ISSUE_OVER_CURE: "Over-cured base (elephant foot / hard to remove)",
    ISSUE_LAYER_SEPARATION: "Layers split / delaminated mid-print",
    ISSUE_SUPPORTS: "Supports broke / part dropped off its supports",
    ISSUE_FEATURES_CLOSED: "Channels or holes closed up / details blurred",
    ISSUE_OTHER: "Other (resin, slicing, machine, …)",
}

ADHESION_ISSUES = {ISSUE_NO_ADHESION, ISSUE_DETACHED}

ADHESION_BUMP = 1.15
OVER_CURE_CUT = 0.90
RECENCY_DECAY = 0.85
OTHER_RESIN_WEIGHT = 0.5
MAX_BOTTOM_EXPOSURE = 120.0
EARLY_FAIL_LAYER_MARGIN = 10

_ADHESION_WORDS = (
    "stick",
    "adhes",
    "plate",
    "fell",
    "fall off",
    "detach",
    "came off",
    "on the film",
    "on film",
    "stuck to fep",
    "stuck to film",
)
_OVER_CURE_WORDS = ("elephant", "over cure", "overcure", "over-cure", "hard to remove", "too hard")
_CLOSED_WORDS = ("clog", "channel closed", "channels closed", "blocked", "filled in", "closed up", "blurr")
_SEPARATION_WORDS = ("delamin", "layer split", "layers split", "separat", "layer shift")
_SUPPORT_WORDS = ("support",)


@dataclass
class BottomCalibration:
    value: float
    detail: str
    n_runs: int
    calibrated: bool


def infer_issue(rec: HistoryRecord) -> str:
    """Explicit issue if logged, else infer from notes and failure layer."""
    if rec.outcome == PrintOutcome.SUCCESS:
        return ISSUE_NONE
    if rec.failure_issue and rec.failure_issue != ISSUE_OTHER:
        return rec.failure_issue
    notes = (rec.notes or "").lower()
    if any(w in notes for w in _OVER_CURE_WORDS):
        return ISSUE_OVER_CURE
    if any(w in notes for w in _CLOSED_WORDS):
        return ISSUE_FEATURES_CLOSED
    if any(w in notes for w in _SEPARATION_WORDS):
        return ISSUE_LAYER_SEPARATION
    if any(w in notes for w in _SUPPORT_WORDS):
        return ISSUE_SUPPORTS
    if any(w in notes for w in _ADHESION_WORDS):
        return ISSUE_NO_ADHESION
    if rec.failure_issue == ISSUE_OTHER:
        return ISSUE_OTHER
    s = rec.settings
    if rec.end_layer is not None and rec.total_layers:
        early = s.bottom_layer_count + s.transition_layer_count + EARLY_FAIL_LAYER_MARGIN
        if rec.end_layer < rec.total_layers and rec.end_layer <= early:
            return ISSUE_NO_ADHESION
    return ISSUE_OTHER


def calibrate_bottom_exposure(
    history: HistoryStore,
    printer_id: str,
    resin_id: str,
    current: float,
    floor: float,
    limit: int = 60,
    baseline_ref: Callable[[str, float], tuple[float, float] | None] | None = None,
    run_filter: Callable[[HistoryRecord], bool] | None = None,
    scope: str = "this resin's",
) -> BottomCalibration:
    """Calibrate from this resin's own runs on this printer (newest first).

    `run_filter` narrows those runs further (e.g. one part family or size class).
    With no runs for this resin yet, start from the printer's successes with other resins,
    scaled by the ratio of official bottom exposures; their failures are not carried over.
    """
    all_runs = history.list_for_printer(printer_id, limit=limit if run_filter is None else 1000)
    runs = [r for r in all_runs if r.resin_id == resin_id and (run_filter is None or run_filter(r))]
    if run_filter is None:
        runs = runs[:limit]
    if not runs:
        return _from_other_resins(all_runs, resin_id, current, floor, baseline_ref)

    anchor_num = 0.0
    anchor_den = 0.0
    n_success = 0
    n_adhesion = 0
    n_over = 0
    target_up: float | None = None
    target_down: float | None = None
    best_success_after: list[float] = []  # bottom values of newer successes

    for i, rec in enumerate(runs):
        weight = RECENCY_DECAY**i
        bottom = max(floor, float(rec.settings.bottom_exposure_time))
        if rec.outcome == PrintOutcome.SUCCESS:
            anchor_num += weight * bottom
            anchor_den += weight
            n_success += 1
            best_success_after.append(bottom)
            continue
        issue = infer_issue(rec)
        if issue in ADHESION_ISSUES:
            # Resolved if a newer success already ran at a meaningfully higher bottom.
            if any(b >= bottom * 1.05 for b in best_success_after):
                continue
            n_adhesion += 1
            need = bottom * ADHESION_BUMP
            target_up = need if target_up is None else max(target_up, need)
        elif issue == ISSUE_OVER_CURE:
            if any(b <= bottom * 0.95 for b in best_success_after):
                continue
            n_over += 1
            cap = bottom * OVER_CURE_CUT
            target_down = cap if target_down is None else min(target_down, cap)

    if anchor_den > 0:
        value = anchor_num / anchor_den
    else:
        value = max(floor, current)
    if target_up is not None:
        value = max(value, target_up)
    if target_down is not None and n_adhesion == 0:
        value = min(value, target_down)

    value = min(MAX_BOTTOM_EXPOSURE, max(floor, value))
    value = round(value * 2) / 2  # 0.5 s steps are what people type into Chitubox

    bits: list[str] = []
    if n_success:
        bits.append(f"{n_success} success{'es' if n_success != 1 else ''}")
    if n_adhesion:
        bits.append(f"{n_adhesion} adhesion fail{'s' if n_adhesion != 1 else ''} → +{int((ADHESION_BUMP - 1) * 100)}%")
    if n_over:
        bits.append(f"{n_over} over-cure → −{int((1 - OVER_CURE_CUT) * 100)}%")
    calibrated = bool(n_success or n_adhesion or n_over)
    if not calibrated:
        detail = f"no adhesion data yet — floor {floor:g}s"
    else:
        detail = f"calibrated from {scope} " + ", ".join(bits)
        if value <= floor + 1e-6:
            detail += f" (at floor {floor:g}s)"
    return BottomCalibration(value=value, detail=detail, n_runs=len(runs), calibrated=calibrated)


def _from_other_resins(
    runs: list[HistoryRecord],
    resin_id: str,
    current: float,
    floor: float,
    baseline_ref: Callable[[str, float], tuple[float, float] | None] | None,
) -> BottomCalibration:
    target = baseline_ref(resin_id, 0.05) if baseline_ref else None
    num = den = 0.0
    n = 0
    for i, rec in enumerate(r for r in runs if r.outcome == PrintOutcome.SUCCESS):
        ratio = 1.0
        if baseline_ref is not None:
            ref = baseline_ref(rec.resin_id, rec.layer_height)
            if ref is None or target is None:
                continue
            ratio = target[1] / ref[1]
        w = (RECENCY_DECAY**i) * OTHER_RESIN_WEIGHT
        num += w * float(rec.settings.bottom_exposure_time) * ratio
        den += w
        n += 1
    if not n:
        return BottomCalibration(
            value=max(floor, current),
            detail=f"no runs with this resin yet — floor {floor:g}s"
            if current <= floor
            else "no runs with this resin yet — official baseline",
            n_runs=0,
            calibrated=False,
        )
    value = min(MAX_BOTTOM_EXPOSURE, max(floor, num / den))
    value = round(value * 2) / 2
    detail = f"no runs with this resin yet — starting from {n} successes with other resins on this printer"
    if value <= floor + 1e-6:
        detail += f" (at floor {floor:g}s)"
    return BottomCalibration(value=value, detail=detail, n_runs=0, calibrated=True)
