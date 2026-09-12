# MarkeVita AI Series — Complete Engineer Handoff

Prepared: 2026-09-12  
Repository: https://github.com/westccmortgage/markevita  
Status: implementation handoff; this document contains specifications and materials, not pipeline code.

## Directive to Claude / implementation engineer

You are the implementation engineer responsible for building the autonomous AI-series generation pipeline for MarkeVita.

Read this entire file before coding. It is a self-contained handoff: the specification, character and world bible, Episode 01 brief, environment-variable contract, and unresolved product questions are embedded below. You should not need another document to understand the intended system.

### Working rules

1. Work in the existing public repository: https://github.com/westccmortgage/markevita
2. Preserve the current static Netlify website and its live behavior. Do not replace or destabilize the existing site.
3. You own the implementation code, agent design, orchestration, queues, retries, state machine, provider adapters, validations, tests, deployment configuration, and operational tooling.
4. Implement against the contracts in this handoff. Do not create a second competing specification or a parallel incompatible pipeline.
5. Keep the generation backend separate from the static site. The repository root remains the website unless you deliberately add a clearly isolated service or worker directory.
6. Never commit API keys, R2 credentials, voice IDs, signing secrets, generated private URLs, or other secrets. Use environment variables only.
7. The provider choices, budgets, retry limits, clip-duration limits, storage naming, continuity rules, and episode schema below are implementation requirements unless explicitly marked provisional or unresolved.
8. You may scaffold and test the pipeline with mocked provider responses before product questions are answered.
9. Do not trigger paid generation, create production storage resources, or auto-publish media until the P0 questions in the final section have been answered by Anatoliy.
10. The repository is public. A GitHub read token is not required to clone or inspect it.
11. Before declaring the implementation complete, verify schema validation, idempotency, resumability, cost enforcement, retry limits, secret handling, media upload to R2, provenance logging, and a safe human approval gate before publication.
12. If implementation reality conflicts with this specification, document the exact conflict and propose the smallest compatible change before altering the contract.

### Expected implementation outcome

The finished system should accept an episode brief in the embedded schema, lock character/location/style references, generate bounded scene assets, synthesize dialogue, perform lip sync where needed, assemble the episode, run automated checks, store reproducible outputs and metadata in Cloudflare R2, and stop for human approval before publishing. Every paid call must be budgeted, logged, retry-bounded, and safe to resume without duplicating successful work.

---

## Embedded source 1 — Pipeline specification

# MarkeVita AI Series Pipeline — Engineer Handoff Specification

Status: implementation specification only. This repository contains no pipeline code in this change.

## 1. Objective

Build an autonomous pipeline that converts a structured series/episode brief into a finished 90–120 second episode. The orchestrator, agent code, persistence logic, retries, and deployment will be implemented by the engineer. This package defines the production contract, assets, provider choices, limits, and first episode data.

The v1 output is a mastered MP4 stored in Cloudflare R2. Automated Instagram publishing is a separate final stage and must remain disabled until the target professional account and Meta permissions are supplied.

## 2. Existing repository audit

Repository: `https://github.com/westccmortgage/markevita`

Default branch: `main`

The current project is a static MarkeVita marketing website deployed without a build step.

| Path | Current purpose |
|---|---|
| `/index.html` | Entire live site: HTML, CSS, and browser JavaScript in one file. Contains the hero video and a Netlify form. |
| `/_redirects` | Netlify catch-all rule: `/* /index.html 200`. |
| `/README.md` | Original drag-and-drop Netlify deployment and avatar-layer notes. Parts of the asset list are stale. |
| `/images/markevita-avatar-still.png` | Transparent full-body still of the existing blonde MarkeVita presenter. This is the seed reference for the provisional character `vita`. |
| `/images/markevita-lobby-background.webp` | Existing ivory-and-gold MarkeVita lobby reference. |
| `/videos/markevita_hero.mp4` | Current hero master: H.264/AAC, 1264×720, 24 fps, approximately 10.04 seconds. |
| `/videos/Vita.mp4` | Earlier presenter/video asset. |
| `/videos/markevita.mov` | Earlier high-size master asset. |
| `/images/work/` | Placeholder portfolio asset directory. |

Observed deployment:

- The README describes Netlify drag-and-drop deployment.
- The live response is served through Cloudflare and reports `Netlify Edge` as the origin cache.
- The consultation form uses Netlify Forms (`data-netlify="true"`).
- There is no `package.json`, application framework, API route, serverless function, database schema, authentication layer, queue, AI SDK, or current environment template.
- The README describes transparent multilingual WebM avatar layers, but the default branch currently uses a single full-bleed MP4 hero. The README must not be treated as proof that EN/RU/ES transparent avatar files exist.

The current website must remain operational. The autonomous pipeline should be added as a separate service/application boundary; do not turn `index.html` into the orchestrator.

## 3. Final provider choices

### 3.1 Video generation

Primary model: **Google Veo 3.1 Fast through fal.ai**.

Primary endpoint for a scene prepared from a composed keyframe:

- `fal-ai/veo3.1/fast/image-to-video`

Optional endpoint when up to three separate subject references must be passed without first compositing the shot:

- `fal-ai/veo3.1/fast/reference-to-video`

Optional endpoint when both entrance and exit composition must be locked:

- `fal-ai/veo3.1/fast/first-last-frame-to-video`

Required settings for v1:

- `aspect_ratio`: `9:16` for the pilot.
- `resolution`: `1080p` for the accepted final take; `720p` may be used for inexpensive draft evaluation.
- `duration`: only `4s`, `6s`, or `8s`.
- `generate_audio`: `false`.
- `auto_fix`: `false` for identity-critical production shots so provider-side prompt rewriting does not silently change the shot contract.
- Safety filtering remains enabled.

Why fal.ai instead of direct Google Vertex for v1:

- one API key and one asynchronous queue/webhook pattern for video, reference images, and lip-sync;
- no Google Cloud project, region, service-account, quota, or long-running-operation adapter is required in the first implementation;
- all required Veo modes are exposed under consistent endpoint IDs;
- the provider can be replaced later without changing the episode/scene contracts in this repository.

The orchestration layer must persist the exact endpoint ID and provider request ID for every take. It must copy every accepted provider output to R2 immediately; fal-hosted result URLs are transport URLs, not the system of record.

### 3.2 Reference and keyframe generation

Primary model: **Nano Banana 2 Edit through fal.ai**.

Endpoint:

- `fal-ai/nano-banana-2/edit`

Use it to compose each scene's first frame from the locked character, wardrobe, location, prop, and visual-style references. It accepts multiple reference images and supports 9:16 output. The production frame becomes the `image_url` input to Veo 3.1 Fast image-to-video.

Required settings:

- `aspect_ratio`: match the episode (`9:16` for episode 01).
- `resolution`: `1K` for drafts; `2K` only when the 1K image fails detail review.
- `num_images`: `1` per request; alternatives are separate traceable takes.
- `output_format`: `png` for locked reference masters, `jpeg` only for disposable drafts.
- `enable_web_search`: `false`.
- Reuse the canonical reference images on every scene request. A seed alone is not an identity lock.

### 3.3 Voice generation

Final choice: **ElevenLabs, not Veo native dialogue**.

Primary TTS model:

- `eleven_v3`

Each character receives one permanent `voice_id`. Voice settings, language, pronunciation decisions, and generated audio must be versioned with the take. Dialogue should be generated one speaker line at a time, then placed on the episode timeline. Do not allow Veo to improvise speech or rewrite dialogue.

Reason for disabling native Veo audio: the pipeline requires a stable named voice across scenes and future episodes. Separating voice from video also permits correction of a line without paying to regenerate the visual take.

### 3.4 Lip synchronization

Final choice: **Sync Lipsync 2.0 through fal.ai**.

Endpoint:

- `fal-ai/sync-lipsync/v2`

Default model: `lipsync-2`. Use the Pro variant only after an ordinary take fails QA and the episode budget permits it. Lip-sync is run only on an accepted visual take and accepted ElevenLabs audio, never on discarded drafts.

For two visible speakers, either split the exchange into separate shots or provide an explicit active-speaker selection. Episode 01 intentionally uses one speaking character per generated shot wherever practical.

### 3.5 Story, screenplay, direction, and QA model

The engineer is responsible for Anthropic agent code and orchestration. All structured outputs must conform to repository-owned JSON schemas/contracts rather than free-form chat history. `ANTHROPIC_API_KEY` is reserved in `.env.example`; the exact Anthropic model remains an engineer-controlled configuration value.

## 4. Cloudflare R2 storage contract

Bucket name: **`markevita-series-media`**

Public media domain: **`https://media.markevita.com`**

Storage class: R2 Standard.

The bucket is the durable system of record for reference images, generated takes, audio, intermediate masters, QA artifacts, and final episodes.

Recommended object-key layout:

```text
series/{series_id}/bible/characters/{character_id}/{version}/...
series/{series_id}/bible/locations/{location_id}/{version}/...
series/{series_id}/episodes/{episode_id}/brief.json
series/{series_id}/episodes/{episode_id}/scenes/{scene_id}/references/...
series/{series_id}/episodes/{episode_id}/scenes/{scene_id}/takes/{take_id}/...
series/{series_id}/episodes/{episode_id}/audio/{character_id}/{line_id}/...
series/{series_id}/episodes/{episode_id}/masters/{version}/...
series/{series_id}/episodes/{episode_id}/qa/{version}/...
series/{series_id}/episodes/{episode_id}/public/...
```

Access rules:

- Raw prompts, rejected takes, source audio, and QA artifacts are private.
- Provider inputs should use short-lived R2 S3 presigned GET URLs.
- Presigned URLs are bearer credentials and must not be logged in full.
- R2 presigned URLs use the S3 endpoint, not `media.markevita.com`.
- `media.markevita.com` should expose only objects promoted to the `/public/` namespace through a controlled gateway or equivalent allowlist. Do not expose the entire production bucket by default.
- Disable the `r2.dev` development URL in production.
- Copy provider results to R2 before a job is marked complete.

## 5. Pipeline stages and artifact contracts

### Stage 0 — Intake and validation

Input: episode brief JSON.

Validate before spending money:

- schema version supported;
- 12–18 scenes;
- total target duration between 90 and 120 seconds;
- every scene duration is 4, 6, or 8 seconds and never above 10 seconds;
- every referenced character and location exists in the approved bible;
- dialogue fits within the scene duration;
- projected cost does not exceed the remaining episode budget.

Output: immutable normalized brief plus content hash.

### Stage 1 — Continuity plan

For each scene, resolve the exact character version, wardrobe ID, location version, prop state, time of day, entrance state, and exit state. No provider request begins until these values are explicit.

Output: per-scene continuity manifest.

### Stage 2 — Reference-frame composition

Compose one first-frame/keyframe per scene with Nano Banana 2 Edit using the approved reference pack and the global visual-style sentence in `docs/CHARACTER_BIBLE.md`.

Output: keyframe, model ID, prompt, negative constraints, seed, provider request ID, R2 object key, and QA result.

### Stage 3 — Silent video generation

Animate the accepted keyframe with Veo 3.1 Fast. The prompt contains only visible action, expression, camera behavior, and environmental motion. Spoken dialogue remains metadata for timing; no generated speech is requested from Veo.

Output: silent scene take plus full provenance and cost.

### Stage 4 — Voice generation

Generate each approved line with its character's locked ElevenLabs voice. Preserve the original text, normalized pronunciation text, voice ID, model ID, voice settings, duration, and audio object key.

Output: one audio file per line and a scene dialogue mix.

### Stage 5 — Lip-sync

Run Sync Lipsync 2.0 only when a visible mouth speaks. Reaction shots, establishing shots, inserts, and voice-over shots skip this stage.

Output: synchronized accepted scene take.

### Stage 6 — Assembly

The engineer's compositor joins accepted scenes in numeric order, applies dialogue, room tone, approved sound effects/music, loudness normalization, transitions, captions, title/end card, and the final 9:16 encoding profile.

Deliverables:

- MP4 master;
- subtitle file;
- poster frame;
- caption text and episode metadata;
- manifest containing all source take IDs and checksums.

### Stage 7 — Automated QA

Minimum checks:

- exact duration and aspect ratio;
- no missing/black/corrupt frames;
- identity, hair, wardrobe, jewelry, and prop consistency;
- location geometry and lighting consistency;
- dialogue text and speaker attribution;
- lip-sync and audio/video duration alignment;
- no extra people, duplicate objects, malformed anatomy, or unauthorized readable text;
- safe-area compliance for captions;
- audio loudness and clipping;
- cost and retry limits.

QA produces pass/fail reasons per scene. Only the failed scene is eligible for regeneration.

### Stage 8 — Delivery and optional publishing

Always deliver the final assets to R2. Instagram publishing remains feature-flagged off until Anatoliy supplies the account, confirms automatic publishing, and the engineer verifies current Meta permissions and container/publish requirements.

## 6. Hard production limits

| Limit | Value | Enforcement |
|---|---:|---|
| Episode target duration | 90–120 seconds | Reject invalid brief before generation. |
| Scenes per episode | 12–18 | Reject invalid brief. |
| Maximum generated clip duration | 10 seconds | Hard invariant. Selected Veo endpoints use only 4/6/8 seconds. |
| Maximum scene regenerations | 2 after the initial attempt | At most 3 generated visual takes per scene. |
| Hard episode budget | **USD 50.00** | Stop new paid calls when the next estimated call would exceed the cap. |
| Draft video resolution | 720p | Allowed for evaluation only. |
| Accepted final visual take | 1080p | Required before mastering unless explicitly overridden. |

Budget behavior:

- Track actual provider cost at request/take level.
- Reserve estimated cost before submission; reconcile after result.
- A failed provider request that is billed still counts toward the episode budget.
- Lip-sync runs only after visual acceptance to avoid unnecessary cost.
- Reaching the hard cap sets the episode to `needs_budget_override`; it must not silently continue.
- No single agent may raise limits. Overrides require an explicit external decision recorded with actor, time, old value, new value, and reason.

At currently published fal.ai rates, Veo 3.1 Fast without audio is listed at $0.10 per generated second for 720p/1080p. A 108-second first pass is therefore approximately $10.80 before retries. The $50 cap leaves room for bounded scene retries, reference frames, ElevenLabs speech, lip-sync, language-model use, and storage while still preventing runaway generation. Prices must be read from configuration or a maintained pricing table because provider pricing can change.

## 7. Job state and idempotency requirements

Suggested terminal/non-terminal vocabulary for the engineer's implementation:

`draft` → `validated` → `references_pending` → `video_pending` → `voice_pending` → `lipsync_pending` → `assembly_pending` → `qa_pending` → `complete`

Exceptional states:

`blocked_open_question`, `failed_provider`, `failed_qa`, `needs_budget_override`, `cancelled`

Every paid request needs an idempotency key derived from episode, scene, stage, and take number. Webhook redelivery must update the same job rather than create a second billable request.

## 8. Provenance requirements

Persist for every generated asset:

- provider and exact endpoint/model version;
- provider request ID;
- complete prompt and negative constraints;
- input asset checksums and versions;
- generation parameters and seed when returned;
- timestamps, attempt number, estimated cost, actual cost;
- moderation/safety outcome;
- QA verdict and reasons;
- R2 object key and checksum;
- parent/derived asset relationships.

Do not store API keys, bearer tokens, presigned URLs, or access tokens in provenance.

## 9. Sources checked on 2026-09-12

- fal.ai Veo 3.1 Fast API and supported text/image/first-last-frame modes: https://fal.ai/docs/model-api-reference/video-generation-api/veo3.1-fast
- fal.ai Veo 3.1 endpoints and current per-second pricing: https://fal.ai/models/fal-ai/veo3.1
- fal.ai Nano Banana 2 API: https://fal.ai/docs/model-api-reference/image-generation-api/nano-banana-2
- fal.ai Sync Lipsync 2.0 API: https://fal.ai/models/fal-ai/sync-lipsync/v2/api
- ElevenLabs model selection: https://elevenlabs.io/docs/overview/models
- ElevenLabs TTS API: https://elevenlabs.io/docs/api-reference/text-to-speech/convert
- Cloudflare R2 presigned URLs: https://developers.cloudflare.com/r2/api/s3/presigned-urls/
- Cloudflare R2 public/custom domains: https://developers.cloudflare.com/r2/buckets/public-buckets/
- Cloudflare R2 pricing: https://developers.cloudflare.com/r2/pricing/

---

## Embedded source 2 — Character and world bible

# MarkeVita Series — Provisional Character and World Bible

Status: production-ready draft for episode 01, pending the approvals listed in `docs/OPEN_QUESTIONS.md`.

Series working title: **VITA: After Hours**

Core premise: after MarkeVita closes for the night, its composed AI concierge Vita and human creative director Leo solve impossible digital briefs while an unknown presence begins entering the studio's production system.

## Canonical global prompt sentence

Append this exact sentence to every reference-image and video prompt:

> Premium cinematic photorealism in a warm ivory-and-champagne-gold MARKEVITA world, soft architectural practical lighting, restrained contrast, natural skin texture and physically plausible motion, elegant contemporary wardrobe, 35mm or 50mm lenses with shallow controlled depth of field, deliberate stabilized camera movement, consistent faces, voices, props and spatial layout, vertical 9:16 composition with clean upper and lower caption-safe areas, no visual-style drift, no character redesign, no wardrobe change, no extra people, no malformed hands, no duplicated objects and no readable on-screen text unless explicitly requested.

This sentence is immutable for episode 01. A style change requires a new bible version; it must not be introduced ad hoc in a scene prompt.

## Character: Vita

Character ID: `vita`

Role: MarkeVita's AI concierge and the central protagonist.

Apparent age: 32.

Physical appearance:

- Height: approximately 168 cm / 5'6".
- Build: slim, proportionate, elegant posture; shoulders relaxed and upright.
- Skin: fair with a warm neutral undertone; natural texture; no heavy contouring.
- Face: softly oval with a tapered jaw, balanced forehead, and gently defined cheekbones.
- Eyes: blue-green, almond shaped, direct but warm gaze.
- Brows: medium-thickness dark blonde brows with a soft natural arch.
- Nose: straight, narrow bridge, softly rounded tip.
- Lips: medium-full, muted rose tone; controlled smile rather than exaggerated expressions.
- Hair: long honey-blonde hair reaching the upper chest, side part slightly left of center, large polished waves, consistent volume, no bangs.
- Hands: natural proportions, short neutral manicure.

Canonical wardrobe for episode 01:

- Warm ivory single-breasted tailored blazer with narrow lapels and one light button.
- Matching ankle-length tailored trousers.
- Ivory V-neck silk camisole with no visible logo.
- Nude-beige closed-toe pumps.
- Small round gold stud earrings.
- One thin gold ring on the right hand.
- No necklace, watch, visible belt, glasses, handbag, or wardrobe pattern.

Voice lock:

- Permanent ElevenLabs `voice_id` to be selected once and never changed within the season.
- Language for episode 01: American English.
- Vocal age: early thirties.
- Register: warm low mezzo-soprano; clear, calm, and confident.
- Delivery: approximately 145–155 words per minute, precise consonants, short intentional pauses, dry humor without sarcasm.
- Accent: neutral educated American; no regional caricature.
- Default emotion: composed attentiveness.
- Avoid breathy influencer delivery, sing-song intonation, shouting, vocal fry, or a different accent between lines.

Personality and behavior:

- Calm when everyone else is urgent.
- Solves contradictions by identifying the underlying intention.
- Never boasts about being AI.
- Humor is concise and observational.
- Makes minimal, precise gestures; often pauses before the decisive sentence.
- Looks directly at the person speaking, not randomly toward the camera.
- Her composure breaks only when the unknown system presence appears.

Immutable continuity rules:

- Do not change face geometry, eye color, apparent age, hair color, hair length, part, or wave pattern.
- Do not change the ivory suit, camisole, shoes, earrings, or ring during episode 01.
- Do not add glasses, a necklace, a watch, bright lipstick, dark eye makeup, tattoos, or nail colors.
- Do not make her taller than Leo.
- Do not give her exaggerated smiles, broad comedy gestures, or frantic movement.
- The existing `/images/markevita-avatar-still.png` is the seed identity reference. It must be supplemented by an approved front, three-quarter, profile, full-body, and expression reference pack before unattended production.

## Character: Leo Mercer

Character ID: `leo_mercer`

Role: MarkeVita's human creative director; Vita's skeptical but increasingly impressed counterpart.

Age: 38.

Physical appearance:

- Height: approximately 183 cm / 6'0".
- Build: lean, fit but not muscular; slightly forward working posture that straightens when challenged.
- Skin: light olive with neutral undertone and natural texture.
- Face: rectangular-oval, defined jaw without extreme sharpness, medium cheekbones.
- Eyes: dark brown, deep set, expressive brows.
- Hair: dark brown, thick, short on the sides, softly wavy textured top, no hard fade.
- Facial hair: consistent two-day dark stubble; never clean-shaven and never a full beard.
- Distinguishing feature: faint narrow scar through the outer third of the left eyebrow.
- Hands: natural proportions; no rings.

Canonical wardrobe for episode 01:

- Charcoal unstructured overshirt-jacket, worn open.
- Matte black crew-neck T-shirt with no print or logo.
- Dark charcoal tailored trousers.
- Minimal white leather sneakers without visible branding.
- Brushed-steel watch with black leather strap on left wrist.
- Carries one plain matte-black reusable coffee cup in scenes 02–08; the cup is absent after he places it on the control-room console in scene 08.

Voice lock:

- Permanent ElevenLabs `voice_id` to be selected once and never changed within the season.
- Language for episode 01: American English.
- Vocal age: late thirties.
- Register: warm restrained baritone.
- Delivery: approximately 160–170 words per minute, intelligent and slightly impatient, slowing when he realizes Vita is correct.
- Accent: neutral American with no strong regional markers.
- Default emotion: controlled urgency.
- Avoid announcer voice, comedy caricature, growling, shouting, or exaggerated cynicism.

Personality and behavior:

- Talented, practical, deadline-driven, and allergic to vague client language.
- Uses humor as pressure relief.
- Initially treats Vita as a tool but increasingly addresses her as a collaborator.
- Gestures more than Vita but remains physically believable.
- Never becomes incompetent; his value is judgment, taste, and knowledge of the client.

Immutable continuity rules:

- Do not change face, hair texture, stubble length, eyebrow scar, height, or body type.
- Do not change wardrobe during episode 01.
- Watch remains on the left wrist.
- Coffee cup continuity must follow the scene rule above.
- Do not add glasses, jewelry, tattoos, tie, dress shirt, bright shoes, or visible logos.

## Supporting voice: The Client

Character ID: `client_voice`

Role: unseen owner whose contradictory late-night brief starts the pilot story.

Apparent age: 48–55.

Voice lock:

- Male American voice, medium baritone.
- Successful-business confidence mixed with deadline anxiety.
- Approximately 175 words per minute.
- Clean phone-call processing may be added in the mix, but the underlying voice ID remains constant.
- Never appears visually in episode 01.

Immutable rules:

- The client is voice-only in episode 01.
- No face, silhouette, avatar, portrait, or identifying company logo is generated.
- Do not imitate a real public figure or a real client.

## Location: MarkeVita Lobby

Location ID: `markevita_lobby`

Canonical reference: `/images/markevita-lobby-background.webp`.

Description:

- Large contemporary luxury lobby in warm ivory limestone and pale polished stone.
- Recessed ceiling coves produce soft warm light; no visible daylight during the pilot's after-hours setting.
- Tall sheer curtains on the left wall.
- Main feature wall on the right has a dimensional champagne-gold `M` monogram and `MARKEVITA` wordmark.
- Sparse gold wall sconces and one thin-branch arrangement in a gold vessel on a pale pedestal.
- Clean reflective floor, no reception desk in the canonical wide background.
- The geometry, logo placement, curtains, sconces, pedestal, and plant must not move between shots.

After-hours state:

- Exterior light is deep blue night through the curtains.
- Interior practical lighting remains warm and premium, never dark horror lighting.
- Lobby doors are off-frame until the final shot, where they may be represented by a consistent glass entrance aligned with the left side of the room.

## Location: Production Control Room

Location ID: `production_control_room`

Description:

- Private room directly behind the lobby, approximately 7 m by 5 m.
- Dark charcoal glass walls with warm champagne-metal trim.
- One central matte-charcoal standing console with rounded corners.
- Three large wall displays in a horizontal arrangement; displays show abstract blocks of light and image compositions, never legible interface text.
- One narrow warm ceiling light track and soft console edge lighting.
- Entrance door is behind Leo's right shoulder in the canonical master angle.
- Vita's default mark is left of the console; Leo's default mark is right of the console.
- No additional staff, chairs, windows, plants, or visible cables.

Immutable continuity rules:

- Console, screens, doorway, and character marks remain spatially consistent.
- Screen content may change, but screen dimensions and positions do not.
- The coffee cup is placed on the front-right corner of the console in scene 08 and remains there through scene 13.
- Lighting stays warm and controlled until scene 13, when one brief cool flicker is allowed.

## Camera language

- Master lens family: 35mm for establishing/two-shots; 50mm for medium and close shots.
- Eye line and screen direction must match across cuts.
- Vita generally occupies the visually stable side of the composition; Leo introduces movement.
- Camera movement is limited to slow push-in, slow lateral track, restrained pan, or locked tripod.
- No handheld shake, drone view, fisheye, extreme wide-angle distortion, crash zoom, whip pan, Dutch angle, or floating impossible camera.
- Do not cross the 180-degree line inside the control room unless an explicit new establishing shot resets geography.
- Close-ups must preserve enough headroom and lower safe area for captions.

## Color and lighting lock

- MarkeVita ivory: warm off-white, never clinical white.
- Champagne gold: muted metallic accent, never yellow chrome.
- Charcoal: neutral deep gray, never blue-black.
- Skin tones remain natural and consistent.
- Night exterior accents may be restrained deep blue only.
- No neon palette, oversaturation, orange-and-teal blockbuster grade, crushed blacks, heavy bloom, or beauty-filter skin.

## Reference pack required before unattended generation

The engineer must treat the following as required immutable assets, not optional inspiration:

1. Vita front headshot.
2. Vita left and right three-quarter headshots.
3. Vita left and right profiles.
4. Vita full-body front and three-quarter views in canonical wardrobe.
5. Vita neutral, amused, concerned, and alarmed expressions.
6. Leo equivalent identity, full-body, wardrobe, and expression set.
7. Lobby wide, medium, feature-wall, entrance, and reverse angles.
8. Control-room wide, Vita medium, Leo medium, console insert, and reverse angles.
9. Coffee cup and watch prop references.

Every file must have an asset ID, version, approval status, checksum, and R2 object key.

---

## Embedded source 3 — Episode 01 brief

```json
{
  "schema_version": "1.0",
  "series_id": "vita_after_hours",
  "series_title": "VITA: After Hours",
  "episode_id": "ep01",
  "episode_number": 1,
  "title": "The Impossible Brief",
  "status": "provisional_pending_anatoliy_approval",
  "logline": "Six minutes after MarkeVita closes, Vita and Leo receive a launch brief containing five impossible contradictions, solve it before morning, and discover that an unknown presence has entered their production system.",
  "dialogue_language": "en-US",
  "format": {
    "aspect_ratio": "9:16",
    "target_width": 1080,
    "target_height": 1920,
    "target_duration_seconds": 108,
    "minimum_duration_seconds": 90,
    "maximum_duration_seconds": 120,
    "caption_safe_areas_required": true
  },
  "production_limits": {
    "maximum_episode_budget_usd": 50,
    "maximum_regenerations_per_scene": 2,
    "maximum_clip_duration_seconds": 10,
    "allowed_generated_clip_durations_seconds": [
      4,
      6,
      8
    ]
  },
  "characters": [
    "vita",
    "leo_mercer",
    "client_voice"
  ],
  "locations": [
    "markevita_lobby",
    "production_control_room"
  ],
  "scenes": [
    {
      "scene_id": "sc01",
      "sequence": 1,
      "duration_seconds": 6,
      "location": "markevita_lobby",
      "characters_in_frame": [
        "vita"
      ],
      "action": "After-hours lobby. Vita stands alone in her canonical position, finishes a small welcoming gesture, notices that the room has gone quiet, and looks toward an off-screen alert.",
      "dialogue": [
        {
          "speaker": "vita",
          "text": "MarkeVita closed six minutes ago.",
          "delivery": "calm, faintly amused"
        }
      ],
      "shot_type": "medium shot, 50mm",
      "camera_motion": "locked frame with a very slow push-in",
      "continuity_out": "Vita turns her gaze toward the lobby entrance."
    },
    {
      "scene_id": "sc02",
      "sequence": 2,
      "duration_seconds": 8,
      "location": "markevita_lobby",
      "characters_in_frame": [
        "vita",
        "leo_mercer"
      ],
      "action": "Leo enters quickly from the left carrying the matte-black coffee cup. Vita remains still and watches him approach.",
      "dialogue": [
        {
          "speaker": "leo_mercer",
          "text": "New client. Launch at nine. They sent no logo, no copy, no assets.",
          "delivery": "controlled urgency"
        }
      ],
      "shot_type": "two-shot, 35mm",
      "camera_motion": "slow lateral track following Leo, settling into the two-shot",
      "continuity_in": "Vita is looking toward the entrance.",
      "continuity_out": "Leo stops one meter from Vita with coffee cup in right hand."
    },
    {
      "scene_id": "sc03",
      "sequence": 3,
      "duration_seconds": 8,
      "location": "markevita_lobby",
      "characters_in_frame": [
        "vita"
      ],
      "action": "Vita gives Leo one measured look, then lets a minimal smile appear.",
      "dialogue": [
        {
          "speaker": "vita",
          "text": "So, a miracle with brand guidelines.",
          "delivery": "dry, precise humor"
        }
      ],
      "shot_type": "medium close-up, 50mm",
      "camera_motion": "locked tripod",
      "continuity_in": "Vita faces Leo off-camera right.",
      "continuity_out": "Her smile disappears as she waits for the actual brief."
    },
    {
      "scene_id": "sc04",
      "sequence": 4,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "vita",
        "leo_mercer"
      ],
      "action": "First establishing view of the production room. Abstract light appears on the three displays as Vita and Leo take their fixed positions at the console.",
      "dialogue": [
        {
          "speaker": "leo_mercer",
          "text": "Can your system build a campaign from one sentence?",
          "delivery": "skeptical but serious"
        }
      ],
      "shot_type": "wide establishing two-shot, 35mm",
      "camera_motion": "slow stabilized push toward the console",
      "continuity_in": "Leo still carries the coffee cup.",
      "continuity_out": "Vita stands left of console; Leo stands right."
    },
    {
      "scene_id": "sc05",
      "sequence": 5,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "vita"
      ],
      "action": "Vita studies the abstract display without touching it, then shifts her eyes toward Leo.",
      "dialogue": [
        {
          "speaker": "vita",
          "text": "Yes. The dangerous part is which sentence.",
          "delivery": "calm warning"
        }
      ],
      "shot_type": "medium shot, 50mm",
      "camera_motion": "subtle push-in",
      "continuity_in": "Vita remains on the left side of the console.",
      "continuity_out": "A call indicator appears only as an abstract pulse, with no text."
    },
    {
      "scene_id": "sc06",
      "sequence": 6,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "vita",
        "leo_mercer"
      ],
      "action": "Vita and Leo listen to the unseen client's recorded message. Neither speaks; their reactions remain restrained.",
      "dialogue": [
        {
          "speaker": "client_voice",
          "text": "Make us established, disruptive, timeless, young—and don't change anything.",
          "delivery": "confident, rushed phone message",
          "voice_over": true
        }
      ],
      "shot_type": "balanced two-shot, 35mm",
      "camera_motion": "locked tripod",
      "continuity_in": "Call pulse remains on the center display.",
      "continuity_out": "Leo slowly turns his head toward Vita."
    },
    {
      "scene_id": "sc07",
      "sequence": 7,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "leo_mercer"
      ],
      "action": "Leo counts the contradictions silently on the fingers of his free left hand, then lowers it.",
      "dialogue": [
        {
          "speaker": "leo_mercer",
          "text": "Five contradictions. New record.",
          "delivery": "deadpan disbelief"
        }
      ],
      "shot_type": "medium close-up, 50mm",
      "camera_motion": "locked tripod",
      "continuity_in": "Coffee remains in Leo's right hand; watch remains on left wrist.",
      "continuity_out": "Leo looks from Vita to the displays."
    },
    {
      "scene_id": "sc08",
      "sequence": 8,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "vita",
        "leo_mercer"
      ],
      "action": "Leo places the coffee cup on the front-right corner of the console. Vita raises one hand slightly and the displays reorganize into clean abstract compositions.",
      "dialogue": [
        {
          "speaker": "vita",
          "text": "Separate the intentions. Keep the trust. Remove the panic.",
          "delivery": "decisive, controlled"
        }
      ],
      "shot_type": "medium two-shot, 35mm",
      "camera_motion": "slow pan from the cup to Vita",
      "continuity_in": "Coffee cup begins in Leo's right hand.",
      "continuity_out": "Coffee cup is fixed on the console's front-right corner."
    },
    {
      "scene_id": "sc09",
      "sequence": 9,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "vita"
      ],
      "action": "Several overly bright abstract campaign concepts flash across the displays. Vita makes one small dismissive gesture; the noisy concepts vanish and a restrained composition replaces them.",
      "dialogue": [],
      "shot_type": "over-shoulder medium shot from behind Vita, 50mm",
      "camera_motion": "slow controlled push toward the displays",
      "continuity_in": "Coffee cup remains visible on far right of console.",
      "continuity_out": "One clean ivory, gold, and charcoal concept remains."
    },
    {
      "scene_id": "sc10",
      "sequence": 10,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "vita",
        "leo_mercer"
      ],
      "action": "Leo studies the restrained concept, then glances at Vita. Vita keeps her attention on the work.",
      "dialogue": [
        {
          "speaker": "leo_mercer",
          "text": "You killed the fireworks.",
          "delivery": "testing her reasoning"
        },
        {
          "speaker": "vita",
          "text": "They sell fireworks.",
          "delivery": "quiet certainty"
        }
      ],
      "shot_type": "two-shot with shallow focus shift, 50mm",
      "camera_motion": "locked camera; rack focus from Leo to Vita",
      "continuity_in": "Clean concept remains on displays; cup remains on console.",
      "continuity_out": "Leo accepts the logic with a small nod."
    },
    {
      "scene_id": "sc11",
      "sequence": 11,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "vita",
        "leo_mercer"
      ],
      "action": "The final concept fills the center display as abstract shapes, with no readable text. Vita and Leo listen to the client's reply.",
      "dialogue": [
        {
          "speaker": "client_voice",
          "text": "That's exactly what I meant.",
          "delivery": "immediate satisfaction over phone",
          "voice_over": true
        }
      ],
      "shot_type": "wide two-shot, 35mm",
      "camera_motion": "very slow pull back",
      "continuity_in": "Final concept and coffee cup remain in place.",
      "continuity_out": "Call pulse disappears."
    },
    {
      "scene_id": "sc12",
      "sequence": 12,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "vita",
        "leo_mercer"
      ],
      "action": "Leo leans lightly against his side of the console. Vita turns from the display toward him.",
      "dialogue": [
        {
          "speaker": "leo_mercer",
          "text": "He never meant it.",
          "delivery": "quiet realization"
        },
        {
          "speaker": "vita",
          "text": "Clients recognize decisions after they see them.",
          "delivery": "warm, matter-of-fact"
        }
      ],
      "shot_type": "medium two-shot, 50mm",
      "camera_motion": "subtle lateral slide toward Vita",
      "continuity_in": "Coffee cup remains on front-right console corner.",
      "continuity_out": "Both briefly relax before the system changes."
    },
    {
      "scene_id": "sc13",
      "sequence": 13,
      "duration_seconds": 8,
      "location": "production_control_room",
      "characters_in_frame": [
        "vita",
        "leo_mercer"
      ],
      "action": "The warm room lights flicker cool once. An unfamiliar dark geometric presence appears across the abstract displays. Vita becomes visibly concerned for the first time; Leo straightens.",
      "dialogue": [
        {
          "speaker": "vita",
          "text": "Someone entered the production system.",
          "delivery": "low, controlled alarm"
        }
      ],
      "shot_type": "slow tightening two-shot, 35mm",
      "camera_motion": "measured push-in, no shake",
      "continuity_in": "Coffee cup remains on console; final concept is replaced by dark geometry.",
      "continuity_out": "Vita and Leo look toward the lobby."
    },
    {
      "scene_id": "sc14",
      "sequence": 14,
      "duration_seconds": 6,
      "location": "markevita_lobby",
      "characters_in_frame": [
        "vita"
      ],
      "action": "Back in the lobby, the glass entrance locks with a quiet mechanical movement. Vita stands in foreground profile, then turns her eyes toward camera without smiling.",
      "dialogue": [
        {
          "speaker": "vita",
          "text": "And it wasn't the client.",
          "delivery": "quiet cliffhanger"
        }
      ],
      "shot_type": "medium close-up profile to three-quarter, 50mm",
      "camera_motion": "locked frame with a final minimal push-in",
      "continuity_in": "Vita has moved from the control room to the lobby; wardrobe remains unchanged.",
      "continuity_out": "Cut to black immediately after the line."
    }
  ]
}
```

---

## Embedded source 4 — Environment variable names

```dotenv
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=

FAL_KEY=
FAL_VIDEO_MODEL=
FAL_IMAGE_MODEL=
FAL_LIPSYNC_MODEL=
FAL_WEBHOOK_SECRET=

ELEVENLABS_API_KEY=
ELEVENLABS_MODEL_ID=
ELEVENLABS_VOICE_ID_VITA=
ELEVENLABS_VOICE_ID_LEO_MERCER=
ELEVENLABS_VOICE_ID_CLIENT=

SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=

R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=
R2_S3_ENDPOINT=
R2_PUBLIC_BASE_URL=

APP_BASE_URL=
PIPELINE_WEBHOOK_BASE_URL=
PIPELINE_WEBHOOK_SECRET=

META_APP_ID=
META_APP_SECRET=
INSTAGRAM_USER_ID=
INSTAGRAM_ACCESS_TOKEN=

MAX_EPISODE_BUDGET_USD=
MAX_SCENE_REGENERATIONS=
MAX_CLIP_SECONDS=
```

---

## Embedded source 5 — Open questions for Anatoliy

# Открытые вопросы к Анатолию

Ниже только те решения, которые нельзя безопасно принять за владельца проекта. Они не блокируют чтение спецификации, но блокируют полностью автоматический production-run или публикацию.

## P0 — до первой платной генерации

1. **Утверждаем ли концепцию пилота?** Рабочее название сериала — `VITA: After Hours`, первая серия — `The Impossible Brief`. Если нет, нужна новая тема и жанр до создания reference pack.

2. **Существующая блондинка становится каноническим персонажем Vita?** Seed-файл уже находится в `/images/markevita-avatar-still.png`. Нужно подтвердить, что MarkeVita имеет право использовать её внешность коммерчески и создавать новые изображения/видео на её основе.

3. **Можно ли использовать существующий голос из видео?** Если это голос реального человека или лицензированного аватара, нужны источник и условия лицензии. Без подтверждения создаём новый синтетический голос ElevenLabs, не имитирующий реального человека.

4. **Утверждаем ли второго героя Leo Mercer?** Нужно подтвердить имя, возраст, внешность и роль либо дать замену. Его визуальных reference-файлов сейчас нет.

5. **Основной язык сериала:** английский, русский или отдельные версии на нескольких языках? `ep01` сейчас подготовлен на `en-US`.

6. **Основной формат:** подтверждаем вертикальный `9:16` для Instagram или нужен дополнительный master `16:9`? Пилот сейчас спроектирован как `9:16`.

7. **Утверждаем жёсткий бюджет $50 на серию и максимум две перегенерации каждой сцены?** Без отдельного подтверждения система не должна превышать эти пределы.

## P1 — до автоматического запуска сезона

8. **Нужно ли показывать бренд MARKEVITA внутри сюжета постоянно?** В пилоте логотип присутствует в существующем лобби, но не добавляется поверх каждого кадра.

9. **Допустимые темы и возрастной рейтинг:** только business comedy / technology mystery без насилия, политики, религии, секса и profanity — или разрешён более широкий диапазон?

10. **Музыка:** полностью оригинальная AI-музыка, лицензированная библиотека или сериал без музыкальной темы? Нужны правила коммерческого использования и хранения подтверждений лицензии.

11. **Субтитры:** burned-in captions обязательны или достаточно отдельного `.srt`? Нужны утверждённые шрифт, размер, цвет и положение.

12. **Частота и объём сезона:** сколько серий в первом сезоне и по какому расписанию они должны производиться?

13. **Нужен ли единственный финальный вариант или система должна сохранять две версии финала для выбора/тестирования?** Второй финальный master увеличивает стоимость и должен иметь отдельный бюджет.

## P2 — инфраструктура и публикация

14. **Cloudflare:** в каком Cloudflare account/zone создавать R2 bucket `markevita-series-media` и домен `media.markevita.com`? Сам bucket и DNS в рамках этой документационной задачи не создавались.

15. **Политика доступа к R2:** подтверждаем, что исходники и rejected takes остаются private, а `media.markevita.com` выдаёт только специально опубликованный namespace `/public/`?

16. **Netlify:** существующий `markevita.com` пока остаётся на Netlify за Cloudflare или его переносится отдельно? Спецификация не меняет текущий live-site.

17. **Instagram:** какой Professional account должен получать готовые Reels? Нужны точный Instagram User ID, Meta app, разрешения и решение: автоматическая публикация без просмотра или только подготовка scheduled draft.

18. **Порог автономности:** после утверждения bible система публикует всё, что прошло автоматический QA, или первый сезон всё равно требует одного финального approval перед Instagram?

19. **Хранение:** сколько времени хранить rejected takes и intermediate masters — 30, 90 или 365 дней? Final masters и утверждённые reference assets предполагаются бессрочными.

20. **Уведомления:** куда отправлять только критические статусы `failed`, `needs_budget_override` и `blocked_open_question` — email, SMS, Slack или другое место?
