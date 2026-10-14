"""Per-printer print-run history with geometry, outcomes, and SDCP open jobs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import GeometryClass, PrintOutcome, PrintSettings
from .paths import user_path
from .quality import clean_quality
from .sdcp import filename_mismatches, settings_from_slice_info

# Runs are kept in SQLite and mirrored to an append-only JSONL journal next to it.
# The journal is a portable backup: copy it to another computer and ResinPilot imports it.
DB_NAME = "history.db"
JOURNAL_NAME = "runs.jsonl"

_EXTRA_RUN_COLUMNS: dict[str, str] = {
    "filename": "TEXT DEFAULT ''",
    "uv_led_temp_c": "REAL",
    "total_layers": "INTEGER",
    "source": "TEXT DEFAULT 'manual'",
    "started_at": "TEXT",
    "ended_at": "TEXT",
    "task_id": "TEXT DEFAULT ''",
    "end_layer": "INTEGER",
    "duration_s": "REAL",
    "eta_total_s": "REAL",
    "release_film": "INTEGER",
    "heat_status": "INTEGER",
    "target_tank_temp_c": "REAL",
    "vat_temp_min_c": "REAL",
    "vat_temp_max_c": "REAL",
    "vat_temp_avg_c": "REAL",
    "uv_temp_min_c": "REAL",
    "uv_temp_max_c": "REAL",
    "uv_temp_avg_c": "REAL",
    "error_number": "INTEGER DEFAULT 0",
    "error_message": "TEXT DEFAULT ''",
    "print_substatus": "TEXT DEFAULT ''",
    "exposure_from_file_s": "REAL",
    "telemetry_json": "TEXT DEFAULT '{}'",
    "share_key": "TEXT DEFAULT ''",
    "preview_path": "TEXT DEFAULT ''",
    "part_application": "TEXT DEFAULT ''",
    "failure_issue": "TEXT DEFAULT ''",
    "quality_json": "TEXT DEFAULT '{}'",
}

_EXTRA_JOB_COLUMNS: dict[str, str] = {
    "task_id": "TEXT DEFAULT ''",
    "duration_s": "REAL",
    "eta_total_s": "REAL",
    "release_film": "INTEGER",
    "heat_status": "INTEGER",
    "target_tank_temp_c": "REAL",
    "vat_temp_min_c": "REAL",
    "vat_temp_max_c": "REAL",
    "uv_temp_min_c": "REAL",
    "uv_temp_max_c": "REAL",
    "temp_sample_count": "INTEGER DEFAULT 0",
    "vat_temp_sum": "REAL DEFAULT 0",
    "uv_temp_sum": "REAL DEFAULT 0",
    "error_number": "INTEGER DEFAULT 0",
    "error_message": "TEXT DEFAULT ''",
    "exposure_from_file_s": "REAL",
    "telemetry_json": "TEXT DEFAULT '{}'",
    "preview_path": "TEXT DEFAULT ''",
    "part_application": "TEXT DEFAULT ''",
    "thumbnail_url": "TEXT DEFAULT ''",
}


SETTINGS_FROM_PRINTER = "printer"
SETTINGS_FROM_HAND = "manual"

# key -> (type, min, max, label) for settings that can be corrected on a logged run
EDITABLE_RUN_SETTINGS: dict[str, tuple[type, float, float, str]] = {
    "layer_height": (float, 0.01, 0.2, "Layer height (mm)"),
    "normal_exposure_time": (float, 0.1, 60.0, "Layer exposure (s)"),
    "bottom_exposure_time": (float, 1.0, 300.0, "Bottom exposure (s)"),
    "bottom_layer_count": (int, 0, 100, "Bottom layers"),
    "transition_layer_count": (int, 0, 100, "Transition layers"),
}


def slice_info_telemetry(slice_info: dict[str, Any], filename_check: list[str]) -> dict[str, Any]:
    return {
        "settings_source": SETTINGS_FROM_PRINTER,
        "slice_info": slice_info,
        "filename_check": filename_check,
    }


@dataclass
class HistoryRecord:
    id: int
    printer_id: str
    resin_id: str
    layer_height: float
    vat_temperature_c: float
    settings: PrintSettings
    created_at: str
    notes: str = ""
    outcome: PrintOutcome = PrintOutcome.SUCCESS
    geometry_class: GeometryClass = GeometryClass.NORMAL
    cross_section_mm2: float = 0.0
    filename: str = ""
    uv_led_temp_c: float | None = None
    total_layers: int | None = None
    source: str = "manual"
    started_at: str = ""
    ended_at: str = ""
    task_id: str = ""
    end_layer: int | None = None
    duration_s: float | None = None
    eta_total_s: float | None = None
    release_film: int | None = None
    heat_status: int | None = None
    target_tank_temp_c: float | None = None
    vat_temp_min_c: float | None = None
    vat_temp_max_c: float | None = None
    vat_temp_avg_c: float | None = None
    uv_temp_min_c: float | None = None
    uv_temp_max_c: float | None = None
    uv_temp_avg_c: float | None = None
    error_number: int = 0
    error_message: str = ""
    print_substatus: str = ""
    exposure_from_file_s: float | None = None
    telemetry: dict[str, Any] | None = None
    preview_path: str = ""
    part_application: str = ""
    failure_issue: str = ""
    quality: dict[str, bool] = field(default_factory=dict)


@dataclass
class OpenJob:
    printer_id: str
    filename: str = ""
    resin_id: str = ""
    layer_height: float = 0.05
    vat_temperature_c: float = 25.0
    uv_led_temp_c: float | None = None
    total_layers: int | None = None
    current_layer: int | None = None
    settings_json: str = ""
    geometry_class: str = GeometryClass.NORMAL.value
    cross_section_mm2: float = 0.0
    started_at: str = ""
    last_status: str = ""
    awaiting_outcome: bool = False
    print_substatus: str = ""
    task_id: str = ""
    duration_s: float | None = None
    eta_total_s: float | None = None
    release_film: int | None = None
    heat_status: int | None = None
    target_tank_temp_c: float | None = None
    vat_temp_min_c: float | None = None
    vat_temp_max_c: float | None = None
    uv_temp_min_c: float | None = None
    uv_temp_max_c: float | None = None
    temp_sample_count: int = 0
    vat_temp_sum: float = 0.0
    uv_temp_sum: float = 0.0
    error_number: int = 0
    error_message: str = ""
    exposure_from_file_s: float | None = None
    telemetry_json: str = "{}"
    preview_path: str = ""
    part_application: str = ""
    thumbnail_url: str = ""

    @property
    def settings(self) -> PrintSettings | None:
        if not self.settings_json:
            return None
        try:
            return PrintSettings.from_dict(json.loads(self.settings_json))
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            return None

    @property
    def vat_temp_avg_c(self) -> float | None:
        if self.temp_sample_count <= 0 or self.vat_temp_sum <= 0:
            # count vat samples separately via min being set
            return None
        # Approximate: only count polls where vat was present by using min presence
        if self.vat_temp_min_c is None:
            return None
        # Recompute properly from sum of vat-only samples stored in telemetry if needed
        tel = self.telemetry
        n = int(tel.get("vat_samples", 0) or 0)
        if n <= 0:
            return None
        return float(tel.get("vat_sum", 0.0)) / n

    @property
    def uv_temp_avg_c(self) -> float | None:
        tel = self.telemetry
        n = int(tel.get("uv_samples", 0) or 0)
        if n <= 0:
            return None
        return float(tel.get("uv_sum", 0.0)) / n

    @property
    def telemetry(self) -> dict[str, Any]:
        try:
            data = json.loads(self.telemetry_json or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @property
    def settings_from_printer(self) -> bool:
        return self.telemetry.get("settings_source") == SETTINGS_FROM_PRINTER

    def apply_printer_settings(
        self, settings: PrintSettings, slice_info: dict[str, Any], filename_check: list[str]
    ) -> None:
        """Replace app-estimated settings with what the printer says the sliced file uses."""
        tel = self.telemetry
        tel.update(slice_info_telemetry(slice_info, filename_check))
        self.telemetry_json = json.dumps(tel)
        self.settings_json = json.dumps(settings.to_dict())
        if settings.layer_height:
            self.layer_height = settings.layer_height

    def record_sample(self, live: Any) -> None:
        """Accumulate SDCP poll into running min/max/avg + sample trail."""
        snap = live.telemetry_snapshot() if hasattr(live, "telemetry_snapshot") else {}
        tel = self.telemetry
        samples = list(tel.get("samples") or [])
        # Keep a capped trail so DB stays small (~every poll, max ~90 = 30 min @20s)
        samples.append(
            {
                "t": datetime.now(timezone.utc).isoformat(),
                "layer": snap.get("current_layer"),
                "vat": snap.get("vat_temp_c"),
                "uv": snap.get("uv_led_temp_c"),
                "sub": snap.get("print_substatus"),
                "ticks": snap.get("current_ticks"),
            }
        )
        if len(samples) > 120:
            samples = samples[-120:]
        tel["samples"] = samples
        tel["last"] = snap

        vat = snap.get("vat_temp_c")
        if vat is not None:
            tel["vat_samples"] = int(tel.get("vat_samples", 0)) + 1
            tel["vat_sum"] = float(tel.get("vat_sum", 0.0)) + float(vat)
            self.vat_temp_min_c = float(vat) if self.vat_temp_min_c is None else min(self.vat_temp_min_c, float(vat))
            self.vat_temp_max_c = float(vat) if self.vat_temp_max_c is None else max(self.vat_temp_max_c, float(vat))
            self.vat_temperature_c = float(vat)

        uv = snap.get("uv_led_temp_c")
        if uv is not None:
            tel["uv_samples"] = int(tel.get("uv_samples", 0)) + 1
            tel["uv_sum"] = float(tel.get("uv_sum", 0.0)) + float(uv)
            self.uv_temp_min_c = float(uv) if self.uv_temp_min_c is None else min(self.uv_temp_min_c, float(uv))
            self.uv_temp_max_c = float(uv) if self.uv_temp_max_c is None else max(self.uv_temp_max_c, float(uv))
            self.uv_led_temp_c = float(uv)

        self.temp_sample_count = int(tel.get("vat_samples", 0)) + int(tel.get("uv_samples", 0))
        self.telemetry_json = json.dumps(tel)

        if snap.get("filename"):
            self.filename = str(snap["filename"])
        if snap.get("task_id"):
            self.task_id = str(snap["task_id"])
        if snap.get("total_layers") is not None:
            self.total_layers = int(snap["total_layers"])
        if snap.get("current_layer") is not None:
            self.current_layer = int(snap["current_layer"])
        if snap.get("duration_s") is not None:
            self.duration_s = float(snap["duration_s"])
        if snap.get("eta_total_s") is not None:
            self.eta_total_s = float(snap["eta_total_s"])
        if snap.get("release_film") is not None:
            self.release_film = int(snap["release_film"])
        if snap.get("heat_status") is not None:
            self.heat_status = int(snap["heat_status"])
        if snap.get("target_tank_temp_c") is not None:
            self.target_tank_temp_c = float(snap["target_tank_temp_c"])
        if snap.get("print_substatus"):
            self.print_substatus = str(snap["print_substatus"])
        if snap.get("error_number"):
            self.error_number = int(snap["error_number"])
        if snap.get("error_message"):
            self.error_message = str(snap["error_message"])
        self.last_status = str(snap.get("status_kind") or self.last_status)


@dataclass
class PendingJob:
    """A finished SDCP print that was never reported before the next one started."""

    id: int
    parked_at: str
    job: OpenJob

    @property
    def finished_at(self) -> str:
        if self.job.started_at and self.job.duration_s:
            try:
                start = datetime.fromisoformat(self.job.started_at)
                return (start + timedelta(seconds=float(self.job.duration_s))).isoformat()
            except ValueError:
                pass
        return self.parked_at


def apply_slice_to_job(job: OpenJob, slice_info: dict[str, Any]) -> bool:
    """Adopt printer-reported slice settings for a tracked job. Returns True when changed."""
    if job.settings_from_printer:
        return False
    settings = settings_from_slice_info(slice_info)
    if settings is None:
        return False
    job.apply_printer_settings(settings, slice_info, filename_mismatches(job.filename, settings))
    return True


def is_new_print(job: OpenJob, live: Any) -> bool:
    """True when the printer started a different print than the tracked job (same file reprints too)."""
    filename = getattr(live, "filename", "") or ""
    task_id = getattr(live, "task_id", "") or ""
    if filename and filename != job.filename:
        return True
    return bool(task_id and job.task_id and task_id != job.task_id)


def _retag_notes(notes: str, old: PrintOutcome, new: PrintOutcome) -> str:
    """Swap the outcome word in auto-generated prefixes like 'SDCP fail — file'."""
    head, sep, rest = (notes or "").partition(" — ")
    words = head.split(" ")
    if len(words) == 2 and words[1] == old.value:
        head = f"{words[0]} {new.value}"
    return head + sep + rest


class HistoryStore:
    def __init__(
        self,
        db_path: Path | None = None,
        team_file: Path | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else user_path(DB_NAME)
        self.team_file = Path(team_file) if team_file is not None else self.db_path.parent / JOURNAL_NAME
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.sync_team_data()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def make_share_key(
        printer_id: str,
        resin_id: str,
        created_at: str,
        outcome: str,
        filename: str,
        layer_height: float,
        normal_exposure: float,
        notes: str = "",
    ) -> str:
        raw = "|".join(
            [
                printer_id,
                resin_id,
                created_at,
                outcome,
                filename or "",
                f"{layer_height:.4f}",
                f"{normal_exposure:.3f}",
                (notes or "")[:80],
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    def sync_team_data(self) -> dict[str, int]:
        """Pull shared JSONL into local DB, then export any local-only runs."""
        imported = self._import_team_runs()
        exported = self._export_missing_to_team()
        return {"imported": imported, "exported": exported}

    def _known_share_keys(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT share_key FROM print_runs WHERE share_key IS NOT NULL AND share_key != ''"
            ).fetchall()
        return {str(r[0]) for r in rows}

    def _team_keys_on_disk(self) -> set[str]:
        keys: set[str] = set()
        if not self.team_file.is_file():
            return keys
        for line in self.team_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(obj.get("share_key") or "")
            if key:
                keys.add(key)
        return keys

    def _deleted_share_keys(self) -> set[str]:
        keys: set[str] = set()
        if not self.team_file.is_file():
            return keys
        for line in self.team_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("delete"):
                keys.add(str(obj["delete"]))
        return keys

    def delete_runs(self, run_ids: list[int], reason: str = "") -> int:
        """Remove runs (e.g. test / fake prints) locally and for everyone syncing the team file."""
        if not run_ids:
            return 0
        marks = ",".join("?" for _ in run_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, share_key FROM print_runs WHERE id IN ({marks})", run_ids
            ).fetchall()
            conn.execute(f"DELETE FROM print_runs WHERE id IN ({marks})", run_ids)
            conn.commit()
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            if row["share_key"]:
                self._append_team_payload(
                    {"delete": row["share_key"], "reason": reason, "deleted_at": now}
                )
        return len(rows)

    def _append_team_payload(self, payload: dict[str, Any]) -> None:
        self.team_file.parent.mkdir(parents=True, exist_ok=True)
        with self.team_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _record_to_team_payload(self, rec: HistoryRecord) -> dict[str, Any]:
        key = self.make_share_key(
            rec.printer_id,
            rec.resin_id,
            rec.created_at,
            rec.outcome.value,
            rec.filename,
            rec.layer_height,
            rec.settings.normal_exposure_time,
            rec.notes,
        )
        return {
            "share_key": key,
            "printer_id": rec.printer_id,
            "resin_id": rec.resin_id,
            "layer_height": rec.layer_height,
            "vat_temperature_c": rec.vat_temperature_c,
            "geometry_class": rec.geometry_class.value,
            "cross_section_mm2": rec.cross_section_mm2,
            "outcome": rec.outcome.value,
            "settings": rec.settings.to_dict(),
            "notes": rec.notes,
            "created_at": rec.created_at,
            "filename": rec.filename,
            "uv_led_temp_c": rec.uv_led_temp_c,
            "total_layers": rec.total_layers,
            "source": rec.source,
            "started_at": rec.started_at,
            "ended_at": rec.ended_at,
            "task_id": rec.task_id,
            "end_layer": rec.end_layer,
            "duration_s": rec.duration_s,
            "eta_total_s": rec.eta_total_s,
            "release_film": rec.release_film,
            "heat_status": rec.heat_status,
            "target_tank_temp_c": rec.target_tank_temp_c,
            "vat_temp_min_c": rec.vat_temp_min_c,
            "vat_temp_max_c": rec.vat_temp_max_c,
            "vat_temp_avg_c": rec.vat_temp_avg_c,
            "uv_temp_min_c": rec.uv_temp_min_c,
            "uv_temp_max_c": rec.uv_temp_max_c,
            "uv_temp_avg_c": rec.uv_temp_avg_c,
            "error_number": rec.error_number,
            "error_message": rec.error_message,
            "print_substatus": rec.print_substatus,
            "exposure_from_file_s": rec.exposure_from_file_s,
            "telemetry": rec.telemetry or {},
            "preview_path": rec.preview_path,
            "part_application": rec.part_application,
            "failure_issue": rec.failure_issue,
            "quality": rec.quality,
        }

    def _import_team_runs(self) -> int:
        if not self.team_file.is_file():
            return 0
        known = self._known_share_keys()
        deleted = self._deleted_share_keys()
        if deleted & known:
            with self._connect() as conn:
                conn.executemany(
                    "DELETE FROM print_runs WHERE share_key = ?", [(k,) for k in deleted & known]
                )
                conn.commit()
            known -= deleted
        imported = 0
        amendments: list[dict[str, Any]] = []
        for line in self.team_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("amend") or obj.get("amend_settings"):
                amendments.append(obj)
                continue
            if obj.get("delete"):
                continue
            key = str(obj.get("share_key") or "")
            if not key or key in known or key in deleted:
                continue
            try:
                settings = PrintSettings.from_dict(obj.get("settings") or {})
                outcome = PrintOutcome(str(obj.get("outcome") or "success"))
                geometry = GeometryClass(str(obj.get("geometry_class") or "normal"))
            except (TypeError, ValueError, KeyError):
                continue
            self._insert_run_raw(
                printer_id=str(obj.get("printer_id") or ""),
                resin_id=str(obj.get("resin_id") or ""),
                layer_height=float(obj.get("layer_height") or 0.05),
                vat_temperature_c=float(obj.get("vat_temperature_c") or 25.0),
                settings=settings,
                outcome=outcome,
                geometry_class=geometry,
                cross_section_mm2=float(obj.get("cross_section_mm2") or 0.0),
                notes=str(obj.get("notes") or ""),
                created_at=str(obj.get("created_at") or datetime.now(timezone.utc).isoformat()),
                filename=str(obj.get("filename") or ""),
                uv_led_temp_c=obj.get("uv_led_temp_c"),
                total_layers=obj.get("total_layers"),
                source=str(obj.get("source") or "team"),
                started_at=str(obj.get("started_at") or ""),
                ended_at=str(obj.get("ended_at") or ""),
                task_id=str(obj.get("task_id") or ""),
                end_layer=obj.get("end_layer"),
                duration_s=obj.get("duration_s"),
                eta_total_s=obj.get("eta_total_s"),
                release_film=obj.get("release_film"),
                heat_status=obj.get("heat_status"),
                target_tank_temp_c=obj.get("target_tank_temp_c"),
                vat_temp_min_c=obj.get("vat_temp_min_c"),
                vat_temp_max_c=obj.get("vat_temp_max_c"),
                vat_temp_avg_c=obj.get("vat_temp_avg_c"),
                uv_temp_min_c=obj.get("uv_temp_min_c"),
                uv_temp_max_c=obj.get("uv_temp_max_c"),
                uv_temp_avg_c=obj.get("uv_temp_avg_c"),
                error_number=int(obj.get("error_number") or 0),
                error_message=str(obj.get("error_message") or ""),
                print_substatus=str(obj.get("print_substatus") or ""),
                exposure_from_file_s=obj.get("exposure_from_file_s"),
                telemetry=obj.get("telemetry") if isinstance(obj.get("telemetry"), dict) else {},
                share_key=key,
                preview_path=str(obj.get("preview_path") or ""),
                part_application=str(obj.get("part_application") or ""),
                failure_issue=str(obj.get("failure_issue") or ""),
                quality=clean_quality(obj.get("quality")),
            )
            known.add(key)
            imported += 1
        # Later lines win, so replaying every amendment in file order is idempotent.
        for obj in amendments:
            self._apply_amendment(obj)
        return imported

    def _apply_amendment(self, obj: dict[str, Any]) -> None:
        if obj.get("amend_settings"):
            self._apply_settings_amendment(obj)
            return
        try:
            outcome = PrintOutcome(str(obj.get("outcome") or ""))
        except ValueError:
            return
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE print_runs
                SET outcome = ?, failure_issue = ?, quality_json = ?, notes = ?, part_application = ?
                WHERE share_key = ?
                """,
                (
                    outcome.value,
                    str(obj.get("failure_issue") or ""),
                    json.dumps(clean_quality(obj.get("quality"))),
                    str(obj.get("notes") or ""),
                    str(obj.get("part_application") or ""),
                    str(obj["amend"]),
                ),
            )
            conn.commit()

    def _export_missing_to_team(self) -> int:
        on_disk = self._team_keys_on_disk()
        exported = 0
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM print_runs ORDER BY id ASC").fetchall()
        for row in rows:
            rec = self._row_to_record(row)
            payload = self._record_to_team_payload(rec)
            # Amended runs must keep their original key or teammates re-import the old line as a duplicate.
            stored = row["share_key"] if "share_key" in row.keys() else ""
            if stored:
                payload["share_key"] = stored
            key = payload["share_key"]
            if key in on_disk:
                # Backfill empty share_key in DB
                if not (row["share_key"] if "share_key" in row.keys() else ""):
                    with self._connect() as conn:
                        conn.execute(
                            "UPDATE print_runs SET share_key = ? WHERE id = ?",
                            (key, rec.id),
                        )
                        conn.commit()
                continue
            self._append_team_payload(payload)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE print_runs SET share_key = ? WHERE id = ?",
                    (key, rec.id),
                )
                conn.commit()
            on_disk.add(key)
            exported += 1
        return exported

    def _insert_run_raw(
        self,
        *,
        printer_id: str,
        resin_id: str,
        layer_height: float,
        vat_temperature_c: float,
        settings: PrintSettings,
        outcome: PrintOutcome,
        geometry_class: GeometryClass,
        cross_section_mm2: float,
        notes: str,
        created_at: str,
        filename: str,
        uv_led_temp_c: Any,
        total_layers: Any,
        source: str,
        started_at: str,
        ended_at: str,
        task_id: str,
        end_layer: Any,
        duration_s: Any,
        eta_total_s: Any,
        release_film: Any,
        heat_status: Any,
        target_tank_temp_c: Any,
        vat_temp_min_c: Any,
        vat_temp_max_c: Any,
        vat_temp_avg_c: Any,
        uv_temp_min_c: Any,
        uv_temp_max_c: Any,
        uv_temp_avg_c: Any,
        error_number: int,
        error_message: str,
        print_substatus: str,
        exposure_from_file_s: Any,
        telemetry: dict[str, Any],
        share_key: str,
        preview_path: str = "",
        part_application: str = "",
        failure_issue: str = "",
        quality: dict[str, bool] | None = None,
    ) -> int:
        payload = json.dumps(settings.to_dict())
        tel = json.dumps(telemetry or {})
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO print_runs
                (printer_id, resin_id, layer_height, vat_temperature_c,
                 geometry_class, cross_section_mm2, outcome, settings_json, notes, created_at,
                 filename, uv_led_temp_c, total_layers, source, started_at, ended_at,
                 task_id, end_layer, duration_s, eta_total_s, release_film, heat_status,
                 target_tank_temp_c, vat_temp_min_c, vat_temp_max_c, vat_temp_avg_c,
                 uv_temp_min_c, uv_temp_max_c, uv_temp_avg_c, error_number, error_message,
                 print_substatus, exposure_from_file_s, telemetry_json, share_key,
                 preview_path, part_application, failure_issue, quality_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    printer_id,
                    resin_id,
                    layer_height,
                    vat_temperature_c,
                    geometry_class.value,
                    float(cross_section_mm2),
                    outcome.value,
                    payload,
                    notes,
                    created_at,
                    filename or "",
                    uv_led_temp_c,
                    total_layers,
                    source,
                    started_at or "",
                    ended_at or created_at,
                    task_id or "",
                    end_layer,
                    duration_s,
                    eta_total_s,
                    release_film,
                    heat_status,
                    target_tank_temp_c,
                    vat_temp_min_c,
                    vat_temp_max_c,
                    vat_temp_avg_c,
                    uv_temp_min_c,
                    uv_temp_max_c,
                    uv_temp_avg_c,
                    int(error_number or 0),
                    error_message or "",
                    print_substatus or "",
                    exposure_from_file_s,
                    tel,
                    share_key,
                    preview_path or "",
                    part_application or "",
                    failure_issue or "",
                    json.dumps(clean_quality(quality or {})),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS print_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_id TEXT NOT NULL,
                    resin_id TEXT NOT NULL,
                    layer_height REAL NOT NULL,
                    vat_temperature_c REAL NOT NULL,
                    geometry_class TEXT NOT NULL DEFAULT 'normal',
                    cross_section_mm2 REAL NOT NULL DEFAULT 0,
                    outcome TEXT NOT NULL DEFAULT 'success',
                    settings_json TEXT NOT NULL,
                    notes TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runs_printer
                ON print_runs (printer_id, created_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_runs_lookup
                ON print_runs (printer_id, resin_id, outcome, layer_height)
                """
            )
            self._ensure_columns(conn, "print_runs", _EXTRA_RUN_COLUMNS)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS open_jobs (
                    printer_id TEXT PRIMARY KEY,
                    filename TEXT DEFAULT '',
                    resin_id TEXT DEFAULT '',
                    layer_height REAL DEFAULT 0.05,
                    vat_temperature_c REAL DEFAULT 25.0,
                    uv_led_temp_c REAL,
                    total_layers INTEGER,
                    current_layer INTEGER,
                    settings_json TEXT DEFAULT '',
                    geometry_class TEXT DEFAULT 'normal',
                    cross_section_mm2 REAL DEFAULT 0,
                    started_at TEXT DEFAULT '',
                    last_status TEXT DEFAULT '',
                    awaiting_outcome INTEGER DEFAULT 0,
                    print_substatus TEXT DEFAULT ''
                )
                """
            )
            self._ensure_columns(conn, "open_jobs", _EXTRA_JOB_COLUMNS)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS unreported_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    printer_id TEXT NOT NULL,
                    parked_at TEXT NOT NULL,
                    job_json TEXT NOT NULL
                )
                """
            )
            self._migrate_legacy(conn)
            conn.commit()

    def _ensure_columns(
        self, conn: sqlite3.Connection, table: str, columns: dict[str, str]
    ) -> None:
        existing = {
            r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _migrate_legacy(self, conn: sqlite3.Connection) -> None:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "successful_prints" not in tables:
            return
        count = conn.execute("SELECT COUNT(*) FROM print_runs").fetchone()[0]
        if count:
            return
        legacy = conn.execute("SELECT * FROM successful_prints").fetchall()
        for row in legacy:
            conn.execute(
                """
                INSERT INTO print_runs
                (printer_id, resin_id, layer_height, vat_temperature_c,
                 geometry_class, cross_section_mm2, outcome, settings_json, notes, created_at)
                VALUES (?, ?, ?, ?, 'normal', 0, 'success', ?, ?, ?)
                """,
                (
                    row["printer_id"],
                    row["resin_id"],
                    row["layer_height"],
                    row["vat_temperature_c"],
                    row["settings_json"],
                    row["notes"] or "",
                    row["created_at"],
                ),
            )

    def log_run(
        self,
        printer_id: str,
        resin_id: str,
        layer_height: float,
        vat_temperature_c: float,
        settings: PrintSettings,
        outcome: PrintOutcome,
        geometry_class: GeometryClass = GeometryClass.NORMAL,
        cross_section_mm2: float = 0.0,
        notes: str = "",
        filename: str = "",
        uv_led_temp_c: float | None = None,
        total_layers: int | None = None,
        source: str = "manual",
        started_at: str = "",
        ended_at: str = "",
        task_id: str = "",
        end_layer: int | None = None,
        duration_s: float | None = None,
        eta_total_s: float | None = None,
        release_film: int | None = None,
        heat_status: int | None = None,
        target_tank_temp_c: float | None = None,
        vat_temp_min_c: float | None = None,
        vat_temp_max_c: float | None = None,
        vat_temp_avg_c: float | None = None,
        uv_temp_min_c: float | None = None,
        uv_temp_max_c: float | None = None,
        uv_temp_avg_c: float | None = None,
        error_number: int = 0,
        error_message: str = "",
        print_substatus: str = "",
        exposure_from_file_s: float | None = None,
        telemetry: dict[str, Any] | None = None,
        sync_team: bool = True,
        preview_path: str = "",
        part_application: str = "",
        failure_issue: str = "",
        quality: dict[str, bool] | None = None,
        created_at: str = "",
    ) -> int:
        created_at = created_at or datetime.now(timezone.utc).isoformat()
        quality = clean_quality(quality or {})
        share_key = self.make_share_key(
            printer_id,
            resin_id,
            created_at,
            outcome.value,
            filename or "",
            layer_height,
            settings.normal_exposure_time,
            notes,
        )
        run_id = self._insert_run_raw(
            printer_id=printer_id,
            resin_id=resin_id,
            layer_height=layer_height,
            vat_temperature_c=vat_temperature_c,
            settings=settings,
            outcome=outcome,
            geometry_class=geometry_class,
            cross_section_mm2=cross_section_mm2,
            notes=notes,
            created_at=created_at,
            filename=filename or "",
            uv_led_temp_c=uv_led_temp_c,
            total_layers=total_layers,
            source=source,
            started_at=started_at or "",
            ended_at=ended_at or created_at,
            task_id=task_id or "",
            end_layer=end_layer,
            duration_s=duration_s,
            eta_total_s=eta_total_s,
            release_film=release_film,
            heat_status=heat_status,
            target_tank_temp_c=target_tank_temp_c,
            vat_temp_min_c=vat_temp_min_c,
            vat_temp_max_c=vat_temp_max_c,
            vat_temp_avg_c=vat_temp_avg_c,
            uv_temp_min_c=uv_temp_min_c,
            uv_temp_max_c=uv_temp_max_c,
            uv_temp_avg_c=uv_temp_avg_c,
            error_number=int(error_number or 0),
            error_message=error_message or "",
            print_substatus=print_substatus or "",
            exposure_from_file_s=exposure_from_file_s,
            telemetry=telemetry or {},
            share_key=share_key,
            preview_path=preview_path,
            part_application=part_application,
            failure_issue=failure_issue,
            quality=quality,
        )
        if sync_team and share_key not in self._team_keys_on_disk():
            self._append_team_payload(
                {
                    "share_key": share_key,
                    "printer_id": printer_id,
                    "resin_id": resin_id,
                    "layer_height": layer_height,
                    "vat_temperature_c": vat_temperature_c,
                    "geometry_class": geometry_class.value,
                    "cross_section_mm2": float(cross_section_mm2),
                    "outcome": outcome.value,
                    "settings": settings.to_dict(),
                    "notes": notes,
                    "created_at": created_at,
                    "filename": filename or "",
                    "uv_led_temp_c": uv_led_temp_c,
                    "total_layers": total_layers,
                    "source": source,
                    "started_at": started_at or "",
                    "ended_at": ended_at or created_at,
                    "task_id": task_id or "",
                    "end_layer": end_layer,
                    "duration_s": duration_s,
                    "eta_total_s": eta_total_s,
                    "release_film": release_film,
                    "heat_status": heat_status,
                    "target_tank_temp_c": target_tank_temp_c,
                    "vat_temp_min_c": vat_temp_min_c,
                    "vat_temp_max_c": vat_temp_max_c,
                    "vat_temp_avg_c": vat_temp_avg_c,
                    "uv_temp_min_c": uv_temp_min_c,
                    "uv_temp_max_c": uv_temp_max_c,
                    "uv_temp_avg_c": uv_temp_avg_c,
                    "error_number": int(error_number or 0),
                    "error_message": error_message or "",
                    "print_substatus": print_substatus or "",
                    "exposure_from_file_s": exposure_from_file_s,
                    "telemetry": telemetry or {},
                    "preview_path": preview_path or "",
                    "part_application": part_application or "",
                    "failure_issue": failure_issue or "",
                    "quality": quality,
                }
            )
        return run_id

    def log_success(
        self,
        printer_id: str,
        resin_id: str,
        layer_height: float,
        vat_temperature_c: float,
        settings: PrintSettings,
        notes: str = "",
        geometry_class: GeometryClass = GeometryClass.NORMAL,
        cross_section_mm2: float = 0.0,
    ) -> int:
        return self.log_run(
            printer_id=printer_id,
            resin_id=resin_id,
            layer_height=layer_height,
            vat_temperature_c=vat_temperature_c,
            settings=settings,
            outcome=PrintOutcome.SUCCESS,
            geometry_class=geometry_class,
            cross_section_mm2=cross_section_mm2,
            notes=notes,
        )

    def _row_to_record(self, row: sqlite3.Row) -> HistoryRecord:
        data: dict[str, Any] = json.loads(row["settings_json"])
        keys = row.keys()

        def _f(name: str) -> float | None:
            if name not in keys:
                return None
            val = row[name]
            return float(val) if val is not None else None

        def _i(name: str) -> int | None:
            if name not in keys:
                return None
            val = row[name]
            return int(val) if val is not None else None

        def _s(name: str, default: str = "") -> str:
            if name not in keys:
                return default
            return str(row[name] or default)

        geom_raw = row["geometry_class"] if "geometry_class" in keys else "normal"
        outcome_raw = row["outcome"] if "outcome" in keys else "success"
        try:
            geometry = GeometryClass(geom_raw)
        except ValueError:
            geometry = GeometryClass.NORMAL
        try:
            outcome = PrintOutcome(outcome_raw)
        except ValueError:
            outcome = PrintOutcome.SUCCESS
        cs = float(row["cross_section_mm2"]) if "cross_section_mm2" in keys else 0.0
        tel: dict[str, Any] | None = None
        if "telemetry_json" in keys and row["telemetry_json"]:
            try:
                parsed = json.loads(row["telemetry_json"])
                tel = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                tel = None
        return HistoryRecord(
            id=int(row["id"]),
            printer_id=row["printer_id"],
            resin_id=row["resin_id"],
            layer_height=float(row["layer_height"]),
            vat_temperature_c=float(row["vat_temperature_c"]),
            settings=PrintSettings.from_dict(data),
            created_at=row["created_at"],
            notes=row["notes"] or "",
            outcome=outcome,
            geometry_class=geometry,
            cross_section_mm2=cs,
            filename=_s("filename"),
            uv_led_temp_c=_f("uv_led_temp_c"),
            total_layers=_i("total_layers"),
            source=_s("source", "manual"),
            started_at=_s("started_at"),
            ended_at=_s("ended_at"),
            task_id=_s("task_id"),
            end_layer=_i("end_layer"),
            duration_s=_f("duration_s"),
            eta_total_s=_f("eta_total_s"),
            release_film=_i("release_film"),
            heat_status=_i("heat_status"),
            target_tank_temp_c=_f("target_tank_temp_c"),
            vat_temp_min_c=_f("vat_temp_min_c"),
            vat_temp_max_c=_f("vat_temp_max_c"),
            vat_temp_avg_c=_f("vat_temp_avg_c"),
            uv_temp_min_c=_f("uv_temp_min_c"),
            uv_temp_max_c=_f("uv_temp_max_c"),
            uv_temp_avg_c=_f("uv_temp_avg_c"),
            error_number=int(_i("error_number") or 0),
            error_message=_s("error_message"),
            print_substatus=_s("print_substatus"),
            exposure_from_file_s=_f("exposure_from_file_s"),
            telemetry=tel,
            preview_path=_s("preview_path"),
            part_application=_s("part_application"),
            failure_issue=_s("failure_issue"),
            quality=clean_quality(_s("quality_json", "{}")),
        )

    def list_for_printer(
        self,
        printer_id: str,
        resin_id: str | None = None,
        outcome: PrintOutcome | None = None,
        limit: int = 200,
    ) -> list[HistoryRecord]:
        clauses = ["printer_id = ?"]
        params: list[Any] = [printer_id]
        if resin_id:
            clauses.append("resin_id = ?")
            params.append(resin_id)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome.value)
        params.append(limit)
        where = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM print_runs
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_recent(
        self,
        printer_id: str | None = None,
        resin_id: str | None = None,
        limit: int = 20,
        outcome: PrintOutcome | None = PrintOutcome.SUCCESS,
    ) -> list[HistoryRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if printer_id:
            clauses.append("printer_id = ?")
            params.append(printer_id)
        if resin_id:
            clauses.append("resin_id = ?")
            params.append(resin_id)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM print_runs
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_successes(
        self,
        printer_id: str,
        resin_id: str | None = None,
        limit: int = 500,
    ) -> list[HistoryRecord]:
        return self.list_for_printer(
            printer_id, resin_id=resin_id, outcome=PrintOutcome.SUCCESS, limit=limit
        )

    def find_exact(
        self,
        printer_id: str,
        resin_id: str,
        layer_height: float,
        vat_temperature_c: float,
        geometry_class: GeometryClass = GeometryClass.NORMAL,
        temp_tol: float = 1.0,
        layer_tol: float = 0.0005,
    ) -> HistoryRecord | None:
        rows = self.list_successes(printer_id, resin_id, limit=200)
        for rec in rows:
            if (
                abs(rec.layer_height - layer_height) <= layer_tol
                and abs(rec.vat_temperature_c - vat_temperature_c) <= temp_tol
                and rec.geometry_class == geometry_class
            ):
                return rec
        return None

    def find_nearest_layer(
        self,
        printer_id: str,
        resin_id: str,
        layer_height: float,
        geometry_class: GeometryClass | None = None,
    ) -> HistoryRecord | None:
        rows = self.list_successes(printer_id, resin_id, limit=200)
        if geometry_class is not None:
            same_geom = [r for r in rows if r.geometry_class == geometry_class]
            if same_geom:
                rows = same_geom
        if not rows:
            return None
        best: HistoryRecord | None = None
        best_dist = float("inf")
        for rec in rows:
            dist = abs(rec.layer_height - layer_height)
            if dist < best_dist:
                best_dist = dist
                best = rec
        return best

    def printer_stats(self, printer_id: str) -> dict[str, Any]:
        rows = self.list_for_printer(printer_id, limit=1000)
        total = len(rows)
        successes = sum(1 for r in rows if r.outcome == PrintOutcome.SUCCESS)
        fails = sum(1 for r in rows if r.outcome == PrintOutcome.FAIL)
        partial = sum(1 for r in rows if r.outcome == PrintOutcome.PARTIAL)
        return {
            "total": total,
            "success": successes,
            "fail": fails,
            "partial": partial,
            "success_rate": (successes / total) if total else 0.0,
        }

    def _apply_settings_amendment(self, obj: dict[str, Any]) -> None:
        try:
            settings = PrintSettings.from_dict(obj.get("settings") or {})
        except (TypeError, ValueError):
            return
        share_key = str(obj.get("amend_settings") or "")
        if not share_key:
            return
        with self._connect() as conn:
            row = conn.execute(
                "SELECT telemetry_json FROM print_runs WHERE share_key = ?", (share_key,)
            ).fetchone()
            if row is None:
                return
            try:
                tel = json.loads(row["telemetry_json"] or "{}")
            except json.JSONDecodeError:
                tel = {}
            if not isinstance(tel, dict):
                tel = {}
            if obj.get("source") == SETTINGS_FROM_HAND:
                tel["settings_source"] = SETTINGS_FROM_HAND
                edits = dict(tel.get("manual_edits") or {})
                edits.update(obj.get("edited") or {})
                tel["manual_edits"] = edits
            elif obj.get("source") != "":
                tel.update(
                    slice_info_telemetry(
                        obj.get("slice_info") or {}, list(obj.get("filename_check") or [])
                    )
                )
            if obj.get("resin_id"):
                conn.execute(
                    "UPDATE print_runs SET resin_id = ? WHERE share_key = ?",
                    (str(obj["resin_id"]), share_key),
                )
            conn.execute(
                """
                UPDATE print_runs SET settings_json = ?, layer_height = ?, telemetry_json = ?
                WHERE share_key = ?
                """,
                (
                    json.dumps(settings.to_dict()),
                    settings.layer_height,
                    json.dumps(tel),
                    share_key,
                ),
            )
            conn.commit()

    def set_run_printer_settings(
        self,
        run_id: int,
        settings: PrintSettings,
        slice_info: dict[str, Any],
        filename_check: list[str],
    ) -> None:
        """Overwrite a logged run's settings with the printer-reported slice parameters."""
        share_key = self._share_key_for(run_id)
        payload = {
            "amend_settings": share_key,
            "settings": settings.to_dict(),
            "slice_info": slice_info,
            "filename_check": filename_check,
            "amended_at": datetime.now(timezone.utc).isoformat(),
        }
        self._apply_settings_amendment(payload)
        if share_key:
            self._append_team_payload(payload)

    def edit_run_settings(self, run_id: int, changes: dict[str, Any]) -> HistoryRecord:
        """Hand-correct a logged run's print settings (e.g. a wrong bottom exposure)."""
        rec = self.get_run(run_id)
        if rec is None:
            raise ValueError(f"Run #{run_id} not found")
        current = rec.settings.to_dict()
        edited: dict[str, Any] = {}
        changes = dict(changes or {})
        new_resin = str(changes.pop("resin_id", "") or "").strip()
        resin_changed = bool(new_resin) and new_resin != rec.resin_id
        for key, raw in changes.items():
            if key not in EDITABLE_RUN_SETTINGS or raw is None or raw == "":
                continue
            kind, low, high, label = EDITABLE_RUN_SETTINGS[key]
            try:
                value = kind(float(raw))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label} must be a number") from exc
            if not low <= value <= high:
                raise ValueError(f"{label} must be between {low:g} and {high:g}")
            if abs(float(value) - float(current.get(key) or 0)) > 1e-9:
                edited[key] = value
        if not edited and not resin_changed:
            return rec
        current.update(edited)
        share_key = self._share_key_for(run_id)
        payload: dict[str, Any] = {
            "amend_settings": share_key,
            "source": SETTINGS_FROM_HAND if edited else "",
            "settings": current,
            "edited": edited,
            "amended_at": datetime.now(timezone.utc).isoformat(),
        }
        if resin_changed:
            payload["resin_id"] = new_resin
        if not share_key:
            raise ValueError(f"Run #{run_id} has no team sync key — try Sync team first")
        self._apply_settings_amendment(payload)
        self._append_team_payload(payload)
        updated = self.get_run(run_id)
        assert updated is not None
        return updated

    def runs_missing_preview(self) -> list[HistoryRecord]:
        """Runs with a printer task id whose preview image isn't on this machine."""
        out = []
        for rec in self.list_recent(outcome=None, limit=100000):
            if not rec.task_id:
                continue
            path = Path(rec.preview_path) if rec.preview_path else None
            if path is not None and not path.is_absolute():
                path = self.db_path.parent.parent / path
            if path is None or not path.is_file():
                out.append(rec)
        return out

    def set_run_preview(self, run_id: int, preview_path: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE print_runs SET preview_path = ? WHERE id = ?", (preview_path, run_id))
            conn.commit()

    def _known_task_ids(self, printer_id: str) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id FROM print_runs WHERE printer_id = ? AND task_id != ''", (printer_id,)
            ).fetchall()
        known = {str(r["task_id"]) for r in rows}
        job = self.get_open_job(printer_id)
        if job and job.task_id:
            known.add(job.task_id)
        known.update(p.job.task_id for p in self.list_unreported(printer_id) if p.job.task_id)
        return known

    def import_printer_prints(
        self,
        printer_id: str,
        details: list[Any],
        resin_id: str,
        vat_temperature_c: float,
        dry_run: bool = False,
        since: datetime | None = None,
    ) -> dict[str, Any]:
        """Log a printer's own print history (SDCP TaskDetail list) as successful runs.

        Only prints the printer marks completed with every layer done are imported;
        stopped / errored prints are skipped because they are usually cancellations.
        """
        known = self._known_task_ids(printer_id)
        result: dict[str, Any] = {
            "imported": 0, "already": 0, "incomplete": 0, "no_settings": 0, "too_old": 0, "rows": []
        }
        for d in sorted(details, key=lambda x: x.begin_time or 0):
            if since and (d.begin_time or 0) < since.timestamp():
                result["too_old"] += 1
                continue
            raw = d.raw or {}
            info = d.slice_information or {}
            if not d.task_id or d.task_id in known:
                result["already"] += 1
                continue
            total = int(info.get("total_layer_numbers") or 0)
            done = int(raw.get("AlreadyPrintLayer") or 0)
            if d.task_status != 1 or int(raw.get("ErrorStatusReason") or 0) or (total and done < total):
                result["incomplete"] += 1
                continue
            settings = settings_from_slice_info(info)
            if settings is None:
                result["no_settings"] += 1
                continue
            started = (
                datetime.fromtimestamp(d.begin_time, timezone.utc).isoformat() if d.begin_time else ""
            )
            ended = datetime.fromtimestamp(d.end_time, timezone.utc).isoformat() if d.end_time else ""
            filename = Path(d.task_name or "").name
            result["rows"].append(
                {
                    "file": filename,
                    "started_at": started,
                    "layer_height": settings.layer_height,
                    "resin_id": resin_id,
                }
            )
            known.add(d.task_id)
            result["imported"] += 1
            if dry_run:
                continue
            tel = slice_info_telemetry(info, filename_mismatches(filename, settings))
            tel.update({"imported_from_printer": True, "vat_temp_estimated": True})
            self.log_run(
                printer_id,
                resin_id,
                settings.layer_height,
                vat_temperature_c,
                settings,
                PrintOutcome.SUCCESS,
                notes=f"Printer history success — {filename}",
                filename=filename,
                total_layers=total or None,
                source="printer_history",
                started_at=started,
                ended_at=ended,
                task_id=d.task_id,
                end_layer=done or None,
                duration_s=float(d.end_time - d.begin_time) if d.begin_time and d.end_time else None,
                telemetry=tel,
                created_at=ended or started,
            )
        return result

    @property
    def vat_file(self) -> Path:
        return self.team_file.parent / "vat_resin.json"

    def vat_resins(self) -> dict[str, dict[str, str]]:
        """Resin physically in each printer's vat: {printer_id: {resin_id, set_at}}."""
        try:
            data = json.loads(self.vat_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict) and v.get("resin_id")}

    def vat_resin(self, printer_id: str) -> str:
        return str(self.vat_resins().get(printer_id, {}).get("resin_id") or "")

    def set_vat_resin(self, printer_id: str, resin_id: str) -> None:
        """Record a resin swap; a print already running on that printer is re-tagged too."""
        data = self.vat_resins()
        data[printer_id] = {"resin_id": resin_id, "set_at": datetime.now(timezone.utc).isoformat()}
        tmp = self.vat_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.vat_file)
        job = self.get_open_job(printer_id)
        if job and not job.awaiting_outcome and job.resin_id != resin_id:
            job.resin_id = resin_id
            self.upsert_open_job(job)

    def _share_key_for(self, run_id: int) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT share_key FROM print_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Run #{run_id} not found")
        if not row["share_key"]:
            self.sync_team_data()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT share_key FROM print_runs WHERE id = ?", (run_id,)
                ).fetchone()
        return str(row["share_key"] or "")

    def has_run_for_task(self, printer_id: str, task_id: str) -> bool:
        """True when this SDCP print was already logged (e.g. reported while still printing)."""
        if not task_id:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM print_runs WHERE printer_id = ? AND task_id = ? LIMIT 1",
                (printer_id, task_id),
            ).fetchone()
        return row is not None

    def get_run(self, run_id: int) -> HistoryRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM print_runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def amend_run(
        self,
        run_id: int,
        outcome: PrintOutcome,
        notes: str = "",
        failure_issue: str = "",
        quality: dict[str, bool] | None = None,
        part_application: str | None = None,
    ) -> HistoryRecord:
        """Re-report a logged run (late or corrected outcome) and share the change."""
        rec = self.get_run(run_id)
        if rec is None:
            raise ValueError(f"Run #{run_id} not found")
        new_notes = _retag_notes(rec.notes, rec.outcome, outcome)
        extra = (notes or "").strip()
        if extra and extra not in new_notes:
            new_notes = f"{new_notes} — {extra}" if new_notes else extra
        app = rec.part_application if part_application is None else part_application.strip()
        issue = "" if outcome == PrintOutcome.SUCCESS else (failure_issue or "")
        checks = clean_quality(quality or {})
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE print_runs
                SET outcome = ?, failure_issue = ?, quality_json = ?, notes = ?, part_application = ?
                WHERE id = ?
                """,
                (outcome.value, issue, json.dumps(checks), new_notes, app, run_id),
            )
            share_key = conn.execute(
                "SELECT share_key FROM print_runs WHERE id = ?", (run_id,)
            ).fetchone()[0]
            conn.commit()
        if share_key:
            self._append_team_payload(
                {
                    "amend": share_key,
                    "outcome": outcome.value,
                    "failure_issue": issue,
                    "quality": checks,
                    "notes": new_notes,
                    "part_application": app,
                    "amended_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        updated = self.get_run(run_id)
        assert updated is not None
        return updated

    # ----- SDCP open jobs -------------------------------------------------
    def _job_from_row(self, row: sqlite3.Row) -> OpenJob:
        keys = row.keys()

        def _f(name: str) -> float | None:
            if name not in keys:
                return None
            val = row[name]
            return float(val) if val is not None else None

        def _i(name: str) -> int | None:
            if name not in keys:
                return None
            val = row[name]
            return int(val) if val is not None else None

        def _s(name: str, default: str = "") -> str:
            if name not in keys:
                return default
            return str(row[name] or default)

        return OpenJob(
            printer_id=row["printer_id"],
            filename=_s("filename"),
            resin_id=_s("resin_id"),
            layer_height=float(row["layer_height"] or 0.05),
            vat_temperature_c=float(row["vat_temperature_c"] or 25.0),
            uv_led_temp_c=_f("uv_led_temp_c"),
            total_layers=_i("total_layers"),
            current_layer=_i("current_layer"),
            settings_json=_s("settings_json"),
            geometry_class=_s("geometry_class", GeometryClass.NORMAL.value),
            cross_section_mm2=float(row["cross_section_mm2"] or 0.0),
            started_at=_s("started_at"),
            last_status=_s("last_status"),
            awaiting_outcome=bool(row["awaiting_outcome"]),
            print_substatus=_s("print_substatus"),
            task_id=_s("task_id"),
            duration_s=_f("duration_s"),
            eta_total_s=_f("eta_total_s"),
            release_film=_i("release_film"),
            heat_status=_i("heat_status"),
            target_tank_temp_c=_f("target_tank_temp_c"),
            vat_temp_min_c=_f("vat_temp_min_c"),
            vat_temp_max_c=_f("vat_temp_max_c"),
            uv_temp_min_c=_f("uv_temp_min_c"),
            uv_temp_max_c=_f("uv_temp_max_c"),
            temp_sample_count=int(_i("temp_sample_count") or 0),
            vat_temp_sum=float(_f("vat_temp_sum") or 0.0),
            uv_temp_sum=float(_f("uv_temp_sum") or 0.0),
            error_number=int(_i("error_number") or 0),
            error_message=_s("error_message"),
            exposure_from_file_s=_f("exposure_from_file_s"),
            telemetry_json=_s("telemetry_json", "{}"),
            preview_path=_s("preview_path"),
            part_application=_s("part_application"),
            thumbnail_url=_s("thumbnail_url"),
        )

    def get_open_job(self, printer_id: str) -> OpenJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM open_jobs WHERE printer_id = ?", (printer_id,)
            ).fetchone()
        return self._job_from_row(row) if row else None

    def list_open_jobs(self, awaiting_only: bool = False) -> list[OpenJob]:
        sql = "SELECT * FROM open_jobs"
        if awaiting_only:
            sql += " WHERE awaiting_outcome = 1"
        with self._connect() as conn:
            rows = conn.execute(sql).fetchall()
        return [self._job_from_row(r) for r in rows]

    def upsert_open_job(self, job: OpenJob) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO open_jobs (
                    printer_id, filename, resin_id, layer_height, vat_temperature_c,
                    uv_led_temp_c, total_layers, current_layer, settings_json,
                    geometry_class, cross_section_mm2, started_at, last_status,
                    awaiting_outcome, print_substatus, task_id, duration_s, eta_total_s,
                    release_film, heat_status, target_tank_temp_c,
                    vat_temp_min_c, vat_temp_max_c, uv_temp_min_c, uv_temp_max_c,
                    temp_sample_count, vat_temp_sum, uv_temp_sum,
                    error_number, error_message, exposure_from_file_s, telemetry_json,
                    preview_path, part_application, thumbnail_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(printer_id) DO UPDATE SET
                    filename=excluded.filename,
                    resin_id=CASE WHEN excluded.resin_id != '' THEN excluded.resin_id ELSE open_jobs.resin_id END,
                    layer_height=excluded.layer_height,
                    vat_temperature_c=excluded.vat_temperature_c,
                    uv_led_temp_c=excluded.uv_led_temp_c,
                    total_layers=excluded.total_layers,
                    current_layer=excluded.current_layer,
                    settings_json=CASE
                        WHEN excluded.settings_json != '' THEN excluded.settings_json
                        ELSE open_jobs.settings_json END,
                    geometry_class=excluded.geometry_class,
                    cross_section_mm2=excluded.cross_section_mm2,
                    started_at=CASE
                        WHEN open_jobs.started_at != '' THEN open_jobs.started_at
                        ELSE excluded.started_at END,
                    last_status=excluded.last_status,
                    awaiting_outcome=excluded.awaiting_outcome,
                    print_substatus=excluded.print_substatus,
                    task_id=excluded.task_id,
                    duration_s=excluded.duration_s,
                    eta_total_s=excluded.eta_total_s,
                    release_film=excluded.release_film,
                    heat_status=excluded.heat_status,
                    target_tank_temp_c=excluded.target_tank_temp_c,
                    vat_temp_min_c=excluded.vat_temp_min_c,
                    vat_temp_max_c=excluded.vat_temp_max_c,
                    uv_temp_min_c=excluded.uv_temp_min_c,
                    uv_temp_max_c=excluded.uv_temp_max_c,
                    temp_sample_count=excluded.temp_sample_count,
                    vat_temp_sum=excluded.vat_temp_sum,
                    uv_temp_sum=excluded.uv_temp_sum,
                    error_number=excluded.error_number,
                    error_message=excluded.error_message,
                    exposure_from_file_s=excluded.exposure_from_file_s,
                    telemetry_json=excluded.telemetry_json,
                    preview_path=CASE
                        WHEN excluded.preview_path != '' THEN excluded.preview_path
                        ELSE open_jobs.preview_path END,
                    part_application=CASE
                        WHEN excluded.part_application != '' THEN excluded.part_application
                        ELSE open_jobs.part_application END,
                    thumbnail_url=CASE
                        WHEN excluded.thumbnail_url != '' THEN excluded.thumbnail_url
                        ELSE open_jobs.thumbnail_url END
                """,
                (
                    job.printer_id,
                    job.filename,
                    job.resin_id,
                    job.layer_height,
                    job.vat_temperature_c,
                    job.uv_led_temp_c,
                    job.total_layers,
                    job.current_layer,
                    job.settings_json,
                    job.geometry_class,
                    job.cross_section_mm2,
                    job.started_at,
                    job.last_status,
                    1 if job.awaiting_outcome else 0,
                    job.print_substatus,
                    job.task_id,
                    job.duration_s,
                    job.eta_total_s,
                    job.release_film,
                    job.heat_status,
                    job.target_tank_temp_c,
                    job.vat_temp_min_c,
                    job.vat_temp_max_c,
                    job.uv_temp_min_c,
                    job.uv_temp_max_c,
                    job.temp_sample_count,
                    job.vat_temp_sum,
                    job.uv_temp_sum,
                    job.error_number,
                    job.error_message,
                    job.exposure_from_file_s,
                    job.telemetry_json or "{}",
                    job.preview_path,
                    job.part_application,
                    job.thumbnail_url,
                ),
            )
            conn.commit()

    def mark_awaiting_outcome(self, printer_id: str) -> OpenJob | None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE open_jobs SET awaiting_outcome = 1 WHERE printer_id = ?",
                (printer_id,),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM open_jobs WHERE printer_id = ?", (printer_id,)
            ).fetchone()
        return self._job_from_row(row) if row else None

    def clear_open_job(self, printer_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM open_jobs WHERE printer_id = ?", (printer_id,))
            conn.commit()

    def resolve_open_job(
        self,
        printer_id: str,
        outcome: PrintOutcome,
        notes: str = "",
        settings: PrintSettings | None = None,
        resin_id: str | None = None,
        part_application: str | None = None,
        failure_issue: str = "",
        quality: dict[str, bool] | None = None,
    ) -> int | None:
        job = self.get_open_job(printer_id)
        if not job:
            return None
        row_id = self._log_job(
            job,
            outcome,
            notes=notes,
            settings=settings,
            resin_id=resin_id,
            part_application=part_application,
            failure_issue=failure_issue,
            quality=quality,
        )
        self.clear_open_job(printer_id)
        return row_id

    # ----- Unreported (missed) SDCP jobs ------------------------------------
    def park_open_job(self, printer_id: str) -> int | None:
        """Keep an awaiting job reportable instead of dropping it when a new print starts."""
        job = self.get_open_job(printer_id)
        if job is None or not job.awaiting_outcome:
            return None
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO unreported_jobs (printer_id, parked_at, job_json) VALUES (?, ?, ?)",
                (printer_id, datetime.now(timezone.utc).isoformat(), json.dumps(asdict(job))),
            )
            conn.execute("DELETE FROM open_jobs WHERE printer_id = ?", (printer_id,))
            conn.commit()
            return int(cur.lastrowid)

    @staticmethod
    def _pending_from_row(row: sqlite3.Row) -> PendingJob:
        data = json.loads(row["job_json"] or "{}")
        known = set(OpenJob.__dataclass_fields__)
        job = OpenJob(**{k: v for k, v in data.items() if k in known})
        return PendingJob(id=int(row["id"]), parked_at=str(row["parked_at"]), job=job)

    def list_unreported(self, printer_id: str | None = None) -> list[PendingJob]:
        sql = "SELECT * FROM unreported_jobs"
        params: tuple[Any, ...] = ()
        if printer_id:
            sql += " WHERE printer_id = ?"
            params = (printer_id,)
        sql += " ORDER BY parked_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._pending_from_row(r) for r in rows]

    def get_unreported(self, pending_id: int) -> PendingJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM unreported_jobs WHERE id = ?", (pending_id,)
            ).fetchone()
        return self._pending_from_row(row) if row else None

    def dismiss_unreported(self, pending_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM unreported_jobs WHERE id = ?", (pending_id,))
            conn.commit()

    def resolve_unreported(
        self,
        pending_id: int,
        outcome: PrintOutcome,
        notes: str = "",
        settings: PrintSettings | None = None,
        resin_id: str | None = None,
        part_application: str | None = None,
        failure_issue: str = "",
        quality: dict[str, bool] | None = None,
    ) -> int:
        pending = self.get_unreported(pending_id)
        if pending is None:
            raise ValueError(f"Unreported print #{pending_id} not found")
        row_id = self._log_job(
            pending.job,
            outcome,
            notes=notes,
            settings=settings,
            resin_id=resin_id,
            part_application=part_application,
            failure_issue=failure_issue,
            quality=quality,
            created_at=pending.finished_at,
        )
        self.dismiss_unreported(pending_id)
        return row_id

    def _log_job(
        self,
        job: OpenJob,
        outcome: PrintOutcome,
        notes: str = "",
        settings: PrintSettings | None = None,
        resin_id: str | None = None,
        part_application: str | None = None,
        failure_issue: str = "",
        quality: dict[str, bool] | None = None,
        created_at: str = "",
    ) -> int:
        printer_id = job.printer_id
        if part_application is not None:
            job.part_application = part_application.strip()
        use_settings = job.settings if job.settings_from_printer else (settings or job.settings)
        if use_settings is None:
            raise ValueError("No print settings captured for this job — Recommend once first.")
        rid = resin_id or job.resin_id
        if not rid:
            raise ValueError("No resin linked to this job — pick a resin on Calibrate.")
        try:
            geom = GeometryClass(job.geometry_class)
        except ValueError:
            geom = GeometryClass.NORMAL
        note_bits = [f"SDCP {outcome.value}"]
        if job.filename:
            note_bits.append(job.filename)
        if job.part_application:
            note_bits.append(job.part_application)
        if notes:
            note_bits.append(notes)
        ended = created_at or datetime.now(timezone.utc).isoformat()
        row_id = self.log_run(
            printer_id=printer_id,
            resin_id=rid,
            layer_height=job.layer_height or use_settings.layer_height,
            vat_temperature_c=job.vat_temperature_c,
            settings=use_settings,
            outcome=outcome,
            geometry_class=geom,
            cross_section_mm2=job.cross_section_mm2,
            notes=" — ".join(note_bits),
            filename=job.filename,
            uv_led_temp_c=job.uv_led_temp_c,
            total_layers=job.total_layers,
            source="sdcp",
            started_at=job.started_at,
            ended_at=ended,
            task_id=job.task_id,
            end_layer=job.current_layer,
            duration_s=job.duration_s,
            eta_total_s=job.eta_total_s,
            release_film=job.release_film,
            heat_status=job.heat_status,
            target_tank_temp_c=job.target_tank_temp_c,
            vat_temp_min_c=job.vat_temp_min_c,
            vat_temp_max_c=job.vat_temp_max_c,
            vat_temp_avg_c=job.vat_temp_avg_c,
            uv_temp_min_c=job.uv_temp_min_c,
            uv_temp_max_c=job.uv_temp_max_c,
            uv_temp_avg_c=job.uv_temp_avg_c,
            error_number=job.error_number,
            error_message=job.error_message,
            print_substatus=job.print_substatus,
            exposure_from_file_s=job.exposure_from_file_s,
            telemetry=job.telemetry,
            preview_path=job.preview_path,
            part_application=job.part_application,
            failure_issue=failure_issue if outcome != PrintOutcome.SUCCESS else "",
            quality=quality,
            created_at=created_at,
        )
        return row_id
