"""Shape features from sliced files: internal channels and thin features, via UVtools layer images.

Chitubox .ctb files are encrypted, so layers are decoded with UVtoolsCmd (UVtools, open source),
then a sample of layer masks is analysed here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage

from .paths import user_path

SCANS_NAME = "shape_scans.json"
# Usual install locations; UVTOOLS_CMD or PATH win over these.
UVTOOLS_CANDIDATES = (
    Path(r"C:\Program Files\UVtools\UVtoolsCmd.exe"),
    Path("/Applications/UVtools.app/Contents/MacOS/UVtoolsCmd"),
    Path("/usr/bin/UVtoolsCmd"),
    Path("/opt/uvtools/UVtoolsCmd"),
)

SAMPLE_LAYERS = 60
# Analyse at ≥ this pixel size; 0.5 mm channels are still ~20 px across.
TARGET_PX_MM = 0.025
# Enclosed holes smaller than this are anti-aliasing noise, larger than this are cut-outs, not channels.
MIN_HOLE_MM2 = 0.01
CHANNEL_MAX_DIAMETER_MM = 4.0
# Solid regions narrower than this count as fine features.
THIN_FEATURE_MM = 0.5
# Tag thresholds
CHANNEL_LAYER_PCT = 10.0
FINE_PCT = 8.0

TAG_CHANNELS = "channels"
TAG_FINE = "fine"
TAG_LABELS = {TAG_CHANNELS: "Internal channels", TAG_FINE: "Fine features"}

_lock = threading.Lock()
# 16K layers are ~94 MP — above PIL's default decompression-bomb limit.
MAX_LAYER_PIXELS = 200_000_000


def uvtools_cmd() -> Path | None:
    env = os.environ.get("UVTOOLS_CMD")
    if env and Path(env).is_file():
        return Path(env)
    found = shutil.which("UVtoolsCmd")
    if found:
        return Path(found)
    return next((c for c in UVTOOLS_CANDIDATES if c.is_file()), None)


def _run(args: list[str], timeout_s: float) -> str:
    cmd = uvtools_cmd()
    if cmd is None:
        raise RuntimeError("UVtools is not installed")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run(
        [str(cmd), "--no-progress", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        creationflags=flags,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "UVtools failed").strip()[-400:])
    return proc.stdout


def file_properties(path: Path) -> dict[str, float]:
    """Pixel size and layer info from `UVtoolsCmd print-properties`."""
    out = _run(["print-properties", str(path)], timeout_s=300)
    props: dict[str, float] = {}
    for key in ("ResolutionX", "ResolutionY", "DisplayWidth", "DisplayHeight", "LayerCount", "LayerHeight"):
        m = re.search(rf"^{key}: ([\d.]+)", out, flags=re.MULTILINE)
        if m:
            props[key] = float(m.group(1))
    if not props.get("ResolutionX") or not props.get("DisplayWidth"):
        raise RuntimeError("Could not read resolution from the file")
    props["px_mm"] = props["DisplayWidth"] / props["ResolutionX"]
    return props


def _downsample(mask: np.ndarray, factor: int) -> np.ndarray:
    if factor <= 1:
        return mask
    h, w = (mask.shape[0] // factor) * factor, (mask.shape[1] // factor) * factor
    return mask[:h, :w].reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3)) >= 0.5


def analyze_mask(mask: np.ndarray, px_mm: float) -> dict[str, Any] | None:
    """Channels (enclosed holes) and thin-feature share of one layer's solid area."""
    factor = max(1, int(round(TARGET_PX_MM / px_mm)))
    mask = _downsample(mask, factor)
    px = px_mm * factor
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return None
    mask = np.pad(mask[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1], 2)
    solid = int(mask.sum())

    empty, n_empty = ndimage.label(~mask)
    border = np.unique(np.concatenate([empty[0], empty[-1], empty[:, 0], empty[:, -1]]))
    areas = ndimage.sum(np.ones_like(empty, dtype=np.int32), empty, index=np.arange(1, n_empty + 1)) * px * px
    channel_max_mm2 = np.pi * (CHANNEL_MAX_DIAMETER_MM / 2) ** 2
    diameters = sorted(
        2 * float(np.sqrt(a / np.pi))
        for lab, a in enumerate(areas, start=1)
        if lab not in border and MIN_HOLE_MM2 <= a <= channel_max_mm2
    )

    # Morphological opening via two distance transforms: solid not covered by a
    # THIN_FEATURE_MM-wide disc anywhere is a fine feature.
    r = THIN_FEATURE_MM / 2 / px
    core = ndimage.distance_transform_edt(mask) >= r
    if core.any():
        covered = ndimage.distance_transform_edt(~core) <= r
        thin = int((mask & ~covered).sum())
    else:
        thin = solid
    return {
        "solid_mm2": solid * px * px,
        "channels": len(diameters),
        "channel_diameters_mm": diameters,
        "thin_pct": 100.0 * thin / solid,
        "bodies": int(ndimage.label(mask)[1]),
    }


def summarize(layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Whole-print features from per-layer results."""
    layers = [l for l in layers if l]
    if not layers:
        return {"layers_sampled": 0, "tags": []}
    with_channels = [l for l in layers if l["channels"]]
    diameters = [d for l in with_channels for d in l["channel_diameters_mm"]]
    thin = [l["thin_pct"] for l in layers]
    out = {
        "layers_sampled": len(layers),
        "channel_layer_pct": 100.0 * len(with_channels) / len(layers),
        "max_channels": max((l["channels"] for l in layers), default=0),
        "channel_mm": float(np.median(diameters)) if diameters else None,
        "min_channel_mm": float(np.percentile(diameters, 10)) if diameters else None,
        "thin_pct": float(np.percentile(thin, 75)),
        "max_bodies": max(l["bodies"] for l in layers),
    }
    tags = []
    if out["channel_layer_pct"] >= CHANNEL_LAYER_PCT:
        tags.append(TAG_CHANNELS)
    if out["thin_pct"] >= FINE_PCT:
        tags.append(TAG_FINE)
    out["tags"] = tags
    return out


def scan_file(path: Path, sample: int = SAMPLE_LAYERS) -> dict[str, Any]:
    """Decode a sliced file with UVtools and summarise its shape (takes ~1–3 min)."""
    props = file_properties(path)
    work = Path(tempfile.mkdtemp(prefix="shape_scan_"))
    try:
        _run(["extract", str(path), str(work), "-c", "Layers"], timeout_s=1800)
        files = sorted(work.glob("layer*.png"))
        if not files:
            raise RuntimeError("UVtools extracted no layers")
        # Skip the bottom/transition layers (elephant foot) and the very top.
        lo, hi = min(len(files) - 1, max(16, len(files) // 50)), max(1, len(files) - len(files) // 50)
        idx = np.unique(np.linspace(lo, hi - 1, num=min(sample, max(1, hi - lo))).astype(int))
        layers = []
        Image.MAX_IMAGE_PIXELS = max(Image.MAX_IMAGE_PIXELS or 0, MAX_LAYER_PIXELS)
        for i in idx:
            with Image.open(files[i]) as im:
                layers.append(analyze_mask(np.asarray(im.convert("L")) > 127, props["px_mm"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {
        **summarize(layers),
        "px_mm": props["px_mm"],
        "layer_count": int(props.get("LayerCount") or len(files)),
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def load_scans(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or user_path(SCANS_NAME)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_scan(filename: str, result: dict[str, Any], path: Path | None = None) -> None:
    path = path or user_path(SCANS_NAME)
    with _lock:
        scans = load_scans(path)
        scans[filename] = result
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(scans, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)


def shape_text(scan: dict[str, Any] | None) -> str:
    """'Internal channels ~0.54 mm (in 80% of layers) · Fine features 10%'."""
    if not scan or not scan.get("layers_sampled"):
        return ""
    bits = []
    if TAG_CHANNELS in scan.get("tags", []):
        bits.append(
            f"Internal channels ~{scan['channel_mm']:.2f} mm (min {scan['min_channel_mm']:.2f} mm, "
            f"in {scan['channel_layer_pct']:.0f}% of layers)"
        )
    if TAG_FINE in scan.get("tags", []):
        bits.append(f"Fine features: {scan['thin_pct']:.0f}% of the cross-section is under {THIN_FEATURE_MM:g} mm wide")
    return " · ".join(bits) or "Solid — no internal channels or fine features found"
