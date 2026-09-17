"""Credential diagnostics cannot expose secrets or submit paid generations."""
import json
from types import SimpleNamespace

import httpx
import pytest

from app import integrations, preflight, web
from app.config import settings
from serial.config import Config
from serial.fal_auth import key_problem, key_id_prefix
from test_live_episodes import short_package
from test_i18n import language_ui

KEY = '580f46c9-1111-4111-8111-111111111111:private-test-secret'


@pytest.mark.parametrize('value', ['', '580f46c9-1111-4111-8111-111111111111',
    'id:', ':secret', 'id:secret:extra', 'Key ' + KEY, 'Bearer ' + KEY,
    'FAL_KEY=' + KEY, '"' + KEY + '"', 'id:********', 'id:se cret', 'id:se\ncret', 'id:секрет'])
def test_malformed_keys_produce_only_safe_instructions(value):
    assert key_problem(value)
    assert 'private-test-secret' not in key_problem(value)
    assert key_id_prefix(value) is None


def test_config_preserves_runtime_key_over_dotenv_and_trims_clipboard_whitespace(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('FAL_KEY=old-id:old-secret\n')
    monkeypatch.setenv('FAL_KEY', ' \n' + KEY + '\t')
    cfg = Config.load(tmp_path, live=True)
    assert cfg.fal_key == KEY
    assert key_problem(cfg.fal_key) is None
    assert key_id_prefix(cfg.fal_key) == '580f46c9'


def test_preflight_catches_bad_key_without_a_provider_call(monkeypatch):
    cfg = Config(mode='live', fal_key='Key ' + KEY, r2_account_id='a',
                 r2_access_key_id='a', r2_secret_access_key='a', r2_bucket='a')
    pkg = SimpleNamespace(limits=lambda _: {'budget': 50}, characters={})
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: pytest.fail('local check contacted provider'))
    assert any('header or variable prefix' in e for e in preflight.problems(cfg, ['references'], pkg))


@pytest.mark.parametrize('status', [200, 301, 401, 403, 404, 429, 500])
def test_probe_uses_authenticated_metadata_only(status, monkeypatch):
    monkeypatch.setenv('FAL_KEY', ' ' + KEY + ' ')
    def get(url, **kwargs):
        assert url == 'https://api.fal.ai/v1/models/pricing'
        assert kwargs['headers'] == {'Authorization': 'Key ' + KEY}
        assert kwargs['params'] == {'endpoint_id': 'fal-ai/nano-banana-2/edit'}
        assert kwargs['follow_redirects'] is False
        assert kwargs['timeout'] == 10
        return SimpleNamespace(status_code=status, json=lambda: {'prices': [{'endpoint_id': 'fal-ai/nano-banana-2/edit'}]})
    monkeypatch.setattr(httpx, 'get', get)
    result = integrations._probe('fal')
    assert (result is None) == (status == 200)
    if result:
        assert f'HTTP {status}' in result and 'private-test-secret' not in result


def test_probe_rejects_html_and_does_not_echo_credentials(monkeypatch):
    monkeypatch.setenv('FAL_KEY', KEY)
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: {}))
    assert 'unexpected' in integrations._probe('fal')
    def fail(*a, **k):
        raise httpx.ConnectError('private-test-secret')
    monkeypatch.setattr(httpx, 'get', fail)
    assert integrations._probe('fal') == 'Connection check failed (ConnectError).'


def test_check_is_offline_until_enabled_and_invalidated_on_key_change(short_package, monkeypatch):
    store, _, _ = short_package
    monkeypatch.setattr(integrations, 'store', store)
    monkeypatch.setattr(integrations, '_fal_check', None)
    monkeypatch.setenv('FAL_KEY', KEY)
    monkeypatch.setattr(settings, 'allow_connection_tests', False)
    calls = []
    monkeypatch.setattr(integrations, '_probe', lambda p: calls.append(p))
    result = integrations.test_connection('fal')
    assert calls == [] and not result['auth_verified']
    assert result['state'] == 'Configured — access not verified'
    monkeypatch.setattr(settings, 'allow_connection_tests', True)
    result = integrations.test_connection('fal')
    assert calls == ['fal'] and result['auth_verified']
    assert result['key_id_prefix'] == '580f46c9'
    assert 'private-test-secret' not in json.dumps(result)
    assert 'private-test-secret' not in json.dumps(store.list('integration_status'))
    monkeypatch.setenv('FAL_KEY', KEY.replace('private-test-secret', 'replacement-secret'))
    # The same public key ID with a different secret also invalidates success.
    assert not integrations.status('fal')['auth_verified']
    assert integrations.status('fal')['last_test_at'] is None


def test_russian_page_explains_loaded_key_and_does_not_claim_offline_auth(language_ui, monkeypatch):
    client, _, _, _ = language_ui
    monkeypatch.setenv('FAL_KEY', KEY)
    monkeypatch.setattr(integrations, '_fal_check', None)
    monkeypatch.setattr(settings, 'allow_connection_tests', False)
    client.post('/studio/ui-language', data={'language': 'ru', 'next': '/studio/integrations'})
    page = client.get('/studio/integrations')
    assert page.status_code == 200
    assert '580f46c9' in page.text and 'private-test-secret' not in page.text
    assert 'ID загруженного ключа начинается с' in page.text
    response = client.post('/studio/integrations/fal/test')
    assert response.status_code == 303 and 'connected.' not in response.headers['location']


def test_malformed_key_is_not_probed_even_when_connections_enabled(short_package, monkeypatch):
    store, _, _ = short_package
    monkeypatch.setattr(integrations, 'store', store)
    monkeypatch.setenv('FAL_KEY', 'Key ' + KEY)
    monkeypatch.setattr(settings, 'allow_connection_tests', True)
    monkeypatch.setattr(integrations, '_probe', lambda *a: pytest.fail('malformed key sent to provider'))
    result = integrations.test_connection('fal')
    assert not result['connected'] and not result['auth_verified']


def test_startup_report_does_not_print_secret(monkeypatch, capsys):
    import asyncio
    from app.main import report_configuration
    monkeypatch.setenv('FAL_KEY', KEY)
    asyncio.run(report_configuration())
    output = capsys.readouterr().out
    assert 'key ID prefix=580f46c9; format=OK' in output
    assert 'provider access not checked' in output
    assert 'private-test-secret' not in output
