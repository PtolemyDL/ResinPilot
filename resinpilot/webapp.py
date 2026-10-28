"""FastAPI web UI for ResinPilot."""

from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .branding import APP_NAME, APP_TAGLINE, DEFAULT_PORT, REPO_URL, web_urls
from .drivers import drivers
from .paths import ASSETS_DIR, WEB_DIR, preview_dir
from .quality import quality_checks_payload
from .web_service import get_service

INDEX_HTML = WEB_DIR / "templates" / "index.html"
# Set by the command-line launcher before the server starts.
SERVER: dict[str, Any] = {"port": DEFAULT_PORT, "lan": False}


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return ips


@asynccontextmanager
async def lifespan(_app: FastAPI):
    svc = get_service()
    svc.refresh_fleet()
    svc.start_polling()
    yield
    svc.stop_polling()


app = FastAPI(title=APP_NAME, version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")
app.mount("/previews", StaticFiles(directory=str(preview_dir())), name="previews")


class ReportBody(BaseModel):
    printer_id: str
    outcome: str = Field(description="success | fail | partial")
    notes: str = ""
    resin_id: str | None = None
    part_application: str | None = None
    failure_issue: str = ""
    layer_height: float | None = None
    quality: dict[str, bool] = Field(default_factory=dict)
    settings: dict[str, float | None] = Field(default_factory=dict)


class RecommendBody(BaseModel):
    printer_id: str
    resin_id: str
    layer_height: float = 0.05
    vat_temperature_c: float | None = None
    geometry: str = "normal"
    cross_section_mm2: float = 0.0
    part_family: str = ""
    size_class: str = ""


class PartFamilyBody(BaseModel):
    auto_keys: list[str] = Field(default_factory=list)
    name: str = ""


@app.get("/", response_class=HTMLResponse)
def index(_request: Request) -> HTMLResponse:
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Browsers otherwise pair a fresh index.html with a stale cached app.js after upgrades.
    for name in ("app.js", "app.css"):
        version = int((WEB_DIR / "static" / name).stat().st_mtime)
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={version}\"")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/meta")
def api_meta() -> dict[str, Any]:
    port = int(SERVER["port"])
    svc = get_service()
    return {
        "app_name": APP_NAME,
        "tagline": APP_TAGLINE,
        "version": __version__,
        "repo_url": REPO_URL,
        "web_port": port,
        "lan": bool(SERVER["lan"]),
        "lan_urls": web_urls(lan_ips(), port) if SERVER["lan"] else web_urls([], port),
        "room_temp_printer_id": svc.room_ref_id(),
        "has_printers": bool(svc.catalog.printers),
        "drivers": [{"id": d.id, "label": d.label} for d in drivers().values()],
        "quality_checks": quality_checks_payload(),
    }


@app.get("/api/printers")
def api_printers() -> list[dict[str, Any]]:
    return get_service().printers_payload()


@app.get("/api/resins")
def api_resins() -> list[dict[str, Any]]:
    return get_service().primary_resins()


@app.get("/api/models")
def api_models() -> list[dict[str, Any]]:
    return get_service().models_payload()


@app.get("/api/discover")
def api_discover(target: str = "") -> dict[str, Any]:
    """Look for printers on the network (or only at ``target``, an IP address)."""
    return get_service().discover(target.strip())


class AddPrintersBody(BaseModel):
    printers: list[dict[str, Any]] = Field(default_factory=list)


@app.post("/api/fleet/printers")
def api_add_printers(body: AddPrintersBody) -> dict[str, Any]:
    try:
        return get_service().add_printers(body.printers)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class UpdatePrinterBody(BaseModel):
    name: str = ""
    host: str = ""


@app.patch("/api/fleet/printers/{printer_id}")
def api_update_printer(printer_id: str, body: UpdatePrinterBody) -> dict[str, Any]:
    try:
        return get_service().update_printer(printer_id, body.name, body.host)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.delete("/api/fleet/printers/{printer_id}")
def api_remove_printer(printer_id: str) -> dict[str, Any]:
    return get_service().remove_printer(printer_id)


@app.get("/api/fleet/new")
def api_new_printers() -> dict[str, Any]:
    """Printers seen on the network that are not in the fleet and not dismissed."""
    return get_service().new_printers_payload()


class IgnorePrinterBody(BaseModel):
    key: str = ""


@app.post("/api/fleet/ignore")
def api_ignore_printer(body: IgnorePrinterBody) -> dict[str, Any]:
    try:
        return get_service().ignore_printer(body.key)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/fleet/removed")
def api_removed_printers() -> dict[str, Any]:
    """Printers taken out of the fleet. Their history is still stored under the old id."""
    return get_service().removed_printers()


@app.get("/api/settings")
def api_settings() -> dict[str, Any]:
    return get_service().settings_payload()


@app.patch("/api/settings")
def api_update_settings(body: dict[str, Any]) -> dict[str, Any]:
    try:
        return get_service().update_settings(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/fleet")
def api_fleet(refresh: bool = Query(False)) -> dict[str, Any]:
    svc = get_service()
    if refresh:
        return svc.refresh_fleet()
    return svc.fleet_payload()


class ImportHistoryBody(BaseModel):
    printer_ids: list[str] = Field(default_factory=list)
    dry_run: bool = False


@app.post("/api/history/import-printers")
def api_import_printer_history(body: ImportHistoryBody) -> dict[str, Any]:
    return get_service().import_printer_history(body.printer_ids or None, body.dry_run)


class VatResinBody(BaseModel):
    resin_id: str


@app.post("/api/vat-resin/{printer_id}")
def api_set_vat_resin(printer_id: str, body: VatResinBody) -> dict[str, Any]:
    try:
        return get_service().set_vat_resin(printer_id, body.resin_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/today")
def api_today(resin_id: str | None = None, part_family: str = "", size_class: str = "") -> dict[str, Any]:
    return get_service().today_profiles(resin_id or "", part_family, size_class)


@app.get("/api/geometries")
def api_geometries() -> list[dict[str, str]]:
    from .models import GEOMETRY_CHOICES

    return [{"id": g.value, "label": g.label} for g in GEOMETRY_CHOICES]


@app.post("/api/recommend")
def api_recommend(body: RecommendBody) -> dict[str, Any]:
    svc = get_service()
    try:
        return svc.recommend(
            body.printer_id,
            body.resin_id,
            body.layer_height,
            body.vat_temperature_c,
            body.geometry,
            body.cross_section_mm2,
            body.part_family,
            body.size_class,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/parts")
def api_parts(printer_id: str = "") -> dict[str, Any]:
    return get_service().parts(printer_id)


class PartGroupBody(BaseModel):
    family_ids: list[str] = Field(default_factory=list)
    group: str = ""


@app.post("/api/part-groups")
def api_set_part_group(body: PartGroupBody) -> dict[str, Any]:
    try:
        return get_service().set_part_group(body.family_ids, body.group)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/shape-scan")
def api_shape_scan() -> dict[str, Any]:
    return get_service().start_shape_scan()


@app.get("/api/detected")
def api_detected() -> dict[str, Any]:
    """Model opened in Chitubox and the last file sent from it, with exposure checks."""
    return get_service().detected_payload()


@app.post("/api/part-families")
def api_rename_part_family(body: PartFamilyBody) -> dict[str, Any]:
    try:
        return get_service().rename_part_family(body.auto_keys, body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/history")
def api_history_all() -> dict[str, Any]:
    return get_service().history_for(None, limit=5000)


@app.get("/api/history/{printer_id}")
def api_history(printer_id: str) -> dict[str, Any]:
    svc = get_service()
    if printer_id not in svc.catalog.printers:
        raise HTTPException(404, "Unknown printer")
    return svc.history_for(printer_id)


@app.get("/api/charts-fleet")
def api_charts_fleet() -> dict[str, Any]:
    return get_service().fleet_charts()


@app.get("/api/charts/{printer_id}")
def api_charts(printer_id: str, resin_id: str = "") -> dict[str, Any]:
    svc = get_service()
    if printer_id not in svc.catalog.printers:
        raise HTTPException(404, "Unknown printer")
    return svc.charts_for(printer_id, resin_id)


@app.post("/api/report")
def api_report(body: ReportBody) -> dict[str, Any]:
    svc = get_service()
    if body.printer_id not in svc.catalog.printers:
        raise HTTPException(404, "Unknown printer")
    try:
        return svc.report_outcome(
            body.printer_id,
            body.outcome,
            body.notes,
            body.resin_id,
            body.part_application,
            body.failure_issue,
            body.layer_height,
            body.quality,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/history/run/{run_id}/report")
def api_amend_run(run_id: int, body: ReportBody) -> dict[str, Any]:
    try:
        return get_service().amend_run(
            run_id,
            body.outcome,
            body.notes,
            body.part_application,
            body.failure_issue,
            body.quality,
            {**body.settings, "resin_id": body.resin_id} if body.resin_id else body.settings,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/history/pending/{pending_id}/report")
def api_report_pending(pending_id: int, body: ReportBody) -> dict[str, Any]:
    try:
        return get_service().report_pending(
            pending_id,
            body.outcome,
            body.notes,
            body.resin_id,
            body.part_application,
            body.failure_issue,
            body.quality,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/history/pending/{pending_id}")
def api_dismiss_pending(pending_id: int) -> dict[str, Any]:
    get_service().history.dismiss_unreported(pending_id)
    return {"ok": True}


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    for name in ("favicon.ico", "favicon.png"):
        path = ASSETS_DIR / "branding" / name
        if path.is_file():
            return FileResponse(path)
    raise HTTPException(404)
