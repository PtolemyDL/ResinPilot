"""Turn qualitative print outcomes into quantitative layer-exposure corrections.

Every report carries a failure type and/or quality-check answers. Each maps to a
direction and step for the *layer* exposure (bottom exposure is handled in
bottom_calibration from the adhesion / over-cure types):

  under-cure signs  → raise:  layers split +10 %, supports broke +8 %, missing features +6 %
  over-cure signs   → lower:  channels/holes closed −8 %, dimensions off −4 %,
                              uncured residue in a channel part −4 % (trapped resin cures in the channel)

At the same layer height a signal is anchored on the exposure that print actually used
("this run needed +10 %"); from other layer heights it nudges the recommended value by
the same percentage. It is dropped once a newer clean print at that layer height already
ran past the fix.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .bottom_calibration import (
    ISSUE_FEATURES_CLOSED,
    ISSUE_LAYER_SEPARATION,
    ISSUE_SUPPORTS,
    infer_issue,
)
from .history import HistoryRecord
from .models import PrintOutcome
from .shape_scan import TAG_CHANNELS

FAILURE_LAYER_STEPS: dict[str, tuple[float, str]] = {
    ISSUE_LAYER_SEPARATION: (+0.10, "layers split"),
    ISSUE_SUPPORTS: (+0.08, "supports broke"),
    ISSUE_FEATURES_CLOSED: (-0.08, "channels/holes closed"),
}
# quality key → (answer that signals a problem, step, label, only for parts with these shape tags)
QUALITY_LAYER_STEPS: dict[str, tuple[bool, float, str, str]] = {
    "missing_features": (True, +0.06, "missing features", ""),
    "dimensions_ok": (False, -0.04, "dimensions off", ""),
    "resin_residue": (True, -0.04, "resin trapped in channels", TAG_CHANNELS),
}
LOOKBACK_RUNS = 20
RECENCY_DECAY = 0.85
# Resin-wide recommendations (no part chosen) mix many parts, so one part's signal moves them half as much.
RESIN_WIDE_WEIGHT = 0.5

ScaleFn = Callable[[float, float, float], float]
TagsFn = Callable[[HistoryRecord], tuple[str, ...]]


@dataclass
class Signal:
    step: float
    label: str
    run: HistoryRecord


@dataclass
class LayerCalibration:
    value: float
    detail: str = ""
    raised: list[str] = field(default_factory=list)
    lowered: list[str] = field(default_factory=list)


def run_signals(rec: HistoryRecord, tags: Iterable[str] = ()) -> list[Signal]:
    """Layer-exposure signals carried by one report (failure type + quality answers)."""
    out: list[Signal] = []
    if rec.outcome != PrintOutcome.SUCCESS:
        issue = infer_issue(rec)
        if issue in FAILURE_LAYER_STEPS:
            step, label = FAILURE_LAYER_STEPS[issue]
            out.append(Signal(step, label, rec))
    tags = set(tags)
    for key, (bad, step, label, needs_tag) in QUALITY_LAYER_STEPS.items():
        if needs_tag and needs_tag not in tags:
            continue
        if key in (rec.quality or {}) and rec.quality[key] == bad:
            out.append(Signal(step, label, rec))
    return out


def is_clean_success(rec: HistoryRecord, tags: Iterable[str] = ()) -> bool:
    """A success whose exposure is worth copying: no exposure-related quality problem."""
    return rec.outcome == PrintOutcome.SUCCESS and not run_signals(rec, tags)


def calibrate_layer_exposure(
    runs: list[HistoryRecord],
    current: float,
    layer_height: float,
    floor: float,
    scale: ScaleFn,
    tags_of: TagsFn | None = None,
    weight: float = 1.0,
    scope: str = "",
) -> LayerCalibration:
    """Adjust `current` for unresolved under/over-cure signals in `runs` (newest first)."""
    tags_of = tags_of or (lambda _r: ())
    runs = runs[:LOOKBACK_RUNS]
    # Newer clean successes as (layer height, exposure) — a fix only counts at the same layer height.
    clean_after: list[tuple[float, float]] = []
    target_up: float | None = None
    target_down: float | None = None
    raised: list[str] = []
    lowered: list[str] = []
    for i, rec in enumerate(runs):
        exp = rec.settings.normal_exposure_time
        signals = run_signals(rec, tags_of(rec))
        if not signals:
            if rec.outcome == PrintOutcome.SUCCESS:
                clean_after.append((rec.layer_height, exp))
            continue
        same = [e for h, e in clean_after if abs(h - rec.layer_height) < 1e-3]
        # Same layer height: "that run needed ±x %". Other heights: nudge today's value by ±x %
        # (converting exposures across layer heights is too rough to anchor on).
        anchor = exp if abs(rec.layer_height - layer_height) < 1e-3 else current
        fade = RECENCY_DECAY**i
        for sig in signals:
            step = sig.step * weight * fade
            fixed = anchor * (1 + step)
            when = (rec.started_at or rec.created_at or "")[:10]
            if step > 0:
                if any(e >= exp * (1 + step / 2) for e in same):
                    continue
                target_up = fixed if target_up is None else max(target_up, fixed)
                raised.append(f"{sig.label} ({when})")
            else:
                if any(e <= exp * (1 + step / 2) for e in same):
                    continue
                target_down = fixed if target_down is None else min(target_down, fixed)
                lowered.append(f"{sig.label} ({when})")
    value = current
    if target_up is not None:
        value = max(value, target_up)
    elif target_down is not None:
        value = min(value, target_down)
    value = round(max(floor, value), 2)
    bits = []
    if raised:
        bits.append(f"raised for {', '.join(raised[:3])}{' …' if len(raised) > 3 else ''}")
    if lowered:
        verb = "lowered" if target_up is None else "not lowered (an under-cure sign wins)"
        bits.append(f"{verb} for {', '.join(lowered[:3])}{' …' if len(lowered) > 3 else ''}")
    detail = ""
    if bits and current and abs(value / current - 1) >= 0.005:
        pct = (value / current - 1) * 100 if current else 0.0
        detail = f"Layer exposure {pct:+.0f}% from {scope or 'recent'} reports: " + "; ".join(bits)
    elif bits:
        detail = f"Already covered by this value — {scope or 'recent'} reports: " + "; ".join(bits)
    return LayerCalibration(value=value, detail=detail, raised=raised, lowered=lowered)
