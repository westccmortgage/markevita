"""Supabase (PostgREST) driver. Uses the service role key server-side only."""
from __future__ import annotations

import time
from typing import Any

from .base import NATURAL_KEYS

# A connection the database dropped mid-answer is not a reply, and a run that
# has already generated and paid for a full reference pack must not die of
# one. A whole episode's production ended at "RemoteProtocolError" after the
# references were finished, on a bookkeeping write, with no attempt to ask
# again. These are the failures where asking again is the right answer:
# nothing was decided, so nothing is repeated by repeating the question.
TRANSPORT_FAULTS = ("RemoteProtocolError", "ReadError", "WriteError", "ConnectError",
                    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
                    "RemoteDisconnected", "ConnectionResetError", "IncompleteRead")
ATTEMPTS = 4
BACKOFF = 0.5


def _is_transport(exc) -> bool:
    names = {type(e).__name__ for e in _causes(exc)}
    return bool(names.intersection(TRANSPORT_FAULTS))


def _causes(exc):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def _retrying(call):
    """Ask again when the connection failed, not when the database answered."""
    for attempt in range(ATTEMPTS):
        try:
            return call()
        except Exception as exc:
            if attempt == ATTEMPTS - 1 or not _is_transport(exc):
                raise
            time.sleep(BACKOFF * (2 ** attempt))


class SupabaseDriver:
    name = "supabase"

    def __init__(self, url: str, service_key: str):
        from supabase import create_client
        self.client = create_client(url, service_key)

    def _q(self, table: str, where: dict[str, Any] | None):
        q = self.client.table(table).select("*")
        for k, v in (where or {}).items():
            q = q.eq(k, v)
        return q

    def list(self, table, where=None, order=None, desc=False, limit=None, offset=0):
        q = self._q(table, where)
        if order:
            q = q.order(order, desc=desc)
        # PostgREST caps how many rows it will return, and without an order
        # the ones it picks are arbitrary. A total summed from such a page was
        # a different number every time the screen refreshed — worse than no
        # number, because it is believed. Anything that asks for a page says
        # which page, and in what order.
        if offset:
            q = q.range(offset, offset + (limit or 1000) - 1)
        elif limit:
            q = q.limit(limit)
        try:
            return _retrying(q.execute).data or []
        except Exception as exc:
            # An id the database cannot even parse matches nothing. Saying so
            # is the honest answer; raising turned a mistyped or truncated link
            # into "Something in the studio broke" with a stack trace. Only
            # this one error is treated as an empty result — anything else is
            # a real fault and must not be hidden as "not found".
            if '22P02' in str(exc):
                return []
            raise

    def get(self, table, where):
        rows = self.list(table, where, limit=1)
        return rows[0] if rows else None

    def insert(self, table, row):
        res = _retrying(self.client.table(table).insert(row).execute)
        return (res.data or [row])[0]

    def upsert(self, table, row):
        keys = NATURAL_KEYS.get(table)
        opts = {"on_conflict": ",".join(keys)} if keys else {}
        res = _retrying(self.client.table(table).upsert(row, **opts).execute)
        return (res.data or [row])[0]

    def update(self, table, where, patch):
        q = self.client.table(table).update(patch)
        for k, v in where.items():
            q = q.eq(k, v)
        return len(_retrying(q.execute).data or [])

    def delete(self, table, where):
        q = self.client.table(table).delete()
        for k, v in where.items():
            q = q.eq(k, v)
        return len(_retrying(q.execute).data or [])
