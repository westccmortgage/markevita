"""Episode 4's approved story, rhythm, and spend controls."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from app.the_wild_cat_episode_4 import (  # noqa: E402
    DURATIONS, KLING, SCENES, VIDEO_ROUTING, VEO_FAST, _brief,
)
from serial import media  # noqa: E402
from serial.package import estimate_first_pass  # noqa: E402
from serial.schema import EPISODE  # noqa: E402


def test_episode_four_is_a_valid_single_forest_scene_story():
    brief = _brief()
    jsonschema.validate(brief, EPISODE)
    assert sum(DURATIONS) == 76
    assert len(SCENES) == 12
    assert [s["scene_id"] for s in SCENES if s["location"] == "spring_forest"] == ["sc01"]
    assert SCENES[6]["relationship_changes"][0]["state"] == "misunderstanding"
    assert SCENES[9]["relationship_changes"][0]["state"] == "separated"


def test_first_pass_prices_only_the_eight_selected_video_scenes():
    norm = {
        "scenes": [
            {"scene_id": s["scene_id"], "source_scene_id": s["scene_id"],
             "duration": s["duration_seconds"]}
            for s in SCENES
        ],
        "limits": {"budget": 35.0},
        "video_routing": VIDEO_ROUTING,
    }
    package = SimpleNamespace(characters={}, locations={}, props={})
    cfg = SimpleNamespace(video_generate_audio=False, video_resolution="1080p",
                          fal_video_model=VEO_FAST, image_resolution="1K",
                          lipsync_variant="lipsync-2")

    estimate = estimate_first_pass(norm, package, cfg, refs_needed=False)
    paid = [item for item in estimate["video_plan"] if item["mode"] == "video"]
    stills = [item for item in estimate["video_plan"] if item["mode"] == "motion_still"]
    assert len(paid) == 8 and len(stills) == 4
    assert {item["provider"] for item in paid} == {VEO_FAST, KLING}
    assert estimate["video"] == pytest.approx(5.54)
    assert estimate["paid_fallbacks"] == "manual"
    assert estimate["total_first_pass"] < estimate["budget_cap"]


def test_motion_still_has_exact_duration_and_no_external_provider(tmp_path):
    still = tmp_path / "still.png"
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=forestgreen:s=320x568", "-frames:v", "1", str(still),
    ], check=True)
    result = media.slow_push_from_still(still, 1.0, tmp_path / "push.mp4", 320, 568)
    info = media.probe(result)
    assert info["duration"] == pytest.approx(1.0, abs=0.05)
    assert (info["width"], info["height"]) == (320, 568)
    assert info["has_audio"] is True
