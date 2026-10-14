"""Part families (same part across versions, from the sliced file name) and size classes.

Chitubox names files like ``3in1_Manifold_v59.stl_#45_0.025_1.600_2026_10_02_15_13.ctb``.
The family is the model name with version tokens removed, so v59, v60 and v61 group together.
Names can be overridden (renamed or merged) in ``part_families.json`` in the user folder.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .history import HistoryRecord

from .paths import user_path


def families_path() -> Path:
    return user_path("part_families.json")


UNNAMED = "Unnamed"

SIZE_SMALL = "small"
SIZE_MEDIUM = "medium"
SIZE_BULKY = "bulky"
SIZE_LABELS = {SIZE_SMALL: "Small", SIZE_MEDIUM: "Medium", SIZE_BULKY: "Bulky"}
SMALL_BELOW_MM2 = 300.0
BULKY_FROM_MM2 = 700.0

_EXT = re.compile(r"\.(ctb|goo|prz|jxs|cbddlp|photon\w*|pwmx|pw0)$", re.I)
_SLICE_TAIL = re.compile(r"[\s_]*(#\d+[\s_]*)?_?\d+(\.\d+)?_\d+(\.\d+)?_\d{4}_\d{2}_\d{2}(_\d{2}){2,3}$")
_VERSION = re.compile(r"[\s_\-]*v\d+[a-z]?(\.\d+)*[a-z]?$", re.I)
_TRAILING_TAG = re.compile(r"[\s_\-]*#\d+$")
_TRAILING_NUMBER = re.compile(r"[\s_\-]+\d+$")
_LEADING_DATE = re.compile(r"^\d{4}\s+")


def model_stem(filename: str) -> str:
    """Model name as typed in the slicer, without slicer suffix or extension."""
    name = _EXT.sub("", (filename or "").strip())
    m = re.search(r"\.stl", name, re.I)
    if m:
        name = name[: m.start()]
    else:
        name = _SLICE_TAIL.sub("", name)
    return name.strip(" _-")


def _strip_versions(stem: str) -> str:
    prev = None
    name = _LEADING_DATE.sub("", stem)
    while name != prev:
        prev = name
        for pat in (_VERSION, _TRAILING_TAG):
            stripped = pat.sub("", name)
            if stripped.strip(" _-"):
                name = stripped
        stripped = _TRAILING_NUMBER.sub("", name)
        if stripped.strip(" _-"):
            name = stripped
    return name.strip(" _-")


def family_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def auto_family(filename: str) -> tuple[str, str]:
    """(key, display name) inferred from a sliced file name."""
    base = _strip_versions(model_stem(filename))
    key = family_key(base)
    if not key:
        return family_key(UNNAMED), UNNAMED
    return key, re.sub(r"[_\s]+", " ", base).strip()


def _read_overrides(path: Path, section: str) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): str(v) for k, v in (data.get(section) or {}).items() if str(v).strip()}


def _write_override(path: Path, section: str, key: str, value: str) -> None:
    data = {s: _read_overrides(path, s) for s in ("names", "groups")}
    value = value.strip()
    if value:
        data[section][key] = value
    else:
        data[section].pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({s: dict(sorted(v.items())) for s, v in data.items()}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def load_overrides(path: Path | None = None) -> dict[str, str]:
    return _read_overrides(path or families_path(), "names")


def save_override(auto_key: str, name: str, path: Path | None = None) -> None:
    """Rename an auto family; giving two families the same name merges them. Empty name resets."""
    _write_override(path or families_path(), "names", auto_key, name)


def load_group_overrides(path: Path | None = None) -> dict[str, str]:
    return _read_overrides(path or families_path(), "groups")


def save_group_override(family_id: str, group: str, path: Path | None = None) -> None:
    """Put a part (family id) in a named group; empty goes back to the automatic group."""
    _write_override(path or families_path(), "groups", family_id, group)


OTHER_GROUP = "Other"
# Words naming a component or variant rather than the product; never a group on their own.
_GROUP_STOP = {
    "base", "lid", "top", "bottom", "cap", "cover", "test", "testing", "part", "new", "front", "left",
    "right", "side", "closed", "close", "open", "single", "with", "for", "and", "the", "larger", "wider",
    "extra", "gap", "safety", "ver", "version", "copy", "final", "fix", "fixed", "no", "port",
}
# Tooling is grouped by what it is, not by the part it holds.
_GROUP_TOOLING = {"jig": "Jigs", "fixture": "Fixtures", "mold": "Molds", "mould": "Molds"}


def group_tokens(name: str) -> list[str]:
    """Meaningful lowercase words in a part name: 'ValveV88Base' → ['valve'] (base is a component word)."""
    s = re.sub(r"(?<=[A-Za-z])[vV](?=\d)", " v", name)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    words = []
    for w in re.split(r"[^A-Za-z]+", s):
        w = w.lower()
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if len(w) >= 3 and w not in _GROUP_STOP:
            words.append(w)
    return words


def auto_groups(names: dict[str, str]) -> dict[str, str]:
    """family id → group name. A part joins its most widely shared word's group; groups of one → Other."""
    tokens = {fid: group_tokens(name) for fid, name in names.items()}
    df: Counter[str] = Counter(t for toks in tokens.values() for t in set(toks))
    picked: dict[str, str] = {}
    for fid, toks in tokens.items():
        tooling = next((_GROUP_TOOLING[t] for t in toks if t in _GROUP_TOOLING), None)
        if tooling:
            picked[fid] = tooling
            continue
        shared = [t for t in toks if df[t] >= 2]
        picked[fid] = max(shared, key=lambda t: (df[t], -toks.index(t))).capitalize() if shared else OTHER_GROUP
    sizes = Counter(picked.values())
    return {fid: g if sizes[g] >= 2 else OTHER_GROUP for fid, g in picked.items()}


def guess_group(name: str, known_groups: set[str]) -> str:
    """Group for a part not printed yet: tooling, or the first word naming an existing group."""
    toks = group_tokens(name)
    tooling = next((_GROUP_TOOLING[t] for t in toks if t in _GROUP_TOOLING), None)
    if tooling:
        return tooling
    return next((t.capitalize() for t in toks if t.capitalize() in known_groups), OTHER_GROUP)


class PartFamilies:
    """Resolves file names to family ids/names, honouring renames and merges."""

    def __init__(
        self,
        records: list[HistoryRecord],
        overrides: dict[str, str] | None = None,
        scans: dict[str, dict[str, Any]] | None = None,
        group_overrides: dict[str, str] | None = None,
    ) -> None:
        self.overrides = overrides if overrides is not None else load_overrides()
        names: dict[str, Counter[str]] = {}
        for r in records:
            key, display = auto_family(r.filename)
            names.setdefault(key, Counter())[display] += 1
        self.auto_names = {k: c.most_common(1)[0][0] for k, c in names.items()}
        family_names = {}
        for key in self.auto_names:
            fid, name, _ = self.resolve_key(key)
            family_names[fid] = name
        self.auto_group = auto_groups(family_names)
        self.group_overrides = group_overrides if group_overrides is not None else load_group_overrides()
        if scans is None:
            from .shape_scan import load_scans

            scans = load_scans()
        # Newest successful scan of any file in the family stands for the part's shape;
        # sliced files (what was actually printed) win over unsliced models.
        self.shapes: dict[str, dict[str, Any]] = {}
        order = lambda kv: (kv[1].get("source") != "stl", kv[1].get("scanned_at") or "")  # noqa: E731
        for filename, scan in sorted(scans.items(), key=order):
            if scan.get("layers_sampled"):
                self.shapes[self.family_id(filename)] = {**scan, "file": filename}
        # Parts only known from a scanned model (not printed yet): name and best-guess group.
        known = set(self.auto_group.values()) - {OTHER_GROUP}
        self.model_names: dict[str, str] = {}
        for filename in scans:
            fid, name, _ = self.resolve(filename)
            if fid not in self.auto_group and fid not in self.model_names:
                self.model_names[fid] = name
                self.auto_group[fid] = guess_group(name, known)

    def name(self, family_id: str) -> str:
        """Display name of a family id (history first, then scanned models)."""
        for key in self.auto_names:
            fid, name, _ = self.resolve_key(key)
            if fid == family_id:
                return name
        return self.model_names.get(family_id, family_id)

    def shape(self, family_id: str) -> dict[str, Any] | None:
        return self.shapes.get(family_id)

    def shape_tags(self, family_id: str) -> tuple[str, ...]:
        return tuple(sorted((self.shapes.get(family_id) or {}).get("tags") or ()))

    def group(self, family_id: str) -> str:
        """Generic group a part belongs to (e.g. every valve variant → 'Valve')."""
        return self.group_overrides.get(family_id) or self.auto_group.get(family_id) or OTHER_GROUP

    def resolve_key(self, key: str, display: str = "") -> tuple[str, str, str]:
        name = self.overrides.get(key) or self.auto_names.get(key) or display or UNNAMED
        return family_key(name) or key, name, key

    def resolve(self, filename: str) -> tuple[str, str, str]:
        """(family id, family name, auto key). Families renamed to the same name share an id."""
        key, display = auto_family(filename)
        return self.resolve_key(key, display)

    def family_id(self, filename: str) -> str:
        return self.resolve(filename)[0]


def size_metrics(record: HistoryRecord) -> dict[str, float] | None:
    """Resin volume (ml), print height (mm) and average area per layer (mm²) from slice info."""
    si = (record.telemetry or {}).get("slice_info") or {}
    try:
        volume = float(si.get("volume") or 0)
        layers = int(si.get("total_layer_numbers") or record.total_layers or 0)
        layer_h = float(si.get("layer_height") or record.layer_height or 0)
    except (TypeError, ValueError):
        return None
    height = layers * layer_h
    if volume <= 0 or height <= 0:
        return None
    return {"volume_ml": volume, "height_mm": height, "avg_area_mm2": volume * 1000.0 / height}


def size_class_for_area(avg_area_mm2: float) -> str:
    if avg_area_mm2 < SMALL_BELOW_MM2:
        return SIZE_SMALL
    if avg_area_mm2 >= BULKY_FROM_MM2:
        return SIZE_BULKY
    return SIZE_MEDIUM


def size_class(record: HistoryRecord) -> str:
    m = size_metrics(record)
    return size_class_for_area(m["avg_area_mm2"]) if m else ""


def part_info(record: HistoryRecord, families: PartFamilies) -> dict[str, Any]:
    fid, name, key = families.resolve(record.filename)
    m = size_metrics(record)
    return {
        "family_id": fid,
        "family": name,
        "family_key": key,
        "group": families.group(fid),
        "shape_tags": list(families.shape_tags(fid)),
        "size_class": size_class_for_area(m["avg_area_mm2"]) if m else "",
        **(m or {}),
    }
