# Video model: Veo 3.1 Fast (decided 2026-09-14)

The question "should short scenes use Grok Imagine instead?" has now been
raised twice. This records the comparison so it is not researched a third
time. Figures come from fal's own OpenAPI schema and pricing pages, read
2026-09-14.

## Decision

Keep `fal-ai/veo3.1/fast/image-to-video` for all scenes, short ones included.

`studio/app/preflight.py` enforces this: any other `FAL_VIDEO_MODEL` is
rejected with "this production adapter requires ...". That guard is
deliberate, not an oversight — `gen_video` sends Veo-specific arguments
(`duration` as `"8s"`, `generate_audio`, `auto_fix`, `safety_tolerance`) and
`costs.video_cost` reads `veo31_fast_per_sec_*` price keys. Changing model is
an adapter, not a setting.

## Why Veo wins for this pipeline

| | Veo 3.1 Fast | Grok Imagine (`xai/grok-imagine-video/image-to-video`) |
|---|---|---|
| Resolution | 1080p | **480p / 720p only** |
| Audio | `generate_audio=false` | **no flag; audio always generated** |
| Negative prompt | yes | **absent** |
| Duration | 4 / 6 / 8 s only | **integer 1–15 s** |
| Aspect 9:16 | yes | yes |
| Price | $0.10/s silent 1080p | $0.14/s 720p, $0.08/s 480p (v1.5) |

Three of these are disqualifying here:

1. **1080p.** Episode masters are 1080x1920. Grok's image-to-video tops out at
   720p. (Other Grok modes are advertised at 1080p for $0.25/s; that is not in
   this endpoint's schema and was not verified.)
2. **Audio cannot be disabled.** The pipeline depends on silent video so that
   ElevenLabs generates each character's locked `voice_id` and Sync Lipsync
   applies it. Model-generated audio cannot hold a voice across episodes.
   Stripping the track with ffmpeg works but pays for discarded output.
3. **No negative prompt.** `prompts.NEGATIVE_VIDEO` plus per-scene negatives
   are part of how identity and composition drift is contained.

And at comparable quality Grok is dearer: 720p at $0.14/s against 1080p at
$0.10/s.

## The one real advantage, and what to do about it

Grok's duration is any integer 1–15 s; Veo offers only 4, 6 or 8. With a
4-second floor, a two-second line leaves two seconds of padding, which reads
as slack pacing.

This does **not** require a different model. The fix belongs in `assemble`:
cut each clip to the measured line duration plus lead-in and gap, instead of
keeping the full generated length. Not implemented.

## What a model swap would never have fixed

The standing creative complaints are staging, transitions and unclear speech.
None originate in the video model:

- staging and composition — the keyframe (`fal-ai/nano-banana-2/edit`) and its prompt;
- transitions — the `assemble` stage and each scene's `continuity_in` / `continuity_out`;
- speech — ElevenLabs delivery and Sync Lipsync.

Revisit this decision only if the master format drops to 720p, or if locked
per-character voices stop being a requirement.
