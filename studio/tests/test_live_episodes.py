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
    with pytest.raises(RuntimeError, match='reconciliation'):
        PaidCalls(restored, Budget(10, restored)).once('tts', {'text': 'hello'}, 1, interrupted)
    assert len(called) == 1
    assert restored.data['reserved_usd'] == 1


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
