"""Bundled creative seed for The Wild Cat.

The seed is idempotent: it writes the initial series package once, records a
completion marker, and never starts generation, approves spend, or publishes.
"""
from __future__ import annotations

import base64
import json
import zlib
from datetime import datetime, timezone

from .store import store

SERIES_ID = "the_wild_cat"
EPISODE_ID = "s01e01"
MARKER_EVENT = "series.initial_seed.complete"

_DATA_B64 = """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"""

def _payload() -> dict:
    raw = zlib.decompress(base64.b64decode(_DATA_B64))
    return json.loads(raw.decode("utf-8"))

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def seed_if_missing() -> bool:
    marker = store.get("generation_history", {"series_id": SERIES_ID, "event": MARKER_EVENT})
    if marker:
        return False

    data = _payload()
    series = data["series"]
    brief = data["brief"]
    timestamp = _now()

    store.upsert("series", {
        "id": SERIES_ID,
        "title": series["title"],
        "logline": series["logline"],
        "genre": series["genre"],
        "language": series["language"],
        "format": series["format"],
        "production_limits": series["production_limits"],
        "approval": {"status": "draft"},
        "style": data["style"],
        "status": "draft",
        "created_at": timestamp,
        "updated_at": timestamp,
    })

    season = series["seasons"][0]
    store.upsert("seasons", {
        "series_id": SERIES_ID,
        "season_id": season["season_id"],
        "number": season["number"],
        "title": season["title"],
        "arc": season["arc"],
        "episode_order": season["episodes"],
    })

    store.upsert("episodes", {
        "series_id": SERIES_ID,
        "episode_id": EPISODE_ID,
        "season_id": brief["season_id"],
        "number": brief["number"],
        "title": brief["title"],
        "logline": brief["logline"],
        "status": "draft",
        "brief": brief,
        "opening_state": brief.get("opening_state", {}),
        "cliffhanger": brief["cliffhanger"],
        "target_seconds": sum(s["duration_seconds"] for s in brief["scenes"]),
        "budget_usd": series["production_limits"]["maximum_episode_budget_usd"],
        "spent_usd": 0,
        "created_at": timestamp,
        "updated_at": timestamp,
    })

    for character in data["characters"]:
        store.upsert("characters", {
            "series_id": SERIES_ID,
            "character_id": character["id"],
            "name": character["name"],
            "visual": character.get("visual", True),
            "role": character.get("role", ""),
            "age": character.get("age", ""),
            "appearance": character.get("appearance", ""),
            "behavior": character.get("behavior", ""),
            "immutable": character.get("immutable", []),
            "props": character.get("props", []),
            "seed_assets": character.get("seed_assets", []),
            "updated_at": timestamp,
        })

        wardrobe = character.get("wardrobe")
        if wardrobe:
            default_variant = wardrobe.get("default", "")
            for variant_id, variant in wardrobe.get("variants", {}).items():
                store.upsert("clothing", {
                    "series_id": SERIES_ID,
                    "character_id": character["id"],
                    "variant_id": variant_id,
                    "is_default": variant_id == default_variant,
                    "description": variant.get("description", ""),
                    "immutable": variant.get("immutable", []),
                })

        voice = character.get("voice")
        if voice:
            store.upsert("voices", {
                "series_id": SERIES_ID,
                "character_id": character["id"],
                "provider": voice.get("provider", "elevenlabs"),
                "voice_env": voice.get("voice_env", ""),
                "model_id": voice.get("model_id", "eleven_v3"),
                "language": voice.get("language", "en-US"),
                "style_notes": voice.get("style_notes", ""),
                "phone_fx": voice.get("phone_fx", False),
                "locked": True,
            })

    for location in data["locations"]:
        store.upsert("locations", {
            "series_id": SERIES_ID,
            "location_id": location["id"],
            "name": location["name"],
            "description": location["description"],
            "lighting_states": location["lighting_states"],
            "marks": location.get("marks", ""),
            "immutable": location.get("immutable", []),
            "seed_assets": location.get("seed_assets", []),
        })

    for prop in data["props"]:
        store.upsert("props", {
            "series_id": SERIES_ID,
            "prop_id": prop["id"],
            "description": prop["description"],
        })

    for rel in data["relationships"]:
        store.upsert("relationships", {
            "series_id": SERIES_ID,
            "rel_id": rel["id"],
            "a": rel["a"],
            "b": rel["b"],
            "type": rel["type"],
            "state": rel["state"],
            "is_public": rel.get("public", False),
            "allowed_states": rel.get("allowed_states", []),
            "note": rel.get("note", ""),
        })

    store.delete("scenes", {"series_id": SERIES_ID, "episode_id": EPISODE_ID})
    for scene in brief["scenes"]:
        row = dict(scene)
        row["series_id"] = SERIES_ID
        row["episode_id"] = EPISODE_ID
        row["status"] = "draft"
        store.upsert("scenes", row)

    store.insert("scripts", {
        "series_id": SERIES_ID,
        "episode_id": EPISODE_ID,
        "version": 1,
        "source": "seed",
        "filename": "the_wild_cat_s01e01.json",
        "content": json.dumps(brief, ensure_ascii=False, indent=2),
        "parsed": brief,
        "created_by": "ChatGPT creative seed",
        "created_at": timestamp,
    })

    store.insert("generation_history", {
        "series_id": SERIES_ID,
        "episode_id": EPISODE_ID,
        "event": MARKER_EVENT,
        "entity_type": "series",
        "entity_id": SERIES_ID,
        "actor": "seed",
        "detail": {
            "title": series["title"],
            "episode": brief["title"],
            "duration_seconds": sum(s["duration_seconds"] for s in brief["scenes"]),
            "note": "Creative package loaded. No generation, approval, spending or publishing started.",
        },
        "created_at": timestamp,
    })
    return True
