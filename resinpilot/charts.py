"""History table rows, KPIs and plain-language insights for the web UI (charts render in the browser)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import numpy as np

from .bottom_calibration import (
    ADHESION_ISSUES,
    FAILURE_ISSUES,
    ISSUE_DETACHED,
    ISSUE_FEATURES_CLOSED,
    ISSUE_LAYER_SEPARATION,
    ISSUE_NO_ADHESION,
    ISSUE_OTHER,
    ISSUE_OVER_CURE,
    ISSUE_SUPPORTS,
    infer_issue,
)
from .history import OpenJob, PendingJob
from .models import PrintOutcome, PrintSettings
from .outcome_signals import FAILURE_LAYER_STEPS
from .parts import OTHER_GROUP, SIZE_LABELS, PartFamilies, part_info
from .quality import quality_summary
from .shape_scan import shape_text
from .sdcp import settings_from_slice_info

if TYPE_CHECKING:
    from .history import HistoryRecord


def _short_resin_name(name: str) -> str:
    if len(name) > 28:
        return name[:26] + "…"
    return name


def printer_kpis(records: list[HistoryRecord]) -> dict[str, Any]:
    total = len(records)
    ok = sum(1 for r in records if r.outcome == PrintOutcome.SUCCESS)
    fail = sum(1 for r in records if r.outcome == PrintOutcome.FAIL)
    temps = [r.vat_temperature_c for r in records if temp_measured(r)]
    uvs = [r.uv_led_temp_c for r in records if r.uv_led_temp_c is not None]
    durs = [r.duration_s / 60.0 for r in records if r.duration_s]
    resins = {r.resin_id for r in records}
    return {
        "total": total,
        "success": ok,
        "fail": fail,
        "success_rate": (100.0 * ok / total) if total else 0.0,
        "avg_vat_c": float(np.mean(temps)) if temps else None,
        "avg_uv_c": float(np.mean(uvs)) if uvs else None,
        "avg_duration_min": float(np.mean(durs)) if durs else None,
        "resin_count": len(resins),
    }


def _slice_counts(settings: PrintSettings | None) -> dict[str, str]:
    """Bottom layers, transition layers, and rest after retract for the history log."""
    if settings is None:
        return {"bottom_layers": "—", "transition_layers": "—", "rest_after": "—"}
    return {
        "bottom_layers": str(int(settings.bottom_layer_count)),
        "transition_layers": str(int(settings.transition_layer_count)),
        "rest_after": f"{float(settings.rest_time_after_retract):.1f}s",
    }


def format_run_rows(
    records: list[HistoryRecord],
    resin_names: dict[str, str],
    printer_names: dict[str, str] | None = None,
    families: PartFamilies | None = None,
) -> list[dict[str, str]]:
    """Flat rows for the Charts detail table — surfaces telemetry that plots hide."""
    printer_names = printer_names or {}
    families = families or PartFamilies(records)
    rows: list[dict[str, str]] = []
    for r in records:
        part = part_info(r, families)
        temp = f"{r.vat_temperature_c:.0f}°"
        if r.vat_temp_min_c is not None and r.vat_temp_max_c is not None:
            temp += f" ({r.vat_temp_min_c:.0f}–{r.vat_temp_max_c:.0f})"
        uv = "—"
        if r.uv_led_temp_c is not None:
            uv = f"{r.uv_led_temp_c:.0f}°"
            if r.uv_temp_min_c is not None and r.uv_temp_max_c is not None:
                uv += f" ({r.uv_temp_min_c:.0f}–{r.uv_temp_max_c:.0f})"
        dur = f"{r.duration_s/60:.0f} min" if r.duration_s else "—"
        layers = "—"
        if r.end_layer is not None and r.total_layers:
            layers = f"{r.end_layer}/{r.total_layers}"
        elif r.total_layers:
            layers = str(r.total_layers)
        fname = r.filename or "—"
        if len(fname) > 48:
            fname = fname[:45] + "…"
        note = ""
        if r.notes and " — " in r.notes:
            note = r.notes.split(" — ", 1)[-1][:60]
        app = (r.part_application or "").strip()
        tel = r.telemetry or {}
        if tel.get("settings_source") == "manual":
            exp_note = _manual_edit_note(tel)
        elif tel.get("settings_source") == "printer":
            exp_note = "; ".join(tel.get("filename_check") or [])
        else:
            exp_note = "exposures are app estimates, not read from the printer"
        s = r.settings
        rows.append(
            {
                "id": f"#{r.id}",
                "when": (r.created_at or "")[:16].replace("T", " "),
                "outcome": r.outcome.value.upper(),
                "app": app,
                "resin": _short_resin_name(resin_names.get(r.resin_id, r.resin_id)),
                "resin_id": r.resin_id,
                "layer": f"{r.layer_height:.3f}",
                "layer_exp": f"{r.settings.normal_exposure_time:.2f}s",
                "bottom_exp": f"{r.settings.bottom_exposure_time:.1f}s",
                **_slice_counts(s),
                "temp": temp,
                "uv": uv,
                "layers": layers,
                "duration": dur,
                "file": fname,
                "geom": r.geometry_class.label,
                "source": r.source or "manual",
                "issue": FAILURE_ISSUES.get(r.failure_issue, "") if r.failure_issue else "",
                "quality": quality_summary(r.quality),
                "note": note,
                "exp_note": exp_note,
                "preview": r.preview_path or "",
                "part_application": app,
                "part_family": part["family"],
                "part_family_id": part["family_id"],
                "part_group": part["group"],
                "shape_tags": part["shape_tags"],
                "shape": shape_text(families.shape(part["family_id"])),
                "size_class": part["size_class"],
                "size": _size_text(part),
                "report_kind": "run",
                "report_id": r.id,
                "printer_id": r.printer_id,
                "printer": printer_names.get(r.printer_id, r.printer_id),
                "failure_issue": r.failure_issue or "",
                "quality_answers": dict(r.quality),
                "settings": {
                    "layer_height": s.layer_height,
                    "normal_exposure_time": s.normal_exposure_time,
                    "bottom_exposure_time": s.bottom_exposure_time,
                    "bottom_layer_count": s.bottom_layer_count,
                    "transition_layer_count": s.transition_layer_count,
                },
            }
        )
    return rows


def filter_history_rows(
    rows: list[dict[str, Any]],
    search: str = "",
    printer: str = "",
    status: str = "",
    resin: str = "",
) -> list[dict[str, Any]]:
    """History log filter: status is SUCCESS / FAIL / UNREPORTED / PRINTING (FAIL covers partial)."""
    words = search.lower().split()
    out = []
    for r in rows:
        if printer and r.get("printer") != printer:
            continue
        if resin and r.get("resin") != resin:
            continue
        if status:
            kind = r.get("outcome") if r.get("outcome") in {"SUCCESS", "UNREPORTED", "PRINTING"} else "FAIL"
            if kind != status:
                continue
        if words:
            hay = " ".join(
                str(r.get(k) or "")
                for k in ("file", "app", "note", "issue", "quality", "id", "printer", "resin", "part_family", "part_group")
            ).lower()
            if not all(w in hay for w in words):
                continue
        out.append(r)
    return out


_SETTING_LABELS = {
    "layer_height": ("layer height", "mm"),
    "normal_exposure_time": ("layer exposure", "s"),
    "bottom_exposure_time": ("bottom exposure", "s"),
    "bottom_layer_count": ("bottom layers", ""),
    "transition_layer_count": ("transition layers", ""),
}


def _manual_edit_note(tel: dict[str, Any]) -> str:
    """'edited by hand: bottom exposure 40s (printer reported 34s)'."""
    printer = settings_from_slice_info(tel.get("slice_info") or {})
    bits = []
    for key, value in (tel.get("manual_edits") or {}).items():
        label, unit = _SETTING_LABELS.get(key, (key, ""))
        bit = f"{label} {value:g}{unit}"
        if printer is not None:
            bit += f" (printer reported {getattr(printer, key):g}{unit})"
        bits.append(bit)
    return "edited by hand: " + ", ".join(bits) if bits else "edited by hand"


def _size_text(part: dict[str, Any]) -> str:
    """'Bulky · 50 ml, 940 mm²/layer' — empty when the printer reported no volume."""
    if not part.get("size_class"):
        return ""
    return (
        f"{SIZE_LABELS[part['size_class']]} · {part['volume_ml']:.0f} ml, "
        f"{part['avg_area_mm2']:.0f} mm²/layer"
    )


def format_pending_rows(
    pending: list[Any],
    resin_names: dict[str, str],
    printer_names: dict[str, str] | None = None,
    families: PartFamilies | None = None,
) -> list[dict[str, Any]]:
    """Rows for finished prints still waiting for a success/fail report.

    Accepts `PendingJob`s (parked after the next print started) and awaiting `OpenJob`s.
    """
    printer_names = printer_names or {}
    families = families or PartFamilies([])
    rows: list[dict[str, Any]] = []
    for item in pending:
        job = getattr(item, "job", item)
        fam_id, fam_name, _key = families.resolve(job.filename or "")
        pending_id = getattr(item, "id", None)
        printing = pending_id is None and not job.awaiting_outcome
        when = getattr(item, "finished_at", "") or job.started_at or ""
        settings = job.settings
        layers = "—"
        if job.current_layer is not None and job.total_layers:
            layers = f"{job.current_layer}/{job.total_layers}"
        fname = job.filename or "—"
        if len(fname) > 48:
            fname = fname[:45] + "…"
        rows.append(
            {
                "id": "—",
                "when": when[:16].replace("T", " "),
                "outcome": "PRINTING" if printing else "UNREPORTED",
                "app": (job.part_application or "").strip(),
                "resin": _short_resin_name(resin_names.get(job.resin_id, job.resin_id)),
                "resin_id": job.resin_id,
                "layer": f"{job.layer_height:.3f}",
                "layer_exp": f"{settings.normal_exposure_time:.2f}s" if settings else "—",
                "bottom_exp": f"{settings.bottom_exposure_time:.1f}s" if settings else "—",
                **_slice_counts(settings),
                "temp": f"{job.vat_temperature_c:.0f}°",
                "uv": f"{job.uv_led_temp_c:.0f}°" if job.uv_led_temp_c is not None else "—",
                "layers": layers,
                "duration": f"{job.duration_s/60:.0f} min" if job.duration_s else "—",
                "file": fname,
                "issue": "",
                "quality": "",
                "note": "",
                "preview": job.preview_path or "",
                "part_application": (job.part_application or "").strip(),
                "part_family": fam_name,
                "part_family_id": fam_id,
                "part_group": families.group(fam_id),
                "shape_tags": list(families.shape_tags(fam_id)),
                "shape": shape_text(families.shape(fam_id)),
                "size_class": "",
                "size": "",
                "report_kind": "pending" if pending_id is not None else "open",
                "report_id": pending_id,
                "printer_id": job.printer_id,
                "printer": printer_names.get(job.printer_id, job.printer_id),
                "failure_issue": "",
                "quality_answers": {},
            }
        )
    return rows


def build_history_log(
    history: Any,
    printer_names: dict[str, str],
    resin_names: dict[str, str],
    printer_id: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Every print attempt, newest first, numbered #1 … #N by when the print started.

    Prints appear as soon as the printer starts them (status Printing), stay listed as
    Unreported once finished, and keep the same number after their outcome is reported.
    """
    all_records = history.list_recent(printer_id=printer_id, outcome=None, limit=100000)
    open_jobs = [
        job
        for job in history.list_open_jobs()
        if printer_id is None or job.printer_id == printer_id
    ]
    pending: list[Any] = [j for j in open_jobs if j.awaiting_outcome]
    pending.extend(history.list_unreported(printer_id))
    printing = [j for j in open_jobs if not j.awaiting_outcome]

    families = PartFamilies(all_records)
    entries: list[tuple[str, int, dict[str, Any]]] = []
    for rec, row in zip(all_records, format_run_rows(all_records, resin_names, printer_names, families)):
        entries.append((rec.started_at or rec.created_at or "", rec.id, row))
    for items, fallback in ((pending, ""), (printing, "~")):
        for item, row in zip(items, format_pending_rows(items, resin_names, printer_names, families)):
            job = getattr(item, "job", item)
            started = job.started_at or getattr(item, "finished_at", "") or fallback
            entries.append((started, 10**9 + len(entries), row))
    entries.sort(key=lambda e: (e[0], e[1]))
    for n, (_key, _tie, row) in enumerate(entries, start=1):
        row["id"] = f"#{n}"

    total = len(all_records)
    ok = sum(1 for r in all_records if r.outcome == PrintOutcome.SUCCESS)
    return {
        "stats": {
            "total": total,
            "success": ok,
            "fail": sum(1 for r in all_records if r.outcome == PrintOutcome.FAIL),
            "success_rate": (ok / total) if total else 0.0,
            "unreported": len(pending),
            "printing": len(printing),
        },
        "rows": [row for _key, _tie, row in reversed(entries)][:limit],
    }


def build_insights(
    records: list[HistoryRecord],
    resin_names: dict[str, str] | None = None,
    printer_name: str = "",
) -> list[str]:
    """Plain-language summary bullets for the Charts Insights card."""
    resin_names = resin_names or {}
    if not records:
        return [
            "No logged runs for this printer yet.",
            "After SDCP tracks a print and you report success/fail, insights will appear here.",
        ]

    kpis = printer_kpis(records)
    lines: list[str] = []
    who = printer_name or "This printer"
    lines.append(
        f"{who}: {kpis['total']} runs · {kpis['success_rate']:.0f}% success "
        f"({kpis['success']} ok / {kpis['fail']} fail)."
    )

    # Best / worst resins by success rate (min 2 runs)
    by_resin: dict[str, list[HistoryRecord]] = defaultdict(list)
    for r in records:
        by_resin[r.resin_id].append(r)
    ranked: list[tuple[str, float, int]] = []
    for rid, rows in by_resin.items():
        if len(rows) < 1:
            continue
        rate = 100.0 * sum(1 for x in rows if x.outcome == PrintOutcome.SUCCESS) / len(rows)
        ranked.append((rid, rate, len(rows)))
    ranked.sort(key=lambda t: (-t[1], -t[2]))
    if ranked:
        best_id, best_rate, best_n = ranked[0]
        lines.append(
            f"Strongest resin so far: {_short_resin_name(resin_names.get(best_id, best_id))} "
            f"at {best_rate:.0f}% ({best_n} runs)."
        )
        if len(ranked) > 1 and ranked[-1][1] < ranked[0][1]:
            worst_id, worst_rate, worst_n = ranked[-1]
            lines.append(
                f"Watch list: {_short_resin_name(resin_names.get(worst_id, worst_id))} "
                f"at {worst_rate:.0f}% ({worst_n} runs)."
            )

    # Layer height performance
    by_layer: dict[float, list[HistoryRecord]] = defaultdict(list)
    for r in records:
        by_layer[round(r.layer_height, 3)].append(r)
    layer_bits = []
    for h in sorted(by_layer.keys()):
        rows = by_layer[h]
        rate = 100.0 * sum(1 for x in rows if x.outcome == PrintOutcome.SUCCESS) / len(rows)
        layer_bits.append(f"{h:.3f} mm -> {rate:.0f}% ({len(rows)})")
    if layer_bits:
        lines.append("By layer height: " + " · ".join(layer_bits) + ".")

    # Temperature
    if kpis["avg_vat_c"] is not None:
        measured = [r for r in records if temp_measured(r)]
        temp_line = f"Average measured temp {kpis['avg_vat_c']:.0f}°C ({len(measured)} runs with a reading)"
        if kpis["avg_uv_c"] is not None:
            temp_line += f" · UV LED avg {kpis['avg_uv_c']:.0f}°C"
        lines.append(temp_line + ".")
        cold = [r for r in measured if r.vat_temperature_c < 24]
        warm = [r for r in measured if r.vat_temperature_c >= 30]
        if cold:
            c_rate = 100.0 * sum(1 for r in cold if r.outcome == PrintOutcome.SUCCESS) / len(cold)
            lines.append(f"Cooler runs (<24°C): {c_rate:.0f}% success across {len(cold)} prints.")
        if warm:
            w_rate = 100.0 * sum(1 for r in warm if r.outcome == PrintOutcome.SUCCESS) / len(warm)
            lines.append(f"Warmer runs (≥30°C): {w_rate:.0f}% success across {len(warm)} prints.")

    # Exposure suggestion from successes
    successes = [r for r in records if r.outcome == PrintOutcome.SUCCESS]
    if successes:
        # Prefer most common layer among successes
        layer_counts: dict[float, int] = defaultdict(int)
        for r in successes:
            layer_counts[round(r.layer_height, 3)] += 1
        top_h = max(layer_counts, key=layer_counts.get)  # type: ignore[arg-type]
        at_h = [r for r in successes if abs(r.layer_height - top_h) < 0.001]
        mean_exp = float(np.mean([r.settings.normal_exposure_time for r in at_h]))
        mean_bot = float(np.mean([r.settings.bottom_exposure_time for r in at_h]))
        temps_h = [r.vat_temperature_c for r in at_h if temp_measured(r)]
        near = f" near {float(np.mean(temps_h)):.0f}°C" if temps_h else ""
        lines.append(
            f"Winning recipe cluster @ {top_h:.3f} mm: "
            f"layer exposure ~{mean_exp:.2f}s / bottom exposure {mean_bot:.1f}s{near} "
            f"({len(at_h)} successes)."
        )

    fails = [r for r in records if r.outcome == PrintOutcome.FAIL]
    if fails:
        recent_fail = fails[0]
        note = ""
        if recent_fail.notes and " — " in recent_fail.notes:
            note = recent_fail.notes.split(" — ", 1)[-1][:80]
        bit = (
            f"Latest fail: {recent_fail.layer_height:.3f} mm, layer {recent_fail.settings.normal_exposure_time:g}s"
            f" / bottom {recent_fail.settings.bottom_exposure_time:g}s"
        )
        if temp_measured(recent_fail):
            bit += f" @ {recent_fail.vat_temperature_c:.0f}°C"
        if recent_fail.failure_issue in FAILURE_ISSUES:
            bit += f" — {FAILURE_ISSUES[recent_fail.failure_issue]}"
        elif note:
            bit += f" — {note}"
        lines.append(bit + ".")

    if kpis["avg_duration_min"] is not None:
        lines.append(f"Average SDCP print duration ~{kpis['avg_duration_min']:.0f} minutes.")

    return lines


def part_family_summary(
    records: list[HistoryRecord],
    families: PartFamilies,
    printer_names: dict[str, str],
) -> list[dict[str, Any]]:
    """One row per part family: runs, success rate, size, where and how it's printed. Most-printed first."""
    groups: dict[str, list[tuple[HistoryRecord, dict[str, Any]]]] = defaultdict(list)
    for r in records:
        info = part_info(r, families)
        groups[info["family_id"]].append((r, info))
    out = []
    for fid, items in groups.items():
        recs = [r for r, _ in items]
        infos = [i for _, i in items]
        ok = [r for r in recs if r.outcome == PrintOutcome.SUCCESS]
        fails = [r for r in recs if r.outcome == PrintOutcome.FAIL]
        sized = [i for i in infos if i["size_class"]]
        sizes = defaultdict(int)
        for i in sized:
            sizes[i["size_class"]] += 1
        basis = ok or recs
        latest = max(recs, key=lambda r: r.created_at or "")
        out.append(
            {
                "family_id": fid,
                "family": infos[0]["family"],
                "auto_keys": sorted({i["family_key"] for i in infos}),
                "group": families.group(fid),
                "shape_tags": list(families.shape_tags(fid)),
                "shape": shape_text(families.shape(fid)),
                "shape_file": (families.shape(fid) or {}).get("file", ""),
                "runs": len(recs),
                "success": len(ok),
                "fail": len(fails),
                "success_rate": 100.0 * len(ok) / len(recs),
                "size_class": max(sizes, key=sizes.get) if sizes else "",
                "volume_ml": float(np.median([i["volume_ml"] for i in sized])) if sized else None,
                "avg_area_mm2": float(np.median([i["avg_area_mm2"] for i in sized])) if sized else None,
                "printers": sorted({printer_names.get(r.printer_id, r.printer_id) for r in recs}),
                "typical_exp": float(np.median([r.settings.normal_exposure_time for r in basis])),
                "typical_bottom": float(np.median([r.settings.bottom_exposure_time for r in basis])),
                "last_print": latest.created_at,
                "latest_file": latest.filename,
                "fail_issues": sorted({FAILURE_ISSUES.get(r.failure_issue, "") for r in fails} - {""}),
            }
        )
    # Groups by total runs (Other last), parts by runs within each group.
    group_runs: dict[str, int] = defaultdict(int)
    for d in out:
        group_runs[d["group"]] += d["runs"]
    out.sort(
        key=lambda d: (
            d["group"] == OTHER_GROUP, -group_runs[d["group"]], d["group"].lower(), -d["runs"], d["family"].lower()
        )
    )
    return out


BOTTOM_BIN_S = 5.0
UNSPECIFIED_ISSUE = "unspecified"


def temp_measured(record: HistoryRecord) -> bool:
    """False for runs whose vat temperature is a placeholder (e.g. imported from printer history)."""
    tel = record.telemetry or {}
    return record.vat_temperature_c is not None and not tel.get("vat_temp_estimated")


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _week_start(ts: datetime) -> date:
    local = ts.astimezone()
    return (local - timedelta(days=local.weekday())).date()


def _print_hours(record: HistoryRecord) -> float:
    return (record.duration_s or 0.0) / 3600.0


def _outcome_key(record: HistoryRecord) -> str:
    return "fail" if record.outcome == PrintOutcome.FAIL else "success"


def settings_timeline(records: list[HistoryRecord]) -> list[dict[str, Any]]:
    """One point per print, oldest first, for the layer/bottom exposure timeline."""
    points = []
    for r in records:
        ts = _parse_ts(r.created_at)
        if ts is None:
            continue
        points.append(
            {
                "t": int(ts.timestamp() * 1000),
                "exp": r.settings.normal_exposure_time,
                "bottom": r.settings.bottom_exposure_time,
                "layer": r.layer_height,
                "outcome": _outcome_key(r),
                "issue": FAILURE_ISSUES.get(r.failure_issue, "") if r.outcome == PrintOutcome.FAIL else "",
                "file": r.filename,
            }
        )
    points.sort(key=lambda p: p["t"])
    return points


def recipe_clusters(records: list[HistoryRecord]) -> list[dict[str, Any]]:
    """Runs grouped by (layer height, layer exposure) with success/fail counts."""
    groups: dict[tuple[float, float], dict[str, Any]] = {}
    for r in records:
        key = (round(r.layer_height, 3), round(r.settings.normal_exposure_time, 2))
        g = groups.setdefault(key, {"layer": key[0], "exp": key[1], "n": 0, "ok": 0, "fail": 0})
        g["n"] += 1
        g["fail" if r.outcome == PrintOutcome.FAIL else "ok"] += 1
    return sorted(groups.values(), key=lambda g: (g["layer"], g["exp"]))


def bottom_distribution(records: list[HistoryRecord], bin_s: float = BOTTOM_BIN_S) -> list[dict[str, Any]]:
    """Bottom exposure histogram in `bin_s` buckets, gaps filled, split by outcome."""
    values = [(r.settings.bottom_exposure_time, _outcome_key(r)) for r in records]
    if not values:
        return []
    lo = int(min(v for v, _ in values) // bin_s)
    hi = int(max(v for v, _ in values) // bin_s)
    bins = {
        i: {"lo": i * bin_s, "hi": (i + 1) * bin_s, "label": f"{i * bin_s:g}–{(i + 1) * bin_s - 1:g}s", "ok": 0, "fail": 0}
        for i in range(lo, hi + 1)
    }
    for v, outcome in values:
        bins[int(v // bin_s)]["fail" if outcome == "fail" else "ok"] += 1
    return [bins[i] for i in range(lo, hi + 1)]


def weekly_usage(records: list[HistoryRecord], weeks: int | None = None) -> list[dict[str, Any]]:
    """Prints, fails and print hours per local calendar week (Monday start), gaps filled."""
    by_week: dict[date, dict[str, Any]] = {}
    for r in records:
        ts = _parse_ts(r.started_at) or _parse_ts(r.created_at)
        if ts is None:
            continue
        wk = _week_start(ts)
        b = by_week.setdefault(wk, {"prints": 0, "fails": 0, "hours": 0.0})
        b["prints"] += 1
        b["fails"] += 1 if r.outcome == PrintOutcome.FAIL else 0
        b["hours"] += _print_hours(r)
    if not by_week:
        return []
    last = max(by_week)
    first = min(by_week) if weeks is None else last - timedelta(weeks=weeks - 1)
    out = []
    wk = first
    while wk <= last:
        b = by_week.get(wk, {"prints": 0, "fails": 0, "hours": 0.0})
        out.append({"week": wk.isoformat(), **b, "hours": round(b["hours"], 1)})
        wk += timedelta(weeks=1)
    return out


def failure_breakdown(records: list[HistoryRecord]) -> list[dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for r in records:
        if r.outcome == PrintOutcome.FAIL:
            counts[r.failure_issue or UNSPECIFIED_ISSUE] += 1
    labels = {**FAILURE_ISSUES, UNSPECIFIED_ISSUE: "Not specified"}
    return sorted(
        ({"issue": k, "label": labels.get(k, k), "n": n} for k, n in counts.items()),
        key=lambda d: -d["n"],
    )


# Playbook: default cause + actions per failure mode (data patterns refine the "why").
_ISSUE_PLAYBOOK: dict[str, dict[str, Any]] = {
    ISSUE_NO_ADHESION: {
        "cause": "The first layers did not bond hard enough to the build plate, so the print stayed on the film.",
        "actions": [
            "Raise bottom exposure (~15% is typical) — ResinPilot bumps this automatically after adhesion reports.",
            "Warm resin/vat toward 25–30°C before printing; cold resin cures slower.",
            "Check plate leveling and release-film wear; a cloudy or stretched film pulls harder.",
        ],
    },
    ISSUE_DETACHED: {
        "cause": "The print started on the plate, then peeled off mid-print — adhesion or film tension gave way.",
        "actions": [
            "Raise bottom exposure and keep bottom/transition layer counts from dropping.",
            "Inspect release film cycles; replace film if it's past its limit or looks cloudy.",
            "Slow lift / longer rest after retract can help stubborn resins stick through the whole print.",
        ],
    },
    ISSUE_OVER_CURE: {
        "cause": "Bottom layers cured too hard (elephant foot / welded to the plate).",
        "actions": [
            "Lower bottom exposure (~10%) — ResinPilot cuts this after over-cure reports.",
            "Reduce bottom layer count slightly if the base is still overbuilt.",
            "Do not raise layer exposure to fix this; that would make the rest of the part worse.",
        ],
    },
    ISSUE_LAYER_SEPARATION: {
        "cause": "Normal layers were under-cured, so they delaminated as the plate lifted.",
        "actions": [
            f"Raise layer exposure about {abs(FAILURE_LAYER_STEPS[ISSUE_LAYER_SEPARATION][0]) * 100:.0f}% at this layer height.",
            "Prefer a warmer vat; cold resin needs more exposure for the same cure.",
            "Confirm the slicer layer height matches what you intended.",
        ],
    },
    ISSUE_SUPPORTS: {
        "cause": "Supports were too weak or under-cured, so the part dropped mid-print.",
        "actions": [
            f"Raise layer exposure about {abs(FAILURE_LAYER_STEPS[ISSUE_SUPPORTS][0]) * 100:.0f}% so supports cure solidly.",
            "In the slicer: thicker tips, denser supports, or a wider contact diameter.",
            "Avoid large unsupported islands; re-orient the part if needed.",
        ],
    },
    ISSUE_FEATURES_CLOSED: {
        "cause": "Layer exposure was too high for fine channels/holes, so details fused shut.",
        "actions": [
            f"Lower layer exposure about {abs(FAILURE_LAYER_STEPS[ISSUE_FEATURES_CLOSED][0]) * 100:.0f}% for this resin/height.",
            "Use a clearer or lower-pigment resin for tiny channels when possible.",
            "Drain and wash trapped resin promptly after the print.",
        ],
    },
    ISSUE_OTHER: {
        "cause": "The report did not map to a standard exposure failure — machine, slicing, or resin handling may be involved.",
        "actions": [
            "Re-read the notes on those fails and pick a more specific failure type next time.",
            "Check resin mix/age, film condition, and that the file's layer height matches the profile.",
            "Compare settings against a recent success on the same resin before changing much.",
        ],
    },
    UNSPECIFIED_ISSUE: {
        "cause": "These fails were logged without a failure type, so the cause is unclear.",
        "actions": [
            "When reporting, pick the closest failure type so ResinPilot can adjust exposures.",
            "Use the quality checklist (missing features, dimensions, residue) for subtler issues.",
        ],
    },
}


def _median(vals: list[float]) -> float | None:
    return float(np.median(vals)) if vals else None


def _pct_delta(fail_v: float | None, ok_v: float | None) -> float | None:
    if fail_v is None or ok_v is None or ok_v == 0:
        return None
    return (fail_v / ok_v - 1.0) * 100.0


def _issue_evidence(fails: list[HistoryRecord], successes: list[HistoryRecord]) -> list[str]:
    """Data-backed clues comparing this failure mode to successful runs."""
    bits: list[str] = []
    n = len(fails)
    fail_bot = _median([r.settings.bottom_exposure_time for r in fails])
    ok_bot = _median([r.settings.bottom_exposure_time for r in successes])
    fail_exp = _median([r.settings.normal_exposure_time for r in fails])
    ok_exp = _median([r.settings.normal_exposure_time for r in successes])
    d_bot = _pct_delta(fail_bot, ok_bot)
    d_exp = _pct_delta(fail_exp, ok_exp)
    if d_bot is not None and abs(d_bot) >= 8:
        bits.append(
            f"Median bottom exposure on these fails is {fail_bot:.1f}s "
            f"({d_bot:+.0f}% vs {ok_bot:.1f}s on successes)."
        )
    if d_exp is not None and abs(d_exp) >= 6:
        bits.append(
            f"Median layer exposure on these fails is {fail_exp:.2f}s "
            f"({d_exp:+.0f}% vs {ok_exp:.2f}s on successes)."
        )

    fail_temps = [r.vat_temperature_c for r in fails if temp_measured(r)]
    ok_temps = [r.vat_temperature_c for r in successes if temp_measured(r)]
    cold = [t for t in fail_temps if t is not None and t < 24]
    if fail_temps and len(cold) >= max(1, (len(fail_temps) + 1) // 2):
        bits.append(f"{len(cold)} of {len(fail_temps)} measured fails ran below 24°C vat temp.")
    elif fail_temps and ok_temps:
        f_m, o_m = float(np.mean(fail_temps)), float(np.mean(ok_temps))
        if o_m - f_m >= 3:
            bits.append(f"These fails averaged {f_m:.0f}°C vs {o_m:.0f}°C on successes.")

    early = 0
    for r in fails:
        if r.end_layer is None or not r.total_layers:
            continue
        early_cut = r.settings.bottom_layer_count + r.settings.transition_layer_count + 10
        if r.end_layer < r.total_layers and r.end_layer <= early_cut:
            early += 1
    if early and early >= max(1, n // 2):
        bits.append(f"{early} of {n} stopped in the bottom/transition zone (early lift-off).")

    layers = sorted({round(r.layer_height, 3) for r in fails})
    if len(layers) == 1:
        bits.append(f"All {n} happened at {layers[0]:.3f} mm layer height.")
    elif layers:
        bits.append("Layer heights involved: " + ", ".join(f"{h:.3f} mm" for h in layers) + ".")

    return bits


def _diagnostic_issue(rec: HistoryRecord) -> str:
    """Bucket a fail for diagnostics: prefer explicit codes, else infer, else unspecified."""
    explicit = rec.failure_issue or ""
    if explicit in FAILURE_ISSUES and explicit != ISSUE_OTHER:
        return explicit
    inferred = infer_issue(rec)
    if inferred and inferred != ISSUE_OTHER:
        return inferred
    if explicit == ISSUE_OTHER or (rec.notes or "").strip():
        return ISSUE_OTHER
    return UNSPECIFIED_ISSUE


def failure_diagnostics(records: list[HistoryRecord]) -> list[dict[str, Any]]:
    """Per failure mode: why it likely happened and what to change next.

    Uses reported/inferred issue codes, compares settings and temps against successes,
    and attaches the playbook actions ResinPilot already uses for learning.
    """
    fails = [r for r in records if r.outcome == PrintOutcome.FAIL]
    if not fails:
        return []
    successes = [r for r in records if r.outcome == PrintOutcome.SUCCESS]
    by_issue: dict[str, list[HistoryRecord]] = defaultdict(list)
    for r in fails:
        by_issue[_diagnostic_issue(r)].append(r)

    out: list[dict[str, Any]] = []
    total_fail = len(fails)
    labels = {**FAILURE_ISSUES, UNSPECIFIED_ISSUE: "Not specified"}
    for issue, rows in sorted(by_issue.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        book = _ISSUE_PLAYBOOK.get(issue) or _ISSUE_PLAYBOOK[ISSUE_OTHER]
        evidence = _issue_evidence(rows, successes)
        cause = book["cause"]
        fail_exp = _median([r.settings.normal_exposure_time for r in rows])
        ok_exp = _median([r.settings.normal_exposure_time for r in successes])
        fail_bot = _median([r.settings.bottom_exposure_time for r in rows])
        ok_bot = _median([r.settings.bottom_exposure_time for r in successes])
        d_exp = _pct_delta(fail_exp, ok_exp)
        d_bot = _pct_delta(fail_bot, ok_bot)
        if issue in ADHESION_ISSUES and d_bot is not None and d_bot <= -8:
            cause = (
                f"Adhesion fails used lower bottom exposure than successes "
                f"({fail_bot:.1f}s vs {ok_bot:.1f}s) — plate bond was likely too weak."
            )
        elif issue == ISSUE_OVER_CURE and d_bot is not None and d_bot >= 8:
            cause = (
                f"Over-cure reports used higher bottom exposure than successes "
                f"({fail_bot:.1f}s vs {ok_bot:.1f}s)."
            )
        elif issue in (ISSUE_LAYER_SEPARATION, ISSUE_SUPPORTS) and d_exp is not None and d_exp <= -6:
            cause = (
                f"These fails ran lower layer exposure than successes "
                f"({fail_exp:.2f}s vs {ok_exp:.2f}s) — under-cure fits the pattern."
            )
        elif issue == ISSUE_FEATURES_CLOSED and d_exp is not None and d_exp >= 6:
            cause = (
                f"These fails ran higher layer exposure than successes "
                f"({fail_exp:.2f}s vs {ok_exp:.2f}s) — over-cure of fine detail fits."
            )
        out.append(
            {
                "issue": issue,
                "label": labels.get(issue, issue),
                "n": len(rows),
                "share_pct": round(100.0 * len(rows) / total_fail, 1),
                "cause": cause,
                "evidence": evidence,
                "actions": list(book["actions"]),
            }
        )
    return out


def recent_rate(records: list[HistoryRecord], days: int = 28, now: datetime | None = None) -> dict[str, float]:
    """Prints and print hours per week over the last `days`."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    recent = [r for r in records if (ts := _parse_ts(r.started_at) or _parse_ts(r.created_at)) and ts >= cutoff]
    per = days / 7.0
    return {
        "prints_per_week": len(recent) / per,
        "hours_per_week": sum(_print_hours(r) for r in recent) / per,
    }


def _span(started_at: str, duration_s: float | None, fallback_end: str = "") -> tuple[datetime, datetime] | None:
    """Print start and end. A report time can be hours late, so prefer the print duration."""
    start = _parse_ts(started_at)
    if start is None:
        return None
    if duration_s:
        return start, start + timedelta(seconds=float(duration_s))
    end = _parse_ts(fallback_end)
    return (start, end) if end and end >= start else None


def print_timeline(
    records: list[HistoryRecord],
    pending: list[PendingJob],
    open_jobs: list[OpenJob],
    hours: float = 12.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Print bars per printer over the last `hours`: reported, unreported, finished and still printing."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    bars: list[dict[str, Any]] = []

    def add(pid: str, span: tuple[datetime, datetime] | None, kind: str, filename: str, **extra: Any) -> None:
        if span is None or span[1] < cutoff:
            return
        bars.append(
            {
                "printer_id": pid,
                "start": span[0].isoformat(),
                "end": span[1].isoformat(),
                "kind": kind,
                "filename": filename,
                **extra,
            }
        )

    for rec in records:
        kind = "fail" if rec.outcome == PrintOutcome.FAIL else "success"
        add(rec.printer_id, _span(rec.started_at, rec.duration_s, rec.ended_at), kind, rec.filename, run_id=rec.id)
    for item in pending:
        job = item.job
        add(job.printer_id, _span(job.started_at, job.duration_s, item.finished_at), "unreported", job.filename, pending_id=item.id)
    for job in open_jobs:
        start = _parse_ts(job.started_at)
        if start is None:
            continue
        if job.awaiting_outcome:
            add(job.printer_id, _span(job.started_at, job.duration_s, now.isoformat()), "finished", job.filename)
        else:
            end = start + timedelta(seconds=float(job.eta_total_s)) if job.eta_total_s else now
            add(job.printer_id, (start, max(end, now)), "printing", job.filename, task_id=job.task_id)
    bars.sort(key=lambda b: b["start"])
    return {"now": now.isoformat(), "from": cutoff.isoformat(), "hours": hours, "bars": bars}


def fleet_comparison(
    records_by_printer: dict[str, list[HistoryRecord]],
    printer_names: dict[str, str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Side-by-side printer summary plus prints per week per printer."""
    printers = []
    for pid, records in records_by_printer.items():
        kpis = printer_kpis(records)
        rate = recent_rate(records, now=now)
        layer_counts: dict[float, int] = defaultdict(int)
        for r in records:
            layer_counts[round(r.layer_height, 3)] += 1
        top_layer = max(layer_counts, key=layer_counts.get) if layer_counts else None  # type: ignore[arg-type]
        at_layer = [r for r in records if top_layer is not None and abs(r.layer_height - top_layer) < 0.001]
        last = max((r.created_at for r in records), default="")
        printers.append(
            {
                "printer_id": pid,
                "name": printer_names.get(pid, pid),
                "total": kpis["total"],
                "fail": kpis["fail"],
                "success_rate": kpis["success_rate"],
                "prints_per_week": round(rate["prints_per_week"], 1),
                "hours_per_week": round(rate["hours_per_week"], 1),
                "top_layer": top_layer,
                "typical_exp": float(np.median([r.settings.normal_exposure_time for r in at_layer])) if at_layer else None,
                "typical_bottom": float(np.median([r.settings.bottom_exposure_time for r in at_layer])) if at_layer else None,
                "last_print": last,
            }
        )

    weekly = {pid: {w["week"]: w["prints"] for w in weekly_usage(recs)} for pid, recs in records_by_printer.items()}
    all_weeks = sorted({w for per in weekly.values() for w in per})
    if all_weeks:
        filled, wk, end = [], date.fromisoformat(all_weeks[0]), date.fromisoformat(all_weeks[-1])
        while wk <= end:
            filled.append(wk.isoformat())
            wk += timedelta(weeks=1)
        all_weeks = filled
    return {
        "printers": printers,
        "weeks": all_weeks,
        "weekly_prints": {pid: [per.get(w, 0) for w in all_weeks] for pid, per in weekly.items()},
    }
