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
from serial.config import Config
from serial.package import SeriesPackage, validate_episode
from serial.pipeline import Pipeline, STAGES
from serial.paid_calls import PaidCalls
from serial.state import State, now


def runtime_root():
    from .runner import RUNS_ROOT
    return RUNS_ROOT / 'live'


def package_digest(pkg, episode_id):
    content = {'files': pkg.checksums, 'brief': pkg.load_episode(episode_id)}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def review(series_id, episode_id):
    try:
        pkg = SeriesPackage(materialize(series_id))
        return package_digest(pkg, episode_id)
    except Exception:
        return ''


def configuration(audio_mode):
    if not settings.allow_paid:
        raise PermissionError('STUDIO_ALLOW_PAID must be enabled for live production.')
    cfg = Config.load(PIPELINE_DIR, live=True)
    if not cfg.allow_paid_env:
        raise PermissionError('PIPELINE_ALLOW_PAID must be enabled for live production.')
    if settings.store_driver != 'supabase':
        raise PermissionError('Live episodes require STUDIO_STORE=supabase for durable job ownership.')
    if audio_mode not in ('native', 'voices'):
        raise ValueError('Choose native audio or assigned character voices.')
    cfg.native_dialogue = audio_mode == 'native'
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
    cfg = configuration(audio_mode)
    needed = [s for s in stages if not (cfg.native_dialogue and s in ('voice', 'lipsync'))]
    missing = cfg.missing_for(needed)
    if not cfg.r2_configured:
        missing.append('R2 storage credentials')
    if missing:
        raise ValueError('Configure before production: ' + ', '.join(missing))
    source = materialize(series_id)
    frozen = settings.package_dir / '_live_jobs' / str(uuid.uuid4())
    shutil.copytree(source, frozen)
    pkg = SeriesPackage(frozen)
    actual_digest = package_digest(pkg, episode_id)
    if not digest or actual_digest != digest:
        raise ValueError('The script or series settings changed. Refresh this page and review the current version.')
    if not cfg.native_dialogue and 'voice' in stages:
        from serial.costs import PRICE
        voices = {d['speaker'] for s in pkg.load_episode(episode_id)['scenes'] for d in s['dialogue']}
        missing = [c for c in voices if not os.environ.get((pkg.characters[c].get('voice') or {}).get('voice_env', ''), '') and not cfg.voice_ids.get(c)]
        if missing:
            raise ValueError('Assign ElevenLabs voices for ' + ', '.join(missing) + ', or choose Native scene audio.')
        if PRICE['elevenlabs_per_1k_chars_estimate'] <= 0:
            raise ValueError('Set PRICE_ELEVENLABS_PER_1K_CHARS_ESTIMATE for your plan, or choose Native scene audio.')
    lease = SeriesLease(runner.store, series_id, actor)
    try:
        cp = Checkpoint(cfg, runtime_root() / '_workers' / lease.owner, series_id, lease.check)
        cp.restore()
        old = State(cp.root / episode_id)
        prior = old.data.get('live_input_digest')
        if prior and prior != actual_digest:
            raise ValueError('This episode has saved production for a different script/settings version. Keep this episode intact and create a new episode for changed content.')
        if old.data.get('audio_mode', audio_mode) != audio_mode:
            raise ValueError('Keep the original audio mode when resuming this episode.')
        from serial.pipeline import SeriesState
        ss = SeriesState(cp.root / 'series_state.json')
        prev = pkg.previous_episode(episode_id)
        validate_episode(pkg, pkg.load_episode(episode_id), ss.data['episodes'].get(prev, {}).get('end_state') if prev else None, cfg)
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
        pipeline.state.data.update(live_input_digest=progress['input_digest'], audio_mode=progress['audio_mode'], mode='live')
        pipeline.state.save()
        cfg.paid_calls = PaidCalls(pipeline.state, pipeline.budget)
        pipeline.fal = DurableFal(cfg, pipeline.log, pipeline.state, pipeline.budget, pipeline.fal.inputs)
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
                    log('Reference pack ready. Open References, review the images, approve the pack, then Resume.')
                    update(state='paused')
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
        message = str(exc) if isinstance(exc, (ValueError, PermissionError)) else type(exc).__name__ + ': ' + runner.explain(str(exc))
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
    cfg = configuration('native')
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
