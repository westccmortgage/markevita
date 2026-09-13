# First clip: The Wrong Bride

The first live output is an eight-second preview for **Island of No Witnesses**. The preview uses the existing studio, administrator authentication, Supabase records, fal.ai account and Cloudflare R2 bucket. Full-episode production remains mock-only in this release.

## Creative brief

At a private Caribbean island villa before her wedding, Nora, 28, sees her fiance Adrian Vale, 42, a fictional rising politician, kissing her sister Maya, 26. Nora whispers:

> Tomorrow he marries me. Why is he kissing my sister?

One continuous shot begins with the kiss and brings Nora's reaction into focus. The image is vertical 9:16, 1080p, with native English dialogue and ambient sound. The preview establishes a proposed look; it does not approve permanent character identities or voice assignments for a season.

The reviewed source is `studio/previews/first_clip.json`. Any change produces a different specification digest and requires fresh approval.

## Provider and price

- Endpoint: `fal-ai/veo3.1/fast` (text to video).
- Duration: `8s`; resolution: `1080p`; aspect ratio: `9:16`.
- `generate_audio=true`; `auto_fix=false`.
- Price checked September 13, 2026: $0.15 per generated second with audio at 720p/1080p. One eight-second generation is **$1.20**.
- One submission, no automatic regeneration, no social publication. The recorded generation charge is an estimate based on the published tariff, not a reconciled provider invoice. Existing hosting/storage usage is separate.

Sources: [model pricing](https://fal.ai/models/fal-ai/veo3.1/fast), [input/output schema](https://fal.ai/models/fal-ai/veo3.1/fast/api), [queue lifecycle](https://fal.ai/docs/documentation/model-apis/inference/queue).

## Operator flow

1. Open `/studio/clip-preview` while signed in as a studio administrator.
2. Review the exact scene and $1.20 generation limit. Preparation and viewing this page make no paid provider request.
3. Before approval, keep `STUDIO_ALLOW_PAID=false` and `PIPELINE_ALLOW_PAID=false`. For the approved preview both variables must be `true` in Render; an API key alone cannot enable a submission.
4. Approve the displayed specification and one generation. The backend verifies the digest, exact finite spending limit, administrator session and request origin/CSRF protection.
5. Check status to retrieve the existing fal request. A lost response or uncertain submit is blocked for manual reconciliation instead of automatically being sent again.
6. Once generated, the video is checked for duration, vertical frame and audio, then archived privately in R2. Watch/download uses an authenticated studio endpoint that creates a fresh, short-lived R2 link.

The creative prompt, provider request identifier, approval, progress and output references are durable Supabase records. A temporary download may be lost on Render restart; it can be downloaded again from the same completed request. Polling and archive recovery never create another generation.

## Boundaries

This milestone verifies one real video request and delivery through the studio. It does not enable full-episode production, paid Anthropic direction/QC, ElevenLabs voice casting, character reference generation, lipsync, or Instagram publishing. Those require their own integration and budget checks. Native dialogue in this clip is supplied by the video model.

The general episode runner retains its existing restriction on live jobs. Keep one Render application process for this release. No database migration or public-site edit is required for the preview.
