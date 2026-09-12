# Series Package Contract (schema_version 2.0)

The pipeline is content-agnostic. It consumes a **series package**: a folder of JSON files plus optional seed images.
Any package that passes validation runs without code changes. Author the package in a folder such as
`series/<series_id>/` at the repository root. Validate it with:

```bash
cd pipeline && python run_episode.py --validate ../series/<series_id>
```

## Layout

```
series/<series_id>/
  series.json                         # series + seasons + format + limits + approval
  bible/characters.json               # every character: appearance, wardrobe variants, voice assignment, immutables
  bible/locations.json                # recurring locations with lighting states
  bible/props.json                    # optional; continuity props
  bible/relationships.json            # optional; relationship graph with current state
  bible/secrets.json                  # optional; secrets and who knows them at series start
  bible/style.json                    # one immutable style sentence + camera/color rules
  assets/                             # optional seed images referenced by seed_assets (png/jpg/webp)
  assets/room_tone.* / music.*        # optional beds mixed under dialogue
  episodes/<episode_id>/brief.json    # scene-by-scene shooting brief
  episodes/<episode_id>/production_prompts.json   # optional; per-scene keyframe/video prompts. If absent the pipeline drafts them with an LLM
```

All ids: `^[a-z0-9][a-z0-9_]{0,63}$`. All prose that goes to generation models (appearance, wardrobe, location
description, style sentence, prompts) must be in **English**. Dialogue is in the series language.

## series.json

```json
{
  "schema_version": "2.0",
  "series_id": "example_series",
  "title": "Example Title",
  "logline": "One sentence.",
  "genre": "romantic thriller",
  "language": "en-US",
  "format": {"aspect_ratio": "9:16", "width": 1080, "height": 1920, "captions": "both"},
  "seasons": [{"season_id": "s01", "number": 1, "title": "", "arc": "", "episodes": ["s01e01", "s01e02"]}],
  "production_limits": {"maximum_episode_budget_usd": 50, "maximum_regenerations_per_scene": 2,
                        "allowed_clip_seconds": [4, 6, 8], "min_scenes": 12, "max_scenes": 18,
                        "min_episode_seconds": 90, "max_episode_seconds": 120},
  "approval": {"status": "draft"}
}
```

`approval.status` must be `"approved"` (set by the owner) before any live/paid generation runs.
`captions`: `srt` (sidecar only), `burned`, or `both`. Budget and regeneration limits can only be lowered here, never raised above the environment caps.

## bible/characters.json

```json
[
  {
    "id": "char_id",
    "name": "Display Name",
    "visual": true,
    "role": "protagonist",
    "age": "30",
    "appearance": "English, 80-150 words, camera-ready: height, build, skin, face, eyes, brows, nose, lips, hair, hands, distinguishing marks.",
    "wardrobe": {
      "default": "w_arrival",
      "variants": {
        "w_arrival": {"description": "exact items, colors, materials, shoes, jewelry", "immutable": ["no necklace"]},
        "w_evening": {"description": "..."}
      }
    },
    "props": [{"prop_id": "phone", "rule": "always in left hand in scenes at the pier"}],
    "behavior": "gesture and expression style",
    "immutable": ["hair length", "eye color", "no glasses"],
    "voice": {"provider": "elevenlabs", "voice_env": "ELEVENLABS_VOICE_ID_CHAR_ID", "model_id": "eleven_v3",
              "language": "en-US", "style_notes": "warm low mezzo, 150 wpm", "phone_fx": false},
    "seed_assets": ["assets/char_id_seed.png"]
  },
  {"id": "voice_only_id", "name": "Unseen Caller", "visual": false,
   "voice": {"provider": "elevenlabs", "voice_env": "ELEVENLABS_VOICE_ID_VOICE_ONLY_ID", "phone_fx": true}}
]
```

- `visual: false` characters never appear in frame; they may speak as voice-over.
- `voice_env` names the environment variable that holds the ElevenLabs voice id. Never put the id itself in the package.
- Every wardrobe variant gets its own full-body reference; headshots and expressions are generated once per character.

## bible/locations.json

```json
[{"id": "villa_terrace", "name": "Villa Terrace", "description": "English 80-150 words: geometry, materials, fixtures, view, what must never move",
  "lighting_states": {"default": "late afternoon sun", "night": "warm lanterns, deep blue sky"},
  "marks": "where characters stand by default", "immutable": ["railing on the left"], "seed_assets": []}]
```

Scenes reference `lighting_state` by key; `default` is used when omitted.

## bible/props.json, relationships.json, secrets.json

```json
[{"id": "passenger_list", "description": "English product-style description for a reference image"}]

[{"id": "rel_a_b", "a": "char_a", "b": "char_b", "type": "romantic", "state": "secret_love", "public": false,
  "allowed_states": ["secret_love", "confessed", "rejected"], "note": "..."}]

[{"id": "secret_x", "description": "what the secret is", "holders_initial": ["char_b"], "stakes": "why it matters"}]
```

Relationship state and secret knowledge are **tracked across scenes and episodes**. Each episode starts from the
recorded end state of the previous episode (or the bible's initial state for the first episode).

## bible/style.json

```json
{"style_sentence": "One immutable sentence appended to every image and video prompt.",
 "camera_rules": "lens family, allowed moves, forbidden moves, 180-degree rule",
 "color_rules": "palette and lighting lock",
 "negative_image": "extra things to avoid in stills", "negative_video": "extra things to avoid in clips"}
```

Changing any bible file changes the `bible_version`; reference packs are regenerated and must be re-approved.

## episodes/<episode_id>/brief.json

```json
{
  "schema_version": "2.0", "series_id": "example_series", "season_id": "s01", "episode_id": "s01e01", "number": 1,
  "title": "Episode Title", "logline": "One sentence.",
  "opening_state": {"knowledge": {"secret_x": ["char_b"]}, "relationships": {"rel_a_b": "secret_love"}},
  "scenes": [
    {
      "scene_id": "sc01", "sequence": 1, "duration_seconds": 8,
      "location": "villa_terrace", "lighting_state": "night",
      "characters_in_frame": ["char_a"],
      "wardrobe": {"char_a": "w_evening"},
      "action": "What visibly happens in one continuous shot (English).",
      "dialogue": [{"speaker": "char_a", "text": "Spoken line in the series language.", "delivery": "quiet, certain"},
                   {"speaker": "voice_only_id", "text": "...", "delivery": "phone", "voice_over": true}],
      "shot_type": "medium close-up", "lens": "50mm", "camera_motion": "slow push-in",
      "continuity_in": "state at the first frame", "continuity_out": "state at the last frame",
      "props": [{"prop_id": "passenger_list", "state": "folded in jacket pocket"}],
      "knowledge_required": [{"character": "char_a", "secret": "secret_x"}],
      "knowledge_gained": [{"character": "char_a", "secret": "secret_x", "how": "overhears"}],
      "relationship_changes": [{"id": "rel_a_b", "state": "confessed"}],
      "is_cliffhanger": false
    }
  ],
  "cliffhanger": {"scene_id": "sc14", "hook": "The question or image the episode ends on.", "resolves_in": "s01e02"}
}
```

### Rules the validator enforces (no money is spent before these pass)

- 12–18 scenes (unless the series lowers the limits), each exactly 4, 6 or 8 seconds, total 90–120 s.
- Every character, location, wardrobe variant, prop, relationship and secret id exists in the bible.
- Voice-only characters are never `characters_in_frame`.
- Dialogue fits: at most 2.6 words per second of scene duration. Lines are later time-fitted; if a line still does not fit even at 1.15× tempo the run stops and asks for a shorter line.
- **One visible speaker per generated clip.** A scene where two visible characters both speak is automatically split into sub-shots (`sc07a`, `sc07b`, …), one per line, with durations partitioned from the allowed set (8 → 4+4). If the partition is impossible (e.g. 6 s with two lines) the brief is rejected: change the duration or split the scene yourself.
- `knowledge_required` must already be satisfied by the carried state or an earlier `knowledge_gained` in this episode. This is how "who knows which secret" is tracked.
- `relationship_changes.state` must be in `allowed_states` when that list exists.
- `opening_state`, if present, must equal the recorded end state of the previous episode.
- `cliffhanger.scene_id` must be the last scene; `hook` must be non-empty; `resolves_in` must be a later episode or `"tbd"`; the last scene must have action or dialogue.
- Scenes may carry `is_cliffhanger: true` on the final scene only.

## episodes/<episode_id>/production_prompts.json (optional)

```json
{"scenes": {"sc01": {
  "keyframe_prompt": "English. One cinematic film still = the FIRST frame: composition, who is where, pose, expression, gaze, props, lighting, lens. Refer to people ONLY by NAME in CAPS + '(see reference)'; never re-describe faces or wardrobe. No readable text.",
  "video_prompt": "English. Motion during the clip from that still: body, expression, gaze, props, camera motion exactly as specified. If someone speaks: 'X speaks the line with natural lip movement, no audible voice'. Never include dialogue words.",
  "negative": "scene-specific things to avoid",
  "keyframe_expected": "one sentence a QC reviewer checks the still against",
  "video_expected": "one sentence a QC reviewer checks the motion against",
  "lens": "50mm"
}}}
```

Keys may be the original `scene_id` (applied to every sub-shot of a split scene) or the sub-shot id (`sc07a`).
The pipeline appends the style sentence, reference legend, location description and lighting itself.

## What the pipeline produces per episode

`pipeline/runs/<series_id>/<episode_id>/`: `screenplay.md` (human review), `direction.json`, `work/ledger.json`
(knowledge/relationship events and end state), `out/masters/vN/` (episode.mp4, episode.srt, poster.jpg,
metadata.json, manifest.json, provenance.json), `out/qa/vN/report.json`, `state.json`.
`pipeline/runs/<series_id>/series_state.json` carries reference packs, approvals and per-episode end states.
