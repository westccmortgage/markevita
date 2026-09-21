"""Live admission and execution using the existing content-agnostic engine."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import traceback
import threading
from pathlib import Path

from .config import PIPELINE_DIR, settings
from .packaging import materialize
from .live_runtime import Checkpoint, SeriesLease, unexpired
from .live_providers import DurableFal
from .provider_errors import ProviderFailure
from serial.llm import ModelRejected
from . import preflight
from serial.config import Config, DEFAULT_VIDEO_MODEL, VIDEO_MODELS
from serial.package import SeriesPackage, validate_episode
from serial.pipeline import Pipeline, STAGES
from serial.paid_calls import PaidCalls
from serial.state import State, now


REFERENCE_APPROVAL_MESSAGE = 'Reference pack ready. Open References, review the images, approve the pack, then Resume.'


def runtime_root():
    from .runner import RUNS_ROOT
    return RUNS_ROOT / 'live'


def package_digest(pkg, episode_id):
    # Subtitles and music decide only how the finished episode is packaged, so
    # they stay out of the digest that guards already-generated work. Changing
    # one of them mid-episode used to read as a different script and refuse the
    # resume, with the video for all 28 scenes already shot and paid for.
    from serial.package import FINISHING_SETTINGS
    brief = {k: v for k, v in pkg.load_episode(episode_id).items() if k not in FINISHING_SETTINGS}
    content = {'files': pkg.checksums, 'brief': brief}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


# Voice-dependent work. Rewording a line invalidates these and nothing else:
# the video prompt carries only visible action, and the engine never sends
# dialogue text to the video model.
VOICE_STAGES = ('voice', 'lipsync', 'assemble', 'qa', 'deliver')

# Everything that decides what is generated visually. Compared explicitly
# rather than by diffing whole scene dicts, because the saved copy carries
# prompts attached at intake that a freshly validated one does not.
#
# 'lens' belongs to that same group and was in this list by mistake. When the
# script does not name one, the direction stage picks it and writes it back
# into the scene, so a started episode has a lens and a freshly read script
# has none. Comparing it compared a result against its own input, and every
# episode that got as far as direction could never be resumed again.
SCENE_STRUCTURE = ('scene_id', 'sequence', 'duration', 'duration_seconds', 'location',
                   'lighting_state', 'characters_in_frame', 'wardrobe', 'action',
                   'shot_type', 'camera_motion', 'continuity_in', 'continuity_out',
                   'props', 'is_cliffhanger', 'split', 'lipsync_speaker')
LINE_STRUCTURE = ('speaker', 'delivery', 'voice_over')


def spoken_words_removed(checksums, scenes, episode_id):
    """Digest of everything except the WORDS of each spoken line.

    Speaker, delivery, voice-over flag, line count, clip duration and every
    visual field stay in, as do the bible, style and prompt files. Only this
    episode's own brief is excluded from the file checksums, since its text is
    the thing allowed to change.

    Matching therefore means the edit was confined to wording — which is
    exactly what the voice stage asks for when a line will not fit, and what
    must be resumable if that instruction is to be followable at all.
    """
    files = {k: v for k, v in (checksums or {}).items() if episode_id not in k}
    shape = [{**{f: scene.get(f) for f in SCENE_STRUCTURE},
              'dialogue': [{f: line.get(f) for f in LINE_STRUCTURE}
                           for line in scene.get('dialogue', [])]}
             for scene in scenes]
    return hashlib.sha256(json.dumps({'files': files, 'scenes': shape},
                                     sort_keys=True, default=str).encode()).hexdigest()


# What the episode itself declares that decides generation. The rest of the
# brief is bookkeeping (ids, totals) or a digest of its own.
# Visual format is bound to the generated clips.  Spoken language is not: it
# belongs with the wording and can be replaced while preserving every image
# and video take.  It is included only in the full comparison below so a
# language-only edit still drops and regenerates the voice work.
BRIEF_STRUCTURE = ('aspect_ratio', 'width', 'height')


def shape_parts(checksums, brief, scenes, episode_id, with_words=True):
    """The comparison broken into named parts, so a mismatch can say which.

    Twice now a resume was refused as "a different script" and the only way to
    learn what had actually moved was to guess, change something and ask the
    producer to press the button again. The parts are named here so the
    refusal can name them too.
    """
    files = {k: v for k, v in (checksums or {}).items()
             if episode_id not in k and k != 'series.json'}
    line_fields = LINE_STRUCTURE + (('text',) if with_words else ())
    parts = {'the bible and style files': files,
             'the visual format': {f: (brief or {}).get(f) for f in BRIEF_STRUCTURE}}
    if with_words:
        parts['the spoken language'] = {'language': (brief or {}).get('language')}
    for scene in scenes or []:
        parts[f"scene {scene.get('scene_id')}"] = {
            **{f: scene.get(f) for f in SCENE_STRUCTURE},
            'dialogue': [{f: line.get(f) for f in line_fields}
                         for line in scene.get('dialogue', [])]}
    return parts


def shape_differences(saved_parts, current_parts) -> list[str]:
    """Which named parts differ, and for the first few, which fields."""
    out = []
    for name in sorted(set(saved_parts) | set(current_parts)):
        was, now_ = saved_parts.get(name), current_parts.get(name)
        if was == now_:
            continue
        fields = sorted(k for k in set(was or {}) | set(now_ or {})
                        if (was or {}).get(k) != (now_ or {}).get(k))
        out.append(f"{name}: {', '.join(fields[:6])}" if fields else name)
    return out


def generated_shape(checksums, brief, scenes, episode_id, with_words=True):
    """Digest of everything that decides what gets generated, and nothing else.

    Compared instead of the stored input digest, because that digest is a hash
    of whatever the code hashed on the day it was written: the moment the
    recipe changed, every episode already in production read as a different
    script and could not be resumed. This is computed the same way from both
    sides, here and now.

    series.json is left out and its generation-relevant contents — the format
    and the language — are taken from the brief instead, where the saved copy
    also has them. The bible and style files keep their own checksums.
    """
    parts = shape_parts(checksums, brief, scenes, episode_id, with_words)
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def drop_voice_work(state):
    """Forget spoken audio so it is made again from the new wording.

    Reference packs, keyframes and video are untouched: they were generated
    from the visible action, which has not changed. Nothing paid for is
    discarded beyond the speech itself.
    """
    for stage in VOICE_STAGES:
        state.data.get('stages', {}).pop(stage, None)
    for scene in state.data.get('scenes', {}).values():
        scene.pop('voice', None)
        scene.pop('lipsync', None)
    state.data.pop('audio', None)
    state.data['status'] = 'voice_pending'
    state.save()


SCENE_WORK = ('keyframe', 'video', 'lipsync', 'voice', 'final')
PLANNING_STAGES = ('intake', 'direction')


def scene_work_exists(state) -> bool:
    """Has anything been generated that is bound to this episode's scenes?

    Reference images are not: they belong to the series and the engine remakes
    them whenever the bible version changes. Keyframes, video, speech and
    lipsync are, and those are what a changed script or setting would
    contradict.
    """
    for take in (state.data.get('takes') or {}).values():
        if take.get('scene_id') and take.get('status') in ('succeeded', 'submitted'):
            return True
    return any(any(scene.get(k) for k in SCENE_WORK)
               for scene in (state.data.get('scenes') or {}).values())


def drop_planning(state):
    """Let the plan be made again from the bible as it now reads.

    Prompts written by the director stage quote the wardrobe and the
    appearance. Keeping them after the bible changed would send the old
    wording to the image provider — the same refusal, one stage later.
    """
    for stage in PLANNING_STAGES:
        state.data.get('stages', {}).pop(stage, None)
    state.save()


def review(series_id, episode_id):
    try:
        pkg = SeriesPackage(materialize(series_id))
        return package_digest(pkg, episode_id)
    except Exception:
        return ''


def video_model(pkg):
    """The model this series is set to, from its own package.

    Chosen once per series and inherited by every episode: the producer is
    never asked which video model a scene should use. It travels in the
    package, so the approval digest covers a change of model exactly as it
    covers a change of script.
    """
    chosen = (pkg.series.get('production_limits') or {}).get('video_model')
    return chosen or DEFAULT_VIDEO_MODEL


def video_route(pkg, episode_id):
    """Episode-specific ordered QC route, falling back to the series model."""
    load_episode = getattr(pkg, 'load_episode', None)
    route = tuple((load_episode(episode_id).get('video_route') if load_episode else ()) or ())
    return route or (video_model(pkg),)


def recorded_previous_end_state(pkg, episode_id):
    """Read the delivered previous episode's durable continuity ledger."""
    from . import runner
    previous = pkg.previous_episode(episode_id)
    if not previous:
        return None
    rows = runner.store.list('knowledge_state', {
        'series_id': pkg.series['series_id'], 'episode_id': previous,
    })
    if not rows:
        return None
    end = {'knowledge': {}, 'relationships': {}, 'props': {}}
    for row in rows:
        bucket = 'knowledge' if row.get('kind') == 'knowledge' else 'relationships'
        end[bucket][row['subject_id']] = row.get('value')
    return end


# What a producer is really choosing when they ask for it to look like cinema.
# The language model writes the words and costs cents; the picture is where the
# money goes, so these three settings move together under one name.
PICTURE = {
    "standard": {"video_resolution": "1080p", "image_resolution": "1K",
                 "lipsync_variant": "lipsync-2"},
    "high": {"video_resolution": "1080p", "image_resolution": "2K",
             "lipsync_variant": "lipsync-2-pro"},
    "maximum": {"video_resolution": "4k", "image_resolution": "2K",
                "lipsync_variant": "lipsync-2-pro"},
}


def picture(pkg) -> str:
    chosen = (pkg.series.get('production_limits') or {}).get('picture')
    return chosen if chosen in PICTURE else 'standard'


def configuration(audio_mode, model=DEFAULT_VIDEO_MODEL, quality='standard', route=None):
    if not settings.allow_paid:
        raise PermissionError('STUDIO_ALLOW_PAID must be enabled for live production.')
    cfg = Config.load(PIPELINE_DIR, live=True)
    if not cfg.allow_paid_env:
        raise PermissionError('PIPELINE_ALLOW_PAID must be enabled for live production.')
    if settings.store_driver != 'supabase':
        raise PermissionError('Live episodes require STUDIO_STORE=supabase for durable job ownership.')
    if audio_mode not in ('native', 'voices'):
        raise ValueError('Choose native audio or assigned character voices.')
    if model not in VIDEO_MODELS:
        raise ValueError(f'This series is set to an unsupported video model: {model!r}.')
    cfg.fal_video_model = model
    cfg.video_model_route = tuple(route or (model,))
    unknown = [item for item in cfg.video_model_route if item not in VIDEO_MODELS]
    if unknown:
        raise ValueError(f'This episode route contains unsupported video models: {unknown!r}.')
    if cfg.video_model_route[0] != model:
        raise ValueError('The first route engine must match the selected primary video model.')
    if quality not in PICTURE:
        raise ValueError(f'This series is set to an unknown picture quality: {quality!r}.')
    for field, value in PICTURE[quality].items():
        setattr(cfg, field, value)
    cfg.native_dialogue = audio_mode == 'native'
    # Veo generates its own speech unless told not to. With assigned character
    # voices that speech would play underneath ElevenLabs and the lipsync take.
    cfg.video_generate_audio = cfg.native_dialogue
    cfg.video_auto_fix = False
    from serial.costs import UnknownLanguageModel, anthropic_rates
    try:
        anthropic_rates(cfg.anthropic_model)
    except UnknownLanguageModel as exc:
        raise ValueError(str(exc)) from None
    return cfg


# A failure that decided something about this episode: the model would not
# draw it, the budget is spent, a person has to approve the pack. Asking again
# changes nothing and costs money, so these are never carried on by themselves.
DECIDED = ('content_policy', 'would not draw this reference', 'budget',
           'needs_budget_override', 'Generate the reference pack',
           'Review the script and budget', 'Publishing is a separate action',
           'no script yet', 'needs reconciliation', 'Reconcile',
           'outcome is unknown', 'has no script')

# A failure that decided nothing: the provider had a bad minute, the account
# was briefly locked, a connection dropped, the server was restarted under the
# worker. Every stop this week was one of these, and every one of them ended a
# job as "failed" — a state nothing ever picked back up, so the producer was
# the retry mechanism, at ten-minute intervals, for three days.
TRANSIENT = ('User is locked', 'TOP_UP', 'HTTP 403', 'HTTP 429', 'HTTP 500',
             'HTTP 502', 'HTTP 503', 'InternalServerError', 'ServiceUnavailable',
             'Overloaded', 'RemoteProtocolError', 'ReadError', 'WriteError',
             'ConnectError', 'Timeout', 'TimeoutError', 'ConnectionError',
             'Worker stopped', 'lease', 'produced no output', 'APIStatusError',
             'APIConnectionError')

# Attempts since the run last made something. A fault that reproduces stops
# being retried; a run that is getting work done is never given up on, because
# the limit that counted every attempt for all time left an episode dead to
# automation after three bad minutes across three days.
CARRY_ON_LIMIT = 3

# Between attempts, growing: 2, 4, 8, 16 minutes. An account locked for an hour
# should be waited out, not asked sixty times an hour.
BACKOFF_MINUTES = 2


def _worth_another_go(job) -> bool:
    """Did this failure decide anything, or was it just a bad minute?

    A duration-only final-QA failure from the old strict boundary is also
    resumable. The master already exists and every paid generation stage is
    checkpointed; after the encode-tolerance fix, resuming only re-runs QA and
    delivery. This is deliberately narrow so other QA failures still require
    a producer decision.
    """
    text = job.get('error') or ''
    if 'QA failed (duration_range)' in text:
        return True
    if any(mark.lower() in text.lower() for mark in DECIDED):
        return False
    return any(mark.lower() in text.lower() for mark in TRANSIENT)


def _waited_long_enough(job) -> bool:
    """Back off between attempts. An account locked for an hour is waited out."""
    from datetime import datetime, timedelta, timezone
    finished = job.get('finished_at') or job.get('created_at')
    try:
        when = datetime.fromisoformat(str(finished).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return True
    from . import runner
    tries = _attempts_since_progress(runner, job)
    wait = timedelta(minutes=BACKOFF_MINUTES * (2 ** min(tries, 3)))
    return datetime.now(timezone.utc) - when >= wait


def _made_so_far(runner, job) -> int:
    """Paid results this episode holds. It only ever grows, so it marks progress."""
    try:
        from .progress import every
        return len(every('takes', {'series_id': job['series_id'],
                                   'episode_id': job['episode_id']}))
    except Exception:                                              # noqa: BLE001
        return 0


def _attempts_since_progress(runner, job) -> int:
    """Carried on since the run last produced something.

    Counting every attempt for all time left an episode dead to automation
    after three bad minutes spread across three days, while a run that was
    plainly getting work done was refused a fourth try.
    """
    made = _made_so_far(runner, job)
    tries = 0
    for row in sorted(runner.store.list(
            'generation_history', {'series_id': job['series_id'],
                                   'episode_id': job['episode_id'],
                                   'event': 'job.resumed_after_restart'}),
            key=lambda r: r.get('created_at') or '', reverse=True):
        before = ((row.get('detail') or {}).get('made_by_then'))
        if isinstance(before, int) and before < made:
            break          # work happened after that attempt; the slate is clean
        tries += 1
    return tries


def resume_interrupted(manager) -> list[str]:
    """Pick production back up after the worker's process went away.

    The worker lives in the server process, so a restart — a deploy, a
    container recycled, the host moving the instance — leaves the episode
    stopped mid-pack with everything generated so far paid for. Waiting for
    someone to notice and press Resume is how three minutes of film takes days.

    This continues work a producer already approved, at the budget they
    approved, from the last checkpoint, reusing every saved request. It is not
    a fresh authorisation: an episode whose script or settings have changed
    since that approval is left alone, because what was approved is no longer
    what would be made.
    """
    from . import runner
    resumed = []
    if not settings.allow_paid:
        return resumed
    # A worker records its job's state from inside its own process, so a
    # process that goes away leaves the row saying "running" for ever. Nothing
    # in the background looked: the row was only ever corrected when a person
    # opened the Jobs page. So the studio could recover from a restart, but
    # only after someone came to look — which is the thing it was supposed to
    # spare them. Reconcile first, then carry on what that turns up.
    for series_id in {j['series_id'] for state in ('queued', 'running', 'pausing', 'cancelling')
                      for j in runner.store.list('production_jobs', {'mode': 'live', 'state': state})}:
        try:
            runner.jobs.reconcile_abandoned(series_id)
        except Exception:                                          # noqa: BLE001
            continue   # one unreadable series must not stop the rest recovering
    candidates = [j for state in ('interrupted', 'failed')
                  for j in runner.store.list('production_jobs', {'mode': 'live', 'state': state},
                                             order='created_at', desc=True)]
    candidates.sort(key=lambda j: j.get('created_at') or '', reverse=True)
    for job in candidates:
        if job.get('stages') == ['runtime_lease']:
            continue
        progress = job.get('progress') or {}
        digest = progress.get('input_digest')
        if not digest or digest != review(job['series_id'], job['episode_id']):
            continue   # the script or settings moved on; the approval was for something else
        if job['state'] == 'failed' and not _worth_another_go(job):
            continue
        if not _waited_long_enough(job):
            continue
        carried = _attempts_since_progress(runner, job)
        if carried >= CARRY_ON_LIMIT:
            # A fault that reproduces stops being retried. The count is of
            # attempts since the run last made something, so a run that is
            # getting work done is never given up on.
            if job['state'] != 'failed':
                runner.store.update('production_jobs', {'id': job['id']}, {
                    'state': 'failed', 'finished_at': now(),
                    'error': f'Production stopped and was carried on {carried} times without '
                             'making anything. It is left for a person now: something is failing '
                             'the same way each time, and resuming again would only spend more.'})
            continue
        try:
            started = runner.jobs.resume(job['series_id'], job['episode_id'],
                                         job.get('requested_by') or '',
                                         approved_digest=digest, approve_live=True,
                                         audio_mode=(progress.get('audio_mode') or 'native'))
        except Exception as exc:
            # Recorded once and taken out of the queue. Retrying every pass
            # appended a line to the job each time — hundreds of them — and
            # hammered a refusal that was never going to change by itself.
            # Whatever stops an automatic resume needs a person, so the job is
            # handed to one; Resume by hand still works.
            detail = (str(exc) if isinstance(exc, (ValueError, PermissionError))
                      else type(exc).__name__)
            runner.store.update('production_jobs', {'id': job['id']}, {
                'state': 'failed', 'finished_at': now(),
                'error': 'Production stopped and could not carry on by itself:\n\n'
                         f'{detail}\n\nFix that, then resume this episode.'})
            continue
        runner.history(job['series_id'], job['episode_id'], 'job.resumed_after_restart',
                       entity_type='job', entity_id=started['id'], actor='system',
                       detail={'interrupted_job': job['id'], 'was': job['state'],
                               'made_by_then': _made_so_far(runner, job)})
        resumed.append(started['id'])
        break   # one series holds one production lease; the rest wait their turn
    return resumed


def start(manager, series_id, episode_id, stages, actor, force, digest, approved, audio_mode):
    from . import runner
    if not approved or not actor:
        raise PermissionError('Review the script and budget, then approve live production in the form.')
    if 'publish' in stages:
        raise PermissionError('Publishing is a separate action and is not available here.')
    force = list(dict.fromkeys(force or []))
    if force:
        # A live retry is deliberately narrower than the CLI's general
        # ``--force`` switch.  It is allowed only for individual scenes which
        # have a recorded producer approval.  This keeps a single bad frame
        # from turning a resume into a regeneration of the episode or stage.
        invalid = [item for item in force if not (item.startswith('sc') and item[2:].isdigit())]
        approved_scenes = {row['subject_id'] for row in runner.store.list('approvals', {
            'series_id': series_id, 'episode_id': episode_id,
            'subject_type': 'scene_regeneration'}) if row.get('decision') == 'approved'}
        unauthorized = [item for item in force if item not in approved_scenes]
        if invalid or unauthorized:
            raise PermissionError('Live regeneration requires a recorded approval for each individual scene.')
    if not runner.store.list('scenes', {'series_id': series_id, 'episode_id': episode_id}):
        raise ValueError('This episode has no script yet. Describe what should happen and let the '
                         'studio write it first; there is nothing to produce until then.')
    source = materialize(series_id)
    frozen = settings.package_dir / '_live_jobs' / str(uuid.uuid4())
    shutil.copytree(source, frozen)
    pkg = SeriesPackage(frozen)
    route = video_route(pkg, episode_id)
    cfg = configuration(audio_mode, route[0], picture(pkg))
    cfg.video_model_route = route
    actual_digest = package_digest(pkg, episode_id)
    if not digest or actual_digest != digest:
        raise ValueError('The script or series settings changed. Refresh this page and review the current version.')
    errors = preflight.problems(cfg, stages, pkg) + preflight.voice_problems(cfg, stages, pkg, episode_id)
    if errors:
        raise ValueError('\n'.join(errors))
    lease = SeriesLease(runner.store, series_id, actor)
    try:
        # Named for the series, not for this attempt: a fresh directory every
        # time meant nothing verified on the last run could ever be reused, so
        # the whole checkpoint came down the wire again. One worker holds a
        # series at a time — that is what the lease is — so the directory is
        # this worker's alone while it runs.
        cp = Checkpoint(cfg, runtime_root() / '_workers' / series_id, series_id, lease.check)
        # Everything from here on happens in the worker. Fetching the saved
        # work is a download of every keyframe and clip already made, and after
        # a deploy the disk is empty so all of it comes down again — minutes,
        # against the hundred seconds the proxy in front of this allows. Doing
        # it inside the button left the producer with a gateway timeout and no
        # way through at all, on an episode that was one stage from finished.
        job = runner.store.insert('production_jobs', {
            'series_id': series_id, 'episode_id': episode_id, 'stages': stages, 'mode': 'live',
            'state': 'queued', 'requested_by': actor,
            'idempotency_key': f'live:{series_id}:{episode_id}:{lease.owner}',
            'force': force, 'progress': {'audio_mode': audio_mode, 'input_digest': actual_digest,
                                      'approved_budget': pkg.limits(cfg)['budget'],
                                      'done': [], 'total': len(stages)},
            'created_at': now(), 'log': 'Fetching the work already saved for this episode.'})
        control = runner._Control()
        manager._controls[job['id']] = control
        thread = threading.Thread(target=_prepare_and_run,
                                  args=(manager, job, control, cfg, pkg, cp, lease, actor,
                                        series_id, episode_id, stages, audio_mode, actual_digest),
                                  daemon=True)
        manager._threads[job['id']] = thread
        thread.start()
        return job
    except Exception:
        lease.close()
        raise


def _prepare_and_run(manager, job, control, cfg, pkg, cp, lease, actor,
                     series_id, episode_id, stages, audio_mode, actual_digest):
    """Fetch the saved work, check it against the current script, then run.

    A refusal here used to arrive as a red line on the episode page. It now
    arrives on the job's own page, because the request that asked for it has
    long since been answered.
    """
    from . import runner
    try:
        cp.restore()
        old = State(cp.root / episode_id)
        from serial.pipeline import SeriesState
        ss = SeriesState(cp.root / 'series_state.json')
        prev = pkg.previous_episode(episode_id)
        norm = validate_episode(pkg, pkg.load_episode(episode_id),
                                ss.data['episodes'].get(prev, {}).get('end_state') if prev else None, cfg)
        prior = old.data.get('live_input_digest')
        if prior and prior != actual_digest:
            saved_brief = old.data.get('episode') or {}
            saved = saved_brief.get('scenes')

            def _same(with_words):
                return bool(saved) and (
                    generated_shape(old.data.get('package_checksums'), saved_brief, saved,
                                    episode_id, with_words)
                    == generated_shape(pkg.checksums, norm, norm['scenes'],
                                       episode_id, with_words))

            if _same(with_words=True):
                # Only how the finished episode is packaged has moved —
                # subtitles, music. Nothing generated contradicts it, and
                # nothing generated needs to be made again.
                pass
            elif _same(with_words=False):
                # Speech is remade from the new wording. References, keyframes
                # and video stay: they were generated from the visible action.
                drop_voice_work(old)
            elif scene_work_exists(old):
                moved = shape_differences(
                    shape_parts(old.data.get('package_checksums'), saved_brief, saved, episode_id),
                    shape_parts(pkg.checksums, norm, norm['scenes'], episode_id))
                raise ValueError('This episode has saved production for a different script or settings. '
                                 'Only the wording of spoken lines can be changed here. Anything else — a clip '
                                 'duration, the action, who is in frame — would not match the video already '
                                 'generated, so it needs a new episode.\n\nWhat moved since this episode '
                                 'started:\n' + '\n'.join(f'  - {m}' for m in moved[:12]))
            else:
                # Nothing bound to a scene has been made yet, so the new script
                # or settings contradict nothing. Correcting the bible after a
                # provider refused a reference used to leave the episode
                # unproducible for good: the refusal blocked the run, and the
                # correction blocked the resume.
                drop_planning(old)
        if old.data.get('audio_mode', audio_mode) != audio_mode:
            raise ValueError('Keep the original audio mode when resuming this episode.')
        settled = {a['subject_id'] for a in runner.store.list('approvals', {
            'series_id': series_id, 'episode_id': episode_id,
            'subject_type': 'paid_operation_reconciled'}) if a.get('decision') == 'released'}
        errors = preflight.recovery_problems(stages, old, settled)
        if errors:
            raise ValueError('\n'.join(errors))
        # Prove checkpoint write access before creating any paid operation.
        cp.save()
        # Freeze this job's package. The current editor may subsequently change.
        pkg.series['approval'] = {'status': 'approved', 'by': actor, 'at': now()}
        for other in runner.store.list('production_jobs', {'series_id': series_id, 'mode': 'live'}):
            if (other['id'] != job['id'] and other.get('stages') != ['runtime_lease']
                    and other['state'] in ('queued', 'running', 'pausing', 'cancelling')):
                runner.store.update('production_jobs', {'id': other['id']}, {
                    'state': 'interrupted',
                    'error': 'Worker stopped; saved provider requests will be reused.'})
        runner.store.insert('approvals', {'series_id': series_id, 'episode_id': episode_id,
            'subject_type': 'episode_live', 'subject_id': actual_digest, 'decision': 'approved',
            'actor': actor, 'note': f"Estimated spending cap ${pkg.limits(cfg)['budget']:.2f}; audio={audio_mode}",
            'created_at': now()})
    except Exception as exc:
        detail = (str(exc) if isinstance(exc, (ValueError, PermissionError))
                  else f'{type(exc).__name__}: the saved work could not be fetched or checked.')
        runner.store.update('production_jobs', {'id': job['id']}, {
            'state': 'failed', 'finished_at': now(),
            'error': f'Production could not be prepared.\n\n{detail}'})
        lease.close()
        return
    run(manager, job, control, cfg, pkg, cp, lease)


def check_configuration(series_id, episode_id, stages, audio_mode):
    """Read-only local checks; no lease, checkpoint mutation or paid request."""
    pkg = SeriesPackage(materialize(series_id))
    route = video_route(pkg, episode_id)
    cfg = configuration(audio_mode, route[0], picture(pkg))
    cfg.video_model_route = route
    errors = preflight.problems(cfg, stages, pkg) + preflight.voice_problems(cfg, stages, pkg, episode_id)
    try:
        validate_episode(pkg, pkg.load_episode(episode_id),
                         recorded_previous_end_state(pkg, episode_id), cfg)
    except ValueError as exc:
        errors.append(str(exc))
    if errors:
        raise ValueError('\n'.join(errors))


def run(manager, job, control, cfg, pkg, cp, lease):
    from . import runner
    series_id, episode_id = job['series_id'], job['episode_id']
    pipeline = None
    progress = dict(job['progress'])
    done, lines = [], []
    def update(**patch):
        lease.check()
        runner.store.update('production_jobs', {'id': job['id']}, patch)
    def log(message):
        lines.append(message)
        update(log='\n'.join(lines[-200:]))
    try:
        update(state='running', started_at=now())
        pipeline = Pipeline(cfg, pkg, episode_id, cp.root.parent,
                            force=set(job.get('force') or []))
        # A scene retry is a one-shot instruction.  Once its replacement
        # keyframe is present, a process restart must resume the remaining
        # stages rather than regenerate that scene yet again.
        for scene_id in list(pipeline.force):
            if scene_id.startswith('sc') and pipeline.state.scene(scene_id).get('keyframe'):
                pipeline.force.discard(scene_id)
        pipeline.state.on_save = cp.save
        pipeline.sstate.on_save = cp.save
        pipeline.state.data.update(live_input_digest=progress['input_digest'],
                                   audio_mode=progress['audio_mode'], mode='live')
        pipeline.state.save()
        # Scenes whose best attempt the producer has accepted despite the
        # quality check marking it down. The engine offered this as a
        # command-line flag only, so from the studio the sole way past a scene
        # QC kept failing was to pay for it again and hope.
        cfg.accepted_weak = {a['subject_id'] for a in runner.store.list('approvals', {
            'series_id': job['series_id'], 'episode_id': job['episode_id'],
            'subject_type': 'scene_weak_accepted'}) if a.get('decision') == 'approved'}
        cfg.paid_calls = PaidCalls(pipeline.state, pipeline.budget, {
            a['subject_id'] for a in runner.store.list('approvals', {
                'series_id': job['series_id'], 'episode_id': job['episode_id'],
                'subject_type': 'paid_operation_reconciled'}) if a.get('decision') == 'released'})
        released = {a['subject_id'] for a in runner.store.list('approvals', {
            'series_id': job['series_id'], 'episode_id': job['episode_id'],
            'subject_type': 'fal_request_unreachable'}) if a.get('decision') == 'released'}
        reconciled = {a['subject_id'] for a in runner.store.list('approvals', {
            'series_id': job['series_id'], 'episode_id': job['episode_id'],
            'subject_type': 'take_reconciled'}) if a.get('decision') == 'released'}
        original_log = pipeline.log
        def live_log(message):
            original_log(message)
            log(message)
        pipeline.log = live_log
        # Built with the live log, not the engine's own. fal.ai's explanation of
        # a refusal went only to the server's log stream and a file inside the
        # run directory, so the job page showed a bare code and the one sentence
        # saying what the provider actually objected to was unreadable.
        pipeline.fal = DurableFal(cfg, live_log, pipeline.state, pipeline.budget,
                                  pipeline.fal.inputs, released, reconciled)
        for stage in job['stages']:
            lease.check()
            command = runner.store.get('production_jobs', {'id': job['id']}) or {}
            if command.get('state') == 'cancelling' or control.cancel.is_set():
                update(state='cancelled', finished_at=now())
                return
            if command.get('state') == 'pausing' or control.pause.is_set():
                update(state='paused')
                return
            if stage == 'keyframes':
                try:
                    pipeline._require_references_approval()
                except RuntimeError:
                    log(REFERENCE_APPROVAL_MESSAGE)
                    progress.update(stage='references', done=list(done), waiting_for='reference_approval')
                    update(state='paused', progress=progress)
                    return
            progress.update(stage=stage, done=list(done))
            update(progress=progress)
            getattr(pipeline, 'stage_' + stage)()
            cp.save()
            done.append(stage)
            runner.ingest_episode(series_id, episode_id, cp.root.parent)
            runner.ingest_series_state(series_id, cp.root.parent)
        progress.update(stage=None, done=done)
        update(state='done', finished_at=now(), progress=progress)
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)[-5:]
        try:
            log(type(exc).__name__ + " at " + " -> ".join(f"{Path(f.filename).name}:{f.lineno} ({f.name})" for f in frames))
        except Exception:
            pass
        # Keep provider payloads/tokens out of the UI. Details are in private checkpoints.
        if isinstance(exc, (ValueError, PermissionError, ProviderFailure, ModelRejected)):
            message = str(exc)
        else:
            advice = runner.explain(type(exc).__name__ + ': ' + str(exc))
            # The class name always survives. Replacing an unrecognised failure
            # with a sentence about uncertain requests said nothing about what
            # happened, and described a situation that may not be this one.
            message = f'{type(exc).__name__}: {advice}' if advice else (
                f'{type(exc).__name__}. Production stopped. Saved requests are retained; '
                'Resume will not resubmit an uncertain request.')
        if not message.strip(' :.'):
            message = ('Production stopped. Saved requests are retained; '
                       'Resume will not resubmit an uncertain request.')
        try:
            update(state='failed', error=message, finished_at=now())
        except Exception:
            pass
    finally:
        if pipeline:
            try:
                lease.check()
                cp.save()
                runner.ingest_episode(series_id, episode_id, cp.root.parent)
                runner.ingest_series_state(series_id, cp.root.parent)
            except Exception:
                pass
            pipeline.logf.close()
        manager._controls.pop(job['id'], None)
        manager._threads.pop(job['id'], None)
        lease.close()


def approve_references(series_id, actor, note):
    from . import runner
    from serial.pipeline import SeriesState
    pkg_now = SeriesPackage(materialize(series_id))
    cfg = configuration('native', video_model(pkg_now), picture(pkg_now))
    lease = SeriesLease(runner.store, series_id, actor)
    try:
        cp = Checkpoint(cfg, runtime_root() / '_workers' / series_id, series_id, lease.check)
        cp.restore()
        pkg = SeriesPackage(materialize(series_id))
        ss = SeriesState(cp.root / 'series_state.json')
        # The pack's identity is what it was drawn from, not the checksum of
        # every package file: opening an episode used to invalidate an
        # approval that had nothing to do with it.
        want = pkg.reference_version
        if ss.data.get('bible_version') != want or ss.data.get('reference_pack_complete') != want:
            raise ValueError('Generate the reference pack for the current series settings first.')
        ss.data['approvals']['references'] = {'approved': True, 'bible_version': want,
                                            'by': actor, 'at': now(), 'note': note}
        for group in ss.data['references'].values():
            for pack in group.values():
                for rec in ([pack] if 'path' in pack else pack.values()):
                    if isinstance(rec, dict):
                        rec['approval'] = 'approved'
        ss.on_save = cp.save
        ss.save()
        runner.ingest_series_state(series_id, cp.root.parent)
        runner.store.insert('approvals', {'series_id': series_id, 'episode_id': '', 'subject_type': 'references',
            'subject_id': want, 'decision': 'approved', 'actor': actor, 'note': note, 'created_at': now()})
        return {'bible_version': want, 'by': actor}
    finally:
        lease.close()


def continue_after_reference_approval(series_id, actor):
    """Carry on the run that was waiting for exactly this approval.

    Approving the pack is the answer to the only question the run stopped to
    ask. Leaving it paused afterwards meant the producer had to find a second
    button on another page to say yes twice, and an episode sat still for
    hours because nobody knew a further click was owed. Nothing new is
    authorised here: same episode, same approved script and budget, same
    checkpoint, and an episode whose script moved on since is left alone.
    """
    from . import runner
    if not settings.allow_paid:
        return None
    for job in runner.store.list('production_jobs', {'series_id': series_id, 'mode': 'live',
                                                     'state': 'paused'},
                                 order='created_at', desc=True):
        progress = job.get('progress') or {}
        if progress.get('waiting_for') != 'reference_approval':
            continue
        digest = progress.get('input_digest')
        if not digest or digest != review(series_id, job['episode_id']):
            continue
        started = runner.jobs.resume(series_id, job['episode_id'],
                                     actor or job.get('requested_by') or '',
                                     approved_digest=digest, approve_live=True,
                                     audio_mode=(progress.get('audio_mode') or 'native'))
        runner.history(series_id, job['episode_id'], 'job.resumed_after_approval',
                       entity_type='job', entity_id=started['id'], actor=actor or 'system',
                       detail={'paused_job': job['id']})
        return started
    return None


def saved_state(series_id, episode_id):
    """Read only: do not restore/overwrite a running worker's local files."""
    cp = Checkpoint(Config.load(PIPELINE_DIR, live=True), runtime_root(), series_id)
    manifest = cp.read()
    info = (manifest or {}).get('files', {}).get(f'{episode_id}/state.json')
    if not info:
        return {}
    data = cp.client.get_object(Bucket=cp.bucket, Key=cp.prefix + 'objects/' + info['sha256'])['Body'].read()
    if hashlib.sha256(data).hexdigest() != info['sha256']:
        raise ValueError('Checkpoint checksum mismatch')
    return json.loads(data)
