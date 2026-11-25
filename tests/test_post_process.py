"""Every resin in the catalog has a post-processing SOP the printer cards can show."""

from __future__ import annotations

from resinpilot.catalog import load_resins
from resinpilot.post_process import load_sops, sops_payload


def test_every_resin_has_one_sop() -> None:
    payload = sops_payload()
    resins = load_resins()
    assert set(resins) <= set(payload["by_resin"])
    claimed = [rid for sop in load_sops() for rid in sop["resins"]]
    assert len(claimed) == len(set(claimed)), "a resin is listed under two SOPs"


def test_sops_are_complete() -> None:
    for sop in load_sops():
        keys = [s["key"] for s in sop["steps"]]
        assert len(keys) == len(set(keys)), sop["id"]
        assert {"dirty", "clean", "dry", "cure"} <= set(keys), sop["id"]
        assert keys.index("dry") < keys.index("cure"), sop["id"]
        assert all(len(s["rail"]) <= 4 for s in sop["steps"]), sop["id"]
        assert sop["sources"] and all(s["url"].startswith("https://") for s in sop["sources"]), sop["id"]


def test_sculpt_grey_follows_shop_routine() -> None:
    sop = sops_payload()["sops"]["siraya_sculpt_grey"]
    steps = {s["key"]: s for s in sop["steps"]}
    assert [s["key"] for s in sop["steps"]] == ["dirty", "clean", "brush", "dry", "cure"]
    assert steps["dirty"]["time"] == "2 min" and steps["clean"]["time"] == "1 min"
    assert steps["cure"]["time"] == "10 min"
    assert any("5 minutes in alcohol" in c for c in sop["cautions"])
