# Which provider for which job (decided 2026-09-14)

Facts below come from fal's own OpenAPI schemas and pricing pages, read
2026-09-14. Supersedes the first version of this note, which contained an
error — see "Correction" at the end.

## Summary

| Stage | Provider | Alternative considered | Verdict |
|---|---|---|---|
| `references` | `fal-ai/nano-banana-2/edit` | Grok Imagine Image 2.0 | **Keep** — 3-image limit |
| `keyframes` | `fal-ai/nano-banana-2/edit` | Grok Imagine Image 2.0 | **Keep** — 3-image limit |
| `video` | `fal-ai/veo3.1/fast/image-to-video` | Grok Imagine Video 1.5 | **Keep** — audio, negatives, price |
| `voice` | ElevenLabs `eleven_v3` | Grok native audio | **Keep** — voices must stay locked |
| `lipsync` | `fal-ai/sync-lipsync/v2` | — | Keep |

Grok has no place inside the episode pipeline. It has two legitimate places
outside it — see "Where Grok does fit".

## Why not Grok for keyframes or references

`pipeline/serial/pipeline.py:_scene_refs` composes each keyframe from **up to
14 reference images**: per character in frame a front headshot, a
three-quarter, a full body in that scene's wardrobe variant and often an
expression; then the location in two framings; then each prop; then the
previous keyframe so the set, lighting and positions carry over.

`xai/grok-imagine-image/v2.0/edit` accepts **a maximum of 3**
(`image_urls`, "A maximum of 3 images are supported").

A two-character scene needs eleven or more. This is a hard API limit, not a
quality judgement: identity locking and cross-shot continuity cannot be
expressed in three images. Grok's image models also have no negative prompt.

## Why not Grok for the episode's video

`xai/grok-imagine-video/v1.5/image-to-video` **does** reach 1080p — the base
endpoint does not, and the first version of this note wrongly generalised
from the base endpoint. Two objections survive that correction, and a third
appears:

1. **Audio cannot be disabled.** There is no `audio` or `generate_audio`
   property in the schema. The pipeline needs silent video so ElevenLabs can
   speak each character's locked `voice_id` and Sync Lipsync can apply it.
   Model-generated audio cannot hold a voice across episodes. Stripping the
   track with ffmpeg works but pays for discarded output.
2. **No negative prompt.** `prompts.NEGATIVE_VIDEO` plus per-scene negatives
   are part of how identity and composition drift is contained.
3. **Price.** At the 1080p our masters require, Grok is **$0.25/s** against
   Veo 3.1 Fast at **$0.10/s** silent — two and a half times, for the same
   resolution.

Also worth noting: v1.5 image-to-video exposes no `aspect_ratio` at all, so
9:16 would depend entirely on the input frame's shape. Unverified.

## Where Grok does fit

Both are outside the episode pipeline and neither touches the bible.

**Promotional cutdowns.** A vertical teaser does not need a 1080p master and
does not need a locked voice, because titles or music sit over it. Grok base
at $0.05/s (480p) or $0.07/s (720p) is cheaper than Veo, and integer
durations of 1–15 s suit a 9-second teaser that Veo's 4/6/8 cannot express.

**Cheap concept exploration.** `xai/grok-imagine-image` is **$0.02** an image
against nano-banana-2's $0.08 — four times cheaper for throwing twenty looks
at a wall to decide how a location or an outfit should read. Whatever wins is
then *written into the bible as text* and generated properly by
nano-banana-2. Grok output must never enter a reference pack, or identity
stops being reproducible.

Neither is built. Both would need an adapter, a pricing entry and a preflight
allowance, exactly as the main path does.

## Veo modes we are not using

The engine only calls `image-to-video`. Two other Veo 3.1 Fast modes exist and
may be worth it later:

- `fal-ai/veo3.1/fast/reference-to-video` — up to three subject references
  without compositing a keyframe first. Fewer references than our keyframe
  carries, so it is a simplification, not an upgrade.
- `fal-ai/veo3.1/fast/first-last-frame-to-video` — locks both the entrance and
  the exit frame. Directly relevant to the standing complaint about
  transitions, since `continuity_out` of one scene is `continuity_in` of the
  next.

## The duration floor, which no provider choice fixes

Veo offers 4, 6 or 8 seconds. A two-second line in a four-second clip leaves
two seconds of slack, which reads as sluggish pacing.

The fix belongs in `assemble` — cut each clip to the measured line duration
plus lead-in and gap — not in a model swap. Not implemented.

## What a provider swap would never have fixed

The standing creative complaints are staging, transitions and unclear speech:

- staging and composition — the keyframe and its prompt;
- transitions — the `assemble` stage and `continuity_in` / `continuity_out`;
- speech — ElevenLabs delivery and Sync Lipsync.

## Correction

The first version of this note (commit 6bbe1ce) stated that Grok's
image-to-video "caps at 720p". That was read from
`xai/grok-imagine-video/image-to-video`; the **v1.5** endpoint adds 1080p.
The conclusion is unchanged, but it now rests on audio, negative prompts and
price rather than on resolution.

Revisit if the master format drops below 1080p, if locked per-character
voices stop being a requirement, or if Grok exposes a silent-output flag.
