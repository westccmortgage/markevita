"""Idempotent, no-spend preparation seed for The Wild Cat episode two."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .store import store

SERIES_ID = "the_wild_cat"
EPISODE_ID = "s01e02"
MARKER_EVENT = "series.episode_2_prepared.complete"
CORRECTION_EVENT = "series.episode_2_continuity_corrected.complete"
VIDEO_ROUTE = [
    "xai/grok-imagine-video/v1.5/image-to-video",
    "bytedance/seedance-2.0/image-to-video",
    "fal-ai/kling-video/v3/pro/image-to-video",
    "fal-ai/veo3.1/image-to-video",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scene(number: int, location: str, characters: list[str], action: str,
           shot: str, lens: str, motion: str, continuity_in: str,
           continuity_out: str, narration: str = "", cliffhanger: bool = False) -> dict:
    dialogue = ([{"speaker": "narrator", "text": narration,
                  "delivery": "sparse, intimate fairy-tale narration", "voice_over": True}]
                if narration else [])
    return {
        "scene_id": f"sc{number:02d}", "sequence": number, "duration_seconds": 8,
        "location": location, "lighting_state": "default",
        "characters_in_frame": characters,
        "wardrobe": {c: ("natural" if c == "wildcat" else "w_forest")
                     for c in characters if c in ("wildcat", "hunter")},
        "action": action, "dialogue": dialogue, "shot_type": shot, "lens": lens,
        "camera_motion": motion, "continuity_in": continuity_in,
        "continuity_out": continuity_out, "props": [], "knowledge_required": [],
        "knowledge_gained": [], "relationship_changes": [],
        "is_cliffhanger": cliffhanger,
    }


SCENES = [
    _scene(1, "camp_clearing", ["hunter"],
           "At dawn the hunter wakes beside the cold edge of the fire and quietly studies the empty place where the wildcat slept. He accepts that she has gone.",
           "wide dawn camp", "35mm", "slow restrained push", "Ash glows under the last coals.",
           "The hunter shoulders his satchel and leaves without calling for her.",
           "At dawn, he thought the forest had taken her back."),
    _scene(2, "mossy_trail", ["hunter", "wildcat"],
           "The hunter walks the mossy trail. Far behind, the fully realistic wildcat follows in cover with graceful natural feline movement, always keeping several tree trunks between them.",
           "long layered forest view", "85mm", "gentle parallel track", "The hunter has just left camp.",
           "The wildcat stops whenever he slows, pretending to study the ferns."),
    _scene(3, "mossy_trail", ["wildcat", "hunter"],
           "When the hunter glances back, the wildcat turns her head away theatrically and sits with exaggerated feline composure, as if she has no interest in him at all.",
           "medium two-plane composition", "50mm", "locked observational frame", "She has been following at a distance.",
           "He hides a small smile and continues walking; she waits three beats before following."),
    _scene(4, "mossy_trail", ["wildcat"],
           "The wildcat rises and pads after him, proud and unhurried, her thick ringed tail level behind her. Her playful defiance remains entirely natural and feline.",
           "low full-body walking profile", "50mm", "smooth lateral track", "The hunter has moved out of frame.",
           "She slips behind a curtain of young leaves.", "She followed him while pretending not to."),
    _scene(5, "creek_crossing", ["hunter"],
           "At the creek the hunter steps across wet stones, then pauses on the far bank. He does not take food from his satchel and does not beckon; he simply leaves space beside the water.",
           "wide creek crossing", "35mm", "slow pan with his crossing", "Morning light reaches the stream.",
           "He kneels to drink, facing away from the wildcat's approach."),
    _scene(6, "creek_crossing", ["wildcat"],
           "The fully realistic large female wildcat emerges on the opposite bank and studies him with an expressive intelligent feminine gaze conveyed only through her amber-green eyes, attention and proud feline posture.",
           "eye-level feline portrait", "85mm", "almost imperceptible push", "The hunter gives her his back and distance.",
           "Her guarded eyes soften without losing their wild alertness."),
    _scene(7, "creek_crossing", ["wildcat", "hunter"],
           "The wildcat drinks upstream from him. When their reflections briefly align, her whisker pad lifts into a subtle natural feline half-smirk: playful defiance, no human lips or smile.",
           "low reflective two-shot", "50mm", "slow drift along the waterline", "They drink apart without bait or invitation.",
           "She looks away first, then checks whether he noticed."),
    _scene(8, "creek_crossing", ["hunter", "wildcat"],
           "The hunter rises and leaves calmly. The wildcat remains still until he reaches the trees, then crosses the same stones with precise natural feline steps.",
           "wide crossing reprise", "35mm", "locked frame, action crosses depth", "Their reflections have separated.",
           "She lands on his side of the stream but keeps her chosen distance.",
           "He offered no food. She came anyway."),
    _scene(9, "spring_forest", ["hunter", "wildcat"],
           "Through tall spring grass, the hunter walks the old path while the wildcat threads a separate path beside it. Neither looks directly at the other, yet their pace gradually matches.",
           "wide parallel movement", "50mm", "measured side track", "Both have crossed the creek.",
           "Their separate paths bend toward the same clearing."),
    _scene(10, "spring_forest", ["wildcat"],
           "A startled bird bursts from brush. The wildcat springs onto a fallen trunk, balanced and powerful, then glances toward the hunter with bright playful pride before continuing.",
           "full-body natural action", "85mm", "controlled tilt to the trunk", "The paired walk has become easier.",
           "She steps down with graceful feline control, never performing for reward."),
    _scene(11, "camp_clearing", ["hunter"],
           "At evening the hunter rebuilds the small fire. He places his satchel by his pack and settles without setting out meat, a trap, or a lure. The open side of the fire remains hers to choose.",
           "medium evening camp", "50mm", "slow semicircle ending on empty firelight", "The day's walk ends at the familiar camp.",
           "The hunter looks into the flames rather than searching the tree line."),
    _scene(12, "camp_clearing", ["wildcat", "hunter"],
           "The wildcat appears at the clearing's edge, watches him with tenderness held inside a proud feline gaze, then walks closer to the fire than she did the night before.",
           "layered firelight arrival", "85mm", "slow rack of attention from hunter to wildcat", "The open place beside the fire is unclaimed.",
           "She settles just beyond arm's reach, entirely by her own choice.",
           "That evening, the distance between them became something she could choose."),
    _scene(13, "camp_clearing", ["hunter", "wildcat"],
           "In quiet firelight, the hunter repairs a strap while the wildcat grooms one shoulder and watches the forest. Their calm shared silence feels companionable without ownership or touch.",
           "intimate wide two-shot", "50mm", "locked gentle firelit composition", "She has chosen a nearer place.",
           "The fire burns low; both remain awake and peaceful."),
    _scene(14, "camp_clearing", ["wildcat", "hunter"],
           "The wildcat stands, circles once with graceful feline pride, and lies facing the same dark path as the hunter. Her eyes briefly meet his, then return to the forest.",
           "low quiet two-shot", "85mm", "subtle inward drift", "Night gathers around their shared watch.",
           "Dawn light begins beyond the trees while they rest apart but aligned.",
           "She still belonged to the forest. But now, every path she chose seemed to find him."),
    _scene(15, "camp_clearing", ["hunter", "wildcat"],
           "Next morning the hunter discovers a fresh rabbit laid beside his pack as a wild gift, shown without blood or graphic detail. Across the clearing, the wildcat watches with a subtle natural feline half-smirk and alert amber-green eyes.",
           "cliffhanger reveal and gaze", "50mm", "slow reveal from pack to watching wildcat", "Morning follows their quiet shared night.",
           "Her gaze holds his for one beat. Hard cut before he responds.", cliffhanger=True),
]
SCENES[11]["relationship_changes"] = [{
    "id": "hunter_wildcat_bond", "state": "first_trust",
    "note": "The wildcat voluntarily chooses a nearer place by the fire; the hunter leaves her free to go.",
}]


def seed_if_missing() -> bool:
    """Insert the locked draft only; never approve, enqueue, generate or publish."""
    if store.get("episodes", {"series_id": SERIES_ID, "episode_id": EPISODE_ID}):
        return False
    series = store.get("series", {"id": SERIES_ID})
    season = store.get("seasons", {"series_id": SERIES_ID, "season_id": "s01"})
    if not series or not season:
        return False
    timestamp = _now()
    brief = {
        "schema_version": "2.0", "series_id": SERIES_ID, "season_id": "s01",
        "episode_id": EPISODE_ID, "number": 2, "title": "The Shadow",
        "logline": "The wildcat follows the hunter while pretending not to, and chooses a place closer to his fire.",
        "video_route": VIDEO_ROUTE,
        "opening_state": {"relationships": {"hunter_wildcat_bond": "strangers"}},
        "scenes": SCENES,
        "cliffhanger": {"scene_id": "sc15", "hook": "At dawn the wildcat leaves a fresh rabbit beside the hunter's pack and watches his response.", "resolves_in": "tbd"},
    }
    cap = float((series.get("production_limits") or {}).get("maximum_episode_budget_usd") or 100)
    store.upsert("episodes", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "season_id": "s01",
        "number": 2, "title": brief["title"], "logline": brief["logline"],
        "status": "draft", "brief": brief, "opening_state": brief["opening_state"],
        "cliffhanger": brief["cliffhanger"],
        "target_seconds": 120, "budget_usd": min(cap, 100.0), "spent_usd": 0,
        "created_at": timestamp, "updated_at": timestamp,
    })
    for scene in SCENES:
        store.upsert("scenes", {**scene, "series_id": SERIES_ID,
                                "episode_id": EPISODE_ID, "status": "draft"})
    store.insert("scripts", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "version": 1,
        "source": "creative_canon", "filename": "the_wild_cat_s01e02.json",
        "content": json.dumps(brief, ensure_ascii=False, indent=2), "parsed": brief,
        "created_by": "ChatGPT production preparation", "created_at": timestamp,
    })
    order = list(season.get("episode_order") or [])
    if EPISODE_ID not in order:
        order.append(EPISODE_ID)
        store.update("seasons", {"series_id": SERIES_ID, "season_id": "s01"},
                     {"episode_order": order})
    store.insert("generation_history", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "event": MARKER_EVENT,
        "entity_type": "episode", "entity_id": EPISODE_ID, "actor": "seed",
        "detail": {"title": "The Shadow", "duration_seconds": 120,
                   "video_route": VIDEO_ROUTE,
                   "reference_policy": "Reuse Episode 1 Hunter and Wildcat references; require enhanced Wildcat views before approval.",
                   "note": "Draft only. No paid generation, approval, job, or publishing was started."},
        "created_at": timestamp,
    })
    return True


def correct_seeded_draft_if_needed() -> bool:
    """One-time repair for the initial draft; never touch started or edited work."""
    if store.get("generation_history", {"series_id": SERIES_ID, "event": CORRECTION_EVENT}):
        return False
    episode = store.get("episodes", {"series_id": SERIES_ID, "episode_id": EPISODE_ID})
    if not episode or episode.get("status") != "draft" or float(episode.get("spent_usd") or 0):
        return False
    brief = dict(episode.get("brief") or {})
    opening = dict(brief.get("opening_state") or episode.get("opening_state") or {})
    relationships = dict(opening.get("relationships") or {})
    cliff = dict(brief.get("cliffhanger") or episode.get("cliffhanger") or {})
    if relationships.get("hunter_wildcat_bond") != "first_trust" or cliff.get("resolves_in") != "s01e03":
        return False
    relationships["hunter_wildcat_bond"] = "strangers"
    opening["relationships"] = relationships
    cliff["resolves_in"] = "tbd"
    scenes = [dict(scene) for scene in brief.get("scenes") or []]
    scene12 = next((scene for scene in scenes if scene.get("scene_id") == "sc12"), None)
    if not scene12:
        return False
    change = dict(SCENES[11]["relationship_changes"][0])
    scene12["relationship_changes"] = [change]
    brief.update(opening_state=opening, cliffhanger=cliff, scenes=scenes)
    timestamp = _now()
    store.update("episodes", {"series_id": SERIES_ID, "episode_id": EPISODE_ID}, {
        "brief": brief, "opening_state": opening, "cliffhanger": cliff, "updated_at": timestamp,
    })
    store.update("scenes", {"series_id": SERIES_ID, "episode_id": EPISODE_ID, "scene_id": "sc12"}, {
        "relationship_changes": [change],
    })
    store.insert("generation_history", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "event": CORRECTION_EVENT,
        "entity_type": "episode", "entity_id": EPISODE_ID, "actor": "seed",
        "detail": {"note": "Aligned opening relationship with Episode 1 carried state and deferred the uncreated Episode 3 target. No generation or spending."},
        "created_at": timestamp,
    })
    return True
