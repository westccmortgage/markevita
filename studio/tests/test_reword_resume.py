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
    # 'lens' is deliberately absent: the direction stage chooses it when the
    # script does not, so it is a result, not an instruction. See
    # test_a_lens_the_director_chose_is_not_a_changed_script.
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


# ── editing a line in place ────────────────────────────────────────────────

@pytest.fixture
def store(monkeypatch, tmp_path):
    from app import ingest, packaging, runner, scripts, web
    from app.store.local import LocalDriver
    driver = LocalDriver(tmp_path / "store")
    for module in (scripts, web, runner, ingest, packaging):
        monkeypatch.setattr(module, "store", driver, raising=False)
    driver.upsert("series", {"id": "miami", "title": "Miami"})
    driver.upsert("episodes", {"series_id": "miami", "episode_id": EPISODE, "number": 1})
    driver.upsert("scenes", {
        "series_id": "miami", "episode_id": EPISODE, "scene_id": "sc05", "sequence": 5,
        "duration_seconds": 4, "location": "villa", "lighting_state": "night",
        "characters_in_frame": ["nora"], "wardrobe": {"nora": "w_evening"},
        "action": "She reads the list.", "shot_type": "medium", "lens": "50mm",
        "camera_motion": "slow push-in", "continuity_in": "folded", "continuity_out": "open",
        "props": [], "knowledge_required": [], "knowledge_gained": [],
        "relationship_changes": [], "is_cliffhanger": False, "status": "complete",
        "dialogue": [{"speaker": "nora", "delivery": "quiet", "voice_over": False,
                      "text": "Your name is on the passenger list and nobody told me."}]})
    return driver


def _reword(text):
    from app.scripts import reword_scene
    return reword_scene("miami", EPISODE, "sc05", [text], "admin@example.test")


def test_rewording_changes_only_the_words(store):
    _reword("Your name is on it.")
    line = store.get("scenes", {"series_id": "miami", "episode_id": EPISODE,
                                "scene_id": "sc05"})["dialogue"][0]
    assert line["text"] == "Your name is on it."
    assert line["speaker"] == "nora" and line["delivery"] == "quiet"
    assert line["voice_over"] is False


def test_the_edit_is_exactly_what_resume_accepts(store):
    """The whole point: this edit must not look like a different production."""
    before = store.get("scenes", {"series_id": "miami", "episode_id": EPISODE, "scene_id": "sc05"})
    _reword("Your name is on it.")
    after = store.get("scenes", {"series_id": "miami", "episode_id": EPISODE, "scene_id": "sc05"})
    assert spoken_words_removed({}, [before], EPISODE) == spoken_words_removed({}, [after], EPISODE)


def test_the_saved_script_is_rewritten_to_match(store):
    """Otherwise the stored script would describe a different episode."""
    _reword("Your name is on it.")
    script = store.list("scripts", {"series_id": "miami", "episode_id": EPISODE})[-1]
    assert "Your name is on it." in script["content"]
    assert "passenger list" not in script["content"]
    assert "SCENE sc05 | 4s | villa | night" in script["content"]
    assert script["source"] == "reword"


def test_the_rewritten_script_parses_back_to_the_same_scene(store):
    """Round trip: the serializer and the parser must agree."""
    from app.scripts import parse
    _reword("Your name is on it.")
    content = store.list("scripts", {"series_id": "miami", "episode_id": EPISODE})[-1]["content"]
    scene = parse(content)["scenes"][0]
    assert scene["scene_id"] == "sc05" and scene["duration_seconds"] == 4
    assert scene["location"] == "villa" and scene["lighting_state"] == "night"
    assert scene["characters_in_frame"] == ["nora"]
    assert scene["wardrobe"] == {"nora": "w_evening"}
    assert scene["dialogue"][0] == {"speaker": "nora", "delivery": "quiet",
                                    "text": "Your name is on it."}


def test_changing_the_line_count_is_refused(store):
    from app.scripts import ScriptError, reword_scene
    with pytest.raises(ScriptError, match="number of lines"):
        reword_scene("miami", EPISODE, "sc05", ["one", "two"], "admin@example.test")


def test_an_empty_line_is_refused(store):
    from app.scripts import ScriptError
    with pytest.raises(ScriptError, match="cannot be empty"):
        _reword("   ")


def test_an_unknown_scene_is_refused(store):
    from app.scripts import ScriptError, reword_scene
    with pytest.raises(ScriptError, match="not found"):
        reword_scene("miami", EPISODE, "sc99", ["x"], "admin@example.test")


# ── correcting the bible after a provider refused a reference ──────────────

class _Plain:
    """Just enough of the engine's episode state for these checks."""

    def __init__(self, data):
        self.data = data
        self.saved = 0

    def save(self):
        self.saved += 1


def test_a_refused_reference_does_not_strand_the_episode():
    """fal refused a wardrobe reference under its content policy. Rewriting the
    wardrobe changed the package digest, and the resume then refused with "it
    needs a new episode" — so the only fix for the failure also made the
    episode unproducible."""
    from app.live_jobs import scene_work_exists
    state = _Plain({"stages": {"intake": "done", "direction": "done"},
                    "takes": {"ref_v1_maya_fullbody_front__beach_bikini":
                              {"status": "failed", "what": "ref maya/fullbody"}},
                    "scenes": {}})
    assert scene_work_exists(state) is False


def test_a_generated_clip_still_blocks_a_changed_script():
    """The guard must keep doing its job where there is something to protect."""
    from app.live_jobs import scene_work_exists
    assert scene_work_exists(_Plain({"takes": {}, "scenes": {"sc05": {"video": "sc05.mp4"}}}))
    assert scene_work_exists(_Plain({"scenes": {}, "takes": {
        "s01e01_sc05_vid_0": {"scene_id": "sc05", "status": "succeeded"}}}))
    assert scene_work_exists(_Plain({"scenes": {}, "takes": {
        "s01e01_sc05_kf_0": {"scene_id": "sc05", "status": "submitted"}}}))


def test_the_plan_is_made_again_from_the_corrected_bible():
    """The director's prompts quote the wardrobe. Keeping them would send the
    refused wording to the image provider one stage later."""
    from app.live_jobs import drop_planning
    state = _Plain({"stages": {"intake": "done", "direction": "done", "references": "done"}})
    drop_planning(state)
    assert "intake" not in state.data["stages"] and "direction" not in state.data["stages"]
    assert state.data["stages"]["references"] == "done"
    assert state.saved == 1


# ── settings that only decide how the finished episode is packaged ─────────

from app.live_jobs import generated_shape, shape_differences, shape_parts  # noqa: E402

BRIEF = {"language": "en-US", "aspect_ratio": "9:16", "width": 1080, "height": 1920,
         "captions": "both", "music": "off", "bible_version": "aaaaaaaaaaaa",
         "total_seconds": 4, "scenes": SCENES}


def _shape(checksums=CHECKSUMS, brief=None, scenes=SCENES, with_words=True):
    return generated_shape(checksums, brief or BRIEF, scenes, EPISODE, with_words)


def test_turning_music_on_is_the_same_production():
    """The producer was told to set this before resuming, and it stranded an
    episode whose 28 scenes were already shot."""
    after = {**BRIEF, "music": "generate", "captions": "none"}
    assert _shape(brief=after) == _shape()


def test_rehashing_the_series_file_is_the_same_production():
    """The stored digest is a hash of whatever the code hashed that day. When
    the recipe changed, every episode in production read as a different
    script; this comparison is recomputed from both sides instead."""
    rehashed = {**CHECKSUMS, "series.json": "a-different-way-of-hashing"}
    assert _shape(checksums=rehashed) == _shape(checksums={**CHECKSUMS, "series.json": "raw"})


def test_a_different_aspect_ratio_is_not_the_same_production():
    """It is read from the brief precisely because series.json is left out."""
    assert _shape(brief={**BRIEF, "aspect_ratio": "16:9"}) != _shape()


def test_a_changed_bible_is_not_the_same_production():
    assert _shape(checksums={**CHECKSUMS, "bible/characters.json": "zzz"}) != _shape()


def test_a_reworded_line_is_a_changed_production_until_the_words_are_dropped():
    scenes = copy.deepcopy(SCENES)
    scenes[0]["dialogue"][0]["text"] = "Your name is on it."
    assert _shape(scenes=scenes) != _shape()
    assert _shape(scenes=scenes, with_words=False) == _shape(with_words=False)


def test_a_changed_action_is_not_the_same_production_either_way():
    scenes = _edited(action="She burns the list.")
    assert _shape(scenes=scenes) != _shape()
    assert _shape(scenes=scenes, with_words=False) != _shape(with_words=False)


def test_a_refusal_names_what_moved():
    """Guessing what had changed cost two rounds of deploy-and-press-again."""
    from app.live_jobs import shape_differences, shape_parts
    before = shape_parts(CHECKSUMS, BRIEF, SCENES, EPISODE)
    after = shape_parts({**CHECKSUMS, "bible/style.json": "zzz"},
                        {**BRIEF, "aspect_ratio": "16:9"},
                        _edited(action="She burns the list."), EPISODE)
    moved = shape_differences(before, after)
    assert any(m.startswith("the bible and style files") for m in moved)
    assert "the format and language: aspect_ratio" in moved
    assert any(m.startswith("scene sc05: action") for m in moved)


def test_nothing_is_named_when_nothing_moved():
    assert shape_differences(shape_parts(CHECKSUMS, BRIEF, SCENES, EPISODE),
                             shape_parts(CHECKSUMS, BRIEF, SCENES, EPISODE)) == []


def test_a_lens_the_director_chose_is_not_a_changed_script():
    """When the script names no lens, the direction stage picks one and writes
    it into the scene. The saved episode therefore has a lens the freshly read
    script does not, and comparing it compared a result against its own input:
    every episode that reached direction could never be resumed again."""
    directed = copy.deepcopy(SCENES)
    directed[0]["lens"] = "85mm"
    as_written = copy.deepcopy(SCENES)
    as_written[0].pop("lens", None)
    assert shape_differences(shape_parts(CHECKSUMS, BRIEF, directed, EPISODE),
                             shape_parts(CHECKSUMS, BRIEF, as_written, EPISODE)) == []
