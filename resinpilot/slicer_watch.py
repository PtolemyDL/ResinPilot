"""What is being prepared in Chitubox on this computer, before it prints (Windows).

- Models opened in Chitubox: its thumbnail provider logs every model file it renders
  (``%APPDATA%/CHITUBOX_Thumbnail/thumbnail.log``); the newest .stl in those folders also counts.
- Files sent with Chitubox's network upload: ChituManager keeps a copy of each sliced file
  (``%LOCALAPPDATA%/ChituManager/cbdsa-chitubox-network/SlicerFile/<time>/<hash>.ctb``).

On other systems (or without Chitubox) these paths don't exist and the watcher stays idle.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .model_scan import MODEL_EXTENSIONS
from .shape_scan import _run

def _env_path(var: str, *parts: str) -> Path:
    base = os.environ.get(var, "")
    # Without the variable, point somewhere that cannot exist rather than at a relative path.
    return Path(base).joinpath(*parts) if base else Path(os.devnull) / "missing"


THUMBNAIL_LOG = _env_path("APPDATA", "CHITUBOX_Thumbnail", "thumbnail.log")
SLICER_FILE_DIR = _env_path("LOCALAPPDATA", "ChituManager", "cbdsa-chitubox-network", "SlicerFile")
SLICED_EXTENSIONS = (".ctb", ".goo", ".prz", ".jxs")
LOG_TAIL_BYTES = 256_000

_LOG_LINE = re.compile(r"^\[(\d{4}\.\d{2}\.\d{2} - \d{2}:\d{2}:\d{2})\].*GetThumbnail:\d+\)file:(.+?)\s*$")
# Chitubox's default resin profile names carry no information about the resin.
GENERIC_RESIN_NAMES = {"", "normal", "default", "standard", "resin"}


@dataclass(frozen=True)
class ModelFile:
    path: Path
    seen_at: float  # epoch seconds

    @property
    def key(self) -> str:
        try:
            return f"{self.path}|{self.path.stat().st_mtime:.0f}"
        except OSError:
            return str(self.path)


def opened_models(log_path: Path | None = None, limit: int = 20) -> list[ModelFile]:
    """Model files Chitubox rendered recently, newest first (one entry per file)."""
    log_path = log_path or THUMBNAIL_LOG
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    latest: dict[str, ModelFile] = {}
    for line in text.splitlines():
        m = _LOG_LINE.match(line)
        if not m:
            continue
        path = Path(m.group(2))
        if path.suffix.lower() not in MODEL_EXTENSIONS:
            continue
        try:
            when = datetime.strptime(m.group(1), "%Y.%m.%d - %H:%M:%S").timestamp()
        except ValueError:
            continue
        latest[str(path).lower()] = ModelFile(path, when)
    found = sorted(latest.values(), key=lambda f: f.seen_at, reverse=True)
    return [f for f in found if f.path.is_file()][:limit]


def newest_model(log_path: Path | None = None) -> ModelFile | None:
    """Most recent model: opened in Chitubox, or saved into a folder Chitubox opens models from."""
    opened = opened_models(log_path)
    candidates = list(opened)
    for folder in {f.path.parent for f in opened}:
        try:
            for p in folder.iterdir():
                if p.suffix.lower() in MODEL_EXTENSIONS and p.is_file():
                    candidates.append(ModelFile(p, p.stat().st_mtime))
        except OSError:
            continue
    return max(candidates, key=lambda f: f.seen_at, default=None)


def sliced_files(folder: Path | None = None) -> list[tuple[Path, float]]:
    """(path, modified time) of sliced files sent through Chitubox, newest first."""
    folder = folder or SLICER_FILE_DIR
    if not folder.is_dir():
        return []
    out = []
    for p in folder.rglob("*"):
        if p.suffix.lower() in SLICED_EXTENSIONS and p.is_file():
            try:
                out.append((p, p.stat().st_mtime))
            except OSError:
                continue
    return sorted(out, key=lambda t: t[1], reverse=True)


_PROPS = {
    "MachineName": str,
    "ResinName": str,
    "ResinType": str,
    "MaterialName": str,
    "LayerHeight": float,
    "ExposureTime": float,
    "BottomExposureTime": float,
    "BottomLayerCount": int,
    "TransitionLayerCount": int,
    "LayerCount": int,
    "MaterialMilliliters": float,
    "ResinColorR": int,
    "ResinColorG": int,
    "ResinColorB": int,
}


def sliced_properties(path: Path) -> dict[str, Any]:
    """Machine, resin profile and exposure settings stored in a sliced file (via UVtools)."""
    out = _run(["print-properties", str(path)], timeout_s=300)
    props: dict[str, Any] = {}
    for key, cast in _PROPS.items():
        m = re.search(rf"\b{key}: ([^,\r\n]+)", out)
        if not m:
            continue
        try:
            props[key] = cast(m.group(1).strip())
        except ValueError:
            continue
    return props


def resin_profile_name(props: dict[str, Any]) -> str:
    """The resin profile name chosen in Chitubox, unless it is a generic default."""
    for key in ("ResinName", "MaterialName", "ResinType"):
        name = str(props.get(key) or "").strip()
        if name.lower() not in GENERIC_RESIN_NAMES:
            return name
    return ""


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if w}


def match_resin(profile_name: str, resins: dict[str, Any]) -> str:
    """Catalog resin id whose name best matches a Chitubox resin profile name ('' if none fits)."""
    if not profile_name:
        return ""
    want = _words(profile_name)
    brand_words = set().union(*(_words(getattr(r, "brand", "")) for r in resins.values())) if resins else set()
    generic = brand_words | {"resin", "standard", "8k", "12k", "16k", "3", "0", "like", "plus", "pro", "v2", "v3"}
    best, best_score = "", 0.0
    for rid, resin in resins.items():
        have = _words(f"{resin.name} {getattr(resin, 'brand', '')} {getattr(resin, 'color', '')} {rid.replace('_', ' ')}")
        common = want & have
        # Colour/finish words decide between resins of one family (Space Grey vs Translucent).
        distinctive = common - generic
        if not distinctive:
            continue
        # Ties ('Clear' vs 'High Clear') go to the resin with fewer unmatched words.
        score = len(common) / len(want) + len(distinctive) - 0.01 * len(have - want)
        if score > best_score:
            best, best_score = rid, score
    return best


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s*\(.*?\)", "", (text or "").lower())).strip()


def match_printers(machine_name: str, printers: dict[str, Any]) -> list[str]:
    """Printer ids of the model named in a sliced file ('ELEGOO Mars 5 Ultra' → every Mars 5 Ultra)."""
    want = _norm(machine_name)
    if not want:
        return []
    out = []
    for pid, printer in printers.items():
        model = getattr(printer, "model_name", "") or ""
        names = {
            _norm(getattr(printer, "machine_type", "")),
            _norm(model),
            _norm(f"{getattr(printer, 'brand', '')} {model}"),
        } - {""}
        if want in names:
            out.append(pid)
    return out
