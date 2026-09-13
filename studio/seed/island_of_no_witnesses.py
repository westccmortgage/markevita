#!/usr/bin/env python3
"""Seed the studio's first series: Island of No Witnesses.

This creates the STRUCTURE — series, season, episode, a placeholder bible and a
placeholder 12-scene brief that passes validation — so the studio can be
exercised end to end before the creative package exists.

Every piece of prose below is deliberately neutral scaffolding marked
PLACEHOLDER. It is not the series' creative content and is meant to be replaced
from the admin panel once the real package arrives. MarkeVita is the company
operating the studio and never appears inside a series' fiction.

    python seed/island_of_no_witnesses.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.store import store  # noqa: E402

SERIES_ID = "island_of_no_witnesses"
EPISODE_ID = "s01e01"
PLACEHOLDER = "PLACEHOLDER — replace from the admin panel when the creative package arrives."


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed() -> None:
    store.upsert("series", {
        "id": SERIES_ID,
        "title": "Island of No Witnesses",
        "logline": PLACEHOLDER,
        "genre": "mystery",
        "language": "en-US",
        "format": {"aspect_ratio": "9:16", "width": 1080, "height": 1920, "captions": "both"},
        "production_limits": {
            "maximum_episode_budget_usd": 50, "maximum_regenerations_per_scene": 2,
            "allowed_clip_seconds": [4, 6, 8], "min_scenes": 12, "max_scenes": 18,
            "min_episode_seconds": 90, "max_episode_seconds": 120,
        },
        "approval": {"status": "draft"},
        "style": {
            "style_sentence": (
                "PLACEHOLDER style sentence: cinematic photorealism, controlled natural light, "
                "restrained contrast, natural skin texture and physically plausible motion, "
                "50mm lens, deliberate stabilized camera movement, consistent faces, wardrobe and "
                "spatial layout, vertical 9:16 composition with clean caption-safe areas, "
                "no extra people, no malformed hands, no readable on-screen text."
            ),
            "camera_rules": "PLACEHOLDER — 35mm and 50mm only; no whip pans; hold the 180-degree line.",
            "color_rules": "PLACEHOLDER — cool daylight exteriors, warm practical interiors.",
            "negative_image": "text, watermark, extra limbs, duplicated objects",
            "negative_video": "morphing faces, sliding feet, flicker",
        },
        "status": "draft", "created_at": now(), "updated_at": now(),
    })

    store.upsert("seasons", {
        "series_id": SERIES_ID, "season_id": "s01", "number": 1,
        "title": "Season 1", "arc": PLACEHOLDER, "episode_order": [EPISODE_ID],
    })

    store.upsert("episodes", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "season_id": "s01", "number": 1,
        "title": "Episode 1", "logline": PLACEHOLDER, "status": "draft",
        "cliffhanger": {
            "scene_id": "sc12",
            "hook": "PLACEHOLDER — the question the episode ends on.",
            "resolves_in": "tbd",
        },
        "budget_usd": 50, "spent_usd": 0, "created_at": now(), "updated_at": now(),
    })

    # ── placeholder bible ──────────────────────────────────────────────────
    for cid, name, role in [("lead_a", "Lead A", "protagonist"), ("lead_b", "Lead B", "second lead")]:
        store.upsert("characters", {
            "series_id": SERIES_ID, "character_id": cid, "name": name, "visual": True,
            "role": role, "age": "30",
            "appearance": (
                f"PLACEHOLDER camera-ready appearance for {name}. Replace with 80-150 English words "
                "covering height, build, skin, face shape, eyes, brows, nose, lips, hair length, "
                "part and wave pattern, hands and any distinguishing marks. The image models read "
                "this text literally, so vagueness here becomes drift between clips."
            ),
            "behavior": "PLACEHOLDER — minimal, precise gestures.",
            "immutable": ["hair length", "eye colour", "no glasses"],
            "props": [], "seed_assets": [], "updated_at": now(),
        })
        store.upsert("clothing", {
            "series_id": SERIES_ID, "character_id": cid, "variant_id": "w_default",
            "is_default": True,
            "description": "PLACEHOLDER wardrobe: exact items, colours, materials, shoes and jewellery.",
            "immutable": [],
        })
        store.upsert("voices", {
            "series_id": SERIES_ID, "character_id": cid, "provider": "elevenlabs",
            "voice_env": f"ELEVENLABS_VOICE_ID_{cid.upper()}", "model_id": "eleven_v3",
            "language": "en-US", "style_notes": "PLACEHOLDER — register, pace, delivery.",
            "phone_fx": False, "locked": False,
        })

    store.upsert("locations", {
        "series_id": SERIES_ID, "location_id": "shore", "name": "Shore",
        "description": (
            "PLACEHOLDER location description. Replace with 80-150 English words covering geometry, "
            "materials, fixtures, the view, and above all what must never move between clips — that "
            "last part is what keeps the space consistent."
        ),
        "lighting_states": {"default": "PLACEHOLDER — overcast daylight",
                            "night": "PLACEHOLDER — moonlight and a single lamp"},
        "marks": "PLACEHOLDER — where characters stand by default.",
        "immutable": [], "seed_assets": [],
    })

    store.upsert("secrets_bible", {
        "series_id": SERIES_ID, "secret_id": "secret_one",
        "description": PLACEHOLDER, "holders_initial": ["lead_b"],
        "stakes": PLACEHOLDER,
    })

    store.upsert("relationships", {
        "series_id": SERIES_ID, "rel_id": "rel_a_b", "a": "lead_a", "b": "lead_b",
        "type": "PLACEHOLDER", "state": "unaware", "is_public": False,
        "allowed_states": ["unaware", "suspicious", "confronted"], "note": PLACEHOLDER,
    })

    # ── placeholder brief: 12 clips × 8s = 96s, inside the 90-120s window ──
    store.delete("scenes", {"series_id": SERIES_ID, "episode_id": EPISODE_ID})
    for i in range(1, 13):
        scene_id = f"sc{i:02d}"
        speaker = "lead_a" if i % 2 else "lead_b"
        scene = {
            "series_id": SERIES_ID, "episode_id": EPISODE_ID, "scene_id": scene_id,
            "sequence": i, "duration_seconds": 8, "location": "shore",
            "lighting_state": "default" if i <= 8 else "night",
            # One visible speaker per clip: the engine would otherwise split the scene.
            "characters_in_frame": [speaker],
            "wardrobe": {speaker: "w_default"},
            "action": f"PLACEHOLDER action for {scene_id}: one continuous shot, described in English.",
            "dialogue": [{"speaker": speaker, "text": f"Placeholder line {i}.", "delivery": "neutral"}],
            "shot_type": "medium shot", "lens": "50mm", "camera_motion": "slow push-in",
            "continuity_in": f"PLACEHOLDER state at the first frame of {scene_id}.",
            "continuity_out": f"PLACEHOLDER state at the last frame of {scene_id}.",
            "props": [], "knowledge_required": [], "knowledge_gained": [],
            "relationship_changes": [], "is_cliffhanger": i == 12, "status": "draft",
        }
        if i == 6:
            # Exercise the knowledge ledger: lead_a learns the secret mid-episode.
            scene["knowledge_gained"] = [{"character": "lead_a", "secret": "secret_one", "how": "overhears"}]
        if i == 9:
            scene["knowledge_required"] = [{"character": "lead_a", "secret": "secret_one"}]
            scene["relationship_changes"] = [{"id": "rel_a_b", "state": "suspicious"}]
        store.upsert("scenes", scene)

    store.insert("generation_history", {
        "series_id": SERIES_ID, "episode_id": EPISODE_ID, "event": "series.seeded",
        "entity_type": "series", "entity_id": SERIES_ID, "actor": "seed",
        "detail": {"note": "Placeholder structure; creative package pending."}, "created_at": now(),
    })
    print(f"Seeded '{SERIES_ID}' with season s01, episode {EPISODE_ID}, 12 placeholder scenes.")
    print("Everything is PLACEHOLDER scaffolding — replace it from the admin panel.")


if __name__ == "__main__":
    seed()
