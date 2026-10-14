"""Shape of an unsliced model (.stl), before it is printed.

Cross-sections are rasterised straight from the mesh and fed to the same per-layer analysis as
sliced files, so channel / fine-feature tags mean the same thing. The model is taken as oriented
in the file. Size classes are not derived here: History measures the whole plate (copies and
supports), which only the sliced file knows.
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .shape_scan import TARGET_PX_MM, analyze_mask, summarize

MODEL_EXTENSIONS = (".stl",)
SAMPLE_SECTIONS = 40
# Coarser pixels on large parts keep a scan to seconds; 0.5 mm features stay ≥ 10 px wide.
MAX_RASTER_SIDE = 2000


def load_triangles(path: Path) -> np.ndarray:
    """(n, 3, 3) float64 triangle vertices from a binary or ASCII STL."""
    data = Path(path).read_bytes()
    if len(data) >= 84:
        (n,) = struct.unpack_from("<I", data, 80)
        if n and len(data) == 84 + 50 * n:
            rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]), count=n, offset=84)
            return rec["v"].astype(np.float64)
    text = data.decode("ascii", errors="ignore")
    nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    if not nums or len(nums) % 3:
        raise ValueError("Not a readable STL file")
    return np.array(nums, dtype=np.float64).reshape(-1, 3, 3)


def mesh_volume_mm3(tris: np.ndarray) -> float:
    return float(abs(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum()) / 6.0)


def section_mask(tris: np.ndarray, z: float, origin: np.ndarray, px: float, shape: tuple[int, int]) -> np.ndarray:
    """Solid pixels of the cross-section at height z (even-odd fill of the cut segments)."""
    d = tris[:, :, 2] - z
    hit = (d.min(axis=1) < 0) & (d.max(axis=1) > 0)
    t, dz = tris[hit], d[hit]
    pts = []
    for a, b in ((0, 1), (1, 2), (2, 0)):
        cross = (dz[:, a] < 0) != (dz[:, b] < 0)
        f = np.where(cross, dz[:, a] / np.where(cross, dz[:, a] - dz[:, b], 1.0), np.nan)
        pts.append(t[:, a, :2] + (t[:, b, :2] - t[:, a, :2]) * f[:, None])
    pts = np.stack(pts, axis=1)  # (m, 3 edges, xy); exactly two edges are finite
    ok = ~np.isnan(pts[:, :, 0])
    keep = ok.sum(axis=1) == 2
    pts, ok = pts[keep], ok[keep]
    seg = pts[ok].reshape(-1, 2, 2)
    mask = np.zeros(shape, dtype=bool)
    if not len(seg):
        return mask
    # Row centres crossed by each segment → toggle at the crossing column; XOR-accumulate along the row.
    (x1, y1), (x2, y2) = (seg[:, 0, 0], seg[:, 0, 1]), (seg[:, 1, 0], seg[:, 1, 1])
    r1 = (y1 - origin[1]) / px - 0.5
    r2 = (y2 - origin[1]) / px - 0.5
    lo = np.ceil(np.minimum(r1, r2)).astype(np.int64)
    hi = np.ceil(np.maximum(r1, r2)).astype(np.int64)  # half-open [lo, hi)
    counts = np.clip(hi - lo, 0, None)
    if not counts.sum():
        return mask
    idx = np.repeat(np.arange(len(seg)), counts)
    rows = np.repeat(lo, counts) + (np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts))
    yc = origin[1] + (rows + 0.5) * px
    xa, xb, ya, yb = x1[idx], x2[idx], y1[idx], y2[idx]
    xc = xa + (xb - xa) * (yc - ya) / (yb - ya)
    cols = np.ceil((xc - origin[0]) / px - 0.5).astype(np.int64)
    inside = (rows >= 0) & (rows < shape[0])
    toggles = np.zeros((shape[0], shape[1] + 1), dtype=np.uint8)
    np.add.at(toggles, (rows[inside], np.clip(cols[inside], 0, shape[1])), 1)
    return (np.cumsum(toggles[:, :-1], axis=1, dtype=np.int32) & 1).astype(bool)


def scan_model(path: Path, sample: int = SAMPLE_SECTIONS) -> dict[str, Any]:
    """Shape tags of a model file, comparable to `shape_scan.scan_file` results."""
    tris = load_triangles(path)
    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    size = hi - lo
    height = float(size[2])
    if height <= 0:
        raise ValueError("Model has no height")
    px = max(TARGET_PX_MM, float(max(size[0], size[1])) / MAX_RASTER_SIDE)
    origin = lo[:2] - 2 * px
    shape = (int(np.ceil(size[1] / px)) + 4, int(np.ceil(size[0] / px)) + 4)
    zs = np.linspace(lo[2] + height * 0.02, hi[2] - height * 0.02, num=sample)
    layers, areas = [], []
    for z in zs:
        m = section_mask(tris, float(z), origin, px, shape)
        areas.append(float(m.sum()) * px * px)
        layers.append(analyze_mask(m, px))
    return {
        **summarize(layers),
        "source": "stl",
        "px_mm": px,
        "volume_ml": mesh_volume_mm3(tris) / 1000.0,
        "size_mm": [round(float(s), 2) for s in size],
        "max_area_mm2": max(areas) if areas else 0.0,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
