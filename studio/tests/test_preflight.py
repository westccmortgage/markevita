"""Production audit regressions: local validation never submits paid work."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app import integrations, live_jobs, preflight, runner, web
from app.config import settings
from serial.config import Config
from serial.package import SeriesPackage
from serial.state import State
from test_live_episodes import short_package
from test_i18n import language_ui


def configured():
    cfg = Config(mode='live', allow_paid_env=True, anthropic_api_key='test', fal_key='test:secret',
                 elevenlabs_api_key='test', r2_account_id='test', r2_access_key_id='test',
                 r2_secret_access_key='test', r2_bucket='test')
    cfg.native_dialogue = True
    return cfg


def test_preflight_collects_all_configuration_problems(short_package, monkeypatch):
    _, sid, eid = short_package
    pkg = SeriesPackage(live_jobs.materialize(sid))
    cfg = configured()
    cfg.fal_key = ''
    cfg.fal_video_model = 'bytedance/seedance-2.0/image-to-video'
    cfg.video_resolution = 'typo'
    cfg.provider_input_mode = 'typo'
    monkeypatch.setattr(preflight.shutil, 'which', lambda name: None)
    errors = '\n'.join(preflight.problems(cfg, runner.DEFAULT_STAGES, pkg))
    for message in ('FAL_KEY', 'FAL_VIDEO_MODEL', 'VIDEO_RESOLUTION', 'PROVIDER_INPUT_MODE', 'ffmpeg', 'ffprobe'):
        assert message in errors


def test_default_adapter_settings_pass_without_network(short_package, monkeypatch):
    _, sid, eid = short_package
    monkeypatch.setattr(preflight.shutil, 'which', lambda name: '/usr/bin/' + name)
    assert preflight.problems(configured(), runner.DEFAULT_STAGES, SeriesPackage(live_jobs.materialize(sid))) == []


def test_stage_dependencies_checked_before_paid_work(tmp_path):
    state = State(tmp_path)
    assert 'keyframes' in '\n'.join(preflight.recovery_problems(['video'], state))
    state.data['stages']['keyframes'] = 'done'
    assert preflight.recovery_problems(['video'], state) == []
    assert preflight.recovery_problems(runner.DEFAULT_STAGES, State(tmp_path)) == []


def test_unknown_saved_operation_is_caught_at_admission(tmp_path):
    state = State(tmp_path)
    state.data['takes']['t1'] = {'status': 'reserved'}
    assert 'reconciliation' in '\n'.join(preflight.recovery_problems(runner.DEFAULT_STAGES, state))
    state.data['takes']['t1']['request_id'] = 'saved-id'
    assert preflight.recovery_problems(runner.DEFAULT_STAGES, state) == []
    state.data['paid_operations'] = {'el:abc': {'status': 'reserved'}}
    assert 'paid request' in '\n'.join(preflight.recovery_problems(runner.DEFAULT_STAGES, state))


def test_known_pre_queue_refusal_can_reach_resume_recovery(tmp_path):
    state = State(tmp_path)
    state.data['takes']['t1'] = {'status': 'reserved', 'provider': 'fal.ai', 'endpoint': 'fal-ai/nano-banana-2/edit',
        'provider_error': {'provider': 'fal.ai', 'phase': 'submit', 'http_status': 403}}
    assert preflight.recovery_problems(runner.DEFAULT_STAGES, state) == []


def test_check_action_does_not_create_job_or_require_paid_approval(short_package, monkeypatch):
    store, sid, eid = short_package
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(live_jobs, 'configuration', lambda mode, model=None: configured())
    monkeypatch.setattr(web, 'require_admin', lambda r: {'email': 'tester'})
    monkeypatch.setattr(web, '_check_form', lambda *a: None)
    monkeypatch.setattr(runner.jobs, 'start', lambda *a, **k: pytest.fail('check must not start production'))
    monkeypatch.setattr(live_jobs, 'SeriesLease', lambda *a: pytest.fail('check must not acquire a lease'))
    r = web.production_control(None, sid, eid, action='check', stages=runner.DEFAULT_STAGES,
        force='', csrf_token='test', approve_live='', approved_digest='', audio_mode='native')
    assert r.status_code == 303 and 'Local production checks passed' in parse_qs(urlsplit(r.headers['location']).query)['ok'][0]
    assert store.list('production_jobs') == []


def test_admission_storage_exception_is_safe_in_ui(short_package, monkeypatch):
    _, sid, eid = short_package
    monkeypatch.setattr(settings, 'allow_paid', True)
    monkeypatch.setattr(web, 'require_admin', lambda r: {'email': 'tester'})
    monkeypatch.setattr(web, '_check_form', lambda *a: None)
    def fail(*a, **k):
        raise RuntimeError('storage request with private-token-and-url')
    monkeypatch.setattr(runner.jobs, 'start', fail)
    r = web.production_control(None, sid, eid, action='start', stages=runner.DEFAULT_STAGES,
        force='', csrf_token='test', approve_live='yes', approved_digest='digest', audio_mode='voices')
    assert r.status_code == 303 and 'private-token' not in r.headers['location']
    assert 'Check Jobs' in parse_qs(urlsplit(r.headers['location']).query)['err'][0]


@pytest.mark.parametrize('status', [401, 403, 404, 500])
def test_supabase_probe_rejects_error_status(status, monkeypatch):
    monkeypatch.setenv('SUPABASE_URL', 'https://example.test')
    monkeypatch.setenv('SUPABASE_SERVICE_ROLE_KEY', 'private-test')
    monkeypatch.setattr('httpx.get', lambda *a, **k: SimpleNamespace(status_code=status))
    assert integrations._probe('supabase') == f'HTTP {status}'


def test_failed_test_never_shows_connected(short_package, monkeypatch):
    store, _, _ = short_package
    monkeypatch.setattr(integrations, 'store', store)
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'private-test')
    monkeypatch.setattr(settings, 'allow_connection_tests', True)
    monkeypatch.setattr(integrations, '_probe', lambda _: 'HTTP 403')
    result = integrations.test_connection('elevenlabs')
    assert result['state'] == 'Check failed' and not result['connected']
    monkeypatch.setattr(settings, 'allow_connection_tests', False)
    result = integrations.test_connection('elevenlabs')
    assert result['state'] == 'Configured — access not verified' and result['last_error'] is None


def test_superseded_pause_is_not_current_job(short_package):
    store, sid, eid = short_package
    store.insert('production_jobs', {'series_id': sid, 'episode_id': eid, 'state': 'paused', 'mode': 'live', 'created_at': '2026-01-01'})
    store.insert('production_jobs', {'series_id': sid, 'episode_id': eid, 'state': 'done', 'mode': 'live', 'created_at': '2026-01-02'})
    assert runner.jobs.active_job(sid, eid) is None


def test_resume_form_keeps_saved_character_voice_mode(language_ui, monkeypatch):
    client, _, sid, eid = language_ui
    monkeypatch.setattr(settings, 'allow_paid', True)
    runtime = runner.episode_runtime(sid, eid)
    runtime['audio_mode'] = 'voices'
    monkeypatch.setattr(runner, 'episode_runtime', lambda *a: runtime)
    text = client.get(f'/studio/series/{sid}/episodes/{eid}').text
    assert '<option value="voices" selected>' in text
    assert '<option value="native" selected>' not in text


def test_api_resume_passes_the_same_review_and_audio_as_start(monkeypatch):
    from app import api
    captured = {}
    def resume(*args, **kwargs):
        captured.update(kwargs)
        return {'id': 'saved-job'}
    monkeypatch.setattr(runner.jobs, 'resume', resume)
    response = api.resume('miami', 's01e01_v2',
        {'approved_digest': 'reviewed', 'approve_live': True, 'audio_mode': 'voices'}, {'email': 'tester'})
    assert response['job']['id'] == 'saved-job'
    assert captured == {'approved_digest': 'reviewed', 'approve_live': True, 'audio_mode': 'voices'}


def test_dashboard_does_not_count_superseded_pause(language_ui):
    client, store, sid, eid = language_ui
    store.insert('production_jobs', {'id': 'old-pause', 'series_id': sid, 'episode_id': eid, 'state': 'paused', 'mode': 'live', 'created_at': '2026-01-01'})
    store.insert('production_jobs', {'id': 'new-done', 'series_id': sid, 'episode_id': eid, 'state': 'done', 'mode': 'live', 'created_at': '2026-01-02'})
    assert '/jobs/old-pause' not in client.get('/studio/').text


def test_multiple_preflight_errors_are_readable_in_russian():
    from app import i18n
    context = {'request': SimpleNamespace(cookies={i18n.COOKIE: 'ru'})}
    result = i18n.notice(context, 'Configure before production: FAL_KEY\nAssign ElevenLabs voices for maya, nora, or choose Native scene audio.')
    assert 'До запуска настройте: FAL_KEY\nНазначьте голоса ElevenLabs' in result
