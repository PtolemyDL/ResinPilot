from __future__ import annotations

import os
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from resinpilot.model_scan import load_triangles, mesh_volume_mm3, scan_model
from resinpilot.parts import guess_group
from resinpilot.shape_scan import TAG_CHANNELS
from resinpilot.slicer_watch import match_printers, match_resin, newest_model, opened_models, resin_profile_name


def _box(x0, y0, z0, x1, y1, z1, inward=False):
    v = np.array([[x, y, z] for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)], dtype=float)
    faces = [
        (0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5),  # x0, x1
        (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6),  # y0, y1
        (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3),  # z0, z1
    ]
    tris = np.array([[v[a], v[b], v[c]] for a, b, c in faces])
    return tris[:, ::-1] if inward else tris


def _write_stl(path: Path, tris: np.ndarray) -> None:
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(tris)))
        for t in tris:
            fh.write(struct.pack("<3f", 0, 0, 0) + struct.pack("<9f", *t.reshape(-1)) + b"\0\0")


def test_stl_volume_and_channel(tmp_path):
    # 20 × 10 × 10 mm block with a 1 mm square channel running straight up through it.
    block = _box(0, 0, 0, 20, 10, 10)
    hole = _box(9.5, 4.5, 0, 10.5, 5.5, 10, inward=True)
    solid = _box(0, 0, 0, 20, 10, 10)
    _write_stl(tmp_path / "solid.stl", solid)
    _write_stl(tmp_path / "channel.stl", np.concatenate([block, hole]))

    tris = load_triangles(tmp_path / "solid.stl")
    assert abs(mesh_volume_mm3(tris) - 2000) < 1e-6
    assert scan_model(tmp_path / "solid.stl", sample=8)["tags"] == []
    scan = scan_model(tmp_path / "channel.stl", sample=8)
    assert TAG_CHANNELS in scan["tags"]
    assert 0.9 < scan["channel_mm"] < 1.3
    assert scan["size_mm"] == [20.0, 10.0, 10.0]


def test_opened_models_from_thumbnail_log(tmp_path):
    a, b = tmp_path / "Valve Lid v3.stl", tmp_path / "Jig.stl"
    for p in (a, b):
        p.write_bytes(b"solid x\nendsolid x\n")
    log = tmp_path / "thumbnail.log"
    log.write_text(
        f"[2026.10.08 - 11:41:10]dll-INFO(RecipeThumbnailProvider::GetThumbnail:106)file:{b}\n"
        f"[2026.10.08 - 11:41:10]dll-INFO(FileToSaveImage::isNeedCreateNewImage:96)need generate:{b}\n"
        f"[2026.10.09 - 18:03:08]dll-INFO(RecipeThumbnailProvider::GetThumbnail:106)file:{a}\n"
        f"[2026.10.09 - 18:04:00]dll-INFO(RecipeThumbnailProvider::GetThumbnail:106)file:{tmp_path / 'gone.stl'}\n",
        encoding="utf-8",
    )
    found = opened_models(log)
    assert [f.path.name for f in found] == ["Valve Lid v3.stl", "Jig.stl"]
    # A model saved into the same folder later counts as newer than the logged ones.
    newer = tmp_path / "Brand new.stl"
    newer.write_bytes(b"solid x\nendsolid x\n")
    os.utime(newer, (time.time(), time.time()))
    assert newest_model(log).path == newer


def test_resin_profile_and_matching():
    resins = {
        "elegoo_8k_standard_space_grey": SimpleNamespace(name="ELEGOO 8K Standard Resin Space Grey", brand="ELEGOO", color="Space Grey"),
        "elegoo_abs_like_3_translucent": SimpleNamespace(name="ELEGOO ABS-like 3.0 Translucent", brand="ELEGOO", color="Translucent"),
        "anycubic_clear": SimpleNamespace(name="Anycubic Clear Resin", brand="Anycubic", color="Clear"),
        "anycubic_high_clear": SimpleNamespace(name="Anycubic High Clear Resin", brand="Anycubic", color="Clear"),
        "elegoo_8k_standard_space_grey": SimpleNamespace(name="ELEGOO 8K Standard Resin Space Grey (legacy id)", brand="ELEGOO", color="Space Grey"),
    }
    assert resin_profile_name({"ResinName": "normal", "ResinType": "normal"}) == ""
    assert resin_profile_name({"ResinName": "normal", "ResinType": "Translucent ABS"}) == "Translucent ABS"
    assert match_resin("Space Grey", resins) == "elegoo_8k_standard_space_grey"
    assert match_resin("Translucent ABS", resins) == "elegoo_abs_like_3_translucent"
    assert match_resin("Anycubic Clear", resins) == "anycubic_clear"
    assert match_resin("High Clear", resins) == "anycubic_high_clear"
    assert match_resin("Elegoo Standard 2.0", resins) == ""
    assert match_resin("", resins) == ""


def test_match_printers_by_machine_name():
    printers = {
        "saturn_4_ultra_16k": SimpleNamespace(name="Big", brand="ELEGOO", model_name="Saturn 4 Ultra 16K"),
        "saturn_4_ultra": SimpleNamespace(name="Saturn", brand="ELEGOO", model_name="Saturn 4 Ultra"),
        "mars_5_ultra_left": SimpleNamespace(name="Left", brand="ELEGOO", model_name="Mars 5 Ultra"),
        "mars_5_ultra_right": SimpleNamespace(name="Right", brand="ELEGOO", model_name="Mars 5 Ultra"),
    }
    assert match_printers("ELEGOO Mars 5 Ultra", printers) == ["mars_5_ultra_left", "mars_5_ultra_right"]
    assert match_printers("ELEGOO Saturn 4 Ultra", printers) == ["saturn_4_ultra"]
    assert match_printers("ELEGOO Saturn 4 Ultra 16K", printers) == ["saturn_4_ultra_16k"]
    assert match_printers("Unknown", printers) == []


def test_guess_group_for_new_part():
    known = {"Valve", "Manifold", "Jigs"}
    assert guess_group("Valve Lid Ver.Alex v47", known) == "Valve"
    assert guess_group("Top jig part c", known) == "Jigs"
    assert guess_group("PartALid", known) == "Other"
