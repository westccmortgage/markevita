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
