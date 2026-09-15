"""From a wish in plain words to a script the engine accepts.

Covers the journey the producer actually takes: open the next episode of the
right series, describe what should happen, read it back as prose, change it in
plain words, and see what that change costs to redo.

The model is stubbed. These prove the studio's own rules — which series an
episode lands in, what is sent to the model, what is refused, and what is
written when it is refused — not that the model writes well.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from app import authoring  # noqa: E402
from app.store.local import LocalDriver  # noqa: E402
from serial.package import PackageError  # noqa: E402

MIAMI, ISLAND = "miami", "island_of_no_witnesses"


def _scene(n, speaker="nora", text="You knew?", cliff=False):
    return {"scene_id": f"sc{n:02d}", "sequence": n, "duration_seconds": 4,
            "location": "villa_terrace", "lighting_state": "default",
            "characters_in_frame": [speaker], "wardrobe": {speaker: "w_evening"},
            "action": "She holds the look.", "shot_type": "medium", "lens": "50mm",
            "camera_motion": "locked", "continuity_in": "at the door",
            "continuity_out": "still at the door",
            "dialogue": [{"speaker": speaker, "delivery": "quiet", "text": text}],
            "is_cliffhanger": cliff}


def _open_second(db):
    """Open s01e02 the way the producer does, with episode one already shot."""
    db.upsert("episodes", {"series_id": MIAMI, "episode_id": "s01e01", "season_id": "s01",
                           "number": 1, "title": "Pilot"})
    db.upsert("scenes", {"series_id": MIAMI, "episode_id": "s01e01", **_scene(1)})
    return authoring.next_episode(MIAMI)["episode_id"]


@pytest.fixture
def db(monkeypatch, tmp_path):
    from app import ingest, packaging
    driver = LocalDriver(tmp_path / "store")
    for module in (authoring, packaging, ingest):
        monkeypatch.setattr(module, "store", driver, raising=False)
    for sid, title in ((MIAMI, "Miami"), (ISLAND, "Island of No Witnesses")):
        driver.upsert("series", {"id": sid, "title": title, "language": "en-US",
                                 "production_limits": {"allowed_clip_seconds": [4, 6, 8]}})
        driver.upsert("seasons", {"series_id": sid, "season_id": "s01", "number": 1,
                                  "episode_order": []})
    driver.upsert("characters", {"series_id": MIAMI, "character_id": "nora", "name": "Nora",
                                 "visual": True, "appearance": "…"})
    driver.upsert("clothing", {"series_id": MIAMI, "character_id": "nora",
                               "variant_id": "w_evening", "is_default": True})
    driver.upsert("voices", {"series_id": MIAMI, "character_id": "nora",
                             "voice_env": "ELEVENLABS_VOICE_ID_NORA"})
    driver.upsert("locations", {"series_id": MIAMI, "location_id": "villa_terrace",
                                "name": "Villa terrace", "description": "Tiled terrace over the water.",
                                "lighting_states": {"default": "dusk"}})
    return driver


# ── the next episode lands in the right series ─────────────────────────────

def test_the_episode_is_created_in_the_series_asked_for(db):
    """A continuation of one series was once loaded into another."""
    episode = authoring.next_episode(MIAMI, "admin@example.test")
    assert episode["series_id"] == MIAMI
    assert not db.list("episodes", {"series_id": ISLAND})


def test_the_number_and_id_follow_the_season(db):
    db.upsert("episodes", {"series_id": MIAMI, "episode_id": "s01e01", "season_id": "s01",
                           "number": 1})
    db.upsert("scenes", {"series_id": MIAMI, "episode_id": "s01e01", "scene_id": "sc01"})
    episode = authoring.next_episode(MIAMI)
    assert episode["number"] == 2 and episode["episode_id"] == "s01e02"
    assert episode["season_id"] == "s01"


def test_pressing_twice_does_not_leave_two_blank_episodes(db):
    first = authoring.next_episode(MIAMI)
    second = authoring.next_episode(MIAMI)
    assert second["episode_id"] == first["episode_id"] and second["reused"] is True
    assert len(db.list("episodes", {"series_id": MIAMI})) == 1


def test_a_started_episode_is_never_reused(db):
    first = authoring.next_episode(MIAMI)
    db.insert("production_jobs", {"series_id": MIAMI, "episode_id": first["episode_id"],
                                  "state": "done", "idempotency_key": "k"})
    assert authoring.next_episode(MIAMI)["episode_id"] != first["episode_id"]


def test_technical_fields_are_filled_in(db):
    """The producer fills in none of these."""
    episode = authoring.next_episode(MIAMI)
    assert episode["budget_usd"] and episode["status"] == "draft"
    assert episode["episode_id"] in db.get("seasons", {"series_id": MIAMI,
                                                       "season_id": "s01"})["episode_order"]


def test_a_series_without_a_season_gets_one(db):
    """Opening a season is bookkeeping, not a decision to put to the producer."""
    db.delete("seasons", {"series_id": MIAMI})
    episode = authoring.next_episode(MIAMI, "admin@example.test")
    assert episode["season_id"] == "s01" and episode["episode_id"] == "s01e01"
    assert db.get("seasons", {"series_id": MIAMI, "season_id": "s01"})["number"] == 1


# ── what the model is told ─────────────────────────────────────────────────

def test_the_memory_carries_only_this_series(db):
    db.upsert("characters", {"series_id": ISLAND, "character_id": "someone_else",
                             "name": "Other", "visual": True})
    memory = authoring.series_memory(MIAMI, "s01e02")
    assert [c["id"] for c in memory["characters"]] == ["nora"]
    assert [l["id"] for l in memory["locations"]] == ["villa_terrace"]


def test_the_memory_carries_the_previous_ending(db):
    db.upsert("episodes", {"series_id": MIAMI, "episode_id": "s01e01", "number": 1})
    db.upsert("scenes", {"series_id": MIAMI, "episode_id": "s01e01", **_scene(8, text="You knew?")})
    db.upsert("knowledge_state", {"series_id": MIAMI, "episode_id": "s01e01", "kind": "knowledge",
                                  "subject_id": "secret_child", "value": ["maya"]})
    memory = authoring.series_memory(MIAMI, "s01e02")
    assert memory["previous_episode"] == "s01e01"
    assert memory["previous_final_scenes"][-1]["dialogue"][0]["text"] == "You knew?"
    assert memory["carried_state"]["knowledge"]["secret_child"] == ["maya"]


def test_the_dialogue_language_comes_from_the_series(db):
    """A Russian request must not make the characters speak Russian."""
    memory = authoring.series_memory(MIAMI, "s01e02")
    assert memory["dialogue_language"] == "en-US"
    assert "series dialogue language" in authoring.SYSTEM
    assert "language the producer's request is written in" in authoring.SYSTEM


def test_the_model_is_told_the_hard_limits():
    for rule in ("ONLY character, location", "ONE character who is on camera may speak",
                 "at most FOUR words", "word budget"):
        assert rule in authoring.SYSTEM


# ── a draft that fails validation changes nothing ──────────────────────────

def _stub(monkeypatch, answers):
    """A model that returns the given answers in turn."""
    replies = iter(answers)
    calls = []

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            text = next(replies)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=1000, output_tokens=2000))

    monkeypatch.setattr(authoring, "_client", lambda: SimpleNamespace(messages=_Messages()))
    return calls


def test_an_invalid_draft_never_replaces_existing_scenes(db, monkeypatch):
    authoring.next_episode(MIAMI)
    db.upsert("scenes", {"series_id": MIAMI, "episode_id": "s01e01", **_scene(1)})
    monkeypatch.setattr(authoring, "validate_candidate",
                        lambda *a, **k: (_ for _ in ()).throw(PackageError("sc01: unknown location")))
    _stub(monkeypatch, ['{"scenes": []}', '{"scenes": []}'])
    with pytest.raises(authoring.AuthoringError, match="unknown location"):
        authoring.draft(MIAMI, "s01e01", "something")
    assert db.list("scenes", {"series_id": MIAMI, "episode_id": "s01e01"}) == [
        {**_scene(1), "series_id": MIAMI, "episode_id": "s01e01",
         **{k: v for k, v in db.list("scenes")[0].items() if k == "id"}}]


def test_the_validators_complaint_is_sent_back_once(db, monkeypatch):
    """The correction round should fix the real problem, not guess at it."""
    _open_second(db)
    attempts = {"n": 0}

    def validate(series_id, episode_id, brief):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise PackageError("sc01: 40 words is too many for 4s")
        return {"scenes": brief["scenes"], "total_seconds": 4, "warnings": []}

    monkeypatch.setattr(authoring, "validate_candidate", validate)
    good = json.dumps({"title": "T", "scenes": [_scene(1, cliff=True)],
                       "cliffhanger": {"scene_id": "sc01", "hook": "?"}})
    calls = _stub(monkeypatch, [json.dumps({"scenes": [_scene(1)]}), good])
    authoring.draft(MIAMI, "s01e02", "a wish")
    assert attempts["n"] == 2
    followup = calls[1]["messages"][-1]["content"]
    assert "40 words is too many" in followup


def test_a_failed_draft_still_records_nothing_as_a_script(db, monkeypatch):
    _open_second(db)
    monkeypatch.setattr(authoring, "validate_candidate",
                        lambda *a, **k: (_ for _ in ()).throw(PackageError("nope")))
    _stub(monkeypatch, ['{"scenes": []}', '{"scenes": []}'])
    with pytest.raises(authoring.AuthoringError):
        authoring.draft(MIAMI, "s01e02", "x")
    assert not db.list("scripts", {"series_id": MIAMI})


def test_an_unreadable_answer_is_reported_plainly(db, monkeypatch):
    _open_second(db)
    _stub(monkeypatch, ["I am afraid I cannot do that.", "still not json"])
    with pytest.raises(authoring.AuthoringError, match="could not read"):
        authoring.draft(MIAMI, "s01e02", "x")


def test_writing_into_an_unopened_episode_is_refused(db):
    """Scenes must never be written where no episode holds them."""
    with pytest.raises(authoring.AuthoringError, match="has not been opened"):
        authoring.draft(MIAMI, "s01e07", "x")
    assert not db.list("scenes", {"series_id": MIAMI, "episode_id": "s01e07"})


def test_a_series_with_no_cast_says_what_is_missing(db):
    _open_second(db)
    db.delete("characters", {"series_id": MIAMI})
    with pytest.raises(authoring.AuthoringError, match="no characters"):
        authoring.draft(MIAMI, "s01e02", "x")


# ── a good draft is committed, and the cost recorded ───────────────────────

def _accepting(monkeypatch, scenes):
    monkeypatch.setattr(authoring, "validate_candidate",
                        lambda sid, eid, brief: {"scenes": brief["scenes"],
                                                 "total_seconds": 4 * len(brief["scenes"]),
                                                 "warnings": []})
    return _stub(monkeypatch, [json.dumps({"title": "Not his child", "logline": "L",
                                           "scenes": scenes,
                                           "cliffhanger": {"scene_id": scenes[-1]["scene_id"],
                                                           "hook": "?"}})])


def test_a_valid_draft_becomes_the_episode(db, monkeypatch):
    _open_second(db)
    _accepting(monkeypatch, [_scene(1), _scene(2, cliff=True)])
    result = authoring.draft(MIAMI, "s01e02", "Nora listens at the door", "admin@example.test")
    assert result["clips"] == 2
    assert len(db.list("scenes", {"series_id": MIAMI, "episode_id": "s01e02"})) == 2
    assert db.get("episodes", {"series_id": MIAMI, "episode_id": "s01e02"})["title"] == "Not his child"


def test_the_episode_title_is_kept(db, monkeypatch):
    """A prose script's own heading becomes the episode name."""
    _open_second(db)
    _accepting(monkeypatch, [_scene(1, cliff=True)])
    authoring.draft(MIAMI, "s01e02", "EPISODE 2: NOT HIS CHILD\n\nNora waits...")
    assert db.get("episodes", {"series_id": MIAMI, "episode_id": "s01e02"})["title"] == "Not his child"


def test_the_authoring_call_is_recorded_as_a_cost(db, monkeypatch):
    _open_second(db)
    _accepting(monkeypatch, [_scene(1, cliff=True)])
    result = authoring.draft(MIAMI, "s01e02", "x")
    costs = db.list("costs", {"series_id": MIAMI, "stage": "authoring"})
    assert costs and costs[0]["provider"] == "anthropic"
    assert result["spend_usd"] > 0


def test_an_empty_wish_continues_the_previous_episode(db, monkeypatch):
    db.upsert("episodes", {"series_id": MIAMI, "episode_id": "s01e01", "number": 1})
    db.upsert("scenes", {"series_id": MIAMI, "episode_id": "s01e01", **_scene(8)})
    authoring.next_episode(MIAMI)
    calls = _accepting(monkeypatch, [_scene(1, cliff=True)])
    authoring.draft(MIAMI, "s01e02", "")
    sent = json.loads(calls[0]["messages"][0]["content"])
    assert "recorded ending of the previous episode" in sent["request"]
    assert sent["series_memory"]["previous_final_scenes"]


# ── reading it back, and changing it in plain words ────────────────────────

def test_the_script_reads_as_prose_without_identifiers(db):
    script = authoring.readable([_scene(1)], authoring.series_memory(MIAMI, "s01e02"))
    assert script[0]["who" if False else "lines"][0]["who"] == "Nora"
    assert script[0]["place"] == "Villa terrace"
    assert "SCENE" not in json.dumps(script)


def test_the_word_budget_is_shown_per_scene(db):
    script = authoring.readable([_scene(1)], authoring.series_memory(MIAMI, "s01e02"))
    assert script[0]["word_budget"] > 0 and script[0]["words"] == 2


def test_a_revision_reports_only_speech_when_only_words_changed(db, monkeypatch):
    before = {"sc01": _scene(1, text="Your name is on the passenger list.")}
    after = [_scene(1, text="Your name is on it.")]
    assert authoring.changed_scenes(before, after) == [{"scene_id": "sc01", "redo": "speech"}]


def test_a_revision_admits_when_the_clip_must_be_remade(db):
    """Promising the footage survives a changed action would be a lie."""
    before = {"sc01": _scene(1)}
    changed = copy.deepcopy(_scene(1))
    changed["action"] = "She turns away from the door."
    assert authoring.changed_scenes(before, [changed]) == [
        {"scene_id": "sc01", "redo": "image and video"}]


def test_untouched_scenes_are_not_listed_for_redoing(db):
    before = {"sc01": _scene(1), "sc02": _scene(2)}
    assert authoring.changed_scenes(before, [_scene(1), _scene(2)]) == []


def test_revising_without_a_script_says_so(db):
    with pytest.raises(authoring.AuthoringError, match="no script"):
        authoring.revise(MIAMI, "s01e02", "make it tenser")


def test_an_empty_instruction_is_refused(db):
    db.upsert("scenes", {"series_id": MIAMI, "episode_id": "s01e02", **_scene(1)})
    with pytest.raises(authoring.AuthoringError, match="Describe what to change"):
        authoring.revise(MIAMI, "s01e02", "   ")


def test_a_revision_sends_the_current_script_and_the_instruction(db, monkeypatch):
    _open_second(db)
    db.upsert("scenes", {"series_id": MIAMI, "episode_id": "s01e02", **_scene(1)})
    calls = _accepting(monkeypatch, [_scene(1, text="You knew?", cliff=True)])
    authoring.revise(MIAMI, "s01e02", "Make the ending tenser")
    sent = json.loads(calls[0]["messages"][0]["content"])
    assert sent["current_script"]
    assert "Make the ending tenser" in sent["request"]
    assert "Leave every scene the change does not concern" in sent["request"]


# ── what stops an episode being written is said before the wish is typed ───

def test_a_character_saved_without_an_appearance_is_named(db):
    """The engine said "bible/characters.json/0" — an index, not a person."""
    db.upsert("characters", {"series_id": MIAMI, "character_id": "adrian", "name": "Adrian",
                             "visual": True, "appearance": ""})
    db.upsert("clothing", {"series_id": MIAMI, "character_id": "adrian", "variant_id": "w_suit"})
    problems = authoring.setup_problems(MIAMI)
    assert [p["names"].get("who") for p in problems] == ["Adrian"]
    assert "appearance" in problems[0]["message"]
    assert problems[0]["href"] == f"/series/{MIAMI}/characters"
    # The name travels as a value, never through the translation table.
    assert "Adrian" not in problems[0]["message"]


def test_a_character_with_nothing_to_wear_is_named(db):
    db.upsert("characters", {"series_id": MIAMI, "character_id": "maya", "name": "Maya",
                             "visual": True, "appearance": "described"})
    assert [p["names"].get("who") for p in authoring.setup_problems(MIAMI)] == ["Maya"]


def test_an_off_camera_character_needs_neither(db):
    """A voice on the phone is never rendered, so it needs no face or clothes."""
    db.upsert("characters", {"series_id": MIAMI, "character_id": "caller", "name": "Caller",
                             "visual": False})
    assert authoring.setup_problems(MIAMI) == []


def test_a_location_without_a_description_is_named(db):
    db.upsert("locations", {"series_id": MIAMI, "location_id": "pier", "name": "Pier",
                            "description": ""})
    assert [p["names"].get("where") for p in authoring.setup_problems(MIAMI)] == ["Pier"]


def test_a_ready_series_reports_nothing_to_do(db):
    assert authoring.setup_problems(MIAMI) == []


def test_a_series_with_no_cast_says_so_before_the_wish_is_typed(db, monkeypatch):
    """The refusal used to arrive only after the producer had written one."""
    from app import web
    db.delete("characters", {"series_id": MIAMI})
    captured = {}
    monkeypatch.setattr(web, "store", db, raising=False)
    monkeypatch.setattr(web, "require_admin", lambda request: {"email": "a@b.test"})
    monkeypatch.setattr(web, "render", lambda request, template, **ctx: captured.update(ctx))
    monkeypatch.setattr(web, "_csrf_token", lambda *a: "t")
    monkeypatch.setattr(web.i18n, "language", lambda request: "en")
    monkeypatch.setattr(web.runner, "episode_runtime", lambda *a: {"status": "draft", "stages": {}})
    monkeypatch.setattr(web.runner.jobs, "active_job", lambda *a: None)
    monkeypatch.setattr(web.runner.jobs, "jobs_for", lambda *a, **k: [])
    episode = authoring.next_episode(MIAMI)["episode_id"]
    web.episode_studio(None, MIAMI, episode)
    assert [b["href"] for b in captured["blockers"]] == [f"/series/{MIAMI}/characters"]
    assert "no characters" in captured["blockers"][0]["message"]


def test_a_complete_series_shows_no_blockers(db, monkeypatch):
    from app import web
    captured = {}
    monkeypatch.setattr(web, "store", db, raising=False)
    monkeypatch.setattr(web, "require_admin", lambda request: {"email": "a@b.test"})
    monkeypatch.setattr(web, "render", lambda request, template, **ctx: captured.update(ctx))
    monkeypatch.setattr(web, "_csrf_token", lambda *a: "t")
    monkeypatch.setattr(web.i18n, "language", lambda request: "en")
    monkeypatch.setattr(web.runner, "episode_runtime", lambda *a: {"status": "draft", "stages": {}})
    monkeypatch.setattr(web.runner.jobs, "active_job", lambda *a: None)
    monkeypatch.setattr(web.runner.jobs, "jobs_for", lambda *a, **k: [])
    episode = authoring.next_episode(MIAMI)["episode_id"]
    web.episode_studio(None, MIAMI, episode)
    assert captured["blockers"] == []


# ── filling the bible from what the series already knows ───────────────────

CAST = {"characters": [
    {"id": "nora", "name": "Nora", "role": "protagonist", "age": "29", "visual": True,
     "appearance": "A " + "detailed word " * 50, "behavior": "watchful",
     "wardrobe": [{"id": "w_wedding", "description": "ivory slip dress", "is_default": True}]},
    {"id": "caller", "name": "Caller", "visual": False, "appearance": "", "wardrobe": []}],
 "locations": [{"id": "terrace", "name": "Terrace", "description": "T" * 200,
                "lighting_states": {"default": "dusk", "night": "lanterns"}}]}


def _cast(monkeypatch, answer=None):
    return _stub(monkeypatch, [json.dumps(answer if answer is not None else CAST)])


def test_a_series_started_from_a_clip_gets_its_cast(db, monkeypatch):
    """The first-clip preview writes a series and an episode but no bible."""
    db.delete("characters", {"series_id": MIAMI})
    db.delete("clothing", {"series_id": MIAMI})
    _cast(monkeypatch)
    result = authoring.fill_bible(MIAMI, "admin@example.test")
    assert sorted(result["added"]) == ["caller", "nora"]
    nora = db.get("characters", {"series_id": MIAMI, "character_id": "nora"})
    assert nora["name"] == "Nora" and len(nora["appearance"].split()) > 40
    assert nora["drafted_by_studio"] is True
    assert [w["variant_id"] for w in db.list("clothing", {"series_id": MIAMI,
                                                          "character_id": "nora"})] == ["w_wedding"]
    assert authoring.setup_problems(MIAMI) == []


def test_a_voice_only_character_gets_no_clothes(db, monkeypatch):
    db.delete("characters", {"series_id": MIAMI})
    _cast(monkeypatch)
    authoring.fill_bible(MIAMI)
    assert db.list("clothing", {"series_id": MIAMI, "character_id": "caller"}) == []


def test_every_new_character_gets_a_voice_binding(db, monkeypatch):
    db.delete("characters", {"series_id": MIAMI})
    db.delete("voices", {"series_id": MIAMI})
    _cast(monkeypatch)
    authoring.fill_bible(MIAMI)
    voice = db.get("voices", {"series_id": MIAMI, "character_id": "nora"})
    assert voice["voice_env"] == "ELEVENLABS_VOICE_ID_NORA"


def test_what_a_person_wrote_is_never_overwritten(db, monkeypatch):
    """The studio fills gaps; it does not rewrite someone's work."""
    db.upsert("characters", {"series_id": MIAMI, "character_id": "nora", "name": "Nora Vale",
                             "visual": True, "appearance": "Mine, written by hand."})
    _cast(monkeypatch)
    authoring.fill_bible(MIAMI)
    nora = db.get("characters", {"series_id": MIAMI, "character_id": "nora"})
    assert nora["appearance"] == "Mine, written by hand." and nora["name"] == "Nora Vale"
    assert nora["drafted_by_studio"] is False


def test_an_empty_character_is_completed_and_marked(db, monkeypatch):
    """The three characters that arrived with nothing in them."""
    db.upsert("characters", {"series_id": MIAMI, "character_id": "nora", "name": "Nora",
                             "visual": True, "appearance": ""})
    _cast(monkeypatch)
    result = authoring.fill_bible(MIAMI)
    nora = db.get("characters", {"series_id": MIAMI, "character_id": "nora"})
    assert result["completed"] == ["nora"] and nora["drafted_by_studio"] is True
    assert len(nora["appearance"].split()) > 40


def test_a_described_location_is_left_alone(db, monkeypatch):
    before = db.get("locations", {"series_id": MIAMI, "location_id": "villa_terrace"})
    _cast(monkeypatch)
    authoring.fill_bible(MIAMI)
    assert db.get("locations", {"series_id": MIAMI,
                                "location_id": "villa_terrace"})["description"] == before["description"]


def test_the_model_is_shown_what_the_series_already_said(db, monkeypatch):
    db.upsert("episodes", {"series_id": MIAMI, "episode_id": "preview01", "number": 1,
                           "title": "The Wrong Bride", "logline": "She catches them."})
    calls = _cast(monkeypatch)
    authoring.fill_bible(MIAMI)
    sent = json.loads(calls[0]["messages"][0]["content"])
    assert {"title": "The Wrong Bride", "logline": "She catches them.",
            "episode_id": "preview01"} in sent["episodes_so_far"]
    assert sent["dialogue_language"] == "en-US"
    assert [c["id"] for c in sent["existing_characters"]] == ["nora"]


def test_a_series_with_nothing_written_says_so(db, monkeypatch):
    db.update("series", {"id": MIAMI}, {"title": "", "logline": ""})
    db.delete("episodes", {"series_id": MIAMI})
    with pytest.raises(authoring.AuthoringError, match="nothing written yet"):
        authoring.fill_bible(MIAMI)


def test_a_nonsense_id_from_the_model_is_dropped(db, monkeypatch):
    db.delete("characters", {"series_id": MIAMI})
    _cast(monkeypatch, {"characters": [{"id": "Not An Id!", "name": "X", "visual": True}],
                        "locations": []})
    assert authoring.fill_bible(MIAMI)["added"] == ["not_an_id"]


# ── a preview is a sample, not a season of the show ────────────────────────

def _preview_only(db):
    db.delete("seasons", {"series_id": MIAMI})
    db.upsert("seasons", {"series_id": MIAMI, "season_id": "previews", "number": 0,
                          "title": "Previews", "episode_order": ["preview01"]})
    db.upsert("episodes", {"series_id": MIAMI, "season_id": "previews",
                           "episode_id": "preview01", "number": 1, "status": "preview"})
    db.upsert("scenes", {"series_id": MIAMI, "episode_id": "preview01", **_scene(1)})


def test_a_real_episode_never_lands_in_the_preview_season(db):
    """Continuing from the first clip once produced ids like 'previewse02'."""
    _preview_only(db)
    episode = authoring.next_episode(MIAMI, "admin@example.test")
    assert episode["season_id"] == "s01" and episode["episode_id"] == "s01e02"
    assert db.get("seasons", {"series_id": MIAMI, "season_id": "s01"})["number"] == 1


def test_the_first_real_season_is_opened_once(db):
    _preview_only(db)
    authoring.next_episode(MIAMI)
    authoring.next_episode(MIAMI)
    assert [s["season_id"] for s in db.list("seasons", {"series_id": MIAMI},
                                            order="number")] == ["previews", "s01"]


def test_an_existing_real_season_is_used_as_it_is(db):
    _preview_only(db)
    db.upsert("seasons", {"series_id": MIAMI, "season_id": "s02", "number": 2,
                          "episode_order": []})
    assert authoring.next_episode(MIAMI)["season_id"] == "s02"
