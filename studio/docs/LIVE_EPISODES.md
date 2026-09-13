# Live episode production

The existing episode engine can now run through the studio with durable private
R2 checkpoints. The single first-clip preview remains a separate workflow.

## Producer workflow

1. Set the series' minimum and maximum episode seconds in Series settings.
   A duration mismatch also offers a small form on the episode page. For four
   eight-second scenes, a 30–40 second range and minimum four scenes is suitable.
2. Review the saved script and spending cap. Choose Native scene audio, or
   Assigned character voices. Approve the displayed version and start production.
3. Production pauses before keyframes when references need approval. Open
   References, inspect the images, approve the current pack, then Resume from the
   episode page with the same audio mode.
4. When delivery finishes, use Watch or download episode. Files remain private;
   authenticated links expire after 15 minutes. This does not publish the episode.

Native audio includes the literal script dialogue in the video request and keeps
the generated audio through editing. No ElevenLabs voice IDs are required for
this option. Voice identity and exact wording still need human review. Native
audio subtitle timing is estimated, not aligned by speech recognition.

## Configuration

Both `STUDIO_ALLOW_PAID=true` and `PIPELINE_ALLOW_PAID=true` are required. No code
changes these environment values. Live jobs also require Supabase and private R2,
plus the provider credentials for the selected stages. The browser form records
approval of the current package digest and the effective estimated budget cap;
the engine's package approval is scoped to that captured job. Editing the package
invalidates the form digest. Resume refuses a changed package or audio mode.

The existing `claude-sonnet-5` model is retained. Its input/output token rates are
$2/$10 per million tokens; free token counting reserves an output upper bound
before a call, and returned usage settles it. Other LLM models require adding an
explicit rate before live admission. Assigned voices also require configured
voice IDs and a positive `PRICE_ELEVENLABS_PER_1K_CHARS_ESTIMATE` for your plan.
fal and ElevenLabs costs are estimates; provider invoices are not reconciled.
Budget and regeneration ceilings are enforced by the existing engine.

## Recovery and limits

- A renewable Supabase lease serializes workers for a series. After a process
  disappears, its lease expires after three minutes; Resume can then acquire it.
- Each lease uses its own local workspace. Private content-addressed files are
  uploaded before an ETag-conditional checkpoint manifest is committed. A new
  worker restores the recorded files, state, approvals, costs and provider IDs.
- fal submissions use no automatic POST retry and `X-Fal-No-Retry: 1`. Collection
  resumes a recorded request ID. A submission without a saved ID is treated as
  uncertain and is not repeated. Interrupted synchronous LLM/TTS calls likewise
  need reconciliation rather than an automatic second charge.
- Completed takes and synchronous responses are reused. Forced live regeneration
  is currently refused. Changed content should use a new episode rather than
  overwriting a billed episode. Keep the chosen audio mode when resuming.
- Reference images are reviewable in the panel. A complete reference pack for the
  exact current bible version must be approved before keyframes or video.
- Failed QA cannot be delivered. Publishing is rejected by studio job admission.
- Workers still depend on the host staying available during processing; checkpoints
  make interruption recoverable. No claim is made that a Free Render instance is
  sufficient for every 1080p encoding workload.

## Verification

`pytest studio/tests pipeline/tests` includes simulated provider responses with
real engine validation, four-scene 32-second encoding, reference approval,
checkpoint restore after local file loss, repeat-run deduplication, rejected
cross-site forms, unknown submission handling, budget gates and lease fencing.
The 32-second orchestration test uses a small video resolution for speed. It
makes no paid API calls. Existing mock and first-preview regression suites remain.

Provider contracts:
- https://fal.ai/models/fal-ai/veo3.1/fast/image-to-video/api
- https://fal.ai/docs/documentation/model-apis/inference/queue
- https://platform.claude.com/docs/en/models/overview
