"""The app's back end: printers, live status, recommendations, history and the Chitubox watcher."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .branding import asset_url
from .catalog import Catalog
from .drivers import FoundPrinter, drivers, get_driver
from .fleet import FleetEntry, load_fleet, save_fleet, slug, unique_id
from .paths import ctb_cache_dir, user_dir
from .settings import load_settings, save_settings
from .charts import (
    bottom_distribution,
    build_history_log,
    build_insights,
    failure_breakdown,
    fleet_comparison,
    format_run_rows,
    part_family_summary,
    printer_kpis,
    recent_rate,
    recipe_clusters,
    settings_timeline,
    temp_measured,
    weekly_usage,
)
from .engine import RecommendationEngine
from .bottom_calibration import FAILURE_ISSUES
from .history import (
    HistoryStore,
    OpenJob,
    apply_slice_to_job,
    is_new_print,
)
from .model_scan import scan_model
from .parts import (
    SIZE_LABELS,
    PartFamilies,
    save_group_override,
    save_override,
    size_class as parts_size_class,
    size_class_for_area,
)
from .quality import clean_quality, quality_problems
from .shape_scan import load_scans, save_scan, scan_file, shape_text, uvtools_cmd
from .slicer_watch import (
    THUMBNAIL_LOG,
    SLICER_FILE_DIR,
    match_printers,
    match_resin,
    newest_model,
    opened_models,
    resin_profile_name,
    sliced_files,
    sliced_properties,
)
from .models import (
    EDITABLE_FIELDS,
    EDITABLE_UNITS,
    TODAY_LAYER_HEIGHTS,
    GeometryClass,
    PrintOutcome,
    PrintSettings,
)
from .sdcp import (
    RELEASE_FILM_MAX_DEFAULT,
    PrinterLiveStatus,
    download_preview,
    download_print_file,
    ensure_print_preview,
    list_printer_files,
    fetch_print_history,
    guess_part_application,
    parse_ctb_filename,
)
from .chitubox_export import settings_clipboard_text

SHAPE_SCAN_START_DELAY_S = 60.0
SHAPE_SCAN_INTERVAL_S = 600.0
# Slicer copies kept on this computer (ChituManager keeps every file it sent to a printer).
LOCAL_SLICE_DIRS = [SLICER_FILE_DIR]
DISCOVERY_TIMEOUT_S = 2.5
SLICER_WATCH_INTERVAL_S = 5.0
# A sent file is still being written for a moment; wait until it has been quiet this long.
SENT_SETTLE_S = 10.0
# A sent file belongs to the model opened most recently before it, within this window.
SENT_MODEL_WINDOW_S = 6 * 3600
# File exposure within this of the recommendation counts as matching.
SENT_LAYER_TOLERANCE_PCT = 5.0
SENT_BOTTOM_TOLERANCE_PCT = 10.0


def _iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc).isoformat(timespec="seconds")


class WebService:
    def __init__(self) -> None:
        self.settings = load_settings()
        self.catalog = Catalog()
        self.history = HistoryStore()
        self.engine = RecommendationEngine(
            self.catalog, self.history, min_bottom_exposure=float(self.settings["min_bottom_exposure_s"])
        )
        self.fleet: dict[str, PrinterLiveStatus] = {}
        self._prev_kind: dict[str, str] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._shape_thread: threading.Thread | None = None
        self._scan_lock = threading.Lock()
        self.shape_status: dict[str, Any] = {"running": False, "current": "", "scanned": 0, "queued": 0, "last_error": ""}
        # What Chitubox on this PC is working on: the model opened last and the file sent last.
        self.detected: dict[str, Any] = {"model": None, "sent": None, "error": ""}
        self._model_key = ""
        self._sent_seen: set[str] = set()

    def start_polling(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        self._shape_thread = threading.Thread(target=self._shape_loop, daemon=True)
        self._shape_thread.start()
        if self.settings.get("watch_chitubox", True):
            # Files sent before the server started are history, not "about to print".
            self._sent_seen = {str(p) for p, _ in sliced_files()}
            threading.Thread(target=self._slicer_loop, daemon=True).start()

    def _slicer_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_slicer()
                self.detected["error"] = ""
            except Exception as exc:  # noqa: BLE001
                self.detected["error"] = str(exc)[:200]
            self._stop.wait(SLICER_WATCH_INTERVAL_S)

    def check_slicer(self) -> None:
        """Pick up a newly opened model and a newly sent sliced file."""
        model = newest_model()
        if model and model.key != self._model_key:
            self._model_key = model.key
            self.detected["model"] = {"file": model.path.name, "seen_at": _iso(model.seen_at), "status": "scanning"}
            try:
                scan = scan_model(model.path)
                save_scan(model.path.name, scan)
            except Exception as exc:  # noqa: BLE001
                scan = {"error": str(exc)[:200]}
            self.detected["model"] = self._model_detection(model.path.name, model.seen_at, scan)
        now = time.time()
        for path, mtime in sliced_files():
            if str(path) in self._sent_seen or now - mtime < SENT_SETTLE_S:
                continue
            self._sent_seen.add(str(path))
            self.check_sent_file(path, mtime)
            break

    def _model_detection(self, filename: str, seen_at: float, scan: dict[str, Any]) -> dict[str, Any]:
        runs = self._all_runs()
        families = PartFamilies(runs)
        fid, name, _ = families.resolve(filename)
        mine = [r for r in runs if families.family_id(r.filename) == fid]
        sizes = [s for s in (parts_size_class(r) for r in mine[:10]) if s]
        return {
            "file": filename,
            "seen_at": _iso(seen_at),
            "status": "error" if scan.get("error") else "ready",
            "error": scan.get("error", ""),
            "family_id": fid,
            "family": name,
            "group": families.group(fid),
            "runs": len(mine),
            # Plate size from this part's recent prints; a new part's size is only known once sliced.
            "size_class": max(set(sizes), key=sizes.count) if sizes else "",
            "shape_tags": scan.get("tags", []),
            "shape": shape_text(scan) if scan.get("layers_sampled") else "",
            "size_mm": scan.get("size_mm"),
            "volume_ml": scan.get("volume_ml"),
        }

    def check_sent_file(self, path: Path, mtime: float) -> dict[str, Any]:
        """Compare a sliced file sent from Chitubox with what we recommend for its part, printer and resin."""
        props = sliced_properties(path)
        model = next((m for m in opened_models() if mtime - SENT_MODEL_WINDOW_S <= m.seen_at <= mtime + 60), None)
        model_name = model.path.name if model else ""
        if model and model_name not in load_scans():
            try:
                save_scan(model_name, scan_model(model.path))
            except Exception:  # noqa: BLE001
                pass
        families = PartFamilies(self._all_runs())
        fid, name, _ = families.resolve(model_name) if model_name else ("", "", "")
        layer_h = float(props.get("LayerHeight") or 0.05)
        layers = int(props.get("LayerCount") or 0)
        volume = float(props.get("MaterialMilliliters") or 0)
        size = size_class_for_area(volume * 1000.0 / (layers * layer_h)) if volume and layers else ""
        profile = resin_profile_name(props)
        resin_id = match_resin(profile, self.catalog.resins)
        sent = {
            "file": model_name or path.name,
            "sent_at": _iso(mtime),
            "machine": props.get("MachineName", ""),
            "layer_height": layer_h,
            "exposure": props.get("ExposureTime"),
            "bottom_exposure": props.get("BottomExposureTime"),
            "volume_ml": volume,
            "height_mm": round(layers * layer_h, 2),
            "size_class": size,
            "family_id": fid,
            "family": name,
            "resin_profile": profile,
            "resin_id": resin_id,
            "resin_name": self.catalog.resins[resin_id].name if resin_id else "",
            "checks": [],
        }
        for pid in match_printers(sent["machine"], self.catalog.printers):
            sent["checks"].append(self._sent_check(pid, sent))
        self.detected["sent"] = sent
        return sent

    def _sent_check(self, pid: str, sent: dict[str, Any]) -> dict[str, Any]:
        vat_resin = self.vat_resin_id(pid)
        resin_id = sent["resin_id"] or vat_resin
        rec = self.engine.recommend(
            pid, resin_id, sent["layer_height"], self.vat_for_recommend(), GeometryClass.NORMAL, 0.0,
            part_family=sent["family_id"], size_class=sent["size_class"],
        )
        want, want_b = rec.settings.normal_exposure_time, rec.settings.bottom_exposure_time
        got, got_b = sent["exposure"], sent["bottom_exposure"]
        diff = (got / want - 1) * 100 if got and want else None
        diff_b = (got_b / want_b - 1) * 100 if got_b and want_b else None
        notes = []
        if sent["resin_id"] and sent["resin_id"] != vat_resin:
            notes.append(
                f"File's resin profile is {sent['resin_name']}, but the vat is set to "
                f"{self.catalog.resins[vat_resin].name}"
            )
        return {
            "printer_id": pid,
            "short_name": self.catalog.printers[pid].name,
            "resin_id": resin_id,
            "resin_name": self.catalog.resins[resin_id].name,
            "resin_source": "Chitubox resin profile" if sent["resin_id"] else "Resin in vat",
            "recommended": round(want, 2),
            "recommended_bottom": round(want_b, 1),
            "diff_pct": round(diff, 1) if diff is not None else None,
            "bottom_diff_pct": round(diff_b, 1) if diff_b is not None else None,
            "ok": (diff is None or abs(diff) <= SENT_LAYER_TOLERANCE_PCT)
            and (diff_b is None or abs(diff_b) <= SENT_BOTTOM_TOLERANCE_PCT),
            "detail": rec.exposure_detail or rec.part_detail or rec.source_detail,
            "notes": notes,
        }

    def detected_payload(self) -> dict[str, Any]:
        return {
            **self.detected,
            "watching": {
                "chitubox": THUMBNAIL_LOG.is_file(),
                "sent_files": SLICER_FILE_DIR.is_dir(),
                "uvtools": uvtools_cmd() is not None,
            },
        }

    def _shape_loop(self) -> None:
        self._stop.wait(SHAPE_SCAN_START_DELAY_S)
        while not self._stop.is_set():
            try:
                self.scan_printer_files()
            except Exception as exc:  # noqa: BLE001
                self.shape_status["last_error"] = str(exc)[:200]
            self._stop.wait(SHAPE_SCAN_INTERVAL_S)

    def scan_printer_files(self) -> dict[str, Any]:
        """Download and shape-scan sliced files on idle printers that haven't been scanned yet."""
        if uvtools_cmd() is None:
            return {**self.shape_status, "error": "UVtools is not installed on the host PC"}
        if not self._scan_lock.acquire(blocking=False):
            return self.shape_status
        try:
            self.shape_status["running"] = True
            runs = self._all_runs()
            families = PartFamilies(runs, scans={})
            printed = {families.family_id(r.filename) for r in runs if r.filename}
            scans = load_scans()
            # (name, printer id or "", local path or None) — newest version of each printed part,
            # local slicer copies first (no download), then whatever the printers still hold.
            todo: list[tuple[str, str, Path | None]] = []
            seen: set[str] = set()

            def consider(name: str, pid: str, path: Path | None) -> None:
                fid = families.family_id(name)
                if fid not in printed or fid in seen:
                    return
                seen.add(fid)
                if name not in scans:
                    todo.append((name, pid, path))

            local = [f for d in LOCAL_SLICE_DIRS if d.is_dir() for f in d.rglob("*") if f.suffix.lower() in (".ctb", ".goo")]
            for f in sorted(local, key=lambda f: f.stat().st_mtime, reverse=True):
                consider(f.name, "", f)
            for pid, printer in self.catalog.printers.items():
                with self._lock:
                    st = self.fleet.get(pid)
                # Don't pull hundreds of MB over Wi-Fi while the printer is busy.
                if not printer.sdcp_ip or (st and st.status_kind in ("printing", "busy")):
                    continue
                # Printers list oldest first.
                for name in reversed(list_printer_files(printer.sdcp_ip, printer.sdcp_mainboard_id)):
                    consider(name, pid, None)

            self.shape_status["queued"] = len(todo)
            for name, pid, path in todo:
                if self._stop.is_set():
                    break
                self.shape_status["current"] = name
                source = f"printer:{pid}" if pid else f"file:{path}"
                dest = ctb_cache_dir() / name
                try:
                    if path is None:
                        printer = self.catalog.printers[pid]
                        if download_print_file(printer.sdcp_ip, name, dest) is None:
                            continue
                        path = dest
                    result = {**scan_file(path), "source": source}
                except Exception as exc:  # noqa: BLE001
                    result = {"error": str(exc)[:300], "tags": [], "layers_sampled": 0, "source": source}
                    self.shape_status["last_error"] = f"{name}: {result['error']}"
                finally:
                    dest.unlink(missing_ok=True)
                save_scan(name, result)
                self.shape_status["scanned"] += 1
                self.shape_status["queued"] -= 1
        finally:
            self.shape_status.update(running=False, current="", queued=0)
            self._scan_lock.release()
        return self.shape_status

    def start_shape_scan(self) -> dict[str, Any]:
        if not self.shape_status["running"]:
            threading.Thread(target=self.scan_printer_files, daemon=True).start()
        return {"ok": True, "uvtools": uvtools_cmd() is not None, **self.shape_status, "running": True}

    def stop_polling(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_fleet()
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(float(self.settings.get("poll_interval_s") or 20.0))

    def _read_status(self, printer: Any) -> PrinterLiveStatus:
        driver = get_driver(printer.driver)
        if driver is None:
            return PrinterLiveStatus(connected=False, ip=printer.sdcp_ip, raw_error=f"No '{printer.driver}' driver installed")
        if not printer.sdcp_ip:
            return PrinterLiveStatus(connected=False, ip="", raw_error="No network address")
        try:
            return driver.status(printer.sdcp_ip, printer.sdcp_mainboard_id)
        except Exception as exc:  # noqa: BLE001
            return PrinterLiveStatus(connected=False, ip=printer.sdcp_ip, raw_error=str(exc)[:200])

    def refresh_fleet(self) -> dict[str, Any]:
        printers = list(self.catalog.printers.values())
        results: dict[str, PrinterLiveStatus] = {}
        if printers:
            with ThreadPoolExecutor(max_workers=min(8, len(printers))) as pool:
                for p, st in zip(printers, pool.map(self._read_status, printers)):
                    results[p.id] = st
        with self._lock:
            self.fleet = results
            for pid, st in results.items():
                self._sync_open_job(pid, st)
        return self.fleet_payload()

    def room_ref_id(self, fleet: dict[str, PrinterLiveStatus] | None = None) -> str:
        """Printer whose vat sensor stands in for room temperature: the chosen one, else the first reporting one."""
        if fleet is None:
            with self._lock:
                fleet = dict(self.fleet)
        chosen = self.settings.get("room_ref_printer_id") or ""
        if chosen in self.catalog.printers:
            return chosen
        for pid in self.catalog.printers:
            st = fleet.get(pid)
            if st and st.connected and st.enclosure_temp_c is not None:
                return pid
        return ""

    def _room_from(self, fleet: dict[str, PrinterLiveStatus]) -> float | None:
        ref = fleet.get(self.room_ref_id(fleet))
        if ref and ref.connected and ref.enclosure_temp_c is not None:
            return float(ref.enclosure_temp_c)
        return None

    def room_vat_c(self) -> float | None:
        with self._lock:
            fleet = dict(self.fleet)
        return self._room_from(fleet)

    def vat_for_recommend(self) -> float:
        room = self.room_vat_c()
        if room is not None:
            return room
        return float(self.settings.get("fallback_room_c") or 25.0)

    def today_layer_heights(self) -> list[float]:
        return [float(h) for h in self.settings.get("today_layer_heights") or TODAY_LAYER_HEIGHTS]

    def primary_resins(self) -> list[dict[str, Any]]:
        out = []
        for r in self.catalog.primary_resins.values():
            out.append(
                {
                    "id": r.id,
                    "name": r.name,
                    "brand": r.brand,
                    "color": r.color,
                    "material_family": r.material_family,
                    "swatch": r.swatch,
                    "notes": r.notes,
                    "typical_uses": r.typical_uses,
                    "min_normal_exposure": r.min_normal_exposure,
                    "min_bottom_exposure": r.min_bottom_exposure,
                    "wavelength_nm": r.wavelength_nm,
                    "photo": asset_url(r.photo),
                    "store_url": r.store_url,
                    "amazon_url": r.amazon_url,
                    "purchase_url": r.purchase_url,
                }
            )
        return out

    # ---- Printers: discovery, adding, removing ---------------------------------------------

    def discover(self, target: str = "") -> dict[str, Any]:
        """Printers answering on the network, matched to library models, flagged when already added."""
        known = {(p.driver, p.sdcp_mainboard_id or p.sdcp_ip) for p in self.catalog.printers.values()}
        found: list[FoundPrinter] = []
        errors: list[str] = []
        for driver in drivers().values():
            try:
                found.extend(driver.discover(timeout_s=DISCOVERY_TIMEOUT_S, target=target))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{driver.label}: {exc}")
        rows = []
        for f in found:
            model = self.catalog.match_model(f.brand, f.model_name, f.driver)
            unsupported = str(f.details.get("unsupported") or "")
            check = getattr(get_driver(f.driver), "is_resin", None)
            if model is None and not unsupported and check is not None and not check(f):
                continue
            rows.append(
                {
                    **f.to_dict(),
                    "model_id": model.id if model else "",
                    "model_label": model.full_name if model else f"{f.brand} {f.model_name}".strip(),
                    "known_model": model is not None,
                    "photo": asset_url(model.photo) if model else "",
                    "added": (f.driver, f.device_id or f.host) in known,
                    "unsupported": unsupported,
                }
            )
        return {"printers": rows, "errors": errors, "drivers": [d.label for d in drivers().values()]}

    def add_printers(self, picks: list[dict[str, Any]]) -> dict[str, Any]:
        """Add discovered printers (host + device id + driver, optional nickname) to the fleet."""
        fleet = load_fleet()
        taken = {e.id for e in fleet}
        have = {(e.driver, e.device_id or e.host) for e in fleet}
        added = []
        for pick in picks:
            driver = get_driver(str(pick.get("driver") or "sdcp"))
            if driver is None:
                raise ValueError(f"Unknown driver {pick.get('driver')!r}")
            found = FoundPrinter(
                driver=driver.id,
                host=str(pick.get("host") or "").strip(),
                device_id=str(pick.get("device_id") or "").strip(),
                name=str(pick.get("name") or "").strip(),
                model_name=str(pick.get("model_name") or "").strip(),
                brand=str(pick.get("brand") or "").strip(),
            )
            if not found.host:
                raise ValueError("Printer address missing")
            if (pick.get("details") or {}).get("unsupported") or pick.get("unsupported"):
                continue
            if (found.driver, found.device_id or found.host) in have:
                continue
            model = self.catalog.match_model(found.brand, found.model_name, found.driver)
            entry = FleetEntry(
                id=unique_id(found.name or found.model_name or found.host, taken),
                name=found.name or found.model_name or found.host,
                model_id=model.id if model else f"{slug(found.brand)}_{slug(found.model_name)}".strip("_"),
                driver=found.driver,
                host=found.host,
                device_id=found.device_id,
            )
            if model is None:
                described = driver.describe(found) or {}
                entry.model = {
                    "brand": found.brand or described.get("brand", ""),
                    "name": found.model_name or described.get("machine_name", "") or "Resin printer",
                    "driver": found.driver,
                    **{
                        k: described[k]
                        for k in ("resolution_x", "resolution_y", "machine_width", "machine_depth", "machine_height")
                        if described.get(k)
                    },
                }
            fleet.append(entry)
            taken.add(entry.id)
            have.add((entry.driver, entry.device_id or entry.host))
            added.append(entry.id)
        save_fleet(fleet)
        self.catalog.set_fleet(fleet)
        threading.Thread(target=self.refresh_fleet, daemon=True).start()
        return {"ok": True, "added": added}

    def update_printer(self, printer_id: str, name: str = "", host: str = "") -> dict[str, Any]:
        fleet = load_fleet()
        entry = next((e for e in fleet if e.id == printer_id), None)
        if entry is None:
            raise ValueError("Unknown printer")
        if name.strip():
            entry.name = name.strip()
        if host.strip():
            entry.host = host.strip()
        save_fleet(fleet)
        self.catalog.set_fleet(fleet)
        return {"ok": True}

    def remove_printer(self, printer_id: str) -> dict[str, Any]:
        """Stop tracking a printer. Its print history stays (re-adding it brings it back)."""
        fleet = [e for e in load_fleet() if e.id != printer_id]
        save_fleet(fleet)
        self.catalog.set_fleet(fleet)
        with self._lock:
            self.fleet.pop(printer_id, None)
        return {"ok": True}

    def models_payload(self) -> list[dict[str, Any]]:
        return [
            {"id": m.id, "brand": m.brand, "name": m.name, "driver": m.driver, "photo": asset_url(m.photo)}
            for m in sorted(self.catalog.models.values(), key=lambda m: m.full_name)
        ]

    # ---- Settings ------------------------------------------------------------------------------

    def settings_payload(self) -> dict[str, Any]:
        return {
            **self.settings,
            "data_folder": str(user_dir()),
            "room_ref_active": self.room_ref_id(),
        }

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        self.settings = save_settings(changes)
        self.engine.min_bottom_exposure = float(self.settings["min_bottom_exposure_s"])
        return self.settings_payload()

    def printers_payload(self) -> list[dict[str, Any]]:
        out = []
        ref_id = self.room_ref_id()
        for p in self.catalog.printers.values():
            out.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "short_name": p.name,
                    "xy_label": p.xy_label,
                    "volume_label": p.volume_label,
                    "specs": list(p.specs),
                    "sdcp_ip": p.sdcp_ip,
                    "photo": asset_url(p.photo),
                    "is_room_ref": p.id == ref_id,
                    "model_id": p.model_id,
                    "model_name": p.model_name,
                    "brand": p.brand,
                    "driver": p.driver,
                }
            )
        return out

    def vat_resin_id(self, printer_id: str) -> str:
        """Resin marked as in the vat, else the first catalog resin."""
        rid = self.history.vat_resin(printer_id)
        if rid in self.catalog.resins:
            return rid
        return next(iter(self.catalog.primary_resins), "")

    def backfill_previews(self, details_by_printer: dict[str, list[Any]] | None = None) -> dict[str, int]:
        """Download the Chitubox preview the printer kept for every logged print that lacks one."""
        missing = self.history.runs_missing_preview()
        details_by_printer = dict(details_by_printer or {})
        got = failed = 0
        for pid in sorted({r.printer_id for r in missing}):
            p = self.catalog.printers.get(pid)
            if not p or not p.sdcp_ip:
                continue
            if pid not in details_by_printer:
                details_by_printer[pid] = fetch_print_history(p.sdcp_ip, p.sdcp_mainboard_id)
            thumbs = {d.task_id: d.thumbnail_url for d in details_by_printer[pid] if d.task_id}
            for rec in (r for r in missing if r.printer_id == pid):
                if rec.task_id not in thumbs:
                    failed += 1
                    continue
                path = download_preview(p.sdcp_ip, rec.task_id, thumbnail_url=thumbs[rec.task_id])
                if path is None or not path.is_file():
                    failed += 1
                    continue
                self.history.set_run_preview(rec.id, str(path))
                got += 1
        return {"previews": got, "previews_missing": failed}

    def import_printer_history(
        self,
        printer_ids: list[str] | None = None,
        dry_run: bool = False,
        since: datetime | None = None,
        resin_id: str = "",
    ) -> dict[str, Any]:
        """Pull every SLA printer's own print list and log completed prints as successes."""
        if resin_id and resin_id not in self.catalog.resins:
            raise ValueError("Unknown resin")
        vat_c = self.vat_for_recommend()
        printers = []
        fetched: dict[str, list[Any]] = {}
        for p in self.catalog.printers.values():
            if printer_ids and p.id not in printer_ids:
                continue
            if not p.sdcp_ip:
                continue
            details = fetch_print_history(p.sdcp_ip, p.sdcp_mainboard_id)
            fetched[p.id] = details
            rid = resin_id or self.vat_resin_id(p.id)
            res = self.history.import_printer_prints(
                p.id, details, rid, vat_c, dry_run=dry_run, since=since
            )
            resin = self.catalog.resins.get(rid)
            printers.append(
                {
                    "printer_id": p.id,
                    "printer": p.name,
                    "resin": resin.name if resin else rid,
                    "found": len(details),
                    **{k: v for k, v in res.items() if k != "rows"},
                    "oldest": min((r["started_at"] for r in res["rows"]), default=""),
                }
            )
        previews = {} if dry_run else self.backfill_previews(fetched)
        return {
            "ok": True,
            "dry_run": dry_run,
            "imported": sum(p["imported"] for p in printers),
            "printers": printers,
            **previews,
        }

    def set_vat_resin(self, printer_id: str, resin_id: str) -> dict[str, Any]:
        if printer_id not in self.catalog.printers:
            raise ValueError("Unknown printer")
        if resin_id not in self.catalog.resins:
            raise ValueError("Unknown resin")
        self.history.set_vat_resin(printer_id, resin_id)
        return {"ok": True, "printer_id": printer_id, "resin_id": resin_id}

    def _status_dict(self, pid: str, st: PrinterLiveStatus) -> dict[str, Any]:
        room = self.room_vat_c()
        ref_id = self.room_ref_id()
        temp_bits: list[str] = []
        if room is not None:
            if pid == ref_id:
                temp_bits.append(f"{room:.0f}° vat · room ref")
            else:
                ref = self.catalog.printers.get(ref_id)
                temp_bits.append(f"{room:.0f}° room" + (f" · via {ref.name}" if ref else ""))
        elif st.enclosure_temp_c is not None:
            temp_bits.append(f"{st.enclosure_temp_c:.0f}° vat")
        if st.uv_led_temp_c is not None:
            temp_bits.append(f"{st.uv_led_temp_c:.0f}° UV")
        return {
            "printer_id": pid,
            "connected": st.connected,
            "status_kind": st.status_kind if st.connected else "offline",
            "status_label": st.status_label,
            "print_substatus": st.print_substatus,
            "filename": st.filename,
            "error_message": st.error_message or st.raw_error,
            "progress_pct": st.print_progress_pct,
            "current_layer": st.current_layer,
            "total_layers": st.total_layers,
            "time_left_s": (st.total_ticks - st.current_ticks) / 1000.0
            if st.total_ticks and st.current_ticks is not None and st.total_ticks >= st.current_ticks
            else None,
            "vat_c": st.enclosure_temp_c,
            "uv_c": st.uv_led_temp_c,
            "temp_display": " · ".join(temp_bits) if temp_bits else (st.ip or "—"),
            "ip": st.ip,
            "task_id": st.task_id,
            "preview_url": "",
            "part_application": "",
            "release_film": st.release_film,
            "release_film_max": st.release_film_max or RELEASE_FILM_MAX_DEFAULT,
            "film": st.film,
        }

    def fleet_payload(self) -> dict[str, Any]:
        with self._lock:
            fleet = dict(self.fleet)
        printers = []
        n_print = n_idle = n_bad = 0
        n_film = 0
        vats = self.history.vat_resins()
        ref_id = self.room_ref_id(fleet)
        for p in self.catalog.printers.values():
            st = fleet.get(p.id) or PrinterLiveStatus(connected=False, ip=p.sdcp_ip, raw_error="Pending")
            row = self._status_dict(p.id, st)
            vat_rid = self.vat_resin_id(p.id)
            vat_resin = self.catalog.resins.get(vat_rid)
            row.update(
                {
                    "name": p.name,
                    "short_name": p.name,
                    "photo": asset_url(p.photo),
                    "is_room_ref": p.id == ref_id,
                    "model_name": p.model_name,
                    "vat_resin_id": vat_rid,
                    "vat_resin": vat_resin.name if vat_resin else "",
                    "vat_resin_swatch": vat_resin.swatch if vat_resin else "",
                    "vat_resin_set_at": (vats.get(p.id) or {}).get("set_at", ""),
                }
            )
            printers.append(row)
            kind = row["status_kind"]
            if kind == "printing":
                n_print += 1
            elif kind == "idle":
                n_idle += 1
            else:
                n_bad += 1
            film = row.get("film") or {}
            if film.get("level") in {"warn", "critical"}:
                n_film += 1
        awaiting = [
            {
                "printer_id": j.printer_id,
                "filename": j.filename,
                "awaiting_outcome": j.awaiting_outcome,
                "current_layer": j.current_layer,
                "total_layers": j.total_layers,
                "vat_temperature_c": j.vat_temperature_c,
                "uv_led_temp_c": j.uv_led_temp_c,
                "preview_path": j.preview_path,
                "preview_url": f"/previews/{Path(j.preview_path).name}" if j.preview_path else "",
                "part_application": j.part_application,
                "task_id": j.task_id,
                "release_film": j.release_film,
                "resin_id": j.resin_id,
            }
            for j in self.history.list_open_jobs()
        ]
        jobs_by_pid = {j["printer_id"]: j for j in awaiting}
        for row in printers:
            job = jobs_by_pid.get(row["printer_id"])
            if job:
                row["preview_url"] = job["preview_url"]
                row["part_application"] = job["part_application"]
        return {
            "room_vat_c": self.room_vat_c(),
            "summary": {
                "printing": n_print,
                "idle": n_idle,
                "attention": n_bad,
                "film_due": n_film,
                "unreported": len(self.history.list_unreported()),
            },
            "printers": printers,
            "open_jobs": awaiting,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def today_profiles(self, resin_id: str = "", part_family: str = "", size_class: str = "") -> dict[str, Any]:
        """Profiles per printer for the resin in its vat (or one resin for all when given),
        optionally tuned for one part (family id) and/or size class."""
        fixed = resin_id if resin_id in self.catalog.resins else ""
        size_class = size_class if size_class in SIZE_LABELS else ""
        vat = self.vat_for_recommend()
        ref_id = self.room_ref_id()
        cards = []
        for p in self.catalog.printers.values():
            resin_id = fixed or self.vat_resin_id(p.id)
            resin = self.catalog.resins[resin_id]
            profiles = []
            for layer in self.today_layer_heights():
                try:
                    rec = self.engine.recommend(
                        p.id, resin_id, layer, vat, GeometryClass.NORMAL, 0.0,
                        part_family=part_family, size_class=size_class,
                    )
                    s = rec.settings
                    profiles.append(
                        {
                            "layer_height": layer,
                            "normal_exposure_time": s.normal_exposure_time,
                            "bottom_exposure_time": s.bottom_exposure_time,
                            "bottom_detail": rec.bottom_detail,
                            "bottom_calibrated": rec.bottom_calibrated,
                            "part_detail": rec.part_detail,
                            "exposure_detail": rec.exposure_detail,
                            "bottom_layer_count": s.bottom_layer_count,
                            "transition_layer_count": s.transition_layer_count,
                            "rest_time_after_retract": s.rest_time_after_retract,
                            "confidence": rec.confidence.value,
                            "source_detail": rec.source_detail,
                            "warnings": rec.warnings,
                            "settings": s.to_dict(),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    profiles.append({"layer_height": layer, "error": str(exc)})
            with self._lock:
                st = self.fleet.get(p.id)
            status = self._status_dict(p.id, st) if st else {"status_label": "…", "temp_display": "—"}
            cards.append(
                {
                    "printer_id": p.id,
                    "name": p.name,
                    "short_name": p.name,
                    "photo": asset_url(p.photo),
                    "is_room_ref": p.id == ref_id,
                    "model_name": p.model_name,
                    "status": status,
                    "profiles": profiles,
                    "resin_id": resin_id,
                    "resin_name": resin.name,
                    "resin_runs": len(self.history.list_successes(p.id, resin_id=resin_id, limit=500)),
                }
            )
        return {
            "resin_id": fixed,
            "part_family": part_family,
            "size_class": size_class,
            "vat_c": vat,
            "room_ref": ref_id,
            "cards": cards,
        }

    def recommend(
        self,
        printer_id: str,
        resin_id: str,
        layer_height: float,
        vat_temperature_c: float | None = None,
        geometry: str = "normal",
        cross_section_mm2: float = 0.0,
        part_family: str = "",
        size_class: str = "",
    ) -> dict[str, Any]:
        if printer_id not in self.catalog.printers:
            raise ValueError("Unknown printer")
        if resin_id not in self.catalog.resins:
            raise ValueError("Unknown resin")
        try:
            geom = GeometryClass(geometry)
        except ValueError as exc:
            raise ValueError("Invalid geometry") from exc
        if size_class and size_class not in SIZE_LABELS:
            raise ValueError("Invalid size class")
        vat = float(vat_temperature_c) if vat_temperature_c is not None else self.vat_for_recommend()
        rec = self.engine.recommend(
            printer_id,
            resin_id,
            float(layer_height),
            vat,
            geom,
            float(cross_section_mm2 or 0.0),
            part_family=part_family,
            size_class=size_class,
        )
        printer = self.catalog.printers[printer_id]
        resin = self.catalog.resins[resin_id]
        settings = rec.settings.to_dict()
        fields = []
        for key, label, _typ in EDITABLE_FIELDS:
            fields.append(
                {
                    "key": key,
                    "label": label,
                    "unit": EDITABLE_UNITS.get(key, ""),
                    "value": settings.get(key),
                }
            )
        with self._lock:
            st = self.fleet.get(printer_id)
        status = self._status_dict(printer_id, st) if st else {}
        return {
            "printer_id": printer_id,
            "resin_id": resin_id,
            "layer_height": float(layer_height),
            "vat_temperature_c": vat,
            "geometry": geom.value,
            "geometry_label": geom.label,
            "cross_section_mm2": float(cross_section_mm2 or 0.0),
            "confidence": rec.confidence.value,
            "source_detail": rec.source_detail,
            "bottom_detail": rec.bottom_detail,
            "bottom_calibrated": rec.bottom_calibrated,
            "part_detail": rec.part_detail,
            "exposure_detail": rec.exposure_detail,
            "part_family": part_family,
            "size_class": size_class,
            "warnings": rec.warnings,
            "settings": settings,
            "fields": fields,
            "clipboard": settings_clipboard_text(rec.settings),
            "printer": {
                "id": printer.id,
                "name": printer.name,
                "short_name": printer.name,
                "photo": asset_url(printer.photo),
                "specs": list(printer.specs),
                "status": status,
            },
            "resin": {
                "id": resin.id,
                "name": resin.name,
                "photo": asset_url(resin.photo),
                "swatch": resin.swatch,
                "brand": resin.brand,
            },
        }

    def history_for(self, printer_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        """Fleet-wide history log; pass `printer_id` to narrow it to one printer."""
        printer_names = {p.id: p.name for p in self.catalog.printers.values()}
        resin_names = {i: r.name for i, r in self.catalog.resins.items()}
        log = build_history_log(self.history, printer_names, resin_names, printer_id, limit)
        return {"printer_id": printer_id, **log}

    def charts_for(self, printer_id: str, resin_id: str = "") -> dict[str, Any]:
        """Per-printer chart data; `resin_id` narrows every series to that resin."""
        printer = self.catalog.printers.get(printer_id)
        name = printer.name if printer else printer_id
        all_records = self.history.list_for_printer(printer_id, limit=1000)
        names = {i: r.name for i, r in self.catalog.resins.items()}
        used: dict[str, int] = {}
        for r in all_records:
            used[r.resin_id] = used.get(r.resin_id, 0) + 1
        resin_id = resin_id if resin_id in self.catalog.resins else ""
        records = [r for r in all_records if r.resin_id == resin_id] if resin_id else all_records
        kpis = printer_kpis(records)
        label = name + (f" · {names[resin_id]}" if resin_id else "")
        insights = build_insights(records, names, printer_name=label)
        rec_resin = resin_id or self.vat_resin_id(printer_id)
        vat = self.vat_for_recommend()
        recommended = []
        for layer in self.today_layer_heights():
            try:
                rec = self.engine.recommend(printer_id, rec_resin, layer, vat, GeometryClass.NORMAL, 0.0)
            except Exception:  # noqa: BLE001
                continue
            recommended.append(
                {
                    "layer": layer,
                    "exp": rec.settings.normal_exposure_time,
                    "bottom": rec.settings.bottom_exposure_time,
                }
            )
        return {
            "printer_id": printer_id,
            "printer_name": name,
            "kpis": {**kpis, **recent_rate(records), "measured_temp_runs": sum(map(temp_measured, records))},
            "insights": insights,
            "timeline": settings_timeline(records),
            "recipes": recipe_clusters(records),
            "recommended": recommended,
            "recommended_resin": names.get(rec_resin, rec_resin),
            "resin_id": resin_id,
            "resins_used": sorted(
                ({"id": rid, "name": names.get(rid, rid), "n": n} for rid, n in used.items()),
                key=lambda d: -d["n"],
            ),
            "bottom_bins": bottom_distribution(records),
            "weekly": weekly_usage(records),
            "failures": failure_breakdown(records),
            "curves": self.engine.exposure_curves(printer_id, resin_id),
            "rows": self._rows_with_history_numbers(records, names),
        }

    def _all_runs(self) -> list[Any]:
        return self.history.list_recent(printer_id=None, outcome=None, limit=100000)

    def parts(self, printer_id: str = "") -> dict[str, Any]:
        """Part families (optionally one printer's runs) with outcomes, size and typical settings."""
        printer_names = {p.id: p.name for p in self.catalog.printers.values()}
        all_runs = self._all_runs()
        families = PartFamilies(all_runs)
        records = [r for r in all_runs if r.printer_id == printer_id] if printer_id else all_runs
        return {
            "printer_id": printer_id,
            "families": part_family_summary(records, families, printer_names),
            "size_classes": [{"id": k, "label": v} for k, v in SIZE_LABELS.items()],
            "shape_scan": {**self.shape_status, "uvtools": uvtools_cmd() is not None, "files_scanned": len(load_scans())},
        }

    def rename_part_family(self, auto_keys: list[str], name: str) -> dict[str, Any]:
        """Rename a family (all its auto keys); an existing family's name merges them; empty resets."""
        if not auto_keys:
            raise ValueError("No family given")
        for key in auto_keys:
            save_override(key, name)
        return {"ok": True}

    def set_part_group(self, family_ids: list[str], group: str) -> dict[str, Any]:
        """Move parts into a group (renaming a group = moving all its parts); empty = automatic group."""
        if not family_ids:
            raise ValueError("No part given")
        for fid in family_ids:
            save_group_override(fid, group)
        return {"ok": True}

    def _rows_with_history_numbers(self, records: list[Any], names: dict[str, str]) -> list[dict[str, Any]]:
        printer_names = {p.id: p.name for p in self.catalog.printers.values()}
        log = build_history_log(self.history, printer_names, names, None, 100000)
        numbers = {r["report_id"]: r["id"] for r in log["rows"] if r.get("report_kind") == "run"}
        rows = format_run_rows(records, names, printer_names, PartFamilies(self._all_runs()))
        for row in rows:
            row["id"] = numbers.get(row["report_id"], row["id"])
        return rows

    def fleet_charts(self) -> dict[str, Any]:
        names = {p.id: p.name for p in self.catalog.printers.values()}
        by_printer = {pid: self.history.list_for_printer(pid, limit=1000) for pid in self.catalog.printers}
        return fleet_comparison(by_printer, names)

    def report_outcome(
        self,
        printer_id: str,
        outcome: str,
        notes: str = "",
        resin_id: str | None = None,
        part_application: str | None = None,
        failure_issue: str = "",
        layer_height: float | None = None,
        quality: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        oc, issue, checks = self._validate_report(outcome, notes, failure_issue, quality)

        job = self.history.get_open_job(printer_id)
        if job:
            rid = resin_id or job.resin_id or self.vat_resin_id(printer_id)
            settings = self._job_settings(job, rid)
            row_id = self.history.resolve_open_job(
                printer_id,
                oc,
                notes=notes,
                settings=settings,
                resin_id=rid,
                part_application=part_application,
                failure_issue=issue,
                quality=checks,
            )
            return {"ok": True, "id": row_id, "source": "sdcp"}

        # Manual report without open job
        rid = resin_id or self.vat_resin_id(printer_id)
        vat = self.vat_for_recommend()
        layer = float(layer_height) if layer_height else self.today_layer_heights()[0]
        rec = self.engine.recommend(printer_id, rid, layer, vat, GeometryClass.NORMAL, 0.0)
        row_id = self.history.log_run(
            printer_id,
            rid,
            rec.settings.layer_height,
            vat,
            rec.settings,
            oc,
            GeometryClass.NORMAL,
            0.0,
            f"Web {oc.value}" + (f" — {notes}" if notes else ""),
            source="web",
            part_application=part_application or "",
            failure_issue=issue,
            quality=checks,
        )
        return {"ok": True, "id": row_id, "source": "web"}

    @staticmethod
    def _validate_report(
        outcome: str,
        notes: str,
        failure_issue: str,
        quality: dict[str, Any] | None,
    ) -> tuple[PrintOutcome, str, dict[str, bool]]:
        try:
            oc = PrintOutcome(outcome)
        except ValueError as exc:
            raise ValueError("outcome must be success, fail, or partial") from exc
        issue = (failure_issue or "").strip()
        if oc == PrintOutcome.SUCCESS:
            issue = ""
        elif issue and issue not in FAILURE_ISSUES:
            raise ValueError("Unknown failure type")
        checks = clean_quality(quality or {})
        if oc == PrintOutcome.FAIL and not issue and not quality_problems(checks) and not notes.strip():
            raise ValueError("Pick a failure type, mark what went wrong, or add a note")
        return oc, issue, checks

    def _job_settings(self, job: OpenJob, resin_id: str) -> PrintSettings:
        if job.settings is not None:
            return job.settings
        vat = job.vat_temperature_c or self.vat_for_recommend()
        rec = self.engine.recommend(
            job.printer_id, resin_id, job.layer_height or 0.05, vat, GeometryClass.NORMAL, 0.0
        )
        return rec.settings

    def amend_run(
        self,
        run_id: int,
        outcome: str,
        notes: str = "",
        part_application: str | None = None,
        failure_issue: str = "",
        quality: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Report (or correct) the outcome and print settings of an already-logged run."""
        oc, issue, checks = self._validate_report(outcome, notes, failure_issue, quality)
        if settings:
            self.history.edit_run_settings(run_id, settings)
        rec = self.history.amend_run(
            run_id,
            oc,
            notes=notes,
            failure_issue=issue,
            quality=checks,
            part_application=part_application,
        )
        return {"ok": True, "id": rec.id, "outcome": rec.outcome.value}

    def report_pending(
        self,
        pending_id: int,
        outcome: str,
        notes: str = "",
        resin_id: str | None = None,
        part_application: str | None = None,
        failure_issue: str = "",
        quality: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Report a finished print that was skipped before the next one started."""
        oc, issue, checks = self._validate_report(outcome, notes, failure_issue, quality)
        pending = self.history.get_unreported(pending_id)
        if pending is None:
            raise ValueError(f"Unreported print #{pending_id} not found")
        rid = resin_id or pending.job.resin_id or self.vat_resin_id(pending.job.printer_id)
        row_id = self.history.resolve_unreported(
            pending_id,
            oc,
            notes=notes,
            settings=self._job_settings(pending.job, rid),
            resin_id=rid,
            part_application=part_application,
            failure_issue=issue,
            quality=checks,
        )
        return {"ok": True, "id": row_id, "source": "sdcp"}

    def _fetch_preview_for_job(self, pid: str, job: OpenJob) -> None:
        if not job.task_id or (job.preview_path and job.settings_from_printer):
            return
        printer = self.catalog.printers.get(pid)
        if not printer or not printer.sdcp_ip:
            return
        try:
            thumb, rel, slice_info = ensure_print_preview(
                printer.sdcp_ip, printer.sdcp_mainboard_id, job.task_id, timeout_s=5.0
            )
        except Exception:  # noqa: BLE001
            return
        apply_slice_to_job(job, slice_info)
        if rel:
            job.preview_path = rel
        if thumb:
            job.thumbnail_url = thumb
        if not job.part_application:
            job.part_application = guess_part_application(job.filename)

    def _sync_open_job(self, pid: str, st: PrinterLiveStatus) -> None:
        if not st.connected:
            self._prev_kind[pid] = "offline"
            return
        prev = self._prev_kind.get(pid)
        kind = st.status_kind
        job = self.history.get_open_job(pid)
        room = self._room_from({**self.fleet, pid: st})

        if kind == "printing":
            if job and job.awaiting_outcome and is_new_print(job, st):
                self.history.park_open_job(pid)
                job = None
            if job is None:
                if self.history.has_run_for_task(pid, st.task_id):
                    self._prev_kind[pid] = kind
                    return
                job = OpenJob(printer_id=pid, started_at=datetime.now(timezone.utc).isoformat())
            parsed = parse_ctb_filename(st.filename or "")
            if parsed.get("layer_height") is not None:
                job.layer_height = float(parsed["layer_height"])
            if parsed.get("normal_exposure_s") is not None:
                job.exposure_from_file_s = float(parsed["normal_exposure_s"])
            if not job.resin_id:
                job.resin_id = self.vat_resin_id(pid)
            job.awaiting_outcome = False
            job.record_sample(st)
            if room is not None:
                job.vat_temperature_c = room
            if not job.part_application:
                job.part_application = guess_part_application(job.filename or st.filename)
            self._fetch_preview_for_job(pid, job)
            self.history.upsert_open_job(job)
        elif job and not job.awaiting_outcome and kind in {"finished", "idle", "failed"}:
            if prev == "printing" or job.last_status == "printing":
                job.record_sample(st)
                if room is not None:
                    job.vat_temperature_c = room
                job.last_status = kind
                job.awaiting_outcome = True
                if not job.part_application:
                    job.part_application = guess_part_application(job.filename)
                self._fetch_preview_for_job(pid, job)
                self.history.upsert_open_job(job)
        self._prev_kind[pid] = kind


_service: WebService | None = None


def get_service() -> WebService:
    global _service
    if _service is None:
        _service = WebService()
    return _service
