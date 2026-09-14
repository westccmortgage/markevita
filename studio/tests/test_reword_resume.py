"""A line that does not fit must be editable without losing the video.

The voice stage stops with "Shorten it in the script and save again."
Shortening it changed the package digest, and start() then refused with
"create a new episode for changed content". The instruction could not be
followed: miami/s01e01_v2 was stranded with eight generated clips and no way
to reach them.

Rewording is the one edit that cannot invalidate a clip — the video prompt
carries only visible action and the engine never sends dialogue text to the
video model. These tests pin that, and pin that nothing else is let through.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from app.live_jobs import (  # noqa: E402
    VOICE_STAGES, drop_voice_work, spoken_words_removed)

EPISODE = "s01e01_v2"
CHECKSUMS = {"bible/characters.json": "aaa", "bible/style.json": "bbb",
             f"episodes/{EPISODE}/brief.json": "ccc"}
SCENES = [{
    "scene_id": "sc05", "sequence": 5, "duration": 4, "duration_seconds": 4,
    "location": "villa", "lighting_state": "night", "characters_in_frame": ["nora"],
    "wardrobe": {"nora": "w_evening"}, "action": "She reads the list.",
    "shot_type": "medium", "lens": "50mm", "camera_motion": "slow push-in",
    "continuity_in": "list folded", "continuity_out": "list open", "props": [],
    "is_cliffhanger": False, "split": False, "lipsync_speaker": "nora",
    "dialogue": [{"speaker": "nora", "delivery": "quiet", "voice_over": False,
                  "text": "Your name is on the passenger list and nobody told me."}],
}]


def _sig(checksums=CHECKSUMS, scenes=SCENES):
    return spoken_words_removed(checksums, scenes, EPISODE)


def _edited(**scene_changes):
    scenes = copy.deepcopy(SCENES)
    scenes[0].update(scene_changes)
    return scenes


# ── what may change ────────────────────────────────────────────────────────

def test_rewording_a_line_is_recognised_as_the_same_production():
    scenes = copy.deepcopy(SCENES)
    scenes[0]["dialogue"][0]["text"] = "Your name is on it."
    assert _sig(scenes=scenes) == _sig()


def test_the_episode_brief_checksum_is_ignored():
    """Its text is the thing allowed to change, so its file hash must not
    decide the comparison."""
    checksums = {**CHECKSUMS, f"episodes/{EPISODE}/brief.json": "changed"}
    assert _sig(checksums=checksums) == _sig()


# ── what may not ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("change", [
    {"duration": 8, "duration_seconds": 8},        # the clip itself differs
    {"action": "She burns the list."},             # the video prompt differs
    {"characters_in_frame": ["nora", "adrian"]},
    {"wardrobe": {"nora": "w_arrival"}},
    {"location": "pier"},
    {"lighting_state": "default"},
    {"camera_motion": "static"},
    {"shot_type": "close-up"},
    {"lens": "85mm"},
    {"continuity_out": "list burned"},
    {"props": [{"prop_id": "list", "state": "folded"}]},
])
def test_a_visual_change_is_not_treated_as_rewording(change):
    assert _sig(scenes=_edited(**change)) != _sig()


def test_changing_who_speaks_is_not_rewording():
    scenes = copy.deepcopy(SCENES)
    scenes[0]["dialogue"][0]["speaker"] = "adrian"
    assert _sig(scenes=scenes) != _sig()


def test_changing_delivery_is_not_rewording():
    """Delivery reaches the voice model and the lip-sync timing."""
    scenes = copy.deepcopy(SCENES)
    scenes[0]["dialogue"][0]["delivery"] = "shouted"
    assert _sig(scenes=scenes) != _sig()


def test_adding_a_line_is_not_rewording():
    """An added line can re-split the scene and change its clip ids."""
    scenes = copy.deepcopy(SCENES)
    scenes[0]["dialogue"].append({"speaker": "nora", "delivery": "", "voice_over": False, "text": "Say it."})
    assert _sig(scenes=scenes) != _sig()


def test_a_bible_or_style_change_is_not_rewording():
    assert _sig(checksums={**CHECKSUMS, "bible/style.json": "changed"}) != _sig()


def test_dropping_a_scene_is_not_rewording():
    assert _sig(scenes=[]) != _sig()


# ── what is discarded when rewording is accepted ───────────────────────────

class _State:
    def __init__(self, data):
        self.data = data
        self.saves = 0

    def save(self):
        self.saves += 1


def _produced():
    return _State({
        "status": "complete",
        "stages": {"intake": "done", "direction": "done", "references": "done",
                   "keyframes": "done", "video": "done", "voice": "done",
                   "lipsync": "done", "assemble": "done", "qa": "done", "deliver": "done"},
        "scenes": {"sc05": {"keyframe": {"path": "kf.png"}, "video": {"path": "v.mp4"},
                            "voice": {"cues": []}, "lipsync": {"path": "ls.mp4"}}},
        "audio": {"sc05_l00": {"local_path": "a.mp3"}},
        "takes": {"ep_sc05_video_00": {"status": "succeeded"}},
        "spent_usd": 4.12,
    })


def test_only_speech_is_forgotten():
    state = _produced()
    drop_voice_work(state)
    scene = state.data["scenes"]["sc05"]
    # Paid visual work survives.
    assert scene["keyframe"] and scene["video"]
    assert state.data["stages"]["video"] == "done"
    assert state.data["stages"]["keyframes"] == "done"
    assert state.data["stages"]["references"] == "done"
    assert state.data["takes"], "takes and their provenance are never dropped"
    assert state.data["spent_usd"] == 4.12, "spending already incurred still counts"
    # Speech and everything downstream of it is gone.
    assert "voice" not in scene and "lipsync" not in scene
    assert "audio" not in state.data
    assert not any(stage in state.data["stages"] for stage in VOICE_STAGES)
    assert state.data["status"] == "voice_pending"
    assert state.saves == 1


def test_dropping_voice_work_is_idempotent():
    state = _produced()
    drop_voice_work(state)
    first = copy.deepcopy(state.data)
    drop_voice_work(state)
    assert state.data == first
