"""Printer models, resins, baselines (built in + user additions) and the user's printers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from .fleet import FleetEntry, load_fleet
from .models import BaselineEntry, MotionTemplate, PrinterModel, PrintSettings, Printer, Resin
from .paths import BUILTIN_DATA_DIR, user_dir

# Generic starting point for a resin with neither a model baseline nor maker numbers.
GENERIC_EXPOSURE_S = 2.5
GENERIC_BOTTOM_EXPOSURE_S = 30.0


def _load_yaml(path: Path) -> Any:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _items(name: str, key: str, builtin_dir: Path, extra_dir: Path | None) -> list[dict[str, Any]]:
    """Built-in entries, then the user's file of the same name (same id replaces the built-in one)."""
    merged: dict[str, dict[str, Any]] = {}
    unnamed: list[dict[str, Any]] = []
    for base in (builtin_dir, extra_dir):
        if base is None:
            continue
        for item in _load_yaml(base / name).get(key) or []:
            if item.get("id"):
                merged[str(item["id"])] = item
            else:
                unnamed.append(item)
    return list(merged.values()) + unnamed


def _motion(raw: dict[str, Any] | None) -> MotionTemplate:
    known = MotionTemplate.__dataclass_fields__  # type: ignore[attr-defined]
    return MotionTemplate(**{k: float(v) for k, v in (raw or {}).items() if k in known})


def model_from_dict(item: dict[str, Any]) -> PrinterModel:
    return PrinterModel(
        id=str(item["id"]),
        brand=str(item.get("brand") or ""),
        name=str(item.get("name") or item["id"]),
        driver=str(item.get("driver") or "sdcp"),
        match_names=tuple(str(n) for n in item.get("match_names") or [item.get("name") or ""] if n),
        machine_type=str(item.get("machine_type") or f"{item.get('brand', '')} {item.get('name', '')}".strip()),
        machine_width=float(item.get("machine_width") or 0),
        machine_depth=float(item.get("machine_depth") or 0),
        machine_height=float(item.get("machine_height") or 0),
        resolution_x=int(item.get("resolution_x") or 0),
        resolution_y=int(item.get("resolution_y") or 0),
        project_type=str(item.get("project_type") or "LCD_mirror"),
        photo=str(item.get("photo") or ""),
        specs=tuple(item.get("specs") or []),
        motion=_motion(item.get("motion")),
    )


def load_models(builtin_dir: Path = BUILTIN_DATA_DIR, extra_dir: Path | None = None) -> dict[str, PrinterModel]:
    return {m.id: m for m in (model_from_dict(i) for i in _items("printer_models.yaml", "models", builtin_dir, extra_dir))}


def load_resins(builtin_dir: Path = BUILTIN_DATA_DIR, extra_dir: Path | None = None) -> dict[str, Resin]:
    resins: dict[str, Resin] = {}
    for item in _items("resins.yaml", "resins", builtin_dir, extra_dir):
        resins[item["id"]] = Resin(
            id=item["id"],
            name=item["name"],
            brand=item.get("brand", ""),
            color=item.get("color", ""),
            material_family=item.get("material_family", "standard"),
            density=float(item.get("density", 1.1)),
            min_normal_exposure=float(item.get("min_normal_exposure", 1.2)),
            min_bottom_exposure=float(item.get("min_bottom_exposure", 12.0)),
            photo=str(item.get("photo") or ""),
            swatch=str(item.get("swatch") or "#8E8E93"),
            notes=str(item.get("notes") or ""),
            typical_uses=str(item.get("typical_uses") or ""),
            wavelength_nm=int(item.get("wavelength_nm", 405)),
            store_url=str(item.get("store_url") or item.get("purchase_url") or "").strip(),
            amazon_url=str(item.get("amazon_url") or "").strip(),
            starting_exposure=float(item.get("starting_exposure") or 0),
            starting_bottom_exposure=float(item.get("starting_bottom_exposure") or 0),
            translucent=bool(item.get("translucent") or False),
        )
    return resins


def _settings_from(item: dict[str, Any]) -> PrintSettings:
    retract = float(item.get("rest_time_after_retract", 1.2))
    return PrintSettings(
        layer_height=float(item["layer_height"]),
        normal_exposure_time=float(item["normal_exposure_time"]),
        bottom_exposure_time=float(item["bottom_exposure_time"]),
        bottom_layer_count=int(item.get("bottom_layer_count", 5)),
        transition_layer_count=int(item.get("transition_layer_count", 5)),
        bottom_layer_lift_height=float(item.get("bottom_layer_lift_height", 0.0)),
        bottom_layer_lift_speed=float(item.get("bottom_layer_lift_speed", 0.0)),
        normal_layer_lift_height=float(item.get("normal_layer_lift_height", 0.0)),
        normal_layer_lift_speed=float(item.get("normal_layer_lift_speed", 0.0)),
        normal_drop_speed=float(item.get("normal_drop_speed", 0.0)),
        rest_time_after_retract=retract,
        rest_time_before_lift=float(item.get("rest_time_before_lift", 0.0)),
        rest_time_after_lift=float(item.get("rest_time_after_lift", 0.0)),
        bottom_rest_time_after_retract=float(item.get("bottom_rest_time_after_retract", retract)),
        bottom_rest_time_before_lift=float(item.get("bottom_rest_time_before_lift", 0.0)),
        bottom_rest_time_after_lift=float(item.get("bottom_rest_time_after_lift", 0.0)),
    )


def load_baselines(builtin_dir: Path = BUILTIN_DATA_DIR, extra_dir: Path | None = None) -> list[BaselineEntry]:
    entries: list[BaselineEntry] = []
    for item in _items("baselines.yaml", "baselines", builtin_dir, extra_dir):
        entries.append(
            BaselineEntry(
                # Baselines belong to a printer model; Catalog.get_baseline maps printers onto them.
                printer_id=str(item.get("model_id") or item.get("printer_id")),
                resin_id=item["resin_id"],
                layer_height=float(item["layer_height"]),
                vat_temperature_c=float(item.get("vat_temperature_c", 25.0)),
                settings=_settings_from(item),
                notes=item.get("notes", ""),
            )
        )
    return entries


def printer_from_entry(entry: FleetEntry, model: PrinterModel) -> Printer:
    return Printer(
        id=entry.id,
        name=entry.name,
        machine_type=model.machine_type,
        machine_width=model.machine_width,
        machine_depth=model.machine_depth,
        machine_height=model.machine_height,
        resolution_x=model.resolution_x,
        resolution_y=model.resolution_y,
        motion=model.motion,
        project_type=model.project_type,
        photo=entry.photo or model.photo,
        specs=model.specs,
        sdcp_ip=entry.host,
        sdcp_mainboard_id=entry.device_id,
        model_id=model.id,
        driver=entry.driver,
        brand=model.brand,
        model_name=model.name,
    )


class Catalog:
    """Models, resins and baselines (built in + user folder), plus the user's printers."""

    def __init__(self, builtin_dir: Path = BUILTIN_DATA_DIR, extra_dir: Path | None = None, fleet: list[FleetEntry] | None = None) -> None:
        self.builtin_dir = builtin_dir
        self.extra_dir = extra_dir if extra_dir is not None else user_dir()
        self.models = load_models(builtin_dir, self.extra_dir)
        self.resins = load_resins(builtin_dir, self.extra_dir)
        self.baselines = load_baselines(builtin_dir, self.extra_dir)
        self.printers: dict[str, Printer] = {}
        self.set_fleet(load_fleet() if fleet is None else fleet)

    def set_fleet(self, fleet: list[FleetEntry]) -> None:
        printers: dict[str, Printer] = {}
        for entry in fleet:
            model = self.models.get(entry.model_id)
            if model is None and entry.model:
                model = model_from_dict({"id": entry.model_id, **entry.model})
            if model is None:
                continue
            printers[entry.id] = printer_from_entry(entry, model)
        self.printers = printers

    def match_model(self, brand: str, model_name: str, driver: str = "sdcp") -> PrinterModel | None:
        """Library model whose name (or alias) matches what the printer reports."""
        want = model_name.strip().lower()
        brand_l = brand.strip().lower()
        for model in self.models.values():
            if model.driver != driver:
                continue
            names = {n.lower() for n in model.match_names} | {model.name.lower(), model.full_name.lower()}
            if want in names and (not brand_l or not model.brand or model.brand.lower() == brand_l):
                return model
        return None

    def get_baseline(self, printer_id: str, resin_id: str) -> BaselineEntry | None:
        printer = self.printers.get(printer_id)
        model_id = printer.model_id if printer else printer_id
        own = [b for b in self.baselines if b.printer_id == model_id and b.resin_id == resin_id]
        if own:
            return _prefer_005(own)
        borrowed = [b for b in self.baselines if b.resin_id == resin_id]
        if borrowed:
            src = _closest_pixel(borrowed, printer, self.models) or _prefer_005(borrowed)
            donor = self.models.get(src.printer_id)
            note = (
                f"No settings for this printer model yet — starting from the "
                f"{donor.full_name if donor else src.printer_id} values; report a few prints to tune them."
            )
            return replace(src, notes=note)
        resin = self.resins.get(resin_id)
        if resin is None:
            return None
        exp = resin.starting_exposure or GENERIC_EXPOSURE_S
        bottom = resin.starting_bottom_exposure or GENERIC_BOTTOM_EXPOSURE_S
        source = "the resin maker's numbers" if resin.starting_exposure else "a generic starting point"
        return BaselineEntry(
            printer_id=model_id,
            resin_id=resin_id,
            layer_height=0.05,
            vat_temperature_c=25.0,
            settings=PrintSettings(layer_height=0.05, normal_exposure_time=exp, bottom_exposure_time=bottom),
            notes=f"Starting from {source} — run an exposure test and report a few prints to tune it.",
        )

    @property
    def primary_resins(self) -> dict[str, Resin]:
        return dict(self.resins)


def _prefer_005(matches: list[BaselineEntry]) -> BaselineEntry:
    for b in matches:
        if abs(b.layer_height - 0.05) < 1e-6:
            return b
    return matches[0]


def _closest_pixel(candidates: list[BaselineEntry], printer: Printer | None, models: dict[str, PrinterModel]) -> BaselineEntry | None:
    """Among other models' baselines, the one whose screen has the most similar pixel size."""
    if printer is None or not printer.pixel_um:
        return None

    def pixel(model_id: str) -> float:
        m = models.get(model_id)
        return 1000.0 * m.machine_width / m.resolution_x if m and m.resolution_x and m.machine_width else 0.0

    scored = [(abs(pixel(b.printer_id) - printer.pixel_um), b) for b in candidates if pixel(b.printer_id)]
    if not scored:
        return None
    best = min(s for s, _ in scored)
    return _prefer_005([b for s, b in scored if s == best])
