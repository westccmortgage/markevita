"""Генерирует синтетический content-agnostic series package для тестов (placeholder-имена, без сюжета)."""
import json
from pathlib import Path


def build(root: Path, fast: bool = False):
    root = Path(root)
    (root / "bible").mkdir(parents=True, exist_ok=True)
    (root / "assets").mkdir(exist_ok=True)
    from PIL import Image
    Image.new("RGB", (400, 600), (120, 120, 140)).save(root / "assets" / "char_a_seed.png")
    Image.new("RGB", (800, 600), (90, 110, 90)).save(root / "assets" / "loc_a_seed.webp")

    w = lambda rel, obj: (root / rel).write_text(json.dumps(obj, indent=2), encoding="utf-8")
    w("series.json", {
        "schema_version": "2.0", "series_id": "fixture_series", "title": "Fixture Series", "logline": "Synthetic test package.",
        "language": "en-US", "format": {"aspect_ratio": "9:16", "width": 1080, "height": 1920, "captions": "both"},
        "seasons": [{"season_id": "s01", "number": 1, "episodes": ["s01e01", "s01e02"]}],
        "production_limits": ({"maximum_episode_budget_usd": 50, "maximum_regenerations_per_scene": 2, "min_scenes": 1, "max_scenes": 18, "min_episode_seconds": 10, "max_episode_seconds": 120}
                              if fast else {"maximum_episode_budget_usd": 50, "maximum_regenerations_per_scene": 2, "min_scenes": 12, "max_scenes": 18}),
        "approval": {"status": "draft"},
    })
    w("bible/characters.json", [
        {"id": "char_a", "name": "Char A", "visual": True, "age": "30", "appearance": "placeholder appearance A",
         "wardrobe": {"default": "w_day", "variants": {"w_day": {"description": "placeholder day outfit"}, "w_night": {"description": "placeholder evening outfit"}}},
         "props": [{"prop_id": "prop_a", "rule": "carries prop_a in scenes 2-4"}], "immutable": ["hair"], "seed_assets": ["assets/char_a_seed.png"],
         "voice": {"provider": "elevenlabs", "voice_env": "ELEVENLABS_VOICE_ID_CHAR_A", "language": "en-US"}},
        {"id": "char_b", "name": "Char B", "visual": True, "age": "35", "appearance": "placeholder appearance B",
         "wardrobe": {"default": "w_main", "variants": {"w_main": {"description": "placeholder outfit B"}}},
         "voice": {"provider": "elevenlabs", "voice_env": "ELEVENLABS_VOICE_ID_CHAR_B"}},
        {"id": "char_v", "name": "Char V", "visual": False, "voice": {"provider": "elevenlabs", "voice_env": "ELEVENLABS_VOICE_ID_CHAR_V", "phone_fx": True}},
    ])
    w("bible/locations.json", [
        {"id": "loc_a", "name": "Location A", "description": "placeholder location A", "lighting_states": {"default": "day", "night": "night"}, "seed_assets": ["assets/loc_a_seed.webp"]},
        {"id": "loc_b", "name": "Location B", "description": "placeholder location B", "lighting_states": {"default": "interior"}},
    ])
    w("bible/props.json", [{"id": "prop_a", "description": "placeholder prop"}])
    w("bible/relationships.json", [{"id": "rel_ab", "a": "char_a", "b": "char_b", "type": "placeholder", "state": "state_1", "allowed_states": ["state_1", "state_2"]}])
    w("bible/secrets.json", [{"id": "secret_x", "description": "placeholder secret x", "holders_initial": ["char_b"]},
                             {"id": "secret_y", "description": "placeholder secret y", "holders_initial": []}])
    w("bible/style.json", {"style_sentence": "placeholder style sentence.", "camera_rules": "locked or slow moves.", "color_rules": "neutral."})

    def sc(i, d, loc, chars, dialogue, **kw):
        return {"scene_id": f"sc{i:02d}", "sequence": i, "duration_seconds": d, "location": loc, "characters_in_frame": chars,
                "action": f"placeholder action {i}", "dialogue": dialogue, "shot_type": "medium", "camera_motion": "locked tripod",
                "continuity_in": "", "continuity_out": "", **kw}
    line = lambda sp, txt, **k: {"speaker": sp, "text": txt, **k}
    scenes = [
        sc(1, 8, "loc_a", ["char_a"], [line("char_a", "Placeholder line one.")], lighting_state="night"),
        sc(2, 8, "loc_a", ["char_a", "char_b"], [line("char_b", "Placeholder line two.")], props=[{"prop_id": "prop_a", "state": "in hand"}]),
        sc(3, 8, "loc_a", ["char_a", "char_b"], [line("char_a", "Placeholder three."), line("char_b", "Placeholder four.")]),   # split a/b
        sc(4, 8, "loc_b", ["char_a"], [line("char_v", "Placeholder phone line.", voice_over=True, delivery="phone")]),
        sc(5, 8, "loc_b", ["char_a", "char_b"], [line("char_b", "Placeholder five.")], knowledge_gained=[{"character": "char_a", "secret": "secret_x", "how": "told"}]),
        sc(6, 8, "loc_b", ["char_a"], [], knowledge_required=[{"character": "char_a", "secret": "secret_x"}]),
        sc(7, 8, "loc_b", ["char_a"], [line("char_a", "Placeholder seven.")], wardrobe={"char_a": "w_night"}),
        sc(8, 8, "loc_a", ["char_b"], [line("char_b", "Placeholder eight.")], relationship_changes=[{"id": "rel_ab", "state": "state_2"}]),
        sc(9, 8, "loc_a", ["char_a", "char_b"], []),
        sc(10, 8, "loc_a", ["char_a"], [line("char_a", "Placeholder ten.")]),
        sc(11, 6, "loc_b", ["char_b"], [line("char_b", "Placeholder eleven.")], knowledge_gained=[{"character": "char_b", "secret": "secret_y"}]),
        sc(12, 6, "loc_b", ["char_a", "char_b"], [line("char_b", "Placeholder twelve.")], is_cliffhanger=True, continuity_out="cut to black"),
    ]
    if fast:
        keep = {"sc03": 8, "sc04": 4, "sc05": 4, "sc06": 4, "sc08": 4, "sc11": 4, "sc12": 4}
        scenes = [dict(s, duration_seconds=keep[s["scene_id"]], sequence=i + 1) for i, s in enumerate(x for x in scenes if x["scene_id"] in keep)]
    (root / "episodes" / "s01e01").mkdir(parents=True, exist_ok=True)
    w("episodes/s01e01/brief.json", {"schema_version": "2.0", "series_id": "fixture_series", "season_id": "s01", "episode_id": "s01e01", "number": 1,
                                     "title": "Fixture One", "logline": "placeholder", "scenes": scenes,
                                     "cliffhanger": {"scene_id": "sc12", "hook": "placeholder hook", "resolves_in": "s01e02"}})
    w("episodes/s01e01/production_prompts.json", {"scenes": {s["scene_id"]: {"keyframe_prompt": f"placeholder keyframe {s['scene_id']}", "video_prompt": f"placeholder motion {s['scene_id']}"} for s in scenes}})
    n2 = 2 if fast else 11
    scenes2 = [sc(i, 8, "loc_a", ["char_a"], [line("char_a", f"Placeholder ep2 line {i}.")]) for i in range(1, n2 + 1)] + \
              [sc(n2 + 1, 8, "loc_b", ["char_a"], [line("char_a", "Placeholder end.")], is_cliffhanger=True, continuity_out="hard cut")]
    scenes2[0]["knowledge_required"] = [{"character": "char_a", "secret": "secret_x"}]
    (root / "episodes" / "s01e02").mkdir(parents=True, exist_ok=True)
    w("episodes/s01e02/brief.json", {"schema_version": "2.0", "series_id": "fixture_series", "season_id": "s01", "episode_id": "s01e02", "number": 2,
                                     "title": "Fixture Two", "scenes": scenes2,
                                     "opening_state": {"knowledge": {"secret_x": ["char_a", "char_b"], "secret_y": ["char_b"]}, "relationships": {"rel_ab": "state_2"}},
                                     "cliffhanger": {"scene_id": f"sc{n2 + 1:02d}", "hook": "placeholder hook 2", "resolves_in": "tbd"}})
    return root


if __name__ == "__main__":
    import sys
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/series_fixture"), fast="--fast" in sys.argv)
