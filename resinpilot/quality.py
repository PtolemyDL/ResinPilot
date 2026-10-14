"""Post-print quality checklist (replaces free-text notes on reports)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QualityCheck:
    key: str
    question: str
    short: str
    good_answer: bool


QUALITY_CHECKS: tuple[QualityCheck, ...] = (
    QualityCheck("dimensions_ok", "Part geometry / dimensions accurate?", "dimensions", True),
    QualityCheck("resin_residue", "Uncured resin residue?", "residue", False),
    QualityCheck("surface_ok", "Surface condition good?", "surface", True),
    QualityCheck("warped", "Warped?", "warped", False),
    QualityCheck("missing_features", "Missing features?", "missing features", False),
)

_BY_KEY = {c.key: c for c in QUALITY_CHECKS}


def clean_quality(raw: Any) -> dict[str, bool]:
    """Keep only known keys with boolean answers (unanswered checks are omitted)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    return {k: bool(v) for k, v in raw.items() if k in _BY_KEY and isinstance(v, bool)}


def quality_problems(quality: dict[str, bool]) -> list[str]:
    """Short labels for checks answered the bad way."""
    return [c.short for c in QUALITY_CHECKS if c.key in quality and quality[c.key] != c.good_answer]


def quality_summary(quality: dict[str, bool]) -> str:
    if not quality:
        return ""
    bad = quality_problems(quality)
    if not bad:
        return f"all {len(quality)} checks OK"
    return "issues: " + ", ".join(bad)


def quality_checks_payload() -> list[dict[str, Any]]:
    return [
        {"key": c.key, "question": c.question, "short": c.short, "good_answer": c.good_answer}
        for c in QUALITY_CHECKS
    ]
