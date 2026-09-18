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
    # A restore either leaves bytes that match the manifest or it raises.
    # An object it does not need to fetch cannot break that: the local file
    # already hashes to what the manifest names, so it IS the checkpoint's
    # object, and fetching it again would prove nothing.
    key = cp.prefix+'objects/'+cp.files['0.mp4']['sha256']; client.objects[key] = b'corrupt'
    other.restore()
    assert (other.root/'0.mp4').read_bytes() == bytes([0])*1000
    # The moment it does have to fetch it, the corruption is caught.
    (other.root/'0.mp4').unlink()
    with pytest.raises(ValueError, match='checksum'): other.restore()
    # And a local file that no longer matches is replaced, never trusted.
    client.objects[key] = bytes([0])*1000
    other.restore()
    (other.root/'0.mp4').write_bytes(b'damaged by a killed run')
    other.restore()
    assert (other.root/'0.mp4').read_bytes() == bytes([0])*1000


# ── email, because a browser subscription is not where the producer lives ──

class _Repo:
    def __init__(self):
        self.store = {}

    def read(self, key):
        return (self.store.get(key), 'etag')

    def write(self, key, value, etag=None):
        self.store[key] = value
        return {'ETag': 'etag'}

    def keys(self, prefix):
        return [k for k in self.store if k.startswith(prefix)]

    def delete(self, key):
        self.store.pop(key, None)


def _primed(notifications, store):
    """A recipient the studio has already seen, so the next event is news."""
    repo = _Repo()
    notifications.dispatch_email_once(repo, store, deliver=lambda *a: None)
    return repo


def _failed_job(store):
    store.insert('series', {'id': 'island', 'title': 'Island'})
    return store.insert('production_jobs', {
        'series_id': 'island', 'episode_id': 's01e04', 'stages': ['references'],
        'mode': 'live', 'state': 'failed', 'requested_by': 'admin@example.test',
        'idempotency_key': 'k1', 'error': 'fal.ai HTTP 422 · ref adrian/fullbody: refused.',
        'progress': {'stage': 'references', 'done': ['intake'], 'total': 3}})


def test_a_failure_is_emailed_with_the_reason_in_it(monkeypatch):
    """Push needed a live browser. A producer who closed the tab heard nothing
    for hours while the episode sat stopped."""
    from test_clip_preview import StrictStore
    from app import notifications
    store = StrictStore()
    monkeypatch.setattr(settings, 'admin_email', 'admin@example.test')
    monkeypatch.setattr('app.mail.transport', lambda: 'resend')
    repo = _primed(notifications, store)
    _failed_job(store)
    sent = []
    assert notifications.dispatch_email_once(repo, store, deliver=lambda *a: sent.append(a)) == 1
    to, subject, body = sent[0]
    assert to == 'admin@example.test'
    assert 'island / s01e04' in body
    assert 'HTTP 422' in body, 'the reason travels in the letter, not behind a link'
    assert '/jobs/' in body


def test_the_same_event_is_not_emailed_twice(monkeypatch):
    from test_clip_preview import StrictStore
    from app import notifications
    store = StrictStore()
    monkeypatch.setattr(settings, 'admin_email', 'admin@example.test')
    monkeypatch.setattr('app.mail.transport', lambda: 'resend')
    repo = _primed(notifications, store)
    _failed_job(store)
    assert notifications.dispatch_email_once(repo, store, deliver=lambda *a: None) == 1
    assert notifications.dispatch_email_once(repo, store, deliver=lambda *a: None) == 0


def test_a_message_that_could_not_be_sent_is_tried_again(monkeypatch):
    """Losing the one letter that says why production stopped is worse than
    sending it late."""
    from test_clip_preview import StrictStore
    from app import notifications
    store = StrictStore()
    monkeypatch.setattr(settings, 'admin_email', 'admin@example.test')
    monkeypatch.setattr('app.mail.transport', lambda: 'resend')
    repo = _primed(notifications, store)
    _failed_job(store)

    def refuse(*a):
        raise RuntimeError('mail server down')

    assert notifications.dispatch_email_once(repo, store, deliver=refuse) == 0
    assert notifications.dispatch_email_once(repo, store, deliver=lambda *a: None) == 1


def test_nothing_is_emailed_with_no_transport(monkeypatch):
    from test_clip_preview import StrictStore
    from app import notifications
    store = StrictStore()
    monkeypatch.setattr('app.mail.transport', lambda: '')
    _failed_job(store)
    assert notifications.dispatch_email_once(
        _Repo(), store, deliver=lambda *a: pytest.fail('sent with nowhere to send')) == 0


def test_who_is_told_is_not_who_may_sign_in(monkeypatch):
    """Coupling the two meant adding a reader by handing them a way in."""
    from test_clip_preview import StrictStore
    from app import notifications
    store = StrictStore()
    store.insert('studio_admins', {'email': 'someone@example.test'})
    monkeypatch.setattr(settings, 'admin_email', 'admin@example.test')
    monkeypatch.setenv('NOTIFICATION_TO', 'crd@example.test, second@example.test')
    assert notifications.recipients(store) == ['crd@example.test', 'second@example.test']
    monkeypatch.delenv('NOTIFICATION_TO')
    assert notifications.recipients(store) == ['someone@example.test', 'admin@example.test']


def test_switching_email_on_does_not_post_every_old_failure(monkeypatch):
    """Configuring a transport would have sent a letter for every job still on
    file — dozens, about episodes that stopped days ago."""
    from test_clip_preview import StrictStore
    from app import notifications
    store = StrictStore()
    monkeypatch.setattr(settings, 'admin_email', 'admin@example.test')
    monkeypatch.setattr('app.mail.transport', lambda: 'resend')
    store.insert('series', {'id': 'island', 'title': 'Island'})
    for n in range(5):
        store.insert('production_jobs', {
            'series_id': 'island', 'episode_id': f's01e0{n}', 'stages': ['references'],
            'mode': 'live', 'state': 'failed', 'requested_by': 'admin@example.test',
            'idempotency_key': f'old{n}', 'error': 'stopped days ago'})
    repo = _Repo()
    assert notifications.dispatch_email_once(
        repo, store, deliver=lambda *a: pytest.fail('replayed history')) == 0

    # What happens next is delivered.
    store.insert('production_jobs', {
        'series_id': 'island', 'episode_id': 's01e04', 'stages': ['references'],
        'mode': 'live', 'state': 'failed', 'requested_by': 'admin@example.test',
        'idempotency_key': 'new', 'error': 'fal.ai HTTP 422 · ref adrian/fullbody: refused.',
        'progress': {'stage': 'references', 'done': ['intake'], 'total': 3}})
    sent = []
    assert notifications.dispatch_email_once(repo, store, deliver=lambda *a: sent.append(a)) == 1
    assert 's01e04' in sent[0][2]


def test_a_letter_carries_an_address_not_a_path(monkeypatch):
    """The first real letter arrived with "/studio/jobs/…" in it — nothing to
    click, and no host to paste it against."""
    from app import notifications
    job = {'id': 'abc', 'series_id': 'island', 'episode_id': 's01e03',
           'progress': {'stage': 'direction'}, 'error': 'stopped'}
    monkeypatch.setattr(settings, 'public_url', 'https://markevita.com')
    monkeypatch.setattr(settings, 'base_path', '/studio')
    assert 'https://markevita.com/studio/jobs/abc' in notifications.email_body(job, 'failed', 'en')


def test_the_studio_learns_its_own_address_from_its_own_screens(monkeypatch):
    """Depending on an environment variable being set correctly is how a
    letter ends up carrying a path nobody can click."""
    monkeypatch.setattr(settings, 'public_url', '')
    monkeypatch.setattr(settings, 'base_path', '/studio')
    monkeypatch.setattr(settings, '_seen_origin', '', raising=False)
    assert settings.absolute_url('/jobs/abc') == '/studio/jobs/abc'

    settings.remember_origin('https://markevita.com/')
    assert settings.absolute_url('/jobs/abc') == 'https://markevita.com/studio/jobs/abc'

    settings.remember_origin('not-a-url')
    assert settings.absolute_url('/jobs/abc') == 'https://markevita.com/studio/jobs/abc'

    monkeypatch.setattr(settings, 'public_url', 'https://configured.test')
    assert settings.absolute_url('/jobs/abc') == 'https://configured.test/studio/jobs/abc'
