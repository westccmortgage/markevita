"""Supabase (PostgREST) driver. Uses the service role key server-side only."""
from __future__ import annotations

from typing import Any

from .base import NATURAL_KEYS


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

    def list(self, table, where=None, order=None, desc=False, limit=None):
        q = self._q(table, where)
        if order:
            q = q.order(order, desc=desc)
        if limit:
            q = q.limit(limit)
        return q.execute().data or []

    def get(self, table, where):
        rows = self.list(table, where, limit=1)
        return rows[0] if rows else None

    def insert(self, table, row):
        res = self.client.table(table).insert(row).execute()
        return (res.data or [row])[0]

    def upsert(self, table, row):
        keys = NATURAL_KEYS.get(table)
        opts = {"on_conflict": ",".join(keys)} if keys else {}
        res = self.client.table(table).upsert(row, **opts).execute()
        return (res.data or [row])[0]

    def update(self, table, where, patch):
        q = self.client.table(table).update(patch)
        for k, v in where.items():
            q = q.eq(k, v)
        return len(q.execute().data or [])

    def delete(self, table, where):
        q = self.client.table(table).delete()
        for k, v in where.items():
            q = q.eq(k, v)
        return len(q.execute().data or [])
