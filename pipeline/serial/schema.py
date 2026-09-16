"""JSON Schema контракты series package v2.0. Пайплайн принимает любой пакет, проходящий эти схемы, без правок кода.
Контракт для авторов пакета: docs/SERIES_PACKAGE.md."""

SCHEMA_VERSION = "2.0"

_id = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9_]{0,63}$"}
_str = {"type": "string"}
_strlist = {"type": "array", "items": _str}

SERIES = {
    "type": "object",
    "required": ["schema_version", "series_id", "title", "language", "format", "seasons"],
    "additionalProperties": True,
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "series_id": _id,
        "title": _str,
        "logline": _str,
        "genre": _str,
        "language": _str,                      # BCP-47, напр. en-US
        "format": {
            "type": "object", "required": ["aspect_ratio", "width", "height"],
            "properties": {"aspect_ratio": {"enum": ["9:16", "16:9"]}, "width": {"type": "integer"}, "height": {"type": "integer"},
                           "captions": {"enum": ["srt", "burned", "both", "none"]}},
        },
        "seasons": {
            "type": "array", "minItems": 1,
            "items": {"type": "object", "required": ["season_id", "number", "episodes"],
                      "properties": {"season_id": _id, "number": {"type": "integer"}, "title": _str, "arc": _str,
                                     "episodes": {"type": "array", "items": _id, "minItems": 1}}},
        },
        "production_limits": {
            "type": "object",
            "properties": {"maximum_episode_budget_usd": {"type": "number"}, "maximum_regenerations_per_scene": {"type": "integer"},
                           "allowed_clip_seconds": {"type": "array", "items": {"enum": [4, 6, 8]}},
                           "video_model": {"enum": ["fal-ai/veo3.1/fast/image-to-video",
                                                   "fal-ai/veo3.1/image-to-video"]},
                           "picture": {"enum": ["standard", "high", "maximum"]},
                           "min_scenes": {"type": "integer"}, "max_scenes": {"type": "integer"},
                           "min_episode_seconds": {"type": "integer"}, "max_episode_seconds": {"type": "integer"}},
        },
        "approval": {"type": "object", "properties": {"status": {"enum": ["draft", "approved"]}, "by": _str, "at": _str}},
    },
}

WARDROBE_VARIANT = {"type": "object", "required": ["description"],
                    "properties": {"description": _str, "immutable": _strlist}}

CHARACTER = {
    "type": "object",
    "required": ["id", "name", "visual"],
    "properties": {
        "id": _id, "name": _str, "visual": {"type": "boolean"}, "role": _str, "age": _str,
        "appearance": _str,                        # camera-ready, English
        "wardrobe": {"type": "object", "required": ["default", "variants"],
                     "properties": {"default": _id, "variants": {"type": "object", "additionalProperties": WARDROBE_VARIANT, "minProperties": 1}}},
        "props": {"type": "array", "items": {"type": "object", "required": ["prop_id"], "properties": {"prop_id": _id, "rule": _str}}},
        "behavior": _str,
        "immutable": _strlist,
        "voice": {"type": "object", "required": ["provider", "voice_env"],
                  "properties": {"provider": {"enum": ["elevenlabs"]}, "voice_env": {"type": "string", "pattern": r"^[A-Z0-9_]+$"},
                                 "model_id": _str, "language": _str, "style_notes": _str,
                                 "settings": {"type": "object"}, "phone_fx": {"type": "boolean"}}},
        "seed_assets": _strlist,                   # пути относительно папки пакета
    },
    "allOf": [{"if": {"properties": {"visual": {"const": True}}}, "then": {"required": ["appearance", "wardrobe"]}}],
}
CHARACTERS = {"type": "array", "items": CHARACTER, "minItems": 1}

LOCATION = {
    "type": "object", "required": ["id", "name", "description"],
    "properties": {"id": _id, "name": _str, "description": _str,
                   "lighting_states": {"type": "object", "additionalProperties": _str},   # {"default": "...", "night": "..."}
                   "marks": _str, "immutable": _strlist, "seed_assets": _strlist},
}
LOCATIONS = {"type": "array", "items": LOCATION, "minItems": 1}

PROPS = {"type": "array", "items": {"type": "object", "required": ["id", "description"], "properties": {"id": _id, "description": _str}}}

RELATIONSHIP = {
    "type": "object", "required": ["id", "a", "b", "type", "state"],
    "properties": {"id": _id, "a": _id, "b": _id, "type": _str, "state": _str, "public": {"type": "boolean"}, "note": _str,
                   "allowed_states": _strlist},
}
RELATIONSHIPS = {"type": "array", "items": RELATIONSHIP}

SECRET = {
    "type": "object", "required": ["id", "description", "holders_initial"],
    "properties": {"id": _id, "description": _str, "holders_initial": {"type": "array", "items": _id}, "stakes": _str},
}
SECRETS = {"type": "array", "items": SECRET}

STYLE = {
    "type": "object", "required": ["style_sentence"],
    "properties": {"style_sentence": _str, "camera_rules": _str, "color_rules": _str,
                   "negative_image": _str, "negative_video": _str},
}

DIALOGUE_LINE = {"type": "object", "required": ["speaker", "text"],
                 "properties": {"speaker": _id, "text": _str, "delivery": _str, "voice_over": {"type": "boolean"}}}

SCENE = {
    "type": "object",
    "required": ["scene_id", "sequence", "duration_seconds", "location", "characters_in_frame", "action", "shot_type", "camera_motion"],
    "properties": {
        "scene_id": _id, "sequence": {"type": "integer", "minimum": 1}, "duration_seconds": {"enum": [4, 6, 8]},
        "location": _id, "lighting_state": _str,
        "characters_in_frame": {"type": "array", "items": _id},
        "wardrobe": {"type": "object", "additionalProperties": _id},          # {char_id: variant_id}
        "action": _str,
        "dialogue": {"type": "array", "items": DIALOGUE_LINE},
        "shot_type": _str, "lens": {"enum": ["24mm", "35mm", "50mm", "85mm"]}, "camera_motion": _str,
        "continuity_in": _str, "continuity_out": _str,
        "props": {"type": "array", "items": {"type": "object", "required": ["prop_id", "state"], "properties": {"prop_id": _id, "state": _str}}},
        "knowledge_required": {"type": "array", "items": {"type": "object", "required": ["character", "secret"], "properties": {"character": _id, "secret": _id}}},
        "knowledge_gained": {"type": "array", "items": {"type": "object", "required": ["character", "secret"], "properties": {"character": _id, "secret": _id, "how": _str}}},
        "relationship_changes": {"type": "array", "items": {"type": "object", "required": ["id", "state"], "properties": {"id": _id, "state": _str, "note": _str}}},
        "is_cliffhanger": {"type": "boolean"},
    },
}

EPISODE = {
    "type": "object",
    "required": ["schema_version", "series_id", "season_id", "episode_id", "number", "title", "scenes", "cliffhanger"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "series_id": _id, "season_id": _id, "episode_id": _id, "number": {"type": "integer"},
        "title": _str, "logline": _str,
        "opening_state": {"type": "object",
                          "properties": {"knowledge": {"type": "object", "additionalProperties": {"type": "array", "items": _id}},   # {secret_id: [char_ids]}
                                         "relationships": {"type": "object", "additionalProperties": _str}}},                       # {rel_id: state}
        "scenes": {"type": "array", "items": SCENE, "minItems": 1},
        "cliffhanger": {"type": "object", "required": ["scene_id", "hook"],
                        "properties": {"scene_id": _id, "hook": _str, "resolves_in": _str}},
        "status": _str,
    },
}

PRODUCTION_PROMPTS = {
    "type": "object", "required": ["scenes"],
    "properties": {"scenes": {"type": "object", "additionalProperties": {
        "type": "object", "required": ["keyframe_prompt", "video_prompt"],
        "properties": {"keyframe_prompt": _str, "video_prompt": _str, "negative": _str,
                       "keyframe_expected": _str, "video_expected": _str, "lens": _str}}}},
}

FILES = {
    "series.json": SERIES,
    "bible/characters.json": CHARACTERS,
    "bible/locations.json": LOCATIONS,
    "bible/props.json": PROPS,
    "bible/relationships.json": RELATIONSHIPS,
    "bible/secrets.json": SECRETS,
    "bible/style.json": STYLE,
}
OPTIONAL_FILES = {"bible/props.json", "bible/relationships.json", "bible/secrets.json"}
