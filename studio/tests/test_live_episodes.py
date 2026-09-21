"""Offline regression gates for live episode admission, durable requests and duration."""
import hashlib
import io
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import runner, packaging, web, live_jobs, live_runtime
from app.config import settings
from serial.config import Config
from serial.costs import Budget
from serial.state import State
from serial.paid_calls import PaidCalls
from app.live_providers import DurableFal
from test_clip_preview import StrictStore


class Missing(Exception):
    response = {'Error': {'Code': 'NoSuchKey'}}


class R2Fake:
    def __init__(self):
        self.objects = {}
    def put_object(self, Bucket, Key, Body, **kwargs):
        old = self.objects.get(Key)
        if kwargs.get('IfNoneMatch') == '*' and old is not None:
            raise RuntimeError('precondition failed')
        if 'IfMatch' in kwargs and kwargs['IfMatch'] != self.etag(old):
            raise RuntimeError('precondition failed')
        self.objects[Key] = bytes(Body)
        return {'ETag': self.etag(Body)}
    def etag(self, body):
        return hashlib.sha256(body or b'').hexdigest()
    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing()
        body = self.objects[Key]
        return {'Body': io.BytesIO(body), 'ETag': self.etag(body)}
    def generate_presigned_url(self, *a, **kw):
        return 'https://example.test/private-input'
    def upload_file(self, name, bucket, key, **kwargs):
        self.objects[key] = Path(name).read_bytes()


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    client = R2Fake()
    monkeypatch.setattr(live_runtime, 'R2', lambda *a: SimpleNamespace(enabled=True, client=client))
    cfg = SimpleNamespace(r2_bucket='private')
    cp = live_runtime.Checkpoint(cfg, tmp_path / 'live', 'test_series')
    cp.restore()
    return cp, cfg, client


def test_checkpoint_restores_media_and_state_after_container_loss(checkpoint):
    cp, cfg, client = checkpoint
    ep = cp.root / 's01e01'
    ep.mkdir()
    file = ep / 'work' / 'clip.mp4'
    file.parent.mkdir()
    file.write_bytes(b'completed video bytes')
    state = State(ep)
    state.on_save = cp.save
    state.data['takes']['take1'] = {'request_id': 'request-saved', 'local_path': str(file)}
    state.save()
    cp2 = live_runtime.Checkpoint(cfg, cp.root.parent, 'test_series')
    cp2.restore()
    assert file.read_bytes() == b'completed video bytes'
    assert State(ep).data['takes']['take1']['request_id'] == 'request-saved'


def test_old_checkpoint_cannot_replace_newer_manifest(checkpoint):
    cp, cfg, client = checkpoint
    cp.save()
    other = live_runtime.Checkpoint(cfg, cp.root.parent, 'test_series')
    other.read()
    (cp.root / 'new.json').write_text('{}')
    cp.save()
    with pytest.raises(RuntimeError, match='precondition'):
        other.save()


def test_atomic_lease_blocks_another_worker_and_fences_old_owner():
    store = StrictStore()
    lease = live_runtime.SeriesLease(store, 'test_series', 'admin@example.test')
    try:
        with pytest.raises(ValueError, match='active'):
            live_runtime.SeriesLease(store, 'test_series', 'admin@example.test')
        store.update('production_jobs', {'id': lease.id}, {'error': 'another-owner'})
        with pytest.raises(RuntimeError, match='expired'):
            lease.check()
    finally:
        lease.close()
    assert store.get('production_jobs', {'id': lease.id})['state'] == 'leased'


def test_unknown_synchronous_paid_call_is_never_repeated(tmp_path):
    state = State(tmp_path)
    budget = Budget(10, state)
    guard = PaidCalls(state, budget)
    called = []
    def interrupted():
        called.append(1)
        raise TimeoutError('lost response')
    with pytest.raises(TimeoutError):
        guard.once('tts', {'text': 'hello'}, 1, interrupted)
    restored = State(tmp_path)
    with pytest.raises(RuntimeError, match='interrupted before its response was saved') as caught:
        PaidCalls(restored, Budget(10, restored)).once('tts', {'text': 'hello'}, 1, interrupted)
    assert len(called) == 1
    # And it names the call, so the screen can offer the decision that clears
    # it. Named only by provider, there was nothing on it anyone could act on,
    # and the episode stayed where it was for good.
    assert 'tts:' in str(caught.value)
    assert restored.data['reserved_usd'] == 1


def test_approving_references_carries_the_paused_run_on(monkeypatch):
    """Saying yes to the pack is the answer the run stopped for."""
    from app import runner
    paused = {'id': 'j1', 'series_id': 's', 'episode_id': 'e', 'mode': 'live', 'state': 'paused',
              'requested_by': 'producer@example.test',
              'progress': {'waiting_for': 'reference_approval', 'input_digest': 'd1',
                           'audio_mode': 'native'}}
    resumed = {}
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(live_jobs, 'review', lambda sid, eid: 'd1')
    monkeypatch.setattr(runner.store, 'list', lambda table, where=None, **k: [paused] if table == 'production_jobs' else [])
    monkeypatch.setattr(runner.jobs, 'resume', lambda *a, **k: resumed.update(args=a, kw=k) or {'id': 'j2'})
    monkeypatch.setattr(runner, 'history', lambda *a, **k: None)
    started = live_jobs.continue_after_reference_approval('s', 'producer@example.test')
    assert started == {'id': 'j2'}
    assert resumed['kw']['approved_digest'] == 'd1' and resumed['kw']['approve_live'] is True

    # An episode whose script moved on since that approval is left alone: what
    # was approved is no longer what would be made.
    monkeypatch.setattr(live_jobs, 'review', lambda sid, eid: 'd2')
    assert live_jobs.continue_after_reference_approval('s', 'producer@example.test') is None


def test_interrupted_text_call_is_charged_and_asked_again(tmp_path):
    """A cut-off completion must not brick the episode for ever.

    Nothing waits on the provider's side and no artifact can arrive later, so
    the tokens are charged at the reserved estimate and the next run asks
    again. Before this, one lost connection left an episode that could not be
    resumed by hand or by anything else.
    """
    state = State(tmp_path)
    attempts = []
    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError('lost response')
        return {'content': [{'type': 'text', 'text': 'ok'}]}
    with pytest.raises(TimeoutError):
        PaidCalls(state, Budget(10, state)).once('anthropic', {'a': 1}, 1, flaky)
    restored = State(tmp_path)
    assert restored.data['paid_operations'] == {}
    assert restored.data['spent_usd'] == 1        # charged, not silently forgiven
    assert restored.data['reserved_usd'] == 0
    assert PaidCalls(restored, Budget(10, restored)).once('anthropic', {'a': 1}, 1, flaky)
    assert len(attempts) == 2


def test_an_interrupted_text_call_does_not_block_resuming(tmp_path):
    from app import preflight
    state = State(tmp_path)
    state.data['paid_operations'] = {'anthropic:x': {'provider': 'anthropic', 'status': 'reserved'}}
    assert preflight.recovery_problems(['references'], state) == []
    state.data['paid_operations']['elevenlabs:y'] = {'provider': 'elevenlabs', 'status': 'reserved'}
    assert any('elevenlabs' in e for e in preflight.recovery_problems(['references'], state))


def test_synchronous_result_and_cost_reused(tmp_path):
    state = State(tmp_path)
    guard = PaidCalls(state, Budget(10, state))
    assert guard.once('llm', {'a': 1}, 1, lambda: {'ok': True}, lambda r: 0.2) == {'ok': True}
    other = State(tmp_path)
    assert PaidCalls(other, Budget(10, other)).once('llm', {'a': 1}, 1, lambda: pytest.fail('duplicate')) == {'ok': True}
    assert other.data['spent_usd'] == 0.2
    assert other.data['reserved_usd'] == 0


def test_fal_resumes_request_without_resubmission_and_settles_once(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.data['reserved_usd'] = 1
    state.data['takes']['t1'] = {'status': 'submitted', 'request_id': 'saved-id'}
    state.save()
    monkeypatch.setattr('app.live_providers.requests.post', lambda *a, **k: pytest.fail('duplicate submit'))
    fal = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, state, Budget(10, state), None)
    monkeypatch.setattr(fal, '_wait', lambda endpoint, rid: {'video': {'url': 'https://example.test/saved.mp4'}})
    fal.run('fal-ai/test', {}, 't1', 1, 'video', None)
    fal.run('fal-ai/test', {}, 't1', 1, 'video', None)
    assert state.data['spent_usd'] == 1
    assert state.data['reserved_usd'] == 0
    assert len(state.data['cost_log']) == 1


def test_fal_timeout_after_submission_intent_does_not_retry(tmp_path, monkeypatch):
    state = State(tmp_path)
    fal = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, state, Budget(10, state), None)
    calls = []
    def timeout(*a, **kw):
        assert State(tmp_path).data['takes']['t1']['status'] == 'reserved'
        assert kw['headers']['X-Fal-No-Retry'] == '1'
        calls.append(1)
        raise TimeoutError()
    monkeypatch.setattr('app.live_providers.requests.post', timeout)
    with pytest.raises(TimeoutError):
        fal.run('fal-ai/test', {}, 't1', 1, 'video', None)
    with pytest.raises(RuntimeError, match='unknown'):
        fal.run('fal-ai/test', {}, 't1', 1, 'video', None)
    assert len(calls) == 1


def test_fal_refuses_when_checkpoint_write_fails_before_submission(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.on_save = lambda: (_ for _ in ()).throw(OSError('R2 unavailable'))
    fal = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, state, Budget(10, state), None)
    monkeypatch.setattr('app.live_providers.requests.post', lambda *a, **k: pytest.fail('paid call after checkpoint failed'))
    with pytest.raises(OSError):
        fal.run('fal-ai/test', {}, 't1', 1, 'video', None)


def test_live_requires_explicit_package_approval(monkeypatch):
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(runner, 'store', StrictStore())
    with pytest.raises(PermissionError, match='approve'):
        runner.jobs.start('test_series', 's01e01', ['intake'], 'admin@example.test')


def test_publish_is_blocked_even_when_live_is_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(runner, 'store', StrictStore())
    with pytest.raises(PermissionError, match='Publishing'):
        runner.jobs.start('test_series', 's01e01', ['publish'], 'admin@example.test')


def test_live_preserves_flags_and_requires_both(monkeypatch):
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setenv('PIPELINE_ALLOW_PAID', 'false')
    with pytest.raises(PermissionError, match='PIPELINE_ALLOW_PAID'):
        live_jobs.configuration('native')
    assert settings.allow_paid is True


def test_duration_settings_preserve_style_and_other_limits(monkeypatch):
    store = StrictStore()
    store.insert('series', {'id': 'miami', 'title': 'Title', 'style': {'style_sentence': 'Keep this'},
                            'production_limits': {'min_scenes': 4, 'maximum_episode_budget_usd': 50}})
    monkeypatch.setattr(web, 'store', store)
    monkeypatch.setattr(web, 'require_admin', lambda request: {'email': 'admin@example.test'})
    monkeypatch.setattr(web, '_check_form', lambda *a: None)
    monkeypatch.setattr(web, 'history', lambda *a, **kw: None)
    response = web.episode_duration(None, 'miami', 30, 40, 's01e01', 'token')
    row = store.get('series', {'id': 'miami'})
    assert row['style'] == {'style_sentence': 'Keep this'}
    assert row['production_limits']['min_episode_seconds'] == 30
    assert row['production_limits']['max_episode_seconds'] == 40
    assert row['production_limits']['min_scenes'] == 4
    assert row['production_limits']['maximum_episode_budget_usd'] == 50
    assert response.status_code == 303


@pytest.fixture
def short_package(tmp_path, monkeypatch):
    from test_studio import _minimal_series, SERIES
    from app import authoring, scripts
    store = StrictStore()
    monkeypatch.setattr(settings, 'package_dir', tmp_path / 'packages')
    # Production shares one store across every module; a fixture that patches
    # only some of them leaves whole code paths unexercised.
    for module in (runner, packaging, scripts, web, authoring):
        monkeypatch.setattr(module, 'store', store)
    _minimal_series(store)
    store.update('series', {'id': SERIES}, {'production_limits': {'min_scenes': 4, 'min_episode_seconds': 30, 'max_episode_seconds': 40}})
    store.insert('episodes', {'series_id': SERIES, 'episode_id': 's01e01', 'season_id': 's01', 'number': 1,
                             'title': 'Short pilot', 'cliffhanger': {'scene_id': 'sc04', 'hook': 'Who is there?', 'resolves_in': 'tbd'}})
    for n in range(1, 5):
        store.insert('scenes', {'series_id': SERIES, 'episode_id': 's01e01', 'scene_id': f'sc{n:02}', 'sequence': n,
            'duration_seconds': 8, 'location': 'shore', 'lighting_state': 'default', 'characters_in_frame': ['lead_a'],
            'wardrobe': {'lead_a': 'w_default'}, 'action': 'She notices the opening door.',
            'continuity_in': 'The door is closed.', 'continuity_out': 'The door opens. Hard cut.',
            'is_cliffhanger': n == 4, 'dialogue': [{'speaker': 'lead_a', 'text': 'Who is there?'}]})
    return store, SERIES, 's01e01'


def test_32_seconds_validates_with_short_series_range(short_package):
    store, sid, eid = short_package
    result = runner.validate_series(sid)
    assert result['ok'], result
    assert result['episodes'][0]['seconds'] == 32
    limits = store.get('series', {'id': sid})['production_limits']
    store.update('series', {'id': sid}, {'production_limits': {**limits, 'min_episode_seconds': 90, 'max_episode_seconds': 120}})
    assert not runner.validate_series(sid)['ok']


def test_native_audio_mode_does_not_need_voice_ids(monkeypatch):
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(settings, 'store_driver', 'supabase')
    monkeypatch.setenv('PIPELINE_ALLOW_PAID', 'true')
    cfg = live_jobs.configuration('native')
    assert cfg.native_dialogue and cfg.video_generate_audio
    assert cfg.mode == 'live'
    assert os.environ['PIPELINE_ALLOW_PAID'] == 'true'


def test_voice_admission_reports_missing_voices_after_silent_shots(short_package, monkeypatch):
    """The script importer omits optional dialogue on silent reaction shots."""
    from urllib.parse import parse_qs, urlsplit
    store, sid, eid = short_package
    store.update('scenes', {'series_id': sid, 'episode_id': eid, 'scene_id': 'sc01'},
                 {'dialogue': []})
    store.update('scenes', {'series_id': sid, 'episode_id': eid, 'scene_id': 'sc03'},
                 {'dialogue': []})
    cfg = Config(mode='live', allow_paid_env=True, elevenlabs_api_key='offline-test',
                 r2_account_id='offline', r2_bucket='private',
                 r2_access_key_id='offline', r2_secret_access_key='offline')
    cfg.native_dialogue = False
    monkeypatch.setattr(live_jobs, 'configuration', lambda mode, model=None, quality='standard': cfg)
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(web, 'require_admin', lambda request: {'email': 'admin@example.test'})
    monkeypatch.setattr(web, '_check_form', lambda *a: None)
    monkeypatch.setattr(live_jobs, 'SeriesLease', lambda *a: pytest.fail('Voice admission must finish before a worker starts'))
    for variable in list(os.environ):
        if variable.startswith('ELEVENLABS_VOICE_ID_'):
            monkeypatch.delenv(variable)
    digest = live_jobs.review(sid, eid)
    brief = json.loads((packaging.package_dir(sid) / 'episodes' / eid / 'brief.json').read_text())
    assert 'dialogue' not in brief['scenes'][0]
    assert brief['scenes'][1]['dialogue'][0]['speaker'] == 'lead_a'
    response = web.production_control(None, sid, eid, action='start', stages=['voice'],
        force='', csrf_token='offline', approve_live='yes', approved_digest=digest, audio_mode='voices')
    assert response.status_code == 303
    error = parse_qs(urlsplit(response.headers['location']).query)['err'][0]
    assert 'Assign ElevenLabs voices for lead_a, or choose Native scene audio.' in error
    assert not store.list('production_jobs', {'series_id': sid, 'episode_id': eid})
    assert not store.list('costs', {'series_id': sid, 'episode_id': eid})


def test_input_digest_changes_with_saved_dialogue(short_package):
    store, sid, eid = short_package
    before = live_jobs.review(sid, eid)
    store.update('scenes', {'series_id': sid, 'scene_id': 'sc04'}, {'dialogue': [{'speaker': 'lead_a', 'text': 'Goodbye.'}]})
    assert live_jobs.review(sid, eid) != before


@pytest.mark.parametrize('action', ['start', 'resume'])
def test_successful_production_opens_the_returned_job(short_package, monkeypatch, action):
    from urllib.parse import urlsplit
    store, sid, eid = short_package
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(web, 'require_admin', lambda request: {'email': 'admin@example.test'})
    monkeypatch.setattr(web, '_check_form', lambda *args: None)
    calls = []
    def admitted(*args, **kwargs):
        calls.append(kwargs)
        return {'id': 'saved-production-job'}
    monkeypatch.setattr(runner.jobs, action, admitted)
    response = web.production_control(None, sid, eid, action=action, stages=['intake'], force='',
        csrf_token='offline', approve_live='yes', approved_digest='reviewed-digest', audio_mode='voices')
    assert response.status_code == 303
    assert urlsplit(response.headers['location']).path.endswith('/jobs/saved-production-job')
    assert len(calls) == 1
    assert calls[0] == {'approve_live': True, 'approved_digest': 'reviewed-digest', 'audio_mode': 'voices'}


def test_production_form_uses_current_script_budget_and_csrf(short_package, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import auth, deps
    store, sid, eid = short_package
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(settings, 'base_path', '')
    monkeypatch.setattr(settings, 'public_url', 'https://studio.example.test')
    monkeypatch.setattr(runner, 'episode_runtime', lambda *args: {'status': 'draft', 'spent_usd': 0, 'reserved_usd': 0,
                      'takes': {}, 'masters': [], 'stages': {}, 'approvals': {}, 'overrides': [], 'qa': []})
    client = TestClient(app, base_url='https://studio.example.test')
    client.cookies.set(auth.COOKIE, auth.serialize({'email': 'admin@example.test', 'role': 'owner'}))
    response = client.get(f'/series/{sid}/episodes/{eid}/studio')
    assert response.status_code == 200
    assert 'Brief valid — 4 clips, 32s' in response.text
    assert 'Native scene audio' in response.text
    assert 'name="csrf_token"' in response.text
    response = client.post(f'/series/{sid}/episodes/{eid}/production', data={'action': 'start', 'approve_live': 'yes'},
                           headers={'Origin': 'https://attacker.example'})
    assert response.status_code == 403


@pytest.mark.slow
def test_live_orchestration_pauses_for_refs_resumes_and_delivers_32s(short_package, tmp_path, monkeypatch):
    """All provider replies are synthetic; engine validation/edit/encode are real."""
    import shutil
    import subprocess
    from serial import pipeline as engine, providers, media
    from serial.llm import LLM
    from serial.storage import R2
    from app import ingest
    store, sid, eid = short_package
    monkeypatch.setattr(ingest, 'store', store)
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(settings, 'store_driver', 'supabase')
    monkeypatch.setattr(runner, 'RUNS_ROOT', tmp_path / 'runs')
    store.update('series', {'id': sid}, {'format': {'aspect_ratio': '9:16', 'width': 180, 'height': 320, 'captions': 'srt'}})
    cfg = Config(mode='live', allow_paid_env=True, anthropic_api_key='offline-test', fal_key='offline-test:secret',
                 r2_account_id='offline', r2_bucket='private', r2_access_key_id='offline', r2_secret_access_key='offline')
    monkeypatch.setattr(Config, 'load', lambda *a, **k: cfg)
    r2client = R2Fake()
    def storage(*args):
        obj = object.__new__(R2)
        obj.cfg, obj.client, obj.enabled, obj.log, obj._put_cache = cfg, r2client, True, lambda msg: None, set()
        return obj
    monkeypatch.setattr(engine, 'R2', storage)
    monkeypatch.setattr(live_runtime, 'R2', storage)
    def llm_init(self, config, log):
        self.cfg, self.log, self.client, self.calls = config, log, None, 0
    monkeypatch.setattr(LLM, '__init__', llm_init)
    monkeypatch.setattr(LLM, 'direct', lambda self, bible, scenes: original_direct(LLM(Config(mode='mock'), lambda m: None), bible, scenes))
    monkeypatch.setattr(LLM, 'qc_image', lambda *a, **k: {'pass': True, 'score': 10})
    monkeypatch.setattr(LLM, 'qc_video', lambda *a, **k: {'pass': True, 'score': 10})
    from PIL import Image
    image = tmp_path / 'provider.png'
    video = tmp_path / 'provider.mp4'
    Image.new('RGB', (180, 320), (150, 180, 120)).save(image)
    subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=c=green:s=180x320:r=24:d=8',
        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=8', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-shortest', str(video)], check=True)
    submissions = []
    def fake_reply(self, endpoint, args, take_id, estimate, what, stub):
        take = self.state.take(take_id)
        if take.get('result'):
            return take['result'], take
        submissions.append((take_id, args))
        self.budget.reserve(estimate, what)
        result = {'images': [{'url': 'offline-image'}]} if 'image_url' not in args else {'video': {'url': 'offline-video'}}
        take.update(status='succeeded', result=result, request_id='offline-' + take_id,
                    endpoint=endpoint, estimated_cost=estimate, provider='offline')
        self.budget.settle(estimate, estimate, what, take_id)
        return result, take
    monkeypatch.setattr(DurableFal, 'run', fake_reply)
    def download(url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(video if url == 'offline-video' else image, dest)
        return dest
    monkeypatch.setattr(providers, 'download', download)
    monkeypatch.setattr(providers, 'tts', lambda *a, **k: pytest.fail('Native audio must not use ElevenLabs'))
    monkeypatch.setattr(providers, 'lipsync', lambda *a, **k: pytest.fail('Native audio must not use lipsync service'))
    digest = live_jobs.review(sid, eid)
    manager = runner.JobManager()
    def run_once():
        job = manager.start(sid, eid, requested_by='admin@example.test', approve_live=True,
                            approved_digest=digest, audio_mode='native')
        thread = manager._threads.get(job['id'])
        if thread:
            thread.join(50)
            assert not thread.is_alive(), 'offline job did not finish'
        return store.get('production_jobs', {'id': job['id']})
    first = run_once()
    assert first['state'] == 'paused', (first.get('error'), first.get('log'))
    assert first['progress']['waiting_for'] == 'reference_approval'
    assert first['progress']['done'] == ['intake', 'direction', 'references']
    refs_count = len(submissions)
    assert refs_count > 0
    # Discard all local production files, as a Render redeploy would.
    shutil.rmtree(live_jobs.runtime_root())
    live_jobs.approve_references(sid, 'admin@example.test', 'reviewed offline references')
    second = run_once()
    assert second['state'] == 'done', (second.get('error'), second.get('log'))
    assert len({tid for tid, _ in submissions}) == len(submissions)
    videos = [args for tid, args in submissions if '_vid_' in tid]
    assert len(videos) == 4 and all(x['generate_audio'] for x in videos)
    assert all('Who is there?' in x['prompt'] for x in videos)
    assert all('NO audible voice' not in x['prompt'] for x in videos)
    saved_manifest = json.loads(r2client.objects[f'series/{sid}/_studio_runtime/live/manifest.json'])
    master = Path(saved_manifest['root']) / eid / 'out/masters/v1/episode.mp4'
    info = media.probe(master)
    assert 31.9 <= info['duration'] <= 32.2 and info['has_audio']
    assert f'series/{sid}/episodes/{eid}/masters/v1/episode.mp4' in r2client.objects
    assert not any('/public/' in key for key in r2client.objects)
    before = len(submissions)
    third = run_once()
    assert third['state'] == 'done', third
    assert len(submissions) == before


from serial.llm import LLM as _LLM
original_direct = _LLM.direct


def test_producing_an_episode_with_no_script_says_so(monkeypatch):
    """The internal path of a throwaway working copy was shown instead:
    'missing /app/studio/.studio-packages/_live_jobs/<uuid>/episodes/s01e04/brief.json'."""
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(runner, 'store', StrictStore())
    with pytest.raises(ValueError, match='no script yet'):
        runner.jobs.start('test_series', 's01e04', ['intake'], 'admin@example.test',
                          approved_digest='d', approve_live=True)


# ── a job whose worker died must stop claiming to be running ───────────────

def _abandoned(store, *, lease_deadline=None):
    """A live job left saying "running" by a worker that is no longer there."""
    store.insert('production_jobs', {
        'series_id': 'island', 'episode_id': 's01e04', 'stages': ['references'], 'mode': 'live',
        'state': 'running', 'requested_by': 'admin@example.test', 'idempotency_key': 'live:island:s01e04:x',
        'force': [], 'progress': {'stage': 'references', 'done': ['intake'], 'total': 3},
        'created_at': '2026-09-16T10:23:00+00:00', 'log': ''})
    if lease_deadline:
        store.insert('production_jobs', {
            'series_id': 'island', 'episode_id': '', 'stages': ['runtime_lease'], 'mode': 'live',
            'state': 'leased', 'error': 'owner-1', 'finished_at': lease_deadline,
            'idempotency_key': 'episode-worker-lease:island'})


def test_a_job_whose_worker_is_gone_stops_saying_running(monkeypatch):
    """One job showed "running" on the references stage for three hours. The
    worker had died with the process; nothing else ever revisits the row."""
    from datetime import datetime, timedelta, timezone
    store = StrictStore()
    monkeypatch.setattr(runner, 'store', store)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    _abandoned(store, lease_deadline=stale)
    runner.jobs.reconcile_abandoned('island')
    job = store.list('production_jobs', {'series_id': 'island', 'episode_id': 's01e04'})[0]
    assert job['state'] == 'interrupted'
    assert 'Resume this episode' in job['error']


def test_a_live_worker_still_holding_its_lease_is_left_alone(monkeypatch):
    """Renewing every thirty seconds is what "still working" looks like."""
    from datetime import datetime, timedelta, timezone
    store = StrictStore()
    monkeypatch.setattr(runner, 'store', store)
    fresh = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
    _abandoned(store, lease_deadline=fresh)
    runner.jobs.reconcile_abandoned('island')
    job = store.list('production_jobs', {'series_id': 'island', 'episode_id': 's01e04'})[0]
    assert job['state'] == 'running'


def test_a_job_with_no_lease_at_all_is_not_left_running(monkeypatch):
    store = StrictStore()
    monkeypatch.setattr(runner, 'store', store)
    _abandoned(store)
    runner.jobs.reconcile_abandoned('island')
    job = store.list('production_jobs', {'series_id': 'island', 'episode_id': 's01e04'})[0]
    assert job['state'] == 'interrupted'


def test_narration_only_worker_is_not_claimed_by_episode_lease(monkeypatch):
    store = StrictStore()
    monkeypatch.setattr(runner, 'store', store)
    store.insert('production_jobs', {
        'series_id': 'island', 'episode_id': 's01e04', 'stages': ['narration_track'],
        'mode': 'live', 'state': 'running', 'requested_by': 'admin@example.test',
        'idempotency_key': 'narration-track:island:s01e04:digest', 'force': [],
        'progress': {'digest': 'digest'}, 'created_at': '2026-09-20T10:23:00+00:00',
        'log': 'Generating narration only.'})
    runner.jobs.reconcile_abandoned('island')
    job = store.list('production_jobs', {'series_id': 'island', 'episode_id': 's01e04'})[0]
    assert job['state'] == 'running'
    assert runner.jobs.active_job('island', 's01e04') is None


def test_the_live_transport_logs_where_the_producer_reads():
    """The provider transport was built with the engine's own log, captured
    before the job log was wired in, so fal.ai's explanation of a refusal never
    reached the job page."""
    import inspect
    from app import live_jobs
    source = inspect.getsource(live_jobs.run)
    assert source.index("pipeline.log = live_log") < source.index("pipeline.fal = DurableFal")
    assert "DurableFal(cfg, live_log," in source


# ── production carries on after the process it ran in went away ────────────

def _interrupted(store, digest='d1'):
    store.insert('series', {'id': 'island', 'title': 'Island'})
    return store.insert('production_jobs', {
        'series_id': 'island', 'episode_id': 's01e04', 'stages': ['references'], 'mode': 'live',
        'state': 'interrupted', 'requested_by': 'admin@example.test',
        'idempotency_key': 'live:island:s01e04:x', 'force': [],
        'progress': {'stage': 'references', 'input_digest': digest, 'audio_mode': 'voices',
                     'done': ['intake'], 'total': 3},
        'created_at': '2026-09-17T15:54:00+00:00', 'log': ''})


def test_an_episode_carries_on_after_the_worker_went_down(monkeypatch):
    """The worker lives in the server process: a restart stopped production
    mid-pack and it stayed stopped until somebody noticed."""
    from app import live_jobs
    store = StrictStore()
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(runner, 'store', store)
    monkeypatch.setattr(live_jobs, 'review', lambda *a: 'd1')
    seen = {}
    monkeypatch.setattr(runner.jobs, 'resume',
                        lambda *a, **k: seen.update(args=a, kw=k) or {'id': 'new-job'})
    _interrupted(store)
    assert live_jobs.resume_interrupted(runner.jobs) == ['new-job']
    assert seen['args'][:3] == ('island', 's01e04', 'admin@example.test')
    assert seen['kw'] == {'approved_digest': 'd1', 'approve_live': True, 'audio_mode': 'voices'}


def test_a_script_changed_since_the_approval_is_not_carried_on(monkeypatch):
    """What the producer approved is no longer what would be made."""
    from app import live_jobs
    store = StrictStore()
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(runner, 'store', store)
    monkeypatch.setattr(live_jobs, 'review', lambda *a: 'something-else')
    monkeypatch.setattr(runner.jobs, 'resume',
                        lambda *a, **k: pytest.fail('resumed work nobody approved'))
    _interrupted(store)
    assert live_jobs.resume_interrupted(runner.jobs) == []


def test_nothing_is_carried_on_where_paid_calls_are_off(monkeypatch):
    from app import live_jobs
    store = StrictStore()
    monkeypatch.setattr(settings, 'allow_paid', False)
    monkeypatch.setattr(runner, 'store', store)
    monkeypatch.setattr(runner.jobs, 'resume',
                        lambda *a, **k: pytest.fail('paid work started in a mock build'))
    _interrupted(store)
    assert live_jobs.resume_interrupted(runner.jobs) == []


def test_a_resume_that_cannot_work_is_recorded_once_and_stops(monkeypatch):
    """The watchdog retried every thirty seconds and appended a line to the job
    each time — hundreds of them — against a refusal that would never change."""
    from app import live_jobs
    store = StrictStore()
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(runner, 'store', store)
    monkeypatch.setattr(live_jobs, 'review', lambda *a: 'd1')
    attempts = {'n': 0}

    def refuse(*a, **k):
        attempts['n'] += 1
        raise ValueError('An image provider refuses wording like this: adrian/casual_resort_shirt.')

    monkeypatch.setattr(runner.jobs, 'resume', refuse)
    job = _interrupted(store)
    assert live_jobs.resume_interrupted(runner.jobs) == []
    row = store.get('production_jobs', {'id': job['id']})
    assert row['state'] == 'failed', 'handed to a person rather than retried forever'
    assert row['error'].count('could not carry on') == 1
    assert 'adrian/casual_resort_shirt' in row['error'], 'the reason, not just a class name'

    # The next pass leaves it alone.
    assert live_jobs.resume_interrupted(runner.jobs) == []
    assert attempts['n'] == 1


def test_carrying_on_gives_up_before_it_becomes_a_money_loop(monkeypatch):
    """A worker that dies the same way each time was resumed every thirty
    seconds, for ever, and each pass can spend."""
    from app import live_jobs
    store = StrictStore()
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(runner, 'store', store)
    monkeypatch.setattr(live_jobs, 'review', lambda *a: 'd1')
    monkeypatch.setattr(runner.jobs, 'resume', lambda *a, **k: {'id': 'again'})
    job = _interrupted(store)
    for _ in range(live_jobs.CARRY_ON_LIMIT):
        store.insert('generation_history', {
            'series_id': 'island', 'episode_id': 's01e04', 'entity_type': 'job',
            'entity_id': 'x', 'event': 'job.resumed_after_restart', 'detail': {},
            'actor': 'system'})
    assert live_jobs.resume_interrupted(runner.jobs) == []
    row = store.get('production_jobs', {'id': job['id']})
    assert row['state'] == 'failed'
    # The count is of attempts since the run last made something, so what it
    # gives up on is a run that produced nothing across all of them.
    assert 'without making anything' in row['error']


def test_carrying_on_is_allowed_while_it_is_still_making_progress(monkeypatch):
    from app import live_jobs
    store = StrictStore()
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(runner, 'store', store)
    monkeypatch.setattr(live_jobs, 'review', lambda *a: 'd1')
    monkeypatch.setattr(runner.jobs, 'resume', lambda *a, **k: {'id': 'again'})
    _interrupted(store)
    store.insert('generation_history', {
        'series_id': 'island', 'episode_id': 's01e04', 'entity_type': 'job',
        'entity_id': 'x', 'event': 'job.resumed_after_restart', 'detail': {}, 'actor': 'system'})
    assert live_jobs.resume_interrupted(runner.jobs) == ['again']


# ── a blip while renewing the lease is not a lost lease ────────────────────

class _Flaky:
    """A store whose update fails a given number of times, then works."""

    def __init__(self, failures, count=1):
        self.left, self.count, self.calls = failures, count, 0

    def update(self, *a, **k):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise RuntimeError('connection reset')
        return self.count


def _lease_with(store):
    lease = live_runtime.SeriesLease.__new__(live_runtime.SeriesLease)
    lease.store, lease.id, lease.owner = store, 'lease-1', 'owner-1'
    lease.stop, lease.lost = threading.Event(), threading.Event()
    return lease


def test_a_blip_while_renewing_does_not_kill_a_twenty_minute_run(monkeypatch):
    """One timeout anywhere in a long run ended the lease for good, and the
    worker died with "Worker stopped" part-way through a pack of forty."""
    store = _Flaky(failures=live_runtime.RENEWAL_ATTEMPTS - 1)
    lease = _lease_with(store)
    waits = {'n': 0}

    def wait(_seconds):
        waits['n'] += 1
        return waits['n'] > live_runtime.RENEWAL_ATTEMPTS   # stop after the recovery
    monkeypatch.setattr(lease.stop, 'wait', wait)
    lease._heartbeat()
    assert not lease.lost.is_set(), 'survived the failures and renewed'
    assert store.calls == live_runtime.RENEWAL_ATTEMPTS


def test_failing_to_renew_for_too_long_does_give_up(monkeypatch):
    """Not forever, though: the lease outlives a few misses, not many."""
    store = _Flaky(failures=99)
    lease = _lease_with(store)
    monkeypatch.setattr(lease.stop, 'wait', lambda _s: False)
    lease._heartbeat()
    assert lease.lost.is_set()
    assert store.calls == live_runtime.RENEWAL_ATTEMPTS


def test_another_worker_holding_the_lease_is_a_real_loss(monkeypatch):
    """Zero rows updated means somebody else owns it — never retried."""
    store = _Flaky(failures=0, count=0)
    lease = _lease_with(store)
    monkeypatch.setattr(lease.stop, 'wait', lambda _s: False)
    lease._heartbeat()
    assert lease.lost.is_set()
    assert store.calls == 1, 'given up at once, not after retries'


def test_a_request_that_produced_nothing_is_not_polled_for_ever(tmp_path, monkeypatch):
    """One portrait stopped a whole reference pack, permanently.

    fal.ai accepted the request, ran it and answered 422: it had no result to
    give. The take kept its request id and status "submitted", so every run
    afterwards re-read that same request and got the same 422. Nothing could
    have changed. Meanwhile the very same character's other angles went
    through in the same run, so the refusal is borderline, not a wall.
    """
    from app.live_providers import DurableFal, NO_OUTPUT_ATTEMPTS
    from app.provider_errors import ProviderFailure

    state = State(tmp_path)
    budget = Budget(10, state)
    cfg = type('C', (), {'fal_key': 'k' * 8, 'provider_input_mode': 'fal_storage'})()
    fal = DurableFal(cfg, lambda _m: None, state, budget, {}, set())

    waits = []
    submits = []

    class Refused(Exception):
        status_code = 422

    def accepted(*a, **k):
        submits.append(1)
        return _accepted(len(submits) - 1)

    monkeypatch.setattr(fal, '_wait', lambda *a, **k: waits.append(1) or (_ for _ in ()).throw(Refused()))
    monkeypatch.setattr('app.live_providers.requests.post', accepted)

    with pytest.raises(ProviderFailure, match='either attempt'):
        fal.run('fal-ai/x', {'prompt': 'a drowned woman, three-quarter right'}, 't1', 0.225,
                'ref drowned_woman/three_quarter_right', None)

    take = state.take('t1')
    assert len(waits) == NO_OUTPUT_ATTEMPTS, 'it gave up without a second attempt, or kept going'
    assert not take.get('request_id'), 'a request with no result must not be polled again'
    assert take['status'] == 'no_output'
    # The request ids are kept in the record; they are simply no longer
    # treated as results waiting to be collected.
    assert [r['request_id'] for r in take['no_output']] == ['req-0', 'req-1']
    # The estimate is charged, not quietly released: the request did run.
    assert state.data['spent_usd'] == pytest.approx(0.45)
    assert state.data['reserved_usd'] == 0


class _accepted:
    def __init__(self, n):
        self._n = n

    def raise_for_status(self):
        pass

    def json(self):
        return {'request_id': f'req-{self._n}'}


def test_a_restore_keeps_the_objects_it_can_prove_and_fetches_the_rest(tmp_path, monkeypatch):
    """Every resume pulled the whole checkpoint down the wire again.

    The local directory was emptied and refilled from storage on every start,
    and its name was a fresh id each time, so nothing could ever be reused by
    construction. Once the reference pack was large enough the browser was
    answered by the proxy instead of the studio: a 504, with the run's fate
    unknown. A local file whose bytes hash to the checksum the manifest names
    IS the checkpoint's own object; fetching it again proves nothing.
    """
    from app.live_runtime import Checkpoint

    r2 = R2Fake()
    objects = {name: data for name, data in
               (('state.json', b'{"a": 1}'), ('refs/adrian.png', b'a big picture'),
                ('refs/maya.png', b'another big picture'))}
    files = {}
    for name, data in objects.items():
        digest = hashlib.sha256(data).hexdigest()
        files[name] = {'sha256': digest, 'size': len(data)}
        r2.objects['series/s/_studio_runtime/live/objects/' + digest] = data

    root = tmp_path / 'worker'
    manifest = {'root': str(root), 'files': files}
    r2.objects['series/s/_studio_runtime/live/manifest.json'] = json.dumps(manifest).encode()

    cp = Checkpoint.__new__(Checkpoint)
    cp.client, cp.bucket = r2, 'bucket'
    cp.prefix = 'series/s/_studio_runtime/live/'
    cp.key = cp.prefix + 'manifest.json'
    cp.root, cp.check, cp.etag, cp.files = root, lambda: None, None, {}

    fetched = []
    original = r2.get_object
    def counted(Bucket, Key):
        fetched.append(Key)
        return original(Bucket=Bucket, Key=Key)
    r2.get_object = counted

    cp.restore()
    assert (root / 'refs/adrian.png').read_bytes() == b'a big picture'
    first = len([k for k in fetched if '/objects/' in k])
    assert first == 3

    # Second time round: the pictures are already here and provably right.
    fetched.clear()
    stray = root / 'refs' / 'left_over.png'
    stray.write_bytes(b'from some earlier run')
    cp.restore()
    assert [k for k in fetched if '/objects/' in k] == [], "it fetched what it already had"
    assert not stray.exists(), "a file the checkpoint does not name was kept"

    # A local file that does not match is not the checkpoint's, so it is replaced.
    fetched.clear()
    (root / 'refs/maya.png').write_bytes(b'corrupted by a killed run')
    cp.restore()
    assert len([k for k in fetched if '/objects/' in k]) == 1
    assert (root / 'refs/maya.png').read_bytes() == b'another big picture'


def test_the_studio_carries_itself_on_through_a_bad_minute(monkeypatch):
    """The producer was the retry mechanism, at ten-minute intervals, for days.

    Every stop this week ended a job as "failed" — a locked account, a five
    hundred from the model, a dropped connection — and nothing ever picked a
    failed job back up. Only "interrupted" was carried on, which is the one
    state those failures never reached. So the system had a recovery mechanism
    that could not reach any of its actual failures.
    """
    from app import live_jobs, runner

    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(live_jobs, 'review', lambda sid, eid: 'd1')
    monkeypatch.setattr(runner, 'history', lambda *a, **k: None)
    monkeypatch.setattr(live_jobs, '_waited_long_enough', lambda job: True)
    monkeypatch.setattr(live_jobs, '_attempts_since_progress', lambda runner, job: 0)

    def job(state, error):
        return {'id': 'j', 'series_id': 's', 'episode_id': 'e', 'mode': 'live', 'state': state,
                'error': error, 'requested_by': 'p@example.test', 'finished_at': '2026-01-01T00:00:00+00:00',
                'progress': {'input_digest': 'd1', 'audio_mode': 'native'}}

    def carry(state, error):
        rows = {'interrupted': [], 'failed': []}
        rows[state] = [job(state, error)]
        monkeypatch.setattr(runner.store, 'list',
                            lambda table, where=None, **k: rows.get((where or {}).get('state'), [])
                            if table == 'production_jobs' else [])
        started = []
        monkeypatch.setattr(runner.jobs, 'resume',
                            lambda *a, **k: started.append(1) or {'id': 'j2'})
        live_jobs.resume_interrupted(object())
        return bool(started)

    assert carry('failed', 'fal.ai HTTP 403: User is locked. Reason: TOP_UP.')
    assert carry('failed', 'InternalServerError. Production stopped.')
    assert carry('failed', 'RemoteProtocolError. Production stopped.')
    assert carry('interrupted', 'Worker stopped; saved provider requests will be reused.')

    # A failure that decided something is never asked again: it would change
    # nothing and cost money.
    assert not carry('failed', 'fal.ai HTTP 422 (content_policy_violation) · ref maya/x')
    assert not carry('failed', 'Generate the reference pack for the current series settings first.')
    assert not carry('failed', 'budget: spent $200 + next $1 > cap $200')
    assert not carry('failed', 'Something nobody has classified yet')


def test_a_run_that_is_getting_work_done_is_never_given_up_on(monkeypatch):
    """Three bad minutes across three days used up an episode's whole allowance.

    The count was of every attempt ever made, so it never reset — while a run
    plainly producing pictures was refused a fourth try.
    """
    from app import live_jobs, runner

    history = []
    monkeypatch.setattr(runner.store, 'list',
                        lambda table, where=None, **k: history if table == 'generation_history' else [])
    monkeypatch.setattr(live_jobs, '_made_so_far', lambda r, j: 40)
    job = {'series_id': 's', 'episode_id': 'e'}

    history[:] = [{'created_at': f'2026-01-01T0{i}:00:00', 'detail': {'made_by_then': 40}}
                  for i in range(3)]
    assert live_jobs._attempts_since_progress(runner, job) == 3, 'a stuck run is retried for ever'

    # The middle attempt was followed by real work, so the ones before it do
    # not count against the run any more.
    history[1]['detail'] = {'made_by_then': 12}
    assert live_jobs._attempts_since_progress(runner, job) == 1


def test_every_state_a_take_can_be_in_has_a_way_out(tmp_path, monkeypatch):
    """The invariant that was never written down, so was never checked.

    A take could reach a state the studio would not leave and the screen would
    not offer to leave either: a submission with no request id. It is the one
    case the studio must not resolve by itself — without a request id it
    cannot ask what happened, and guessing wrong pays twice for one picture —
    but the producer was never given the button, so the episode simply stayed
    there. Recording the decision is the exit, and it permits exactly one
    fresh submission of that one take.
    """
    from app.live_providers import DurableFal
    from app.provider_errors import ProviderFailure

    def stuck(reconciled):
        state = State(tmp_path / ('yes' if reconciled else 'no'))
        (tmp_path / ('yes' if reconciled else 'no')).mkdir(parents=True, exist_ok=True)
        state.data.update(reserved_usd=1)
        state.data['takes']['t1'] = {'status': 'reserved', 'estimated_cost': 1}
        state.save()
        return DurableFal(type('C', (), {'fal_key': 'k' * 8})(), lambda m: None, state,
                          Budget(10, state), None,
                          reconciled=({'t1'} if reconciled else ()))

    # Without the producer's decision it stops, and names the take so the
    # screen can offer that decision at all.
    fal = stuck(False)
    with pytest.raises(ProviderFailure, match='t1: submission outcome is unknown'):
        fal.run('fal-ai/x', {}, 't1', 1, 'ref', None)

    # With it, that one take is retired and sent again; the reserve is charged
    # rather than quietly released, because it may well have been billed.
    fal = stuck(True)
    posts = []
    monkeypatch.setattr('app.live_providers.requests.post',
                        lambda *a, **kw: posts.append(1) or _accepted(0))
    monkeypatch.setattr(fal, '_wait', lambda *a: {'images': [{'url': 'u'}]})
    result, take = fal.run('fal-ai/x', {}, 't1', 1, 'ref', None)
    assert result == {'images': [{'url': 'u'}]} and len(posts) == 1
    assert take['status'] == 'succeeded'
    assert fal.state.data['spent_usd'] >= 1, 'a possibly billed reserve was quietly released'


def test_an_interrupted_voice_call_is_not_a_life_sentence(tmp_path, monkeypatch):
    """The same dead end, one stage past where anyone had got to.

    An interrupted call to a media provider left a record nothing could
    clear: every resume afterwards was refused over it, and no screen in the
    studio offered the decision that would have cleared it. It was found by
    reading the states rather than by an episode dying on it — which is what
    should have happened with all of them.
    """
    from app import preflight
    from serial.paid_calls import PaidCalls

    import hashlib
    state = State(tmp_path)
    params = {'line': 'aaa'}
    key = 'elevenlabs:' + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    state.data['paid_operations'] = {key: {'provider': 'elevenlabs', 'status': 'reserved',
                                           'estimated_cost': 0.5}}
    state.data['reserved_usd'] = 0.5

    # Named, so a screen can offer the one decision that clears it.
    blocked = preflight.recovery_problems(['voice'], state)
    assert any(key in message for message in blocked)

    # And once the producer has recorded it, the block lifts for that one call.
    # (What is left is the ordinary complaint that earlier stages are unfinished.)
    assert not [m for m in preflight.recovery_problems(['voice'], state, {key}) if key in m]

    made = []
    guard = PaidCalls(state, Budget(10, state), reconciled={key})
    result = guard.once('elevenlabs', params, 0.5,
                        lambda: made.append(1) or {'local_path': 'x'})
    assert result == {'local_path': 'x'} and len(made) == 1
    assert state.data['spent_usd'] >= 0.5, 'a possibly billed reserve was quietly released'

    # Without that decision it still refuses, because only a repeat here can
    # pay twice for one thing.
    again = State(tmp_path / 'other')
    (tmp_path / 'other').mkdir(parents=True, exist_ok=True)
    again.data['paid_operations'] = {key: {'provider': 'elevenlabs', 'status': 'reserved'}}
    with pytest.raises(RuntimeError, match='interrupted'):
        PaidCalls(again, Budget(10, again)).once(
            'elevenlabs', params, 0.5, lambda: pytest.fail('repeated blindly'))


def test_duration_only_final_qa_failure_is_resumable_after_boundary_fix():
    """The old strict 120.00s gate rejected a 120.23s encoded master. After
    the tolerance fix, startup recovery must be allowed to re-run QA/delivery
    without repeating paid generation."""
    from app import live_jobs

    assert live_jobs._worth_another_go({
        'error': 'RuntimeError: QA failed (duration_range), report /tmp/report.json'
    })
    assert not live_jobs._worth_another_go({
        'error': 'RuntimeError: QA failed (scene_qc_all_passed), report /tmp/report.json'
    })


def test_a_restart_is_noticed_without_anyone_opening_a_page(monkeypatch):
    """The recovery could only run after someone came to look.

    A worker writes its job's state from inside its own process, so a process
    that goes away leaves the row saying "running" for ever — and the only
    thing that ever corrected it was a person opening the Jobs page. The
    background pass looked at "interrupted" and "failed", which that row would
    never reach on its own. So the studio recovered from a restart only after
    the producer arrived, which is exactly what it was meant to spare them.
    """
    from app import live_jobs, runner

    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(live_jobs, 'review', lambda sid, eid: 'd1')
    rows = {'running': [{'id': 'j', 'series_id': 'island', 'episode_id': 's01e04',
                         'mode': 'live', 'state': 'running',
                         'progress': {'input_digest': 'd1'}}]}
    monkeypatch.setattr(runner.store, 'list',
                        lambda table, where=None, **k: rows.get((where or {}).get('state'), [])
                        if table == 'production_jobs' else [])
    looked = []
    monkeypatch.setattr(runner.jobs, 'reconcile_abandoned', lambda sid: looked.append(sid))
    monkeypatch.setattr(runner.jobs, 'resume', lambda *a, **k: {'id': 'j2'})

    live_jobs.resume_interrupted(object())
    assert looked == ['island'], 'a job left running by a dead worker is never looked at'

    # One unreadable series must not stop the others recovering.
    def explode(sid):
        looked.append(sid)
        raise RuntimeError('supabase is having a moment')

    monkeypatch.setattr(runner.jobs, 'reconcile_abandoned', explode)
    looked.clear()
    live_jobs.resume_interrupted(object())
    assert looked == ['island']


def test_a_scene_the_check_marked_down_has_a_way_past_it(tmp_path, monkeypatch):
    """The engine named two ways past; the studio implemented the costly one.

    A scene the quality check keeps failing could only be forced again from
    the studio — another paid attempt at the same prompt, which can land in
    the same place. The other way the engine offers, letting the best attempt
    stand, existed as a command-line flag and nowhere a producer could reach.
    So the one state the first full keyframe run reached had one exit, and it
    was a loop.
    """
    from serial.config import Config
    from serial.pipeline import Pipeline
    from app.config import PIPELINE_DIR

    cfg = Config.load(PIPELINE_DIR, live=False)
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.cfg, pipeline.accept_weak = cfg, False

    assert not pipeline._weak_is_allowed('sc13')
    cfg.accepted_weak = {'sc13'}
    assert pipeline._weak_is_allowed('sc13')
    assert not pipeline._weak_is_allowed('sc14'), 'accepting one scene accepted them all'

    # The run-wide flag still works, and a run without the attribute at all
    # behaves as it always did rather than failing.
    pipeline.accept_weak = True
    del cfg.accepted_weak
    assert pipeline._weak_is_allowed('sc99')
    pipeline.accept_weak = False
    assert not pipeline._weak_is_allowed('sc99')


# ---------- music under the scenes ----------

def _music_series(monkeypatch, fmt=None):
    store = StrictStore()
    store.insert('series', {'id': 'island', 'title': 'Island', 'format': dict(fmt or {})})
    monkeypatch.setattr(web, 'store', store)
    monkeypatch.setattr(web, 'require_admin', lambda request: {'email': 'admin@example.test'})
    monkeypatch.setattr(web, 'history', lambda *a, **kw: None)
    return store


def _settings(music):
    # Called directly, unfilled Form() defaults arrive as marker objects, so
    # every field the route reads has to be named here.
    return web.series_settings(None, 'island', title='Island', logline='', genre='',
                               language='en-US', captions='none', music=music, budget='50',
                               regenerations='2', min_scenes='12', max_scenes='18',
                               min_seconds='', max_seconds='', style_sentence='',
                               camera_rules='', color_rules='', negative_image='',
                               negative_video='', video_model='', picture='')


def test_a_series_cannot_be_set_to_music_it_has_not_uploaded(monkeypatch):
    """Otherwise the setting is accepted and the episode fails at assembly,
    an hour of generated video later."""
    store = _music_series(monkeypatch)
    response = _settings('files')
    assert response.status_code == 303
    assert 'err=' in response.headers['location']
    assert (store.get('series', {'id': 'island'})['format'] or {}).get('music') is None


def test_generated_music_needs_nothing_uploaded(monkeypatch):
    store = _music_series(monkeypatch)
    assert _settings('generate').status_code == 303
    assert store.get('series', {'id': 'island'})['format']['music'] == 'generate'


def test_an_unknown_music_setting_is_refused(monkeypatch):
    store = _music_series(monkeypatch)
    assert 'err=' in _settings('loud').headers['location']
    assert (store.get('series', {'id': 'island'})['format'] or {}).get('music') is None


class _Bed:
    def __init__(self, filename, payload=b'x' * 64):
        self.filename, self._payload = filename, payload

    async def read(self):
        return self._payload


def _upload(filename, level='calm', payload=b'x' * 64):
    import asyncio
    return asyncio.run(web.upload_music_bed(None, 'island', level=level, bed=_Bed(filename, payload)))


def test_a_music_bed_that_is_not_audio_is_refused_before_it_is_stored(monkeypatch):
    store = _music_series(monkeypatch)
    assert 'err=' in _upload('bed.exe').headers['location']
    assert (store.get('series', {'id': 'island'})['format'] or {}).get('music_beds') is None


def test_an_empty_music_bed_is_refused(monkeypatch):
    store = _music_series(monkeypatch)
    assert 'err=' in _upload('bed.mp3', payload=b'').headers['location']
    assert (store.get('series', {'id': 'island'})['format'] or {}).get('music_beds') is None


def test_an_uploaded_bed_is_remembered_against_its_level(monkeypatch, tmp_path):
    store = _music_series(monkeypatch)
    kept = {}

    class _R2:
        def __init__(self, *a, **kw):
            pass

        def put(self, path, key):
            kept[key] = path.read_bytes()
            return key

    monkeypatch.setattr('serial.storage.R2', _R2)
    assert 'ok=' in _upload('theme.mp3', level='taut').headers['location']
    beds = store.get('series', {'id': 'island'})['format']['music_beds']
    assert beds == {'taut': 'series/island/music/taut.mp3'}
    assert kept['series/island/music/taut.mp3'] == b'x' * 64
    # And now the series may be set to use its own music.
    assert _settings('files').status_code == 303
    assert store.get('series', {'id': 'island'})['format']['music'] == 'files'


# ---------- the button must not wait for the download ----------

def test_pressing_resume_returns_before_the_saved_work_is_fetched(monkeypatch):
    """Fetching a started episode's keyframes and clips takes minutes, and
    after a deploy the disk is empty so all of it comes down again. Doing it
    inside the request put a gateway timeout in front of the producer, on an
    episode one stage from finished."""
    import threading as _threading
    from app import live_jobs

    store = StrictStore()
    store.insert('scenes', {'series_id': 'island', 'episode_id': 's01e04', 'scene_id': 'sc01'})
    fetching, released = _threading.Event(), _threading.Event()

    class _Pkg:
        series = {}
        checksums = {}

        def limits(self, cfg, episode_id=None):
            return {'budget': 50.0}

    class _Lease:
        owner = 'worker-1'

        def __init__(self, *a, **kw):
            pass

        def check(self):
            pass

        def close(self):
            pass

    class _Checkpoint:
        def __init__(self, *a, **kw):
            self.root = Path('/nonexistent')

        def restore(self):
            fetching.set()
            assert released.wait(5), 'the worker never got to fetch anything'

        def save(self):
            pass

    monkeypatch.setattr(runner, 'store', store)
    monkeypatch.setattr(live_jobs, 'materialize', lambda sid: Path('/nonexistent'))
    monkeypatch.setattr(live_jobs.shutil, 'copytree', lambda src, dst: dst)
    monkeypatch.setattr(live_jobs, 'SeriesPackage', lambda root: _Pkg())
    monkeypatch.setattr(live_jobs, 'configuration', lambda *a, **kw: SimpleNamespace())
    monkeypatch.setattr(live_jobs, 'video_model', lambda pkg: '')
    monkeypatch.setattr(live_jobs, 'picture', lambda pkg: '')
    monkeypatch.setattr(live_jobs, 'package_digest', lambda pkg, eid: 'digest')
    monkeypatch.setattr(live_jobs.preflight, 'problems', lambda *a, **kw: [])
    monkeypatch.setattr(live_jobs.preflight, 'voice_problems', lambda *a, **kw: [])
    monkeypatch.setattr(live_jobs, 'SeriesLease', _Lease)
    monkeypatch.setattr(live_jobs, 'Checkpoint', _Checkpoint)
    monkeypatch.setattr(live_jobs, 'runtime_root', lambda: Path('/nonexistent'))
    monkeypatch.setattr(live_jobs, 'run', lambda *a, **kw: None)

    manager = SimpleNamespace(_controls={}, _threads={})
    try:
        job = live_jobs.start(manager, 'island', 's01e04', ['video'], 'admin@example.test',
                              [], 'digest', True, 'native')
        # Answered while the worker is still downloading.
        assert fetching.wait(5), 'the worker never started fetching'
        assert job['state'] == 'queued'
        assert store.get('production_jobs', {'id': job['id']})['state'] == 'queued'
    finally:
        released.set()
