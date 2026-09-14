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
    content = {'files': pkg.checksums, 'brief': pkg.load_episode(episode_id)}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


# Voice-dependent work. Rewording a line invalidates these and nothing else:
# the video prompt carries only visible action, and the engine never sends
# dialogue text to the video model.
VOICE_STAGES = ('voice', 'lipsync', 'assemble', 'qa', 'deliver')

# Everything that decides what is generated visually. Compared explicitly
# rather than by diffing whole scene dicts, because the saved copy carries
# prompts attached at intake that a freshly validated one does not.
SCENE_STRUCTURE = ('scene_id', 'sequence', 'duration', 'duration_seconds', 'location',
                   'lighting_state', 'characters_in_frame', 'wardrobe', 'action',
                   'shot_type', 'lens', 'camera_motion', 'continuity_in', 'continuity_out',
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


def configuration(audio_mode, model=DEFAULT_VIDEO_MODEL):
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
    cfg.native_dialogue = audio_mode == 'native'
    # Veo generates its own speech unless told not to. With assigned character
    # voices that speech would play underneath ElevenLabs and the lipsync take.
    cfg.video_generate_audio = cfg.native_dialogue
    cfg.video_auto_fix = False
    if cfg.anthropic_model != 'claude-sonnet-5':
        raise ValueError('Set ANTHROPIC_MODEL=claude-sonnet-5 for the supported cost accounting.')
    return cfg


def start(manager, series_id, episode_id, stages, actor, force, digest, approved, audio_mode):
    from . import runner
    if not approved or not actor:
        raise PermissionError('Review the script and budget, then approve live production in the form.')
    if 'publish' in stages:
        raise PermissionError('Publishing is a separate action and is not available here.')
    if force:
        raise ValueError('Forced live regeneration is not available in this release. Existing takes are retained.')
    source = materialize(series_id)
    frozen = settings.package_dir / '_live_jobs' / str(uuid.uuid4())
    shutil.copytree(source, frozen)
    pkg = SeriesPackage(frozen)
    cfg = configuration(audio_mode, video_model(pkg))
    actual_digest = package_digest(pkg, episode_id)
    if not digest or actual_digest != digest:
        raise ValueError('The script or series settings changed. Refresh this page and review the current version.')
    errors = preflight.problems(cfg, stages, pkg) + preflight.voice_problems(cfg, stages, pkg, episode_id)
    if errors:
        raise ValueError('\n'.join(errors))
    lease = SeriesLease(runner.store, series_id, actor)
    try:
        cp = Checkpoint(cfg, runtime_root() / '_workers' / lease.owner, series_id, lease.check)
        cp.restore()
        old = State(cp.root / episode_id)
        from serial.pipeline import SeriesState
        ss = SeriesState(cp.root / 'series_state.json')
        prev = pkg.previous_episode(episode_id)
        norm = validate_episode(pkg, pkg.load_episode(episode_id),
                                ss.data['episodes'].get(prev, {}).get('end_state') if prev else None, cfg)
        prior = old.data.get('live_input_digest')
        if prior and prior != actual_digest:
            saved = (old.data.get('episode') or {}).get('scenes')
            reworded = bool(saved) and (
                spoken_words_removed(old.data.get('package_checksums'), saved, episode_id)
                == spoken_words_removed(pkg.checksums, norm['scenes'], episode_id))
            if not reworded:
                raise ValueError('This episode has saved production for a different script or settings. '
                                 'Only the wording of spoken lines can be changed here. Anything else — a clip '
                                 'duration, the action, who is in frame — would not match the video already '
                                 'generated, so it needs a new episode.')
            # Speech is remade from the new wording. References, keyframes and
            # video stay: they were generated from the visible action.
            drop_voice_work(old)
        if old.data.get('audio_mode', audio_mode) != audio_mode:
            raise ValueError('Keep the original audio mode when resuming this episode.')
        errors = preflight.recovery_problems(stages, old)
        if errors:
            raise ValueError('\n'.join(errors))
        # Prove checkpoint write access before creating any paid operation.
        cp.save()
        # Freeze this job's package. The current editor may subsequently change.
        pkg.series['approval'] = {'status': 'approved', 'by': actor, 'at': now()}
        for job in runner.store.list('production_jobs', {'series_id': series_id, 'mode': 'live'}):
            if job.get('stages') != ['runtime_lease'] and job['state'] in ('queued','running','pausing','cancelling'):
                runner.store.update('production_jobs', {'id': job['id']}, {'state': 'interrupted', 'error': 'Worker stopped; saved provider requests will be reused.'})
        job = runner.store.insert('production_jobs', {
            'series_id': series_id, 'episode_id': episode_id, 'stages': stages, 'mode': 'live',
            'state': 'queued', 'requested_by': actor, 'idempotency_key': f'live:{series_id}:{episode_id}:{lease.owner}',
            'force': [], 'progress': {'audio_mode': audio_mode, 'input_digest': actual_digest,
                                     'approved_budget': pkg.limits(cfg)['budget'], 'done': [], 'total': len(stages)},
            'created_at': now(), 'log': ''})
        runner.store.insert('approvals', {'series_id': series_id, 'episode_id': episode_id,
            'subject_type': 'episode_live', 'subject_id': actual_digest, 'decision': 'approved',
            'actor': actor, 'note': f"Estimated spending cap ${pkg.limits(cfg)['budget']:.2f}; audio={audio_mode}", 'created_at': now()})
        control = runner._Control()
        manager._controls[job['id']] = control
        thread = threading.Thread(target=run, args=(manager, job, control, cfg, pkg, cp, lease), daemon=True)
        manager._threads[job['id']] = thread
        thread.start()
        return job
    except Exception:
        lease.close()
        raise


def check_configuration(series_id, episode_id, stages, audio_mode):
    """Read-only local checks; no lease, checkpoint mutation or paid request."""
    pkg = SeriesPackage(materialize(series_id))
    cfg = configuration(audio_mode, video_model(pkg))
    errors = preflight.problems(cfg, stages, pkg) + preflight.voice_problems(cfg, stages, pkg, episode_id)
    try:
        validate_episode(pkg, pkg.load_episode(episode_id), None, cfg)
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
        pipeline = Pipeline(cfg, pkg, episode_id, cp.root.parent)
        pipeline.state.on_save = cp.save
        pipeline.sstate.on_save = cp.save
        pipeline.state.data.update(live_input_digest=progress['input_digest'],
                                   audio_mode=progress['audio_mode'], mode='live')
        pipeline.state.save()
        cfg.paid_calls = PaidCalls(pipeline.state, pipeline.budget)
        released = {a['subject_id'] for a in runner.store.list('approvals', {
            'series_id': job['series_id'], 'episode_id': job['episode_id'],
            'subject_type': 'fal_request_unreachable'}) if a.get('decision') == 'released'}
        pipeline.fal = DurableFal(cfg, pipeline.log, pipeline.state, pipeline.budget,
                                  pipeline.fal.inputs, released)
        original_log = pipeline.log
        def live_log(message):
            original_log(message)
            log(message)
        pipeline.log = live_log
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
        message = str(exc) if isinstance(exc, (ValueError, PermissionError, ProviderFailure)) else type(exc).__name__ + ': ' + runner.explain(type(exc).__name__ + ': ' + str(exc))
        if not message.split(':', 1)[-1].strip():
            message = 'Production stopped. Saved requests are retained; Resume will not resubmit an uncertain request.'
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
    cfg = configuration('native', video_model(SeriesPackage(materialize(series_id))))
    lease = SeriesLease(runner.store, series_id, actor)
    try:
        cp = Checkpoint(cfg, runtime_root() / '_workers' / lease.owner, series_id, lease.check)
        cp.restore()
        pkg = SeriesPackage(materialize(series_id))
        ss = SeriesState(cp.root / 'series_state.json')
        if ss.data.get('bible_version') != pkg.bible_version or ss.data.get('reference_pack_complete') != pkg.bible_version:
            raise ValueError('Generate the reference pack for the current series settings first.')
        ss.data['approvals']['references'] = {'approved': True, 'bible_version': pkg.bible_version,
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
            'subject_id': pkg.bible_version, 'decision': 'approved', 'actor': actor, 'note': note, 'created_at': now()})
        return {'bible_version': pkg.bible_version, 'by': actor}
    finally:
        lease.close()


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
