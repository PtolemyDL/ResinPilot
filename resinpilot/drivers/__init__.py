"""Printer drivers: how ResinPilot finds printers and reads their status.

A driver is any object with the attributes and methods of :class:`PrinterDriver`.
ResinPilot ships the SDCP driver (ELEGOO and other ChiTu-board printers). Other packages
can add drivers through the ``resinpilot.drivers`` entry-point group::

    [project.entry-points."resinpilot.drivers"]
    mybrand = "resinpilot_mybrand:MyBrandDriver"

See CONTRIBUTING.md for a walkthrough.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

from ..sdcp import PrinterLiveStatus


@dataclass
class FoundPrinter:
    """A printer that answered on the network."""

    driver: str
    host: str
    device_id: str
    name: str  # the nickname set on the printer
    model_name: str  # e.g. "Saturn 4 Ultra"
    brand: str = ""
    firmware: str = ""
    # Filled in by describe(): resolution_x/y, machine_width/depth/height, …
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class PrinterDriver(Protocol):
    id: str
    label: str

    def discover(self, timeout_s: float = 2.5, target: str = "") -> list[FoundPrinter]:
        """Printers on the local network (or only at ``target`` when given)."""

    def describe(self, printer: FoundPrinter) -> dict[str, Any]:
        """Extra facts about one printer (resolution, build volume) for models not in the library."""

    def status(self, host: str, device_id: str) -> PrinterLiveStatus:
        """Live status of one printer."""


_registry: dict[str, PrinterDriver] | None = None


def drivers() -> dict[str, PrinterDriver]:
    global _registry
    if _registry is None:
        from .sdcp_driver import SdcpDriver

        found: dict[str, PrinterDriver] = {"sdcp": SdcpDriver()}
        for ep in entry_points(group="resinpilot.drivers"):
            if ep.name in found:
                continue
            try:
                driver = ep.load()()
            except Exception:  # noqa: BLE001 — a broken plugin must not stop the app
                continue
            if isinstance(driver, PrinterDriver):
                found[driver.id] = driver
        _registry = found
    return _registry


def get_driver(driver_id: str) -> PrinterDriver | None:
    return drivers().get(driver_id)
