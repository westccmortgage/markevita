"""Private R2 checkpoints and a fenced, renewable Supabase lease per series.

Every engine State.save commits a manifest after its files are durable. The
manifest is conditional (ETag), so a replaced worker cannot overwrite newer
state. A reserved request without a provider id is never submitted again.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from serial.storage import R2

LEASE_SECONDS = 180
# Renewals go every 30 seconds, so this many consecutive failures still falls
# short of the lease's own life: we stop before it could have expired.
RENEWAL_ATTEMPTS = 4


def utcnow():
    return datetime.now(timezone.utc)


def unexpired(row):
    try:
        return datetime.fromisoformat(row['finished_at']) > utcnow()
    except (ValueError, TypeError, KeyError):
        return False


def _file_digest(path: Path) -> str:
    """The bytes on disk, named the way the manifest names them."""
    digest = hashlib.sha256()
    try:
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
    except OSError:
        return ''
    return digest.hexdigest()


class SeriesLease:
    def __init__(self, store, series_id, actor):
        self.store, self.series_id = store, series_id
        self.owner = str(uuid.uuid4())
        self.key = 'episode-worker-lease:' + series_id
        self.stop = threading.Event()
        self.lost = threading.Event()
        row = store.get('production_jobs', {'idempotency_key': self.key})
        deadline = (utcnow() + timedelta(seconds=LEASE_SECONDS)).isoformat()
        patch = {'state': 'leased', 'error': self.owner, 'finished_at': deadline,
                 'requested_by': actor}
        if row:
            if row['state'] == 'leased' and unexpired(row):
                raise ValueError('This series already has an active production worker. Open Jobs for its status.')
            if not store.update('production_jobs', {'id': row['id'], 'state': row['state'],
                                                   'error': row['error'], 'finished_at': row['finished_at']}, patch):
                raise ValueError('Another worker claimed this series. Open Jobs.')
            self.id = row['id']
        else:
            row = store.insert('production_jobs', {
                'series_id': series_id, 'episode_id': '', 'stages': ['runtime_lease'],
                'mode': 'live', 'idempotency_key': self.key, **patch})
            self.id = row['id']
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def check(self):
        row = self.store.get('production_jobs', {'id': self.id})
        if self.lost.is_set() or not row or row.get('error') != self.owner or not unexpired(row):
            raise RuntimeError('Production lease expired. Resume from the saved checkpoint.')

    def _heartbeat(self):
        """Renew the lease, surviving a blip that is not a lost lease.

        Any failure at all used to end the lease for good: one timeout, one
        moment the store was unreachable, anywhere in a twenty-minute run, and
        the worker was killed with "Worker stopped". Across a pack of forty
        images that is the difference between finishing and never finishing.

        The renewal is its own authority. One row updated means the lease is
        still ours. Zero means somebody else holds it, which is a real loss. An
        error means we did not find out — and the lease outlives several
        renewals, so we can ask again before giving it up.
        """
        failures = 0
        while not self.stop.wait(30):
            try:
                count = self.store.update('production_jobs', {'id': self.id, 'error': self.owner, 'state': 'leased'},
                    {'finished_at': (utcnow() + timedelta(seconds=LEASE_SECONDS)).isoformat()})
            except Exception:
                failures += 1
                # Give up while the lease is still ours to give up: renewing
                # every 30s into a 180s lease leaves room for a few misses,
                # and none for one that has actually expired.
                if failures >= RENEWAL_ATTEMPTS:
                    self.lost.set()
                    return
                continue
            if count != 1:
                self.lost.set()
                return
            failures = 0

    def close(self):
        self.stop.set()
        self.store.update('production_jobs', {'id': self.id, 'error': self.owner},
                          {'state': 'released', 'finished_at': utcnow().isoformat()})


class Checkpoint:
    def __init__(self, cfg, root: Path, series_id: str, check=lambda: None):
        self.root = root / series_id
        self.storage = R2(cfg, lambda msg: None)
        if not self.storage.enabled:
            raise ValueError('Live episodes require private R2 storage.')
        self.client, self.bucket = self.storage.client, cfg.r2_bucket
        self.prefix = f'series/{series_id}/_studio_runtime/live/'
        self.key = self.prefix + 'manifest.json'
        self.etag = None
        self.files = {}
        self.signatures = {}
        self.check = check

    def read(self):
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.key)
        except Exception as e:
            if getattr(e, 'response', {}).get('Error', {}).get('Code') in ('NoSuchKey', '404'):
                return None
            raise
        self.etag = obj['ETag']
        return json.loads(obj['Body'].read())

    def restore(self):
        """Bring the local directory to exactly what the checkpoint says.

        This directory is a cache, never the authority after a process loss —
        but a local file whose bytes hash to the checksum the manifest names
        IS the checkpoint's own object, and fetching it again proves nothing.
        Emptying the directory and pulling every object down on every start
        meant a whole reference pack crossed the wire before the browser was
        answered, and once the pack was large enough the request timed out at
        the proxy with the run's fate unknown. It would have been hopeless at
        video, where the objects are tens of times heavier.
        """
        self.check()
        manifest = self.read()
        root = self.root.resolve()
        if not manifest:
            if self.root.exists():
                shutil.rmtree(self.root)
            self.root.mkdir(parents=True)
            return
        self.root.mkdir(parents=True, exist_ok=True)
        old_root = manifest.get('root', str(self.root))
        wanted: dict[Path, dict] = {}
        for name, info in manifest['files'].items():
            path = (self.root / name).resolve()
            if not path.is_relative_to(root):
                raise ValueError('Invalid checkpoint path')
            wanted[path] = info
        # Whatever the manifest does not name is not part of this checkpoint.
        # The directory must end up holding what the checkpoint says and
        # nothing it happens to remember from before.
        for path in list(self.root.rglob('*')):
            if path.is_file() and path.resolve() not in wanted:
                path.unlink()
        def restore_file(item):
            path, info = item
            if path.is_file() and _file_digest(path) == info['sha256']:
                return
            key = self.prefix + 'objects/' + info['sha256']
            data = self.client.get_object(Bucket=self.bucket, Key=key)['Body'].read()
            if hashlib.sha256(data).hexdigest() != info['sha256']:
                raise ValueError('Checkpoint checksum mismatch')
            if path.suffix == '.json' and old_root != str(self.root):
                data = data.replace(old_root.encode(), str(self.root).encode())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        # Only immutable, checksummed objects run concurrently. Paid requests,
        # state mutation and the conditional checkpoint commit remain serial.
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(restore_file, wanted.items()))
        self.check()
        self.files = manifest['files']

    def save(self):
        self.check()
        files = {}
        for path in sorted(self.root.rglob('*')):
            if not path.is_file() or path.name in ('log.txt',) or path.suffix == '.tmp':
                continue
            if path.is_symlink():
                raise ValueError('Symlinks cannot enter a production checkpoint')
            name = str(path.relative_to(self.root))
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if self.signatures.get(name) == signature and name in self.files:
                files[name] = self.files[name]
                continue
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if self.files.get(name, {}).get('sha256') != digest:
                self.client.upload_file(str(path), self.bucket, self.prefix + 'objects/' + digest)
            files[name] = {'sha256': digest, 'size': stat.st_size}
            self.signatures[name] = signature
        data = json.dumps({'root': str(self.root), 'files': files}).encode()
        self.check()
        condition = {'IfMatch': self.etag} if self.etag else {'IfNoneMatch': '*'}
        obj = self.client.put_object(Bucket=self.bucket, Key=self.key, Body=data,
                                    ContentType='application/json', **condition)
        self.etag = obj['ETag']
        self.files = files
