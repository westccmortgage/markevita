"""Public provider errors retain useful codes without exposing payloads or charging twice."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import requests
from fal_client.client import FalClientHTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import i18n
from app.live_providers import DurableFal
from app.provider_errors import ProviderFailure, fal_diagnostic
from serial.costs import Budget
from serial.state import State

RID = "3418ab11-f6e9-43f2-a46b-b595f0fb2012"


def test_saved_result_error_is_safe_and_resume_never_resubmits(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.data.update(reserved_usd=1)
    state.data["takes"]["t1"] = {"status": "submitted", "request_id": RID}
    state.save()
    monkeypatch.setattr("app.live_providers.requests.post", lambda *a, **kw: pytest.fail("duplicate paid submit"))
    fal = DurableFal(SimpleNamespace(fal_key="PRIVATE_KEY"), lambda m: None, state, Budget(10, state), None)
    response = httpx.Response(422, json={"detail": [{"type": "file_download_error", "msg": "PRIVATE_PROVIDER_TEXT",
        "input": "https://private.test/image?token=PRIVATE_TOKEN"}]})
    exc = FalClientHTTPError("PRIVATE_PROVIDER_TEXT", 422, {}, response)
    def failed_wait(endpoint, rid):
        assert rid == RID
        raise exc
    monkeypatch.setattr(fal, "_wait", failed_wait)
    with pytest.raises(ProviderFailure) as caught:
        fal.run("fal-ai/test", {}, "t1", 1, "image", None)
    public = str(caught.value)
    assert "HTTP 422" in public and "file_download_error" in public and RID in public
    saved = State(tmp_path).data["takes"]["t1"]
    assert saved["request_id"] == RID and saved["status"] == "submitted"
    assert saved["provider_error"]["message"] == public
    assert "PRIVATE" not in json.dumps(saved["provider_error"])
    assert "https://" not in public
    ru = i18n.notice({"request": SimpleNamespace(cookies={i18n.COOKIE: "ru"})}, public)
    assert "не смог скачать" in ru and RID in ru and "HTTP 422" in ru
    monkeypatch.setattr(fal, "_wait", lambda *a: {"images": [{"url": "https://example.test/saved.png"}]})
    fal.run("fal-ai/test", {}, "t1", 1, "image", None)
    fal.run("fal-ai/test", {}, "t1", 1, "image", None)
    assert "provider_error" not in state.data["takes"]["t1"]
    assert state.data["spent_usd"] == 1 and state.data["reserved_usd"] == 0
    assert len(state.data["cost_log"]) == 1


@pytest.mark.parametrize('status', [401, 402, 403])
def test_explicit_refusal_releases_reservation_and_can_be_resumed(tmp_path, monkeypatch, status):
    state = State(tmp_path)
    state.data['reserved_usd'] = 2  # A different request's reservation is retained.
    fal = DurableFal(SimpleNamespace(fal_key="PRIVATE_KEY"), lambda m: None, state, Budget(10, state), None)
    calls = []
    def denied(*a, **kw):
        assert kw['allow_redirects'] is False
        calls.append(1)
        response = requests.Response(); response.status_code = status
        response._content = b'{"detail":"PRIVATE_BILLING_DETAIL"}'
        return response
    monkeypatch.setattr("app.live_providers.requests.post", denied)
    for expected_calls in (1, 2):
        with pytest.raises(ProviderFailure, match=f"HTTP {status}"):
            fal.run("fal-ai/test", {}, "t1", 1, "image", None)
        assert len(calls) == expected_calls  # Never retries inside a run.
        state = State(tmp_path)
        assert state.data['reserved_usd'] == 2 and state.data['spent_usd'] == 0
        assert state.take('t1')['status'] == 'submission_rejected'
        fal = DurableFal(SimpleNamespace(fal_key="PRIVATE_KEY"), lambda m: None, state, Budget(10, state), None)
    info = state.take('t1')["provider_error"]
    assert info["phase"] == "submit" and "PRIVATE" not in json.dumps(info)
    response = requests.Response(); response.status_code = 200
    response._content = json.dumps({'request_id': RID}).encode()
    monkeypatch.setattr('app.live_providers.requests.post', lambda *a, **kw: response)
    monkeypatch.setattr(fal, '_wait', lambda *a: {'video': {'url': 'https://example.test/saved.mp4'}})
    fal.run('fal-ai/test', {}, 't1', 1, 'image', None)
    fal.run('fal-ai/test', {}, 't1', 1, 'image', None)
    assert state.data['reserved_usd'] == 2 and state.data['spent_usd'] == 1
    assert len(state.take('t1')['submission_rejections']) == 2
    assert len(state.data['cost_log']) == 3


def legacy_refusal(tmp_path, phase='submit', status=403):
    state = State(tmp_path)
    state.data['reserved_usd'] = 1
    state.data['takes']['t1'] = {
        'status': 'reserved', 'provider': 'fal.ai', 'endpoint': 'fal-ai/test', 'estimated_cost': 1,
        'provider_error': {'provider': 'fal.ai', 'phase': phase, 'http_status': status, 'error_type': None}}
    state.save()
    return state


def test_existing_403_checkpoint_recovers_but_subsequent_timeout_stays_blocked(tmp_path, monkeypatch):
    state = legacy_refusal(tmp_path)
    calls = []
    def timeout(*a, **kw):
        calls.append(1)
        saved = State(tmp_path)
        assert saved.take('t1')['status'] == 'reserved'
        assert 'provider_error' not in saved.take('t1')
        assert len(saved.take('t1')['submission_rejections']) == 1
        assert saved.data['reserved_usd'] == 1
        raise requests.exceptions.ReadTimeout('PRIVATE_URL')
    monkeypatch.setattr('app.live_providers.requests.post', timeout)
    fal = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, state, Budget(10, state), None)
    with pytest.raises(ProviderFailure):
        fal.run('fal-ai/test', {}, 't1', 1, 'image', None)
    restored = State(tmp_path)
    resumed = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, restored, Budget(10, restored), None)
    with pytest.raises(RuntimeError, match='unknown'):
        resumed.run('fal-ai/test', {}, 't1', 1, 'image', None)
    assert len(calls) == 1
    assert restored.data['reserved_usd'] == 1 and restored.data['spent_usd'] == 0
    assert len(restored.data['cost_log']) == 1


@pytest.mark.parametrize('status,body', [
    (403, {'request_id': RID}), (403, {'status_url': 'https://private.test/status'}),
    (403, {'error_type': 'file_download_error'}), (403, None),
    (422, {'detail': 'bad input'}), (429, {'detail': 'limit'}), (500, {'detail': 'server failure'})])
def test_ambiguous_submit_errors_remain_blocked(tmp_path, monkeypatch, status, body):
    state = State(tmp_path)
    calls = []
    def failed(*a, **kw):
        calls.append(1)
        response = requests.Response(); response.status_code = status
        response._content = json.dumps(body).encode() if body is not None else b'<html>error</html>'
        return response
    monkeypatch.setattr('app.live_providers.requests.post', failed)
    fal = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, state, Budget(10, state), None)
    with pytest.raises(ProviderFailure):
        fal.run('fal-ai/test', {}, 't1', 1, 'image', None)
    restored = State(tmp_path)
    resumed = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, restored, Budget(10, restored), None)
    with pytest.raises(RuntimeError, match='unknown'):
        resumed.run('fal-ai/test', {}, 't1', 1, 'image', None)
    assert len(calls) == 1 and restored.data['reserved_usd'] == 1
    assert not restored.data['cost_log']


@pytest.mark.parametrize('phase,status', [('collect', 403), ('submit', 422), ('submit', 500), ('submit', None)])
def test_legacy_uncertain_errors_are_not_migrated(tmp_path, monkeypatch, phase, status):
    state = legacy_refusal(tmp_path, phase, status)
    monkeypatch.setattr('app.live_providers.requests.post', lambda *a, **kw: pytest.fail('unsafe submit'))
    fal = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, state, Budget(10, state), None)
    with pytest.raises(RuntimeError, match='unknown'):
        fal.run('fal-ai/test', {}, 't1', 1, 'image', None)
    assert state.data['reserved_usd'] == 1


def test_collect_403_keeps_request_id_and_polls_on_resume(tmp_path, monkeypatch):
    state = legacy_refusal(tmp_path)
    state.take('t1').update(status='submitted', request_id=RID)
    monkeypatch.setattr('app.live_providers.requests.post', lambda *a, **kw: pytest.fail('duplicate submit'))
    fal = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, state, Budget(10, state), None)
    exc = FalClientHTTPError('PRIVATE', 403, {}, httpx.Response(403, json={'detail': 'PRIVATE'}))
    def denied(*a):
        raise exc
    monkeypatch.setattr(fal, '_wait', denied)
    with pytest.raises(ProviderFailure, match='HTTP 403'):
        fal.run('fal-ai/test', {}, 't1', 1, 'image', None)
    assert state.take('t1')['request_id'] == RID and state.data['reserved_usd'] == 1
    monkeypatch.setattr(fal, '_wait', lambda *a: {'images': [{'url': 'https://example.test/saved.png'}]})
    fal.run('fal-ai/test', {}, 't1', 1, 'image', None)
    assert state.data['spent_usd'] == 1 and state.data['reserved_usd'] == 0


def test_failed_checkpoint_during_legacy_reconciliation_never_submits(tmp_path, monkeypatch):
    state = legacy_refusal(tmp_path)
    state.on_save = lambda: (_ for _ in ()).throw(OSError('R2 unavailable'))
    monkeypatch.setattr('app.live_providers.requests.post', lambda *a, **kw: pytest.fail('submit before durable release'))
    fal = DurableFal(SimpleNamespace(fal_key='test'), lambda m: None, state, Budget(10, state), None)
    with pytest.raises(OSError):
        fal.run('fal-ai/test', {}, 't1', 1, 'image', None)


def test_unknown_response_and_request_id_are_never_echoed():
    response = httpx.Response(500, json={"error_type": "PRIVATE_ERROR", "detail": "PRIVATE_DETAIL"})
    exc = FalClientHTTPError("PRIVATE_MESSAGE", 500, {}, response, "PRIVATE_ERROR")
    diagnostic = fal_diagnostic(exc, "https://private.test/PRIVATE_TOKEN")
    assert diagnostic["http_status"] == 500 and diagnostic["error_type"] is None
    assert "PRIVATE" not in json.dumps(diagnostic)


def _refusal(kind="content_policy_violation"):
    response = requests.Response()
    response.status_code = 422
    response._content = json.dumps({"error_type": kind}).encode()
    response.headers["content-type"] = "application/json"
    return requests.exceptions.HTTPError(response=response)


def test_a_refusal_names_what_was_being_made(tmp_path):
    """The failure said only a request id: the producer had to read the log to
    learn which of forty reference images fal refused."""
    exc = _refusal()
    info = fal_diagnostic(exc, RID, what="ref adrian/fullbody_front__linen_suit")
    assert "ref adrian/fullbody_front__linen_suit" in info["message"]
    assert RID in info["message"]


def test_a_content_refusal_says_what_the_producer_changes(tmp_path):
    exc = _refusal()
    message = fal_diagnostic(exc, RID)["message"]
    assert "appearance" in message and "wardrobe" in message


def test_a_label_never_carries_provider_text(tmp_path):
    """Only our own label goes in; it is also bounded."""
    exc = _refusal()
    info = fal_diagnostic(exc, RID, what="x" * 400)
    assert "x" * 121 not in info["message"]


def test_the_providers_own_words_reach_the_log_it_is_given(tmp_path):
    """A 422 with no error code said only "check the saved request". fal's own
    sentence existed — it went to a log the producer cannot read."""
    from app.live_providers import _provider_explanation
    lines = []
    response = requests.Response()
    response.status_code = 422
    response._content = json.dumps({"detail": [
        {"msg": "Input image at https://r2.example/secret.png could not be read",
         "type": "value_error"}]}).encode()
    response.headers["content-type"] = "application/json"
    exc = requests.exceptions.HTTPError(response=response)
    _provider_explanation(exc, RID, "SECRET-KEY", lines.append)
    assert len(lines) == 1
    assert "could not be read" in lines[0] and RID in lines[0]
    assert "r2.example" not in lines[0] and "SECRET-KEY" not in lines[0]
