"""SDCP (Smart Device Control Protocol) — the LAN protocol of ChiTu-board resin printers
such as the ELEGOO Mars/Saturn Ultra series; the same protocol ChituManager uses."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .models import PrintSettings

try:
    import websocket
except ImportError:  # pragma: no cover
    websocket = None  # type: ignore

from .paths import preview_dir

StatusKind = Literal["idle", "printing", "finished", "failed", "offline", "busy"]

MACHINE_STATUS = {
    0: "Idle",
    1: "Printing",
    2: "File transfer",
    3: "Exposure test",
    4: "Devices testing",
}

PRINT_SUBSTATUS = {
    0: "Idle",
    1: "Homing",
    2: "Descending",
    3: "Exposing",
    4: "Lifting",
    5: "Pausing",
    6: "Paused",
    7: "Stopping",
    8: "Stopped",
    9: "Complete",
    10: "File check",
}

PRINT_ERROR = {
    0: "",
    1: "MD5 check failed",
    2: "File read failed",
    3: "Resolution mismatch",
    4: "Format mismatch",
    5: "Model mismatch",
}

KIND_LABEL = {
    "idle": "Idle",
    "printing": "Printing",
    "finished": "Finished",
    "failed": "Failed",
    "offline": "Offline",
    "busy": "Busy",
}

# ELEGOO PTFE / release film service life (peel cycles). Override via attributes when present.
RELEASE_FILM_MAX_DEFAULT = 60_000
RELEASE_FILM_WARN_FRAC = 0.90  # warn from 90% of max


def film_usage(
    cycles: int | None,
    max_cycles: int | None = None,
) -> dict[str, Any]:
    """Normalize ReleaseFilm counter into UI-ready wear metrics."""
    limit = int(max_cycles) if max_cycles and max_cycles > 0 else RELEASE_FILM_MAX_DEFAULT
    if cycles is None:
        return {
            "cycles": None,
            "max": limit,
            "pct": None,
            "remaining": None,
            "level": "unknown",
            "label": "Film —",
            "warning": "",
        }
    used = max(0, int(cycles))
    pct = min(100.0, 100.0 * used / limit) if limit else 0.0
    remaining = max(0, limit - used)
    if used >= limit:
        level = "critical"
        warning = f"Replace PTFE film — {used:,} / {limit:,} cycles"
    elif used >= int(limit * RELEASE_FILM_WARN_FRAC):
        level = "warn"
        warning = f"PTFE film near limit — {remaining:,} cycles left"
    else:
        level = "ok"
        warning = ""
    return {
        "cycles": used,
        "max": limit,
        "pct": pct,
        "remaining": remaining,
        "level": level,
        "label": f"Film {used:,} / {limit:,}",
        "warning": warning,
    }


@dataclass
class DiscoveredPrinter:
    name: str
    ip: str
    mainboard_id: str
    protocol_version: str = ""
    firmware_version: str = ""
    machine_name: str = ""
    brand: str = ""


@dataclass
class PrinterLiveStatus:
    connected: bool
    ip: str
    mainboard_id: str = ""
    status_kind: StatusKind = "offline"
    machine_status: str = "Unknown"
    print_substatus: str = ""
    error_message: str = ""
    enclosure_temp_c: float | None = None
    uv_led_temp_c: float | None = None
    target_tank_temp_c: float | None = None
    heat_status: int | None = None
    release_film: int | None = None
    release_film_max: int | None = None
    timelapse_status: int | None = None
    current_layer: int | None = None
    total_layers: int | None = None
    current_ticks: int | None = None
    total_ticks: int | None = None
    task_id: str = ""
    filename: str = ""
    print_progress_pct: float | None = None
    error_number: int = 0
    raw_error: str = ""
    thumbnail_url: str = ""
    preview_path: str = ""

    @property
    def film(self) -> dict[str, Any]:
        return film_usage(self.release_film, self.release_film_max)

    @property
    def status_label(self) -> str:
        if not self.connected:
            return "Offline"
        return KIND_LABEL.get(self.status_kind, self.machine_status)

    @property
    def duration_s(self) -> float | None:
        if self.current_ticks is None:
            return None
        # ELEGOO TotalTicks / CurrentTicks are milliseconds
        return max(0.0, float(self.current_ticks) / 1000.0)

    @property
    def eta_total_s(self) -> float | None:
        if self.total_ticks is None:
            return None
        return max(0.0, float(self.total_ticks) / 1000.0)

    def temp_display(self) -> str:
        """Human-readable temps. Mars/Saturn 8K often only report UV LED (no tank heater)."""
        bits: list[str] = []
        if self.enclosure_temp_c is not None:
            bits.append(f"{self.enclosure_temp_c:.0f}° vat")
        if self.uv_led_temp_c is not None:
            bits.append(f"{self.uv_led_temp_c:.0f}° UV")
        return " · ".join(bits)

    @property
    def best_temp_c(self) -> float | None:
        """Prefer vat/tank temp; fall back to UV LED when the printer has no heater."""
        if self.enclosure_temp_c is not None:
            return self.enclosure_temp_c
        return self.uv_led_temp_c

    def telemetry_snapshot(self) -> dict[str, Any]:
        """Flat dict of everything SDCP gave us for this poll."""
        return {
            "status_kind": self.status_kind,
            "machine_status": self.machine_status,
            "print_substatus": self.print_substatus,
            "error_number": self.error_number,
            "error_message": self.error_message,
            "filename": self.filename,
            "task_id": self.task_id,
            "thumbnail_url": self.thumbnail_url,
            "preview_path": self.preview_path,
            "current_layer": self.current_layer,
            "total_layers": self.total_layers,
            "print_progress_pct": self.print_progress_pct,
            "current_ticks": self.current_ticks,
            "total_ticks": self.total_ticks,
            "duration_s": self.duration_s,
            "eta_total_s": self.eta_total_s,
            "vat_temp_c": self.enclosure_temp_c,
            "uv_led_temp_c": self.uv_led_temp_c,
            "target_tank_temp_c": self.target_tank_temp_c,
            "heat_status": self.heat_status,
            "release_film": self.release_film,
            "release_film_max": self.release_film_max,
            "timelapse_status": self.timelapse_status,
            "mainboard_id": self.mainboard_id,
            "ip": self.ip,
        }

    def summary_lines(self) -> list[str]:
        if not self.connected:
            return [self.raw_error or "Offline — check Wi‑Fi / sdcp_ip"]
        lines = [f"{self.status_label}"]
        if self.print_substatus and self.status_kind in {"printing", "finished"}:
            lines.append(self.print_substatus)
        if self.error_message:
            lines.append(self.error_message)
        temps = self.temp_display()
        if temps:
            lines.append(temps)
        if self.filename:
            lines.append(self.filename)
        if self.current_layer is not None and self.total_layers:
            pct = f" · {self.print_progress_pct:.0f}%" if self.print_progress_pct is not None else ""
            lines.append(f"Layer {self.current_layer}/{self.total_layers}{pct}")
        film = self.film
        if film["cycles"] is not None:
            lines.append(film["label"] + (" · REPLACE" if film["level"] == "critical" else ""))
        return lines


@dataclass
class TaskDetail:
    task_id: str
    task_name: str = ""
    thumbnail_url: str = ""
    begin_time: int | None = None
    end_time: int | None = None
    task_status: int | None = None
    slice_information: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def parse_ctb_filename(filename: str) -> dict[str, Any]:
    """Pull layer height + layer exposure from Chitubox `*.stl_[tag_]0.025_1.600_YYYY_….ctb` names.

    Chitubox names carry only layer height and layer (normal) exposure — never bottom exposure.
    """
    out: dict[str, Any] = {"layer_height": None, "normal_exposure_s": None, "stem": ""}
    if not filename:
        return out
    out["stem"] = filename
    m = re.search(
        r"\.stl_(?:[^_]+_)*?(\d\.\d+)_(\d+\.\d+)_\d{4}_",
        filename,
        flags=re.IGNORECASE,
    )
    if m is None:
        m = re.search(r"\.stl_(\d+\.\d+)_(\d+\.\d+)_", filename, flags=re.IGNORECASE)
    if m:
        try:
            out["layer_height"] = float(m.group(1))
            out["normal_exposure_s"] = float(m.group(2))
        except ValueError:
            pass
        return out
    m = re.search(r"_(\d+\.\d{2,3})_(\d+\.\d+)", filename)
    if m:
        try:
            out["layer_height"] = float(m.group(1))
            out["normal_exposure_s"] = float(m.group(2))
        except ValueError:
            pass
    return out


def settings_from_slice_info(info: dict[str, Any]) -> PrintSettings | None:
    """Actual sliced parameters reported by the printer (Cmd 321 SliceInformation, times in ms)."""
    if not info or info.get("normal_layer_exposure_time") is None:
        return None

    def sec(key: str) -> float:
        return round(float(info.get(key) or 0) / 1000.0, 3)

    try:
        return PrintSettings(
            layer_height=round(float(info.get("layer_height") or 0.0), 4),
            normal_exposure_time=sec("normal_layer_exposure_time"),
            bottom_exposure_time=sec("bottom_layer_exposure_time"),
            bottom_layer_count=int(info.get("bottom_layer_numbers") or 0),
            transition_layer_count=int(info.get("transition_layer_numbers") or 0),
            rest_time_after_retract=sec("normal_layer_rest_time_after_drop"),
            rest_time_before_lift=sec("normal_layer_rest_time_before_lift"),
            rest_time_after_lift=sec("normal_layer_rest_time_after_lift"),
            bottom_rest_time_after_retract=sec("bottom_layer_rest_time_after_drop"),
            bottom_rest_time_before_lift=sec("bottom_layer_rest_time_before_lift"),
            bottom_rest_time_after_lift=sec("bottom_layer_rest_time_after_lift"),
            light_off_time=sec("normal_layer_light_off_time"),
            bottom_light_off_time=sec("bottom_layer_light_off_time"),
            normal_light_intensity_pwm=int(info.get("normal_layer_pwm") or 255),
            bottom_light_intensity_pwm=int(info.get("bottom_layer_pwm") or 255),
        )
    except (TypeError, ValueError):
        return None


def filename_mismatches(filename: str, settings: PrintSettings) -> list[str]:
    """Cross-check the Chitubox file name against the printer-reported slice settings."""
    parsed = parse_ctb_filename(filename)
    issues: list[str] = []
    lh = parsed.get("layer_height")
    if lh is not None and abs(float(lh) - settings.layer_height) > 0.0005:
        issues.append(f"file name says layer height {lh:g} mm, printer says {settings.layer_height:g} mm")
    exp = parsed.get("normal_exposure_s")
    if exp is not None and abs(float(exp) - settings.normal_exposure_time) > 0.01:
        issues.append(
            f"file name says layer exposure {exp:g}s, printer says {settings.normal_exposure_time:g}s"
        )
    return issues


def layer_height_from_filename(filename: str) -> float | None:
    """Parse Chitubox-style `name.stl_0.025_1.600_….ctb` layer height."""
    return parse_ctb_filename(filename).get("layer_height")  # type: ignore[return-value]


def _classify(
    machine_code: int,
    print_code: int,
    error_code: int,
) -> tuple[StatusKind, str, str]:
    err = PRINT_ERROR.get(error_code, f"Error {error_code}" if error_code else "")
    if error_code and error_code > 0:
        return "failed", MACHINE_STATUS.get(machine_code, "Error"), err
    if print_code == 8:  # stopped
        return "failed", "Stopped", err or "Print stopped"
    if print_code == 9:  # complete — stays until cleared on the printer
        return "finished", "Finished", err
    if machine_code == 1 or print_code in {1, 2, 3, 4, 5, 6, 7, 10}:
        return "printing", MACHINE_STATUS.get(machine_code, "Printing"), err
    if machine_code in {2, 3, 4}:
        return "busy", MACHINE_STATUS.get(machine_code, "Busy"), err
    return "idle", MACHINE_STATUS.get(machine_code, "Idle"), err


def discover_printers(timeout_s: float = 2.0, target: str = "255.255.255.255") -> list[DiscoveredPrinter]:
    """Broadcast the SDCP discovery message (or send it to one address) and collect replies."""
    found: dict[str, DiscoveredPrinter] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.4)
    try:
        sock.bind(("", 0))
        deadline = time.time() + timeout_s
        sock.sendto(b"M99999", (target, 3000))
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            try:
                payload = json.loads(data.decode("utf-8", errors="ignore"))
            except json.JSONDecodeError:
                continue
            body = payload.get("Data") or payload
            ip = str(body.get("MainboardIP") or body.get("ip") or "")
            mid = str(body.get("MainboardID") or body.get("id") or "")
            if not ip:
                continue
            found[ip] = DiscoveredPrinter(
                name=str(body.get("Name") or body.get("MachineName") or "Resin printer"),
                ip=ip,
                mainboard_id=mid,
                protocol_version=str(body.get("ProtocolVersion") or ""),
                firmware_version=str(body.get("FirmwareVersion") or ""),
                machine_name=str(body.get("MachineName") or ""),
                brand=str(body.get("BrandName") or ""),
            )
    finally:
        sock.close()
    return list(found.values())


def _dims(text: Any, cast: type) -> list[Any]:
    try:
        return [cast(float(v)) for v in str(text or "").lower().split("x")]
    except ValueError:
        return []


def fetch_attributes(ip: str, mainboard_id: str, timeout_s: float = 4.0) -> dict[str, Any]:
    """Printer self-description (Cmd 1): resolution, build volume, film limit, capabilities."""
    if websocket is None or not ip:
        return {}
    try:
        ws = websocket.create_connection(f"ws://{ip}:3030/websocket", timeout=timeout_s)
    except Exception:  # noqa: BLE001
        return {}
    attrs: dict[str, Any] = {}
    try:
        ws.send(json.dumps(_request_payload(mainboard_id or "unknown", cmd=1)))
        deadline = time.time() + timeout_s
        while time.time() < deadline and not attrs:
            try:
                msg = json.loads(ws.recv() or "{}")
            except Exception:  # noqa: BLE001
                break
            found = msg.get("Attributes") or (msg.get("Data") or {}).get("Attributes")
            if isinstance(found, dict):
                attrs = found
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    res = _dims(attrs.get("Resolution"), int)
    xyz = _dims(attrs.get("XYZsize"), float)
    out: dict[str, Any] = {
        "machine_name": str(attrs.get("MachineName") or ""),
        "brand": str(attrs.get("BrandName") or ""),
        "firmware_version": str(attrs.get("FirmwareVersion") or ""),
        "release_film_max": attrs.get("ReleaseFilmMax"),
        "file_types": list(attrs.get("SupportFileType") or []),
    }
    if len(res) == 2:
        out["resolution_x"], out["resolution_y"] = res
    if len(xyz) == 3:
        out["machine_width"], out["machine_depth"], out["machine_height"] = xyz
    return out


def _request_payload(
    mainboard_id: str, cmd: int = 0, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "Id": str(uuid.uuid4()),
        "Topic": f"sdcp/request/{mainboard_id}",
        "Data": {
            "Cmd": cmd,
            "Data": data if data is not None else {},
            "RequestID": uuid.uuid4().hex,
            "MainboardID": mainboard_id,
            "TimeStamp": int(time.time() * 1000),
            "From": 0,
        },
    }


def _extract_status_block(msg: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """ELEGOO Ultra pushes Status at the top level (not under Data)."""
    mid = str(msg.get("MainboardID") or "")
    if isinstance(msg.get("Status"), dict):
        return msg["Status"], mid or str((msg.get("Data") or {}).get("MainboardID") or "")
    data = msg.get("Data") or {}
    if isinstance(data.get("Status"), dict):
        return data["Status"], mid or str(data.get("MainboardID") or "")
    if isinstance(data, dict) and ("CurrentStatus" in data or "PrintInfo" in data):
        return data, mid or str(data.get("MainboardID") or "")
    return {}, mid


def _parse_status_message(msg: dict[str, Any], ip: str) -> PrinterLiveStatus:
    status, mid = _extract_status_block(msg)
    if not status:
        return PrinterLiveStatus(
            connected=False,
            ip=ip,
            mainboard_id=mid,
            raw_error="Status frame missing Status block",
        )

    print_info = status.get("PrintInfo") or {}
    current = print_info.get("CurrentLayer")
    total = print_info.get("TotalLayer")
    pct = None
    try:
        cur_i = int(current) if current is not None else None
        tot_i = int(total) if total is not None else None
    except (TypeError, ValueError):
        cur_i, tot_i = None, None
    if cur_i is not None and tot_i and tot_i > 0:
        pct = 100.0 * cur_i / tot_i

    code = status.get("CurrentStatus", status.get("MachineStatus"))
    if isinstance(code, list) and code:
        # Prefer first non-zero when multiple flags present; else first
        nonzero = [c for c in code if c]
        code = nonzero[0] if nonzero else code[0]
    try:
        machine_i = int(code) if code is not None else 0
    except (TypeError, ValueError):
        machine_i = 0
    try:
        print_i = int(print_info.get("Status") or 0)
    except (TypeError, ValueError):
        print_i = 0
    try:
        err_i = int(print_info.get("ErrorNumber") or 0)
    except (TypeError, ValueError):
        err_i = 0

    kind, machine_name, err_msg = _classify(machine_i, print_i, err_i)

    def _temp(*keys: str) -> float | None:
        for key in keys:
            val = status.get(key)
            if val is None:
                continue
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
        return None

    # Vat / resin temp: TempOfTank on Saturn Ultras; TempOfBox when present
    vat = _temp("TempOfTank", "TempOfBox")
    uv = _temp("TempOfUVLED")
    target = _temp("TempTargetTank")

    def _as_int(val: Any) -> int | None:
        if val is None:
            return None
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    return PrinterLiveStatus(
        connected=True,
        ip=ip,
        mainboard_id=mid,
        status_kind=kind,
        machine_status=machine_name,
        print_substatus=PRINT_SUBSTATUS.get(print_i, ""),
        error_message=err_msg,
        enclosure_temp_c=vat,
        uv_led_temp_c=uv,
        target_tank_temp_c=target,
        heat_status=_as_int(status.get("HeatStatus")),
        release_film=_as_int(status.get("ReleaseFilm")),
        release_film_max=_as_int(
            status.get("ReleaseFilmMax")
            or status.get("MaxReleaseFilm")
            or status.get("ReleaseFilmMaximum")
        ),
        timelapse_status=_as_int(status.get("TimeLapseStatus")),
        current_layer=cur_i,
        total_layers=tot_i,
        current_ticks=_as_int(print_info.get("CurrentTicks")),
        total_ticks=_as_int(print_info.get("TotalTicks")),
        task_id=str(print_info.get("TaskId") or ""),
        filename=str(print_info.get("Filename") or print_info.get("TaskName") or ""),
        print_progress_pct=pct,
        error_number=err_i,
    )


def fetch_status(
    ip: str,
    mainboard_id: str = "",
    timeout_s: float = 4.0,
) -> PrinterLiveStatus:
    if websocket is None:
        return PrinterLiveStatus(
            connected=False,
            ip=ip,
            raw_error="Install websocket-client",
        )
    if not ip:
        return PrinterLiveStatus(
            connected=False,
            ip="",
            raw_error="No sdcp_ip configured",
        )

    url = f"ws://{ip}:3030/websocket"
    mid = mainboard_id or "unknown"
    status_msg: dict[str, Any] | None = None
    attr_mid = mainboard_id

    try:
        ws = websocket.create_connection(url, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        return PrinterLiveStatus(
            connected=False,
            ip=ip,
            raw_error=f"Unreachable ({exc})",
        )

    try:
        # Printers often push sdcp/status immediately while printing — read first.
        ws.settimeout(1.2)
        listen_until = time.time() + 1.2
        while time.time() < listen_until and status_msg is None:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            topic = str(msg.get("Topic") or "")
            mid_top = str(msg.get("MainboardID") or "")
            data = msg.get("Data") or {}
            if mid_top:
                attr_mid = mid_top
                mid = mid_top
            elif data.get("MainboardID"):
                attr_mid = str(data["MainboardID"])
                mid = attr_mid
            if topic.startswith("sdcp/status/") and isinstance(msg.get("Status"), dict):
                status_msg = msg
                break
            if topic.startswith("sdcp/attributes/"):
                mid = str(data.get("MainboardID") or msg.get("MainboardID") or mid)
                attr_mid = mid

        # Request an explicit status update as well
        ws.settimeout(timeout_s)
        ws.send(json.dumps(_request_payload(mid, cmd=0)))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            topic = str(msg.get("Topic") or "")
            if msg.get("MainboardID"):
                attr_mid = str(msg["MainboardID"])
                mid = attr_mid
            data = msg.get("Data") or {}
            if data.get("MainboardID"):
                attr_mid = str(data["MainboardID"])
                mid = attr_mid
            if topic.startswith("sdcp/status/") and (
                isinstance(msg.get("Status"), dict)
                or isinstance((msg.get("Data") or {}).get("Status"), dict)
            ):
                status_msg = msg
                break
            if topic.startswith("sdcp/attributes/") and not mainboard_id:
                mid = str(data.get("MainboardID") or msg.get("MainboardID") or mid)
                attr_mid = mid
                ws.send(json.dumps(_request_payload(mid, cmd=0)))
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if not status_msg:
        return PrinterLiveStatus(
            connected=False,
            ip=ip,
            mainboard_id=attr_mid or mainboard_id,
            raw_error="No status frame",
        )

    parsed = _parse_status_message(status_msg, ip)
    if not parsed.mainboard_id:
        parsed.mainboard_id = attr_mid or mainboard_id
    return parsed


def _ws_request(
    ip: str,
    mainboard_id: str,
    cmd: int,
    data: dict[str, Any] | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any] | None:
    """Send one SDCP command and return the first matching response Data block."""
    if websocket is None or not ip:
        return None
    url = f"ws://{ip}:3030/websocket"
    mid = mainboard_id or "unknown"
    try:
        ws = websocket.create_connection(url, timeout=timeout_s)
    except Exception:
        return None
    try:
        ws.settimeout(0.8)
        # Drain initial pushes briefly
        drain_until = time.time() + 0.6
        while time.time() < drain_until:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mid_top = str(msg.get("MainboardID") or "")
            data_block = msg.get("Data") or {}
            if mid_top:
                mid = mid_top
            elif data_block.get("MainboardID"):
                mid = str(data_block["MainboardID"])

        ws.settimeout(timeout_s)
        ws.send(json.dumps(_request_payload(mid, cmd=cmd, data=data)))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            topic = str(msg.get("Topic") or "")
            if not topic.startswith("sdcp/response/"):
                continue
            body = msg.get("Data") or {}
            if int(body.get("Cmd") or -1) != cmd:
                continue
            inner = body.get("Data")
            if isinstance(inner, dict):
                return inner
            return body if isinstance(body, dict) else None
    finally:
        try:
            ws.close()
        except Exception:
            pass
    return None


PRINT_FILE_MOUNTS = ("/media/mmcblk0p3", "/media/mmcblk0p1")


def list_printer_files(ip: str, mainboard_id: str = "", timeout_s: float = 6.0) -> list[str]:
    """Cmd 258 — sliced files on the printer's internal storage (bare file names)."""
    data = _ws_request(ip, mainboard_id, 258, {"Url": "/local"}, timeout_s=timeout_s) or {}
    out = []
    for entry in data.get("FileList") or []:
        name = str(entry.get("name") or "").rsplit("/", 1)[-1]
        if int(entry.get("type", 1)) == 1 and name:
            out.append(name)
    return out


def download_print_file(ip: str, filename: str, dest: Path, timeout_s: float = 30.0) -> Path | None:
    """Download a sliced file the printer still holds (HTTP on port 3030)."""
    quoted = urllib.parse.quote(filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    for mount in PRINT_FILE_MOUNTS:
        url = f"http://{ip}:3030{mount}/{quoted}"
        req = urllib.request.Request(url, headers={"User-Agent": "SLAProfileAssistant"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp, open(tmp, "wb") as fh:
                shutil.copyfileobj(resp, fh, length=1 << 20)
        except Exception:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            continue
        os.replace(tmp, dest)
        return dest
    return None


def fetch_task_details(
    ip: str,
    mainboard_id: str,
    task_ids: list[str],
    timeout_s: float = 5.0,
) -> list[TaskDetail]:
    """Cmd 321 — history/active task detail including Chitubox Thumbnail URL."""
    ids = [t for t in task_ids if t]
    if not ids:
        return []
    body = _ws_request(ip, mainboard_id, cmd=321, data={"Id": ids}, timeout_s=timeout_s)
    if not body:
        return []
    rows = body.get("HistoryDetailList") or []
    out: list[TaskDetail] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Task id may be absent in detail; recover from thumbnail path / TaskName
        thumb = str(row.get("Thumbnail") or "")
        tid = str(row.get("TaskId") or "")
        m = re.search(r"history_image/([0-9a-fA-F-]{16,})", thumb)
        if m and not tid:
            tid = m.group(1)
        name = str(row.get("TaskName") or "")
        out.append(
            TaskDetail(
                task_id=tid,
                task_name=name,
                thumbnail_url=thumb,
                begin_time=int(row["BeginTime"]) if row.get("BeginTime") is not None else None,
                end_time=int(row["EndTime"]) if row.get("EndTime") is not None else None,
                task_status=int(row["TaskStatus"]) if row.get("TaskStatus") is not None else None,
                slice_information=dict(row.get("SliceInformation") or {})
                if isinstance(row.get("SliceInformation"), dict)
                else {},
                raw=row,
            )
        )
    return out


def fetch_print_history(
    ip: str, mainboard_id: str = "", batch: int = 12, timeout_s: float = 8.0
) -> list[TaskDetail]:
    """Every print the printer remembers (Cmd 320 id list → Cmd 321 details, ~48 on ELEGOO)."""
    listing = _ws_request(ip, mainboard_id, cmd=320, data={}, timeout_s=timeout_s)
    ids = [str(i) for i in (listing or {}).get("HistoryData") or [] if i]
    out: list[TaskDetail] = []
    for start in range(0, len(ids), batch):
        out.extend(fetch_task_details(ip, mainboard_id, ids[start : start + batch], timeout_s=timeout_s))
    return out


def preview_candidates(ip: str, task_id: str, thumbnail_url: str = "") -> list[str]:
    """Likely HTTP URLs for the Chitubox preview ChituManager shows on the printer."""
    urls: list[str] = []
    if thumbnail_url:
        urls.append(thumbnail_url)
        # Absolute-ize host-relative paths
        if thumbnail_url.startswith("/"):
            urls.insert(0, f"http://{ip}:3030{thumbnail_url}")
    tid = task_id.strip()
    if tid and ip:
        urls.extend(
            [
                f"http://{ip}:3030/media/mmcblk0p1/history_image/{tid}.bmp",
                f"http://{ip}:3030/media/mmcblk0p3/history_image/{tid}.bmp",
                f"http://{ip}:3030/board-resource/history_image/{tid}.bmp",
                f"http://{ip}:3030/board-resource/history_image/{tid}.png",
                f"http://{ip}/board-resource/history_image/{tid}.png",
            ]
        )
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def local_preview_path(task_id: str) -> Path:
    safe = re.sub(r"[^0-9a-fA-F-]", "", task_id) or "unknown"
    return preview_dir() / f"{safe}.png"


def download_preview(
    ip: str,
    task_id: str,
    thumbnail_url: str = "",
    timeout_s: float = 8.0,
) -> Path | None:
    """Download the slicer preview from the printer and cache it as PNG in the user's previews folder."""
    if not task_id:
        return None
    dest = local_preview_path(task_id)
    if dest.is_file() and dest.stat().st_size > 500:
        return dest
    last_err: Exception | None = None
    for url in preview_candidates(ip, task_id, thumbnail_url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ResinPilot"})
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read()
            if not raw or len(raw) < 200:
                continue
            # Convert BMP/JPEG/PNG → PNG via Pillow when available
            try:
                from io import BytesIO

                from PIL import Image

                img = Image.open(BytesIO(raw))
                img = img.convert("RGB")
                img.save(dest, format="PNG")
            except Exception:
                # Fallback: write raw bytes with original suffix if PNG convert fails
                raw_path = dest.with_suffix(".bin")
                raw_path.write_bytes(raw)
                if dest.suffix == ".png" and raw[:2] == b"BM":
                    continue
                dest.write_bytes(raw)
            if dest.is_file() and dest.stat().st_size > 500:
                return dest
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
            continue
    _ = last_err
    return dest if dest.is_file() else None


def ensure_print_preview(
    ip: str,
    mainboard_id: str,
    task_id: str,
    timeout_s: float = 6.0,
) -> tuple[str, str, dict[str, Any]]:
    """
    Resolve Chitubox preview for a print TaskId.

    Returns (thumbnail_url, preview_path, slice_information); the path is the cached PNG.
    """
    if not task_id or not ip:
        return "", "", {}
    thumb = ""
    slice_info: dict[str, Any] = {}
    details = fetch_task_details(ip, mainboard_id, [task_id], timeout_s=timeout_s)
    if details:
        # Match by id embedded in thumbnail when list returns without TaskId field
        detail = details[0]
        for d in details:
            if d.task_id == task_id or task_id in d.thumbnail_url:
                detail = d
                break
        thumb = detail.thumbnail_url
        slice_info = detail.slice_information
        if not detail.task_id:
            detail.task_id = task_id
    path = download_preview(ip, task_id, thumbnail_url=thumb, timeout_s=timeout_s)
    return thumb, str(path) if path and path.is_file() else "", slice_info


def guess_part_application(filename: str) -> str:
    """Heuristic tag from Chitubox filename — user can override on report."""
    words_in_name = [w for w in re.split(r"[^a-z]+", (filename or "").lower()) if w]
    rules = (
        ("miniature", ("miniature", "figure", "figurine", "bust", "warhammer", "dnd", "statue")),
        ("jewelry", ("ring", "pendant", "earring", "jewel", "bracelet", "charm")),
        ("dental", ("dental", "crown", "aligner", "denture", "tooth", "teeth", "arch")),
        ("microfluidics", ("manifold", "fluidic", "fluid", "channel", "valve", "chip", "mixer", "droplet", "labon")),
        ("mechanical", ("bracket", "enclosure", "housing", "mount", "gear", "jig", "fixture", "clip", "hinge")),
    )
    for application, words in rules:
        if any(t.startswith(w) for t in words_in_name for w in words):
            return application
    return ""
