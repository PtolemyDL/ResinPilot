"""Post-processing SOP per resin (data/post_process.yaml): wash, dry and UV cure steps with sources."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .catalog import _load_yaml
from .paths import BUILTIN_DATA_DIR

STEP_FIELDS = ("key", "label", "rail", "time", "detail")


def load_sops(data_dir: Path | None = None) -> list[dict[str, Any]]:
    raw = _load_yaml((data_dir or BUILTIN_DATA_DIR) / "post_process.yaml") or {}
    sops = []
    for item in raw.get("sops") or []:
        steps = []
        for step in item.get("steps") or []:
            missing = [f for f in STEP_FIELDS if not str(step.get(f) or "").strip()]
            if missing:
                raise ValueError(f"SOP {item.get('id')}: step is missing {', '.join(missing)}")
            steps.append({**{f: str(step[f]).strip() for f in STEP_FIELDS}, "optional": bool(step.get("optional"))})
        sops.append(
            {
                "id": str(item["id"]),
                "name": str(item["name"]),
                "resins": [str(r) for r in item.get("resins") or []],
                "steps": steps,
                "cautions": [str(c).strip() for c in item.get("cautions") or [] if str(c).strip()],
                "sources": [
                    {"label": str(s.get("label") or s.get("url")), "url": str(s["url"])}
                    for s in item.get("sources") or []
                    if s.get("url")
                ],
            }
        )
    return sops


def sops_payload(data_dir: Path | None = None) -> dict[str, Any]:
    """{"sops": {id: sop}, "by_resin": {resin_id: sop_id}} for the web UI."""
    sops = load_sops(data_dir)
    by_resin = {rid: sop["id"] for sop in sops for rid in sop["resins"]}
    return {"sops": {sop["id"]: sop for sop in sops}, "by_resin": by_resin}
