"""Push delivery, isolation, recovery and playback navigation without providers."""
import copy
import json
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid

from test_i18n import language_ui
from test_live_episodes import short_package, R2Fake, checkpoint
from test_clip_preview import StrictStore
from app import auth, notifications as n, notification_web, runner, live_runtime
from app.config import settings


class MemoryR2(R2Fake):
    def get_paginator(self, operation):
        return SimpleNamespace(paginate=lambda Bucket, Prefix: [
            {'Contents': [{'Key': key} for key in self.objects if key.startswith(Prefix)]}])

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def subscription():
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {'endpoint': 'https://fcm.googleapis.com/fcm/send/test-token',
            'keys': {'p256dh': n.b64(key), 'auth': n.b64(b'0123456789abcdef')}}


def job(store, id='new', actor='admin@example.test', state='running', **extra):
    return store.insert('production_jobs', {'id': id, 'requested_by': actor, 'state': state,
        'series_id': 'miami', 'episode_id': 's01e01', 'stages': ['video','deliver'],
        'progress': {'done': ['video','deliver']}, 'created_at': '2026-09-13', **extra})


def test_persistent_keys_and_encrypted_push_without_redirects(monkeypatch):
    repo = n.PushStore(MemoryR2(), 'private')
    keys = repo.keypair()
    assert n.PushStore(repo.client, 'private').keypair() == keys
    assert Vapid.from_string(keys['private']).public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint) == __import__('base64').urlsafe_b64decode(keys['public'] + '==')
    requests = []
    def request(self, method, url, **kwargs):
        requests.append((method, url, kwargs))
        return SimpleNamespace(status_code=201, text='', headers={})
    monkeypatch.setattr('requests.Session.request', request)
    monkeypatch.setattr(settings, 'admin_email', 'admin@example.test')
    n.send(repo, subscription(), {'body': 'Private text', 'url': '/studio/'})
    assert len(requests) == 1
    _, url, kwargs = requests[0]
    assert url.startswith('https://fcm.googleapis.com/')
    assert kwargs['allow_redirects'] is False and kwargs['timeout'] == 10
    assert b'Private text' not in kwargs['data']
    assert kwargs['headers']['content-encoding'] == 'aes128gcm'


@pytest.mark.parametrize('endpoint', ['http://fcm.googleapis.com/a','https://127.0.0.1/a',
    'https://fcm.googleapis.com.evil.test/a','https://user@fcm.googleapis.com/a',
    'https://fcm.googleapis.com:8443/a','file:///tmp/a'])
def test_arbitrary_push_destinations_rejected(endpoint):
    sub = subscription(); sub['endpoint'] = endpoint
    with pytest.raises(ValueError): n.validate_subscription(sub)


def test_completion_review_failure_isolated_and_not_replayed_after_restart(monkeypatch):
    monkeypatch.setattr(settings, 'store_driver', 'supabase')
    monkeypatch.setattr(settings, 'base_path', '/studio')
    store = StrictStore(); store.insert('studio_admins', {'email':'admin@example.test'})
    repo = n.PushStore(MemoryR2(), 'private')
    old = job(store, 'old', state='failed')
    current = job(store)
    job(store, 'other', actor='someone@example.test')
    sub = subscription(); repo.subscribe(sub, 'admin@example.test', 'ru', [old,current])
    sender = Mock()
    store.update('production_jobs', {'id':'new'}, {'state':'done'})
    n.dispatch_once(repo, store, sender)
    assert sender.call_count == 1
    data = sender.call_args.args[2]
    assert data['title'] == 'Ваш эпизод готов' and data['url'].endswith('/s01e01#watch')
    n.dispatch_once(n.PushStore(repo.client, 'private'), store, sender)
    assert sender.call_count == 1
    job(store, 'review', state='paused', progress={'waiting_for':'reference_approval'})
    job(store, 'broken', state='failed')
    n.dispatch_once(repo, store, sender)
    assert {c.args[2]['tag'] for c in sender.call_args_list} == {
        'studio:new:ready','studio:review:review','studio:broken:failed'}
    # Revocation removes the private subscription.
    store.delete('studio_admins', {'email':'admin@example.test'})
    n.dispatch_once(repo, store, sender)
    assert repo.keys(n.PREFIX+'subscriptions/') == []


def test_transient_push_failure_retries_without_touching_production(monkeypatch):
    monkeypatch.setattr(settings, 'store_driver', 'local')
    store = StrictStore(); current = job(store)
    repo = n.PushStore(MemoryR2(), 'private'); repo.subscribe(subscription(), current['requested_by'], 'en', [current])
    store.update('production_jobs', {'id':'new'}, {'state':'done'})
    before = copy.deepcopy(store.list('production_jobs'))
    sender = Mock(side_effect=[RuntimeError('network down'), None])
    n.dispatch_once(repo, store, sender); n.dispatch_once(repo, store, sender); n.dispatch_once(repo, store, sender)
    assert sender.call_count == 2 and before == store.list('production_jobs')


def test_push_setup_requires_auth_csrf_and_can_disable(language_ui, monkeypatch):
    client, store, sid, eid = language_ui
    client.app.include_router(notification_web.router, prefix='/studio')
    monkeypatch.setattr(notification_web, 'store', store)
    repo = n.PushStore(MemoryR2(), 'private'); monkeypatch.setattr(n, 'repository', lambda:repo)
    page = client.get('/studio/notifications')
    assert page.status_code == 200 and 'Enable notifications' in page.text
    config = json.loads(re.search(r'<script id="push-config" type="application/json">(.*?)</script>', page.text, re.S)[1])
    form = {'action':'enable','subscription':json.dumps(subscription()),'csrf_token':config['csrf']}
    assert client.post('/studio/notifications/subscription',data=form).status_code == 403
    origin = {'Origin':'https://studio.example.test'}
    assert client.post('/studio/notifications/subscription',data=form,headers=origin).status_code == 200
    form['action']='status'
    assert client.post('/studio/notifications/subscription',data=form,headers=origin).json()['enabled']
    form['action']='disable'
    assert client.post('/studio/notifications/subscription',data=form,headers=origin).status_code == 200
    assert not repo.keys(n.PREFIX+'subscriptions/')
    assert set(client.get('/studio/notifications/key').json()) == {'public_key'}
    client.cookies.delete(auth.COOKIE)
    assert client.get('/studio/notifications/key').status_code == 401
    worker = client.get('/studio/notifications-sw.js')
    assert worker.status_code == 200 and worker.headers['Service-Worker-Allowed'] == '/studio/'
    assert 'fetch' not in worker.text


def test_ready_episode_player_precedes_production_and_links_latest_master(language_ui, monkeypatch):
    client, store, sid, eid = language_ui
    runtime = runner.episode_runtime(sid,eid)
    runtime.update(stages={'qa':'done','deliver':'done'}, masters=[{'version':'v2','files':['episode.mp4']}])
    monkeypatch.setattr(runner, 'episode_runtime', lambda *args:runtime)
    monkeypatch.setattr(settings, 'allow_paid', True)
    page = client.get(f'/studio/series/{sid}/episodes/{eid}/studio').text
    assert page.index('id="watch"') < page.index('<!-- ── production')
    assert 'preload="none"' in page and '/master/v2' in page
    runtime['stages']['deliver'] = 'failed'
    assert 'id="watch"' not in client.get(f'/studio/series/{sid}/episodes/{eid}/studio').text


def test_parallel_restore_preserves_files_paths_and_detects_corruption(checkpoint, tmp_path):
    cp,cfg,client = checkpoint
    for i in range(8): (cp.root/f'{i}.mp4').write_bytes(bytes([i])*1000)
    (cp.root/'state.json').write_text(json.dumps({'path':str(cp.root/'0.mp4')}))
    cp.save()
    other = live_runtime.Checkpoint(cfg, tmp_path/'another-worker','test_series')
    other.restore()
    for i in range(8): assert (other.root/f'{i}.mp4').read_bytes() == bytes([i])*1000
    assert json.loads((other.root/'state.json').read_text())['path'] == str(other.root/'0.mp4')
    key = cp.prefix+'objects/'+cp.files['0.mp4']['sha256']; client.objects[key] = b'corrupt'
    with pytest.raises(ValueError, match='checksum'): other.restore()
