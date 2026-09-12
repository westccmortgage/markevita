# Implementation notes and engineering decisions

Status: mock-only implementation. No paid generation has been executed. Nothing has been published.

## Decisions taken without asking (routine engineering)

1. **Content is a data package, not code.** Creative material lives in `series/<series_id>/` as JSON validated by
   `serial/schema.py` (JSON Schema, `schema_version 2.0`). Contract for authors: `docs/SERIES_PACKAGE.md`.
   A markdown bible can be converted once with `tools/import_bible_md.py` (LLM call, needs `--live`), then reviewed.
2. **Runtime state is separate from content.** `pipeline/runs/<series_id>/` holds `series_state.json`
   (reference packs per bible version, approvals, per-episode end states) and `<episode_id>/` (state, work, out). Git-ignored.
3. **Knowledge and relationship tracking is explicit, not inferred from prose.** Scenes declare
   `knowledge_required` / `knowledge_gained` / `relationship_changes`; the validator simulates them in order across
   scenes and across episodes and rejects a brief where a character acts on a secret nobody told them.
   Semantic inference from dialogue would be unreliable and non-deterministic.
4. **Two visible speakers in one clip are split into sub-shots.** `fal-ai/sync-lipsync/v2` exposes no active-speaker
   selection (verified against the fal schema), so the spec's own alternative (split the exchange) is applied
   deterministically at intake. Durations are partitioned from {4,6,8}; impossible partitions reject the brief with an actionable message.
5. **Prompts come from the package when provided** (`production_prompts.json`); the LLM director only fills gaps.
   Dialogue text is never rewritten by any model.
6. **Voice time-fit.** Lines are placed sequentially (0.4 s lead-in, 0.35 s gaps); overflow is corrected with
   ffmpeg `atempo` up to 1.15× rather than a second paid TTS call; beyond that the run stops and names the scene.
7. **Lipsync audio is padded to clip length** before submission because `sync_mode=cut_off` would otherwise truncate the video.
8. **Veo is silent** (`generate_audio=false`, `auto_fix=false`, 1080p); 720p costs the same on the Fast tier, so drafts use 1080p directly.
9. **Mock is the default.** Paid calls require `--live` + `PIPELINE_ALLOW_PAID=true` + `series.json approval.status=approved`.
   Publication additionally requires a recorded `--approve publish --by`. Reference packs require `--approve references --by`
   per bible version; any bible change invalidates the approval.
10. **Takes are immutable.** Retries and operator-forced regenerations create new take ids; old takes stay in state and in R2 (private).
    Forced takes are flagged and excluded from the retry-limit QA check, and logged in `state.overrides`.
11. **Provider inputs** default to short-lived R2 presigned GET URLs (spec §4); `PROVIDER_INPUT_MODE=fal_storage` is a fallback when R2 is not configured.
12. **Loudness target −14 LUFS / −1.5 dBTP** (Instagram/Reels practice). Captions in the lower safe area (MarginV=250 of 1920).

## Conflicts with the previous specification (documented, not silently changed)

- Spec §2/§3 named MarkeVita locations/characters and `docs/CHARACTER_BIBLE.md` as inputs. Per the creative correction these are
  obsolete; the pipeline now reads only a series package. Old files were left in place (no data deletion) and are not read.
- Spec suggested bucket `markevita-series-media` and domain `media.markevita.com`. These are infrastructure, not story; they are
  no longer defaults in code and must be set in `.env` (`R2_BUCKET`, `R2_PUBLIC_BASE_URL`).
- Spec's R2 key layout `series/{series_id}/episodes/{episode_id}/...` is kept; episode ids must be unique within a series (e.g. `s01e01`).
  Season is recorded in metadata rather than in the key path to keep the spec layout.
- Spec listed `ELEVENLABS_VOICE_ID_VITA/LEO_MERCER/CLIENT`. Voice env names are now declared per character in the package (`voice.voice_env`).

## Not implemented yet (needs the updated creative package or an owner decision)

- Live provider calls have never been executed; the adapters follow the fal/ElevenLabs schemas read on 2026-09-12 but need one paid smoke test.
- Instagram publishing (feature-flagged off; needs account, Meta app, permissions, and the auto-publish decision).
- Webhook/queue worker (`FAL_WEBHOOK_SECRET`, `SUPABASE_*`, `PIPELINE_WEBHOOK_*` are reserved). The local script polls; a worker is a drop-in later.
- Cloudflare R2 bucket/domain provisioning and the `/public/` gateway allow-list.
