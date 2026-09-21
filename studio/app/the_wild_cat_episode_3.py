"""Idempotent, no-spend preparation seed for The Wild Cat episode three."""
from __future__ import annotations
import json
from datetime import datetime, timezone

from .store import store

SERIES_ID = "the_wild_cat"
EPISODE_ID = "s01e03"
MARKER_EVENT = "series.episode_3_prepared.complete"
LAUNCH_APPROVAL_TYPE = "episode_launch"
VIDEO_ROUTE = [
    "xai/grok-imagine-video/v1.5/image-to-video",
    "bytedance/seedance-2.0/image-to-video",
    "fal-ai/kling-video/v3/pro/image-to-video",
    "fal-ai/veo3.1/image-to-video",
]

NARRATION = [
    "Утром охотник нашёл возле своей сумки первый ответный дар.",
    "Так дикая кошка сказала ему то, чего не могла сказать словами.",
    "С того дня она всё чаще становилась его тенью.",
    "Она шла рядом, но сохраняла расстояние, будто сама выбирала каждую меру близости.",
    "Она училась понимать его молчание, шаги и самые тихие перемены в его взгляде.",
    "Охотник не удерживал её и не пытался изменить её дикую природу.",
    "Именно поэтому ей всё труднее было уходить далеко.",
    "Благодарность незаметно превращалась в привязанность.",
    "Теперь ей хотелось делить с ним не только добычу и тепло костра.",
    "Она мечтала стать его избранной спутницей, оставаясь свободной.",
    "Но однажды охотник остановился ради маленькой раненой птицы.",
    "Кошка смотрела на его осторожные руки, и в её взгляде впервые мелькнула ревность.",
    "Заметив, что он увидел её чувство, она гордо отвернулась и скрылась среди папоротников.",
    "Однако далеко не ушла: сердце уже держало её крепче любой клетки.",
    "И впервые ей захотелось не просто идти за ним, а чтобы однажды он сам выбрал идти рядом с ней.",
]

ACTIONS = [
    "At dawn the Hunter finds the rabbit beside his pack; the Wildcat watches from cover.",
    "Close details: his hand pauses above the gift; her eyes hold his response.",
    "They travel through the waking forest on separate but parallel paths.",
    "Her paws, his boots, wet leaves and moving branches bridge their matched pace.",
    "She reads his pauses and direction from a proud natural distance.",
    "He leaves space for her, never calling, trapping or offering bait.",
    "She drifts away, then quietly returns to the edge of his path.",
    "At water, their reflections approach before either body does.",
    "They share shade and water without touch or ownership.",
    "A sustained gaze suggests the companion she wishes to become.",
    "The Hunter finds a small injured bird and kneels to help it.",
    "The Wildcat watches his careful hands; a first quiet jealousy enters her eyes.",
    "When he notices, she turns away with theatrical feline indifference.",
    "She hides in fern shadow but remains close, listening to his steps.",
    "At dusk she rejoins the parallel trail; he slows, leaving room beside him.",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scene(number: int, action: str, narration: str) -> dict:
    locations = ["camp_clearing", "spring_forest", "mossy_trail", "creek_crossing"]
    lenses = ["50mm", "85mm", "35mm", "85mm", "50mm",
              "50mm", "50mm", "85mm", "35mm", "85mm",
              "50mm", "85mm", "50mm", "85mm", "35mm"]
    return {
        "scene_id": f"sc{number:02d}", "sequence": number, "duration_seconds": 8,
        "location": locations[min((number - 1) // 4, 3)], "lighting_state": "natural",
        "characters_in_frame": ["hunter", "wildcat"],
        "wardrobe": {"hunter": "w_forest", "wildcat": "natural"},
        "action": action,
        "dialogue": [{"speaker": "narrator", "text": narration,
                      "delivery": "calm, intimate, mature Russian fairy-tale narration",
                      "voice_over": True}],
        "shot_type": "cinematic selected coverage", "lens": lenses[number - 1],
        "camera_motion": "slow motivated movement only",
        "continuity_in": "Preserve canonical Hunter, Wildcat, direction and natural ambience.",
        "continuity_out": "End on a stable gaze, gesture or detail suitable for a smooth cut.",
        "props": [], "knowledge_required": [], "knowledge_gained": [],
        "relationship_changes": [], "is_cliffhanger": number == 15,
    }


SCENES = [_scene(i + 1, ACTIONS[i], NARRATION[i]) for i in range(15)]


def seed_if_missing() -> bool:
    """Insert the locked draft only; never approve, enqueue, generate or publish."""
    if store.get("episodes", {"series_id": SERIES_ID, "episode_id": EPISODE_ID}):
        return False
    series = store.get("series", {"id": SERIES_ID})
    season = store.get("seasons", {"series_id": SERIES_ID, "season_id": "s01"})
    if not series or not season:
        return False
    timestamp = _now()
    editorial = {
        "select_duration_seconds": "3-6",
        "bridges": ["eyes", "paws", "birds", "water", "fire", "hands", "leaves"],
        "sound": "continuous natural forest/water/steps/fire ambience; no music; no English speech",
        "cut_rules": "Trim unstable clip heads/tails; motivated motion/gaze/sound transitions; inspect every cut and final second frame by frame.",
    }
    brief = {
        "schema_version": "2.0", "series_id": SERIES_ID, "season_id": "s01",
        "episode_id": EPISODE_ID, "number": 3, "title": "Too Close",
        "logline": "Gratitude becomes attachment, and the Wildcat reveals her first quiet jealousy.",
        "language": "ru-RU", "narration_language": "ru-RU",
        "video_route": VIDEO_ROUTE,
        "max_video_route_attempts": 2,
        "video_qc_fallback_policy": "After the first failed video QC attempt, advance to the next engine; never repeat the same failed engine automatically; preserve canonical references.",
        "tool_policy": "One keyframe attempt. Reuse all approved references. Automatically try at most Grok then Seedance per scene. Kling and Veo require targeted human approval after review. No music generation, provider audio, repeated same-model attempts, or lipsync provider call for narrator voice-over.",
        "reference_policy": "Reuse the locked Episode 1/2 Hunter and enhanced Wildcat canonical reference packs; do not recreate successful references.",
        "audio_policy": "One continuous Russian narrator track; no English; no music; provider audio disabled; continuous natural ambience.",
        "editorial_plan": editorial,
        "opening_state": {"relationships": {"hunter_wildcat_bond": "chosen_companionship"}},
        "scenes": SCENES,
        "cliffhanger": {"scene_id": "sc15", "hook": "He slows and leaves an open place beside him; she considers whether he chose her too.", "resolves_in": "s01e04"},
    }
    cap = float((series.get("production_limits") or {}).get("maximum_episode_budget_usd") or 100)
    store.upsert("episodes", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "season_id": "s01",
        "number": 3, "title": brief["title"], "logline": brief["logline"],
        "status": "draft", "brief": brief, "opening_state": brief["opening_state"],
        "cliffhanger": brief["cliffhanger"], "target_seconds": 120,
        "budget_usd": min(cap, 100.0), "spent_usd": 0,
        "created_at": timestamp, "updated_at": timestamp,
    })
    for scene in SCENES:
        store.upsert("scenes", {**scene, "series_id": SERIES_ID,
                                "episode_id": EPISODE_ID, "status": "draft"})
    store.insert("scripts", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "version": 1,
        "source": "creative_canon", "filename": "the_wild_cat_s01e03.json",
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
        "detail": {"title": "Too Close", "duration_seconds": 120,
                   "video_route": VIDEO_ROUTE, "editorial_plan": editorial,
                   "note": "Draft only. No paid generation, approval, job, or publishing was started."},
        "created_at": timestamp,
    })
    return True


def launch_if_approved() -> str:
    """Start one reviewed live job, and never retry it automatically.

    The approval is stored separately from the normal live-production receipt
    so startup can calculate the current package digest itself.  The existence
    of any Episode 3 job is the permanent idempotency guard: a failed or paused
    run must be inspected and resumed deliberately, never replaced by a fresh
    paid run after a deploy.
    """
    from .config import settings

    if not settings.allow_paid:
        return ""
    approval = store.get("approvals", {
        "series_id": SERIES_ID,
        "episode_id": EPISODE_ID,
        "subject_type": LAUNCH_APPROVAL_TYPE,
        "subject_id": EPISODE_ID,
    })
    if not approval or approval.get("decision") != "approved":
        return ""
    if store.list("production_jobs", {"series_id": SERIES_ID, "episode_id": EPISODE_ID}):
        return ""

    from . import live_jobs, runner

    try:
        from serial.package import SeriesPackage
        package = SeriesPackage(live_jobs.materialize(SERIES_ID))
        digest = live_jobs.package_digest(package, EPISODE_ID)
    except Exception as exc:
        store.insert("generation_history", {
            "series_id": SERIES_ID,
            "episode_id": EPISODE_ID,
            "event": "episode.production_package_preflight_failed",
            "entity_type": "episode",
            "entity_id": EPISODE_ID,
            "actor": "startup",
            "detail": {"error_type": type(exc).__name__, "error": str(exc)[:1000],
                       "paid_job_created": False},
            "created_at": _now(),
        })
        return ""
    try:
        job = runner.jobs.start(
            SERIES_ID,
            EPISODE_ID,
            runner.DEFAULT_STAGES,
            approval.get("actor") or "approved producer",
            [],
            approved_digest=digest,
            approve_live=True,
            audio_mode="voices",
        )
    except Exception as exc:
        store.insert("generation_history", {
            "series_id": SERIES_ID,
            "episode_id": EPISODE_ID,
            "event": "episode.production_launch_preflight_failed",
            "entity_type": "episode",
            "entity_id": EPISODE_ID,
            "actor": "startup",
            "detail": {"error_type": type(exc).__name__, "error": str(exc)[:1000],
                       "paid_job_created": False},
            "created_at": _now(),
        })
        return ""
    store.insert("generation_history", {
        "series_id": SERIES_ID,
        "episode_id": EPISODE_ID,
        "event": "episode.production_started_from_recorded_approval",
        "entity_type": "job",
        "entity_id": job["id"],
        "actor": approval.get("actor") or "approved producer",
        "detail": {"audio_mode": "voices", "publish": False,
                   "automatic_video_route_attempts": 2},
        "created_at": _now(),
    })
    return job["id"]
