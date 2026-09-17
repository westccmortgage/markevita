"""Opt-in Web Push. Private subscriptions and VAPID key persist in R2.

An independent dispatcher observes saved jobs: notification/network failures
cannot fail a production job or resubmit a paid provider call. Seen events are
persisted per browser; Web Push tags collapse duplicates after a process crash.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
from functools import lru_cache
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .config import PIPELINE_DIR, settings
from .i18n import translate

log = logging.getLogger(__name__)
PREFIX = '_studio_private/notifications/'
PUSH_HOSTS = {'fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com'}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def validate_subscription(value):
    try:
        endpoint = value['endpoint']
        parsed = urlsplit(endpoint)
        if (len(endpoint) > 2048 or parsed.scheme != 'https' or parsed.hostname not in PUSH_HOSTS
                or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment):
            raise ValueError()
        keys = value['keys']
        decoded = {}
        for key in ('p256dh', 'auth'):
            raw = keys[key]
            if not isinstance(raw, str) or len(raw) > 128:
                raise ValueError()
            decoded[key] = base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True)
        if len(decoded['auth']) != 16 or len(decoded['p256dh']) != 65:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded['p256dh'])
        return {'endpoint': endpoint, 'keys': {k: keys[k] for k in ('p256dh', 'auth')}}
    except (KeyError, TypeError, ValueError):
        raise ValueError('This browser push subscription is not supported.') from None


def event(job):
    if job.get('stages') == ['runtime_lease'] or not job.get('episode_id'):
        return None
    state = job.get('state')
    progress = job.get('progress') or {}
    if state == 'done':
        kind = 'ready' if 'deliver' in progress.get('done', []) else 'completed'
    elif state in ('failed', 'interrupted'):
        kind = 'failed'
    elif state == 'paused' and (progress.get('waiting_for') == 'reference_approval'
            or 'Reference pack ready. Open References' in (job.get('log') or '')):
        kind = 'review'
    else:
        return None
    return str(job['id']) + ':' + kind, kind


def payload(job, kind, language):
    title = {'ready': 'Your episode is ready', 'completed': 'Selected stages completed',
             'failed': 'Production needs attention', 'review': 'References need your review'}[kind]
    sid, eid = quote(job['series_id'], safe=''), quote(job['episode_id'], safe='')
    path = (f'/series/{sid}/episodes/{eid}#watch' if kind == 'ready' else
            f'/series/{sid}/references' if kind == 'review' else f'/jobs/{quote(job["id"], safe="")}')
    return {'title': translate(title, language), 'body': f'{job["series_id"]} / {job["episode_id"]}',
            'url': settings.url(path), 'tag': f'studio:{job["id"]}:{kind}'}


class PushStore:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    def read(self, key):
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            return json.loads(obj['Body'].read()), obj['ETag']
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                return None, None
            raise

    def write(self, key, value, etag=None):
        return self.client.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(value).encode(),
            ContentType='application/json', **({'IfMatch': etag} if etag else {'IfNoneMatch': '*'}))

    def keys(self, prefix):
        pages = self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket, Prefix=prefix)
        return [obj['Key'] for page in pages for obj in page.get('Contents', [])]

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    @staticmethod
    def subscription_key(subscription):
        # One owner per browser subscription, including after switching accounts.
        return PREFIX + 'subscriptions/' + hashlib.sha256(subscription['endpoint'].encode()).hexdigest() + '.json'

    def keypair(self):
        key = PREFIX + 'vapid.json'
        saved, _ = self.read(key)
        if saved:
            return saved
        private = ec.generate_private_key(ec.SECP256R1())
        saved = {'private': b64(private.private_bytes(serialization.Encoding.DER,
                     serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                 'public': b64(private.public_key().public_bytes(serialization.Encoding.X962,
                     serialization.PublicFormat.UncompressedPoint))}
        try:
            self.write(key, saved)
        except Exception:
            winner, _ = self.read(key)
            if winner:
                return winner
            raise
        return saved

    def subscribe(self, subscription, actor, language, jobs):
        key = self.subscription_key(subscription)
        old, etag = self.read(key)
        if old and old['actor'] == actor:
            record = dict(old, subscription=subscription, language=language)
        else:
            # Baseline existing terminal jobs; enabling push must not replay old failures.
            seen = [e[0] for job in jobs if (e := event(job))]
            record = {'subscription': subscription, 'actor': actor, 'language': language, 'seen': seen}
        self.write(key, record, etag)


@lru_cache(maxsize=1)
def repository():
    from serial.config import Config
    from serial.storage import R2
    cfg = Config.load(PIPELINE_DIR, live=True)
    storage = R2(cfg, lambda _: None)
    if not storage.enabled:
        raise ValueError('Notifications need the configured private R2 storage.')
    return PushStore(storage.client, cfg.r2_bucket)


def send(repo, subscription, message):
    import requests
    from pywebpush import webpush

    class NoRedirects(requests.Session):
        def request(self, method, url, **kwargs):
            kwargs['allow_redirects'] = False
            return super().request(method, url, **kwargs)

    # Validate again when reading stored records. Never POST to arbitrary URLs.
    subscription = validate_subscription(subscription)
    with NoRedirects() as session:
        result = webpush(subscription_info=subscription, data=json.dumps(message),
            vapid_private_key=repo.keypair()['private'],
            vapid_claims={'sub': settings.public_url or ('mailto:' + settings.admin_email)},
            ttl=86400, timeout=10, requests_session=session)
        if not 200 <= result.status_code < 300:
            raise RuntimeError('Push service did not accept the notification.')


EMAIL_PREFIX = PREFIX + 'email/'


def email_body(job, kind, language):
    """What went wrong, in the letter itself — not behind a link."""
    lines = [translate({'ready': 'Your episode is ready',
                        'completed': 'Selected stages completed',
                        'failed': 'Production needs attention',
                        'review': 'References need your review'}[kind], language),
             '', f"{job['series_id']} / {job['episode_id']}"]
    stage = (job.get('progress') or {}).get('stage')
    if stage:
        lines.append(translate('Stage:', language) + ' ' + str(stage))
    if kind == 'failed' and (job.get('error') or '').strip():
        lines += ['', job['error'].strip()]
    lines += ['', settings.absolute_url(f"/jobs/{quote(job['id'], safe='')}")]
    return '\n'.join(lines)


def recipients(store):
    """Everyone who should hear about production, whatever their browser did.

    NOTIFICATION_TO decides it outright when set — who gets told that an
    episode stopped is not the same question as who may sign in, and
    STUDIO_ADMIN_EMAIL answers the second one. Without it, everyone who can
    sign in is told, which is the safer default: better an extra reader than
    a stoppage nobody hears about.
    """
    import os
    explicit = [a.strip() for a in (os.getenv('NOTIFICATION_TO') or '').replace(';', ',').split(',')
                if a.strip()]
    if explicit:
        return list(dict.fromkeys(explicit))
    found = []
    try:
        for admin in store.list('studio_admins'):
            if admin.get('email'):
                found.append(admin['email'])
    except Exception:
        pass
    if settings.admin_email and settings.admin_email not in found:
        found.append(settings.admin_email)
    return list(dict.fromkeys(found))


def dispatch_email_once(repo, store, deliver=None):
    """Email each new job event once. Delivery is not tied to any browser."""
    from .mail import deliver as send_mail, transport
    deliver = deliver or send_mail
    if not transport():
        return 0
    sent = 0
    for actor in recipients(store):
        key = EMAIL_PREFIX + b64(actor.encode())
        try:
            record, etag = repo.read(key)
        except Exception:
            continue
        jobs = store.list('production_jobs', order='created_at', desc=True, limit=100)
        if record is None:
            # Baseline on first sight, exactly as enabling push does. Without
            # this, the moment a transport is configured every past failure
            # still on file is posted at once — dozens of letters about
            # episodes that stopped days ago. Notifications are about what
            # happens from now on.
            record = {'actor': actor, 'seen': [e[0] for job in jobs if (e := event(job))]}
            try:
                repo.write(key, record, etag)
            except Exception:
                pass
            continue
        seen = set(record.get('seen', []))
        for job in reversed(jobs):
            entry = event(job)
            if not entry or entry[0] in seen:
                continue
            subject = f"MarkeVita · {job['series_id']} / {job['episode_id']} — " + \
                      translate({'ready': 'ready', 'completed': 'completed',
                                 'failed': 'needs attention', 'review': 'needs review'}[entry[1]],
                                record.get('language', 'en'))
            try:
                deliver(actor, subject, email_body(job, entry[1], record.get('language', 'en')))
            except Exception as exc:
                # Left unseen on purpose: the next pass tries again rather than
                # losing the one message that says why production stopped.
                log.warning('Email notification deferred (%s)', type(exc).__name__)
                break
            sent += 1
            seen.add(entry[0])
            record['seen'] = [*record.get('seen', []), entry[0]][-500:]
            try:
                result = repo.write(key, record, etag)
                etag = result['ETag']
            except Exception:
                break
    return sent


def jobs_for(store, actor):
    return store.list('production_jobs', {'requested_by': actor}, order='created_at', desc=True, limit=100)


def dispatch_once(repo, store, sender=send):
    jobs_by_actor = {}
    for key in repo.keys(PREFIX + 'subscriptions/'):
        try:
            record, etag = repo.read(key)
            if not record:
                continue
            actor = record['actor']
            # Revoked administrators must not continue receiving private job notifications.
            if settings.store_driver == 'supabase':
                admin = store.get('studio_admins', {'email': actor})
                if not admin:
                    repo.delete(key)
                    continue
            if actor not in jobs_by_actor:
                jobs_by_actor[actor] = jobs_for(store, actor)
            seen = set(record.get('seen', []))
            for job in reversed(jobs_by_actor[actor]):
                entry = event(job)
                if not entry or entry[0] in seen:
                    continue
                sender(repo, record['subscription'], payload(job, entry[1], record['language']))
                seen.add(entry[0])
                record['seen'] = [*record.get('seen', []), entry[0]][-500:]
                result = repo.write(key, record, etag)
                etag = result['ETag']
        except Exception as exc:
            response = getattr(exc, 'response', None)
            if response is not None and getattr(response, 'status_code', None) in (404, 410):
                repo.delete(key)
            else:
                # Do not log endpoint capability URLs, email addresses, tokens or provider payloads.
                log.warning('Notification deferred (%s)', type(exc).__name__)


class Dispatcher:
    def __init__(self):
        self.stop = threading.Event()

    def start(self):
        threading.Thread(target=self.run, name='studio-notifications', daemon=True).start()

    def run(self):
        from .store import store
        while not self.stop.wait(30):
            repo = repository()
            for name, work in (('push', lambda: dispatch_once(repo, store)),
                               ('email', lambda: dispatch_email_once(repo, store)),
                               ('recovery', self.carry_on)):
                try:
                    work()
                except Exception as exc:
                    log.warning('%s check deferred (%s)', name, type(exc).__name__)

    @staticmethod
    def carry_on():
        """Pick up an episode whose worker died while the server kept running.

        Resuming at boot only covers a restart. A worker can also lose its
        lease or its thread while the process lives on, and that episode used
        to wait for a person.
        """
        from .live_jobs import resume_interrupted
        from . import runner
        resume_interrupted(runner.jobs)
