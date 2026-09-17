"""Supabase records → series package on disk.

The v0.3 engine consumes a series package (docs/SERIES_PACKAGE.md) and is
content-agnostic by design. Rather than rebuild the engine around a database,
the studio materialises the admin panel's records into exactly that package
before each run. The engine is used unchanged.

Nothing here writes secrets into the package: characters carry the NAME of the
environment variable holding their voice id (`voice_env`), never the id.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import settings
from .store import store

SCHEMA_VERSION = "2.0"

DEFAULT_LIMITS = {
    "maximum_episode_budget_usd": 150,
    "maximum_regenerations_per_scene": 2,
    "allowed_clip_seconds": [4, 6, 8],
    # A new series is set up for the best picture the studio can make. Both are
    # series settings, so any series can be turned down without touching code.
    "video_model": "fal-ai/veo3.1/image-to-video",
    "picture": "maximum",
    "min_scenes": 23, "max_scenes": 34,
    "min_episode_seconds": 180, "max_episode_seconds": 240,
}

DEFAULT_STYLE = {
    "style_sentence": "",
    "camera_rules": "", "color_rules": "", "negative_image": "", "negative_video": "",
}


def package_dir(series_id: str) -> Path:
    return settings.package_dir / series_id


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean(d: dict) -> dict:
    """Drop empty optional values so the package stays schema-clean."""
    return {k: v for k, v in d.items() if v not in ("", None, [], {})}


# ── series.json ────────────────────────────────────────────────────────────

def build_series_json(series: dict) -> dict:
    seasons = store.list("seasons", {"series_id": series["id"]}, order="number")
    episodes = store.list("episodes", {"series_id": series["id"]})
    preview_ids = {
        ep["episode_id"] for ep in episodes
        if ep.get("status") == "preview" or (ep.get("brief") or {}).get("kind") == "clip_preview"
    }
    # The season's list is appended to as episodes are opened, so it carries the
    # order they happened to be created in. The engine reads it as the order of
    # the story: episode three was told it continues episode four, and was
    # validated against the wrong ending. Episode numbers are what the producer
    # sees and what the ids are built from, so they decide the order here.
    # The sort is stable, so episodes the studio never numbered keep the order
    # they were declared in rather than being shuffled by their ids.
    number_of = {ep["episode_id"]: int(ep.get("number") or 0) for ep in episodes}
    # Standalone camera tests are not part of the full episode package, and a
    # season left with nothing in it is not either: the engine's schema requires
    # every season to hold at least one episode, so emitting an empty one makes
    # the whole package invalid and every episode of the series unwritable.
    seasons = [
        {**s, "episode_order": sorted((eid for eid in (s.get("episode_order") or [])
                                       if eid not in preview_ids),
                                      key=lambda eid: number_of.get(eid, 0))}
        for s in seasons
    ]
    seasons = [s for s in seasons if s["episode_order"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "series_id": series["id"],
        "title": series.get("title") or series["id"],
        "logline": series.get("logline", ""),
        "genre": series.get("genre", ""),
        "language": series.get("language") or "en-US",
        "format": series.get("format") or {"aspect_ratio": "9:16", "width": 1080, "height": 1920, "captions": "both"},
        "seasons": [
            {
                "season_id": s["season_id"],
                "number": int(s.get("number") or 1),
                "title": s.get("title", ""),
                "arc": s.get("arc", ""),
                "episodes": list(s.get("episode_order") or []),
            }
            for s in seasons
        ],
        "production_limits": {**DEFAULT_LIMITS, **(series.get("production_limits") or {})},
        "approval": series.get("approval") or {"status": "draft"},
    }


# ── bible ──────────────────────────────────────────────────────────────────

def build_characters(series_id: str) -> list[dict]:
    out = []
    for c in store.list("characters", {"series_id": series_id}, order="character_id"):
        variants = store.list("clothing", {"series_id": series_id, "character_id": c["character_id"]},
                              order="variant_id")
        voice = store.get("voices", {"series_id": series_id, "character_id": c["character_id"]})
        rec: dict = {
            "id": c["character_id"],
            "name": c.get("name") or c["character_id"],
            "visual": bool(c.get("visual", True)),
        }
        for key in ("role", "age", "appearance", "behavior"):
            if c.get(key):
                rec[key] = c[key]
        if c.get("immutable"):
            rec["immutable"] = list(c["immutable"])
        if c.get("props"):
            rec["props"] = list(c["props"])
        if c.get("seed_assets"):
            rec["seed_assets"] = list(c["seed_assets"])
        if variants:
            default = next((v["variant_id"] for v in variants if v.get("is_default")), variants[0]["variant_id"])
            rec["wardrobe"] = {
                "default": default,
                "variants": {
                    v["variant_id"]: _clean({
                        "description": v.get("description", ""),
                        "immutable": list(v.get("immutable") or []),
                    }) or {"description": ""}
                    for v in variants
                },
            }
        if voice:
            rec["voice"] = _clean({
                "provider": voice.get("provider") or "elevenlabs",
                "voice_env": voice.get("voice_env") or f"ELEVENLABS_VOICE_ID_{c['character_id'].upper()}",
                "model_id": voice.get("model_id") or "",
                "language": voice.get("language") or "",
                "style_notes": voice.get("style_notes") or "",
                "phone_fx": bool(voice.get("phone_fx")),
            })
            rec["voice"].setdefault("provider", "elevenlabs")
            rec["voice"].setdefault("voice_env", f"ELEVENLABS_VOICE_ID_{c['character_id'].upper()}")
        out.append(rec)
    return out


def build_locations(series_id: str) -> list[dict]:
    out = []
    for l in store.list("locations", {"series_id": series_id}, order="location_id"):
        rec = {
            "id": l["location_id"],
            "name": l.get("name") or l["location_id"],
            "description": l.get("description", ""),
        }
        states = l.get("lighting_states") or {}
        if states:
            rec["lighting_states"] = states
        for key in ("marks",):
            if l.get(key):
                rec[key] = l[key]
        if l.get("immutable"):
            rec["immutable"] = list(l["immutable"])
        if l.get("seed_assets"):
            rec["seed_assets"] = list(l["seed_assets"])
        out.append(rec)
    return out


def build_props(series_id: str) -> list[dict]:
    return [{"id": p["prop_id"], "description": p.get("description", "")}
            for p in store.list("props", {"series_id": series_id}, order="prop_id")]


def build_relationships(series_id: str) -> list[dict]:
    out = []
    for r in store.list("relationships", {"series_id": series_id}, order="rel_id"):
        rec = {"id": r["rel_id"], "a": r["a"], "b": r["b"],
               "type": r.get("type", ""), "state": r.get("state", "")}
        if r.get("is_public"):
            rec["public"] = True
        if r.get("allowed_states"):
            rec["allowed_states"] = list(r["allowed_states"])
        if r.get("note"):
            rec["note"] = r["note"]
        out.append(rec)
    return out


def build_secrets(series_id: str) -> list[dict]:
    out = []
    for s in store.list("secrets_bible", {"series_id": series_id}, order="secret_id"):
        rec = {"id": s["secret_id"], "description": s.get("description", ""),
               "holders_initial": list(s.get("holders_initial") or [])}
        if s.get("stakes"):
            rec["stakes"] = s["stakes"]
        out.append(rec)
    return out


# ── episodes ───────────────────────────────────────────────────────────────

SCENE_PASSTHROUGH = [
    "action", "shot_type", "camera_motion", "continuity_in", "continuity_out",
]


def build_brief(series_id: str, episode: dict) -> dict:
    scenes = store.list("scenes", {"series_id": series_id, "episode_id": episode["episode_id"]},
                        order="sequence")
    brief: dict = {
        "schema_version": SCHEMA_VERSION,
        "series_id": series_id,
        "season_id": episode.get("season_id") or "s01",
        "episode_id": episode["episode_id"],
        "number": int(episode.get("number") or 1),
        "title": episode.get("title") or episode["episode_id"],
        "logline": episode.get("logline", ""),
        "scenes": [_scene_json(s) for s in scenes],
        "cliffhanger": episode.get("cliffhanger") or {},
    }
    if episode.get("opening_state"):
        brief["opening_state"] = episode["opening_state"]
    return brief


def _scene_json(s: dict) -> dict:
    rec: dict = {
        "scene_id": s["scene_id"],
        "sequence": int(s.get("sequence") or 1),
        "duration_seconds": int(s.get("duration_seconds") or 6),
        "location": s.get("location", ""),
        "characters_in_frame": list(s.get("characters_in_frame") or []),
        "action": s.get("action", ""),
        "shot_type": s.get("shot_type", ""),
        "camera_motion": s.get("camera_motion", ""),
    }
    if s.get("lighting_state") and s["lighting_state"] != "default":
        rec["lighting_state"] = s["lighting_state"]
    if s.get("lens"):
        rec["lens"] = s["lens"]
    for key in ("wardrobe",):
        if s.get(key):
            rec[key] = s[key]
    for key in ("dialogue", "props", "knowledge_required", "knowledge_gained", "relationship_changes"):
        if s.get(key):
            rec[key] = s[key]
    for key in ("continuity_in", "continuity_out"):
        if s.get(key):
            rec[key] = s[key]
    if s.get("is_cliffhanger"):
        rec["is_cliffhanger"] = True
    return rec


# ── materialisation ────────────────────────────────────────────────────────

def fetch_seeds(root: Path, characters: list[dict]) -> list[str]:
    """Bring each character's locked face into the package as a file.

    A character with no seed is drawn from their description, so every version
    of the bible produces a different person — four Adrians across four edits.
    A locked face is stored as a storage key; the engine needs a file inside
    the package, and generates "the same person as this reference" from it.

    A face that cannot be fetched is dropped rather than faked: an empty file
    would make the provider reject the request, and a run that draws the
    character afresh is better than one that cannot draw them at all.
    """
    fetched: list[str] = []
    pending = [(c, list(c.get("seed_assets") or [])) for c in characters]
    if not any(keys for _, keys in pending):
        return fetched
    from .config import PIPELINE_DIR
    from serial.config import Config
    from serial.storage import R2
    try:
        storage = R2(Config.load(PIPELINE_DIR, live=True), lambda _message: None)
    except Exception:
        for character, _ in pending:
            character.pop("seed_assets", None)
        return fetched
    for character, keys in pending:
        local: list[str] = []
        for key in keys:
            name = f"assets/{character['id']}_{key.rsplit('/', 1)[-1]}"
            try:
                storage.get(key, root / name)
                if (root / name).stat().st_size:
                    local.append(name)
            except Exception:
                continue
        if local:
            character["seed_assets"] = local
            fetched += local
        else:
            character.pop("seed_assets", None)
    return fetched


def materialize(series_id: str, clean: bool = False) -> Path:
    """Write the complete series package to disk and return its folder.

    Called before every validation and every production run so the engine
    always sees exactly what the admin panel currently holds.
    """
    series = store.get("series", {"id": series_id})
    if not series:
        raise KeyError(f"series {series_id!r} not found")
    root = package_dir(series_id)
    if clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    _write(root / "series.json", build_series_json(series))
    characters = build_characters(series_id)
    fetch_seeds(root, characters)
    _write(root / "bible" / "characters.json", characters)
    _write(root / "bible" / "locations.json", build_locations(series_id))
    _write(root / "bible" / "style.json", {**DEFAULT_STYLE, **(series.get("style") or {})})

    props = build_props(series_id)
    rels = build_relationships(series_id)
    secrets = build_secrets(series_id)
    # These three are optional in the contract; write them only when populated
    # so an empty bible does not fail validation on an empty array.
    for name, data in (("props", props), ("relationships", rels), ("secrets", secrets)):
        path = root / "bible" / f"{name}.json"
        if data:
            _write(path, data)
        elif path.exists():
            path.unlink()

    for ep in store.list("episodes", {"series_id": series_id}, order="number"):
        if ep.get("status") == "preview" or (ep.get("brief") or {}).get("kind") == "clip_preview":
            continue
        scenes = store.list("scenes", {"series_id": series_id, "episode_id": ep["episode_id"]})
        if not scenes:
            continue  # brief not written yet; the engine reports it as missing
        _write(root / "episodes" / ep["episode_id"] / "brief.json", build_brief(series_id, ep))

    (root / "assets").mkdir(exist_ok=True)
    return root
