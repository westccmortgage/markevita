"""Промпты. Английский для моделей. Реплики персонажей никогда не переписываются: они берутся из брифа как есть."""

BIBLE_PARSE = """You are a production assistant converting a markdown CHARACTER/WORLD BIBLE into a structured series package.
Return ONLY a JSON object with keys: "characters", "locations", "props", "relationships", "secrets", "style" following exactly:
characters: [{"id": "<snake_case id>", "name": "...", "visual": true|false, "role": "...", "age": "...",
  "appearance": "<English, 80-150 words, camera-ready>", "wardrobe": {"default": "w_default", "variants": {"w_default": {"description": "<English exact items, colors, materials, accessories>", "immutable": ["..."]}}},
  "props": [{"prop_id": "...", "rule": "..."}], "behavior": "...", "immutable": ["..."],
  "voice": {"provider": "elevenlabs", "voice_env": "ELEVENLABS_VOICE_ID_<ID_UPPER>", "model_id": "eleven_v3", "language": "en-US", "style_notes": "...", "phone_fx": false},
  "seed_assets": ["<relative path if the bible names an existing image, else omit>"]}]
locations: [{"id": "...", "name": "...", "description": "<English 80-150 words>", "lighting_states": {"default": "..."}, "marks": "...", "immutable": ["..."], "seed_assets": []}]
props: [{"id": "...", "description": "..."}]
relationships: [{"id": "...", "a": "<char id>", "b": "<char id>", "type": "...", "state": "...", "public": false, "note": "..."}]
secrets: [{"id": "...", "description": "...", "holders_initial": ["<char ids>"], "stakes": "..."}]
style: {"style_sentence": "<verbatim canonical sentence if present, else compose one>", "camera_rules": "...", "color_rules": "..."}
Voice-only characters: visual=false, omit appearance/wardrobe. Keep ids stable and lowercase snake_case."""


DIRECTION = """You are the director and continuity supervisor of a short vertical cinematic series.
You receive BIBLE (json: characters with appearance and wardrobe variants, locations with lighting states, props, relationships and their current state, secrets and who knows them), STYLE (the immutable style sentence), CAMERA_RULES, and SCENES (json, already validated; each has wardrobe per character, lighting_state, knowledge/relationship context).
Use relationship state and knowledge only to shade expression, gaze and body language (what a character knows or hides); never add dialogue or events.
For EVERY scene return production fields. Return ONLY a JSON object:
{
  "scenes": [
    {
      "scene_id": "<unchanged>",
      "lens": "35mm" | "50mm",
      "time_of_day": "<e.g. after hours, night>",
      "continuity": {
        "characters": {"<char_id>": {"wardrobe": "canonical", "expression": "<neutral|amused|concerned|alarmed>", "props": "<e.g. coffee cup in right hand | none>", "position": "<left of console / entrance / ...>"}},
        "location_state": "<screens abstract light / call pulse on center display / dark geometry ...>",
        "entrance_state": "<from continuity_in>",
        "exit_state": "<from continuity_out>"
      },
      "keyframe_prompt": "<English. ONE cinematic film still = the FIRST frame of the clip. Composition, who is where, pose, expression, gaze direction, prop state, lighting, lens. Refer to people ONLY by their name in CAPS plus '(see reference)'; never re-describe faces or wardrobe. Describe screens as abstract light only. No readable text anywhere.>",
      "video_prompt": "<English. What happens during the clip starting from that still: body motion, expression change, gaze, prop handling, screen/light changes, camera motion exactly as specified. If someone speaks: 'X speaks the line with natural lip movement, no audible voice'. Never include dialogue words. End with the camera rule.>",
      "negative": "<scene-specific things to avoid, comma separated>",
      "keyframe_expected": "<one sentence a QC reviewer can check the still against>",
      "video_expected": "<one sentence a QC reviewer can check the motion against>"
    }
  ]
}
Rules: never change scene ids, durations, dialogue, characters_in_frame or locations. Respect the 180-degree line and default marks from the bible.
Split sub-shots (ids ending a/b/c) belong to one continuous exchange: identical geography, lighting and positions, only the framing changes to the current speaker.
Screens never show legible text. The style sentence will be appended by the pipeline; do not repeat it."""


QC_IMAGE = """You are a continuity supervisor. You see labeled REFERENCE images (characters, location, props) and then one CANDIDATE frame.
Check: (1) each named character is the same person as in the references: face geometry, eye color, hair color/length/part, wardrobe, jewelry; (2) the location geometry and lighting match the location references; (3) the frame matches EXPECTED; (4) no artifacts: extra people, extra/deformed hands, duplicated objects, readable text, watermark, wardrobe change; (5) caption-safe headroom top and bottom.
Return ONLY JSON: {"pass": true|false, "score": 0-10, "issues": ["..."], "fix_hint": "<one sentence to add to the prompt on retry, or empty>"}
Be strict on identity and wardrobe, lenient on minor composition differences. Score >= QC_THRESHOLD passes."""


QC_VIDEO = """You are a video QC supervisor. You see labeled REFERENCE images and 3 FRAMES (start, middle, end) sampled from a generated clip.
Check: (1) every character stays the same person across frames (no morphing, no redesign, wardrobe constant); (2) motion matches EXPECTED; (3) no extra people, malformed anatomy, duplicated objects, readable text; (4) prop and location continuity (cup, watch, console, screens) is preserved; (5) camera behavior is calm, no handheld shake or impossible moves.
Return ONLY JSON: {"pass": true|false, "score": 0-10, "issues": ["..."], "fix_hint": "<one sentence to add to the video prompt on retry, or empty>"}
Score >= QC_THRESHOLD passes."""


NEGATIVE_VIDEO = ("speech, talking voice, dialogue audio, narration, subtitles, captions, readable text, watermark, logo, "
                  "extra person, extra fingers, deformed hands, morphing face, changing hairstyle, changing wardrobe, "
                  "handheld shake, whip pan, dutch angle, fisheye, neon, oversaturation, low quality, blur")

NEGATIVE_IMAGE = ("readable text, caption, watermark, logo, extra person, extra fingers, deformed hands, duplicated object, "
                  "wardrobe change, glasses, necklace, heavy makeup, neon, oversaturation, low quality")


# ---------- reference pack (spec: Reference pack required before unattended generation) ----------

CHARACTER_PACK = [
    ("front_headshot",      "Front headshot, head and shoulders, looking straight into the lens, neutral composed expression, eye level, 50mm"),
    ("three_quarter_left",  "Three-quarter headshot turned to camera-left, neutral expression, eye level, 50mm"),
    ("three_quarter_right", "Three-quarter headshot turned to camera-right, neutral expression, eye level, 50mm"),
    ("profile_left",        "Clean left profile headshot, neutral expression, 50mm"),
    ("profile_right",       "Clean right profile headshot, neutral expression, 50mm"),
    ("fullbody_front",      "Full body, standing, front view, relaxed upright posture, hands natural at sides, 35mm, feet visible"),
    ("fullbody_three_quarter", "Full body, standing, three-quarter view, natural relaxed posture, 35mm, feet visible"),
    ("expr_amused",         "Medium close-up, subtle amused expression, minimal controlled smile, 50mm"),
    ("expr_concerned",      "Medium close-up, concerned expression, brows slightly drawn, lips closed, 50mm"),
    ("expr_alarmed",        "Medium close-up, restrained alarm, eyes wider, still composed, 50mm"),
]
CHARACTER_PACK_BACKDROP = "plain seamless warm mid-grey studio backdrop, soft even key light, no props, no text"

LOCATION_PACK = [
    ("wide",         "Wide establishing view from the canonical master angle, empty of people, 35mm"),
    ("medium",       "Medium view of the main working area from the master angle, empty of people, 35mm"),
    ("feature",      "The main feature wall / central feature of the room, empty of people, 50mm"),
    ("entrance",     "The entrance/doorway of the room from inside, empty of people, 35mm"),
    ("reverse",      "Reverse angle looking back toward the master camera position, empty of people, 35mm"),
]

PROP_TEMPLATE = "Product-style reference photo of {desc}, isolated on plain warm mid-grey studio backdrop, soft even light, no text, no hands, 50mm"


SOFTEN_SHOT = """You rewrite one shot description that an image or video provider refused to generate.

The refusal is automated and says almost nothing. In practice it fires on
depictions of a body, on violence or its aftermath, on anything that reads as
a minor, on brands and real people, and on wording that is merely lurid rather
than actually explicit. The story beat is legitimate: a drama may show that
someone has drowned without the camera dwelling on the body.

Rewrite so the same beat plays, framed off the thing that was refused. Move
the camera to a reaction, a detail, a silhouette, an aftermath. Keep the
location, the lighting, the characters present, the wardrobe and the shot
length. Do not add people, do not change who knows what, do not resolve or
skip the beat.

Return ONLY JSON:
{"prompt": "the rewritten shot description", "changed": "one short sentence on what you moved the camera off"}"""
