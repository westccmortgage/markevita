"""Idempotent preparation and explicitly approved launch for Wild Cat episode four."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .store import store

SERIES_ID = "the_wild_cat"
EPISODE_ID = "s01e04"
MARKER_EVENT = "series.episode_4_prepared.complete"
LAUNCH_APPROVAL_TYPE = "episode_launch"
VEO_FAST = "fal-ai/veo3.1/fast/image-to-video"
KLING = "fal-ai/kling-video/v3/pro/image-to-video"
GROK = "xai/grok-imagine-video/v1.5/image-to-video"
SEEDANCE = "bytedance/seedance-2.0/image-to-video"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


LOCATIONS = [
    {
        "location_id": "rocky_river_gorge",
        "name": "Rocky River Gorge",
        "description": "A narrow highland gorge beyond the old forest, with dark wet stone walls, a fast cold river, sparse silver birch and wind-bent grass. The terrain is open and unfamiliar, visibly different from the mossy woodland. No buildings, roads, signs or fantasy structures.",
        "lighting_states": {
            "dawn": "cold blue-gray dawn with pale silver light on wet rock",
            "morning": "clear cool morning light reflected from the river",
            "overcast": "soft gray daylight, wet stone and low moving mist",
        },
        "marks": "The old forest ends behind them; the river runs frame-left to frame-right and the narrow trail climbs along the far bank.",
        "immutable": ["fast cold river", "dark wet gorge walls", "no dense old forest", "no modern objects"],
        "seed_assets": [],
    },
    {
        "location_id": "ancient_stone_bridge",
        "name": "Ancient Stone Bridge",
        "description": "A small weathered medieval stone footbridge spanning a deep rushing channel between black rocks. Its parapet is broken in places and pale lichen covers the stones. Beyond it the path divides: one branch climbs toward open highlands, the other returns toward trees. Natural, grounded and old, never monumental or magical.",
        "lighting_states": {
            "morning": "cool directional morning light, white water below",
            "overcast": "diffuse silver light and drifting spray",
        },
        "marks": "The fork is visible beyond the bridge; the hunter's route continues frame-right while the highland path rises frame-left.",
        "immutable": ["single narrow stone arch", "broken low parapet", "visible fork beyond bridge", "no castle"],
        "seed_assets": [],
    },
    {
        "location_id": "mist_highland_meadow",
        "name": "Mist Highland Meadow",
        "description": "A wide upland meadow of wet grass, low heather and scattered granite boulders above the tree line. Thin mist moves close to the ground and distant hills appear and disappear. The open horizon creates physical and emotional distance between the hunter and the wildcat.",
        "lighting_states": {
            "overcast": "soft pearly daylight through thinning mist",
            "afternoon": "cool daylight with brief pale-gold breaks in cloud",
        },
        "marks": "A lone split boulder stands near the fork; the gorge lies behind and the ridge rises in the distance.",
        "immutable": ["open treeless horizon", "low heather", "split granite boulder", "moving ground mist"],
        "seed_assets": [],
    },
    {
        "location_id": "wind_ridge",
        "name": "Wind Ridge Above the Valley",
        "description": "A long exposed ridge overlooking an immense unfamiliar valley, with short grass, dark rock shelves and clouds moving below the summit line. It is the first landscape that belongs visually to the wildcat rather than the hunter: spacious, dangerous and free.",
        "lighting_states": {
            "dusk": "cool violet shadows with a restrained pale-gold rim on the ridge",
            "late_afternoon": "windy clear light, long shadows and distant blue hills",
        },
        "marks": "The valley opens behind the wildcat; the return path falls away out of frame behind her.",
        "immutable": ["vast valley view", "exposed grass ridge", "dark rock shelves", "no buildings or fantasy glow"],
        "seed_assets": [],
    },
]

NARRATION = [
    "Она всё ещё шла за ним привычной лесной тропой.",
    "За последними деревьями открылось каменное ущелье, незнакомое им обоим.",
    "У быстрой воды кошка первой почувствовала опасность впереди.",
    "Она преградила охотнику путь и впервые потребовала, чтобы он остановился.",
    "Но он принял её тревогу за страх и нетерпеливо отодвинул её с дороги.",
    "Она снова пошла следом, всё ещё надеясь, что он поймёт.",
    "На старом мосту он приказал ей остаться, словно её выбор ничего не значил.",
    "Охотник пошёл дальше, уверенный, что она последует, как всегда.",
    "Но в её взгляде нежность сменилась обидой и тихой женской гордостью.",
    "На развилке она впервые выбрала не его дорогу.",
    "Когда охотник обернулся, рядом уже никого не было.",
    "Высоко над долиной она посмотрела назад — и ушла одна.",
]

ACTIONS = [
    "One final restrained image in the familiar old forest: the Hunter walks ahead without looking back while the Wildcat follows at a voluntary distance.",
    "They leave the last trees and enter a cold rocky river gorge; the new open landscape dwarfs them.",
    "Close on the Wildcat beside fast water: her ears stop, amber-green eyes fix on danger farther along the trail, intelligence without anthropomorphism.",
    "At the ancient stone bridge the Wildcat deliberately steps across the Hunter's path and holds her ground, warning him not to continue.",
    "The Hunter misunderstands, makes one impatient dismissive hand gesture and steps around her; he never strikes, grabs or threatens her.",
    "Across the mist highland meadow she follows more slowly, no longer matching his pace; distance grows between them.",
    "At the bridge fork he points firmly toward the place where he expects her to stay, then turns toward his own route; she remains proud and still.",
    "The Hunter walks away into the mist without looking back; the Wildcat does not move after him.",
    "Restrained 85mm close-up: the Wildcat's gaze changes from tenderness to wounded pride and calm self-possession, fully feline and anatomically unchanged.",
    "At the split boulder she turns away from the Hunter's trail and runs up the open highland path toward the ridge.",
    "The Hunter finally turns in the empty meadow and realizes she is gone; no Wildcat is visible.",
    "On the wind ridge the Wildcat looks back once across the vast valley, then turns and walks alone beyond the rock shelf; hard cut before she disappears completely.",
]

DURATIONS = [4, 6, 6, 6, 8, 6, 8, 6, 8, 6, 6, 6]
LOCATIONS_BY_SCENE = [
    "spring_forest", "rocky_river_gorge", "rocky_river_gorge", "ancient_stone_bridge",
    "ancient_stone_bridge", "mist_highland_meadow", "ancient_stone_bridge",
    "mist_highland_meadow", "mist_highland_meadow", "mist_highland_meadow",
    "mist_highland_meadow", "wind_ridge",
]
LIGHTING = [
    "dusk", "dawn", "morning", "morning", "overcast", "overcast",
    "overcast", "overcast", "afternoon", "afternoon", "afternoon", "dusk",
]
CHARACTERS = [
    ["hunter", "wildcat"], ["hunter", "wildcat"], ["wildcat"], ["hunter", "wildcat"],
    ["hunter", "wildcat"], ["hunter", "wildcat"], ["hunter", "wildcat"],
    ["hunter", "wildcat"], ["wildcat"], ["wildcat"], ["hunter"], ["wildcat"],
]
LENSES = ["50mm", "35mm", "85mm", "50mm", "50mm", "35mm",
          "50mm", "35mm", "85mm", "50mm", "85mm", "85mm"]


def _scene(index: int) -> dict:
    sid = f"sc{index:02d}"
    people = CHARACTERS[index - 1]
    relationship_changes = []
    if sid == "sc07":
        relationship_changes = [{"id": "hunter_wildcat_bond", "state": "misunderstanding",
                                 "note": "He mistakes her warning for disobedience; she feels treated as a follower rather than a chosen equal."}]
    if sid == "sc10":
        relationship_changes = [{"id": "hunter_wildcat_bond", "state": "separated",
                                 "note": "She consciously chooses her own road and leaves him behind."}]
    return {
        "scene_id": sid, "sequence": index, "duration_seconds": DURATIONS[index - 1],
        "location": LOCATIONS_BY_SCENE[index - 1], "lighting_state": LIGHTING[index - 1],
        "characters_in_frame": people,
        "wardrobe": {cid: ("w_forest" if cid == "hunter" else "natural") for cid in people},
        "action": ACTIONS[index - 1],
        "dialogue": [{"speaker": "narrator", "text": NARRATION[index - 1],
                      "delivery": "calm intimate Russian fairy-tale narration, emotional restraint",
                      "voice_over": True}],
        "shot_type": "cinematic story coverage", "lens": LENSES[index - 1],
        "camera_motion": "one simple motivated movement only",
        "continuity_in": "Preserve exact canonical Hunter and Wildcat identity, wardrobe, coat markings, size and screen direction.",
        "continuity_out": ("Hold a clean stable composition for twelve frames; hard cut on her departure."
                           if index == 12 else
                           "End with at least twelve clean stable frames for a motivated editorial cut."),
        "props": [], "knowledge_required": [], "knowledge_gained": [],
        "relationship_changes": relationship_changes,
        "is_cliffhanger": index == 12,
    }


SCENES = [_scene(i) for i in range(1, 13)]

VIDEO_ROUTING = {
    "mode": "manual_after_qc",
    "scenes": {
        "sc01": {"mode": "motion_still", "motion": "push_in"},
        "sc02": {"mode": "video", "primary": VEO_FAST, "fallbacks": [KLING]},
        "sc03": {"mode": "motion_still", "motion": "push_in"},
        "sc04": {"mode": "video", "primary": KLING, "fallbacks": [VEO_FAST]},
        "sc05": {"mode": "video", "primary": KLING, "fallbacks": [VEO_FAST]},
        "sc06": {"mode": "video", "primary": VEO_FAST, "fallbacks": [KLING]},
        "sc07": {"mode": "video", "primary": KLING, "fallbacks": [VEO_FAST]},
        "sc08": {"mode": "video", "primary": VEO_FAST, "fallbacks": [KLING]},
        "sc09": {"mode": "motion_still", "motion": "push_in"},
        "sc10": {"mode": "video", "primary": VEO_FAST, "fallbacks": [KLING]},
        "sc11": {"mode": "motion_still", "motion": "push_in"},
        "sc12": {"mode": "video", "primary": KLING, "fallbacks": [VEO_FAST]},
    },
}


def _prompt(scene: dict) -> dict:
    sid = scene["scene_id"]
    action = scene["action"]
    expected = ("One coherent physically plausible action; exact canonical identities and clothing; "
                "Wildcat remains a true large European wildcat with long ringed tail, amber-green eyes, "
                "left ear notch, stable anatomy and scale; no morphing or jump cuts; twelve clean tail frames.")
    if sid == "sc09":
        expected += (" Her femininity is conveyed only by softened eyelids, sustained intelligent gaze, "
                     "ear angle, whiskers and proud head position; no human features, lashes, makeup or smile.")
    return {
        "keyframe_prompt": (f"First stable frame for this beat: {action} Full-bleed vertical frame, "
                            "single readable composition, no letterbox bars. Keep emotional geography clear."),
        "video_prompt": (f"Perform only this beat, with no added event: {action} Natural restrained speed. "
                         "No dialogue, no species change, no camera repositioning beyond the named simple move. "
                         "Finish on a stable editable frame."),
        "negative": ("human eyes or lips on cat, eyelashes, makeup, anthropomorphic expression, domestic pet, "
                     "lynx tufts, bobtail, size drift, face drift, extra limbs, malformed paws, tail crossing lens, "
                     "foreground occlusion, sudden zoom, jump cut, flicker, wardrobe change, black bars"),
        "keyframe_expected": expected,
        "video_expected": expected,
        "lens": scene["lens"],
    }


PRODUCTION_PROMPTS = {"scenes": {s["scene_id"]: _prompt(s) for s in SCENES}}


def _brief() -> dict:
    return {
        "schema_version": "2.0", "series_id": SERIES_ID, "season_id": "s01",
        "episode_id": EPISODE_ID, "number": 4, "title": "The Road She Chose",
        "logline": "When the Hunter dismisses her warning, the Wildcat stops being his shadow and chooses a road of her own.",
        "language": "ru-RU", "narration_language": "ru-RU",
        "maximum_episode_budget_usd": 35.0,
        "video_route": [VEO_FAST, KLING, GROK, SEEDANCE],
        "max_video_route_attempts": 1,
        "video_routing": VIDEO_ROUTING,
        "video_qc_fallback_policy": "Manual after QC. One paid provider per scene run; no automatic fallback. A different engine requires a recorded scene-specific approval after reviewing the failed take.",
        "tool_policy": "Reuse approved Hunter and Wildcat references. Generate only new location references. One keyframe attempt. Eight paid video scenes and four free editorial motion-stills. No music, provider speech, automatic video fallback, repeated same-model attempt or publication.",
        "reference_policy": "Keep every unchanged canonical character reference. Add only the four new location packs, then review the delta before video.",
        "audio_policy": "One continuous Russian narrator track; no English speech and no music; preserve natural river, wind and footstep ambience in assembly.",
        "editorial_plan": {
            "target_seconds": 76,
            "rhythm": "Variable 4/6/8-second beats; action advances on every generated clip; never repeat an eight-second walking tableau.",
            "old_forest_limit": "Exactly one opening scene, then never return in Episode 4.",
            "cut_rules": "Twelve clean head/tail frames; inspect one second around every cut and every internal motion spike; no dissolve across changed character positions.",
        },
        "opening_state": {"relationships": {"hunter_wildcat_bond": "first_trust"}},
        "scenes": SCENES,
        "cliffhanger": {"scene_id": "sc12", "hook": "She looks back with love but chooses the unknown valley and leaves alone.", "resolves_in": "tbd"},
        "production_prompts": PRODUCTION_PROMPTS,
    }


def seed_if_missing() -> bool:
    """Prepare the new world and locked script; never itself creates a paid call."""
    series = store.get("series", {"id": SERIES_ID})
    season = store.get("seasons", {"series_id": SERIES_ID, "season_id": "s01"})
    if not series or not season:
        return False
    timestamp = _now()
    limits = dict(series.get("production_limits") or {})
    limits.update({"min_episode_seconds": 60, "max_episode_seconds": 120,
                   "min_scenes": 10, "max_scenes": 18,
                   "maximum_regenerations_per_scene": 0})
    store.update("series", {"id": SERIES_ID}, {
        "production_limits": limits,
        "approval": {"status": "approved", "by": "Anatoliy Kanevsky",
                     "at": "2026-09-22T00:00:00+00:00"},
        "updated_at": timestamp,
    })
    for location in LOCATIONS:
        store.upsert("locations", {**location, "series_id": SERIES_ID})
    relation = store.get("relationships", {"series_id": SERIES_ID,
                                            "rel_id": "hunter_wildcat_bond"}) or {}
    allowed = list(relation.get("allowed_states") or [])
    for state in ("misunderstanding", "separated"):
        if state not in allowed:
            allowed.append(state)
    if relation:
        store.update("relationships", {"series_id": SERIES_ID,
                                        "rel_id": "hunter_wildcat_bond"},
                     {"allowed_states": allowed})
    if store.get("episodes", {"series_id": SERIES_ID, "episode_id": EPISODE_ID}):
        return False
    brief = _brief()
    store.upsert("episodes", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "season_id": "s01",
        "number": 4, "title": brief["title"], "logline": brief["logline"],
        "status": "draft", "brief": brief, "opening_state": brief["opening_state"],
        "cliffhanger": brief["cliffhanger"],
        "target_seconds": sum(s["duration_seconds"] for s in SCENES),
        "budget_usd": 35.0, "spent_usd": 0,
        "created_at": timestamp, "updated_at": timestamp,
    })
    for scene in SCENES:
        store.upsert("scenes", {**scene, "series_id": SERIES_ID,
                                "episode_id": EPISODE_ID, "status": "draft"})
    store.insert("scripts", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "version": 1,
        "source": "approved_creative_brief", "filename": "the_wild_cat_s01e04.json",
        "content": json.dumps(brief, ensure_ascii=False, indent=2), "parsed": brief,
        "created_by": "Anatoliy Kanevsky / ChatGPT production authorization",
        "created_at": timestamp,
    })
    order = list(season.get("episode_order") or [])
    if EPISODE_ID not in order:
        order.append(EPISODE_ID)
        store.update("seasons", {"series_id": SERIES_ID, "season_id": "s01"},
                     {"episode_order": order})
    store.insert("generation_history", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "event": MARKER_EVENT,
        "entity_type": "episode", "entity_id": EPISODE_ID, "actor": "seed",
        "detail": {"title": brief["title"], "duration_seconds": 76,
                   "budget_usd": 35.0, "paid_video_scenes": 8,
                   "motion_still_scenes": 4, "automatic_fallbacks": False,
                   "note": "Prepared only; paid launch requires the recorded episode authorization."},
        "created_at": timestamp,
    })
    return True


def launch_if_approved() -> str:
    """Start once from the recorded Episode 4 authorization; never publish."""
    from .config import settings
    if not settings.allow_paid:
        return ""
    approval = store.get("approvals", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID,
        "subject_type": LAUNCH_APPROVAL_TYPE, "subject_id": EPISODE_ID,
    })
    if not approval or approval.get("decision") != "approved":
        return ""
    if store.list("production_jobs", {"series_id": SERIES_ID,
                                      "episode_id": EPISODE_ID}):
        return ""
    from . import live_jobs, runner
    from serial.package import SeriesPackage
    package = SeriesPackage(live_jobs.materialize(SERIES_ID))
    digest = live_jobs.package_digest(package, EPISODE_ID)
    actor = approval.get("actor") or "Anatoliy Kanevsky"
    job = runner.jobs.start(
        SERIES_ID, EPISODE_ID, runner.DEFAULT_STAGES, actor, [],
        approved_digest=digest, approve_live=True, audio_mode="voices")
    store.insert("generation_history", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID,
        "event": "episode.production_started_from_recorded_approval",
        "entity_type": "job", "entity_id": job["id"], "actor": actor,
        "detail": {"budget_usd": 35.0, "publish": False,
                   "paid_video_scenes": 8, "motion_still_scenes": 4,
                   "automatic_video_fallbacks": False},
        "created_at": _now(),
    })
    return job["id"]
