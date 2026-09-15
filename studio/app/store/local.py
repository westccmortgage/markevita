"""Local JSON driver.

Keeps every table in one JSON file under .studio-data/. It exists so the admin
panel opens and the whole pipeline can be exercised without any cloud setup.
Supabase is the production driver; this one mirrors its semantics.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from .base import NATURAL_KEYS, TABLES, check_columns


class LocalDriver:
    name = "local"

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, table: str) -> Path:
        if table not in TABLES:
            raise KeyError(f"unknown table {table!r}")
        return self.dir / f"{table}.json"

    def _read(self, table: str) -> list[dict]:
        p = self._path(table)
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8"))

    def _write(self, table: str, rows: list[dict]) -> None:
        p = self._path(table)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    @staticmethod
    def _match(row: dict, where: dict[str, Any] | None) -> bool:
        if not where:
            return True
        return all(row.get(k) == v for k, v in where.items())

    def list(self, table, where=None, order=None, desc=False, limit=None):
        with self._lock:
            rows = [r for r in self._read(table) if self._match(r, where)]
        if order:
            rows.sort(key=lambda r: (r.get(order) is None, r.get(order)), reverse=desc)
        return rows[:limit] if limit else rows

    def get(self, table, where):
        rows = self.list(table, where, limit=1)
        return rows[0] if rows else None

    def insert(self, table, row):
        check_columns(table, row)
        with self._lock:
            rows = self._read(table)
            row = dict(row)
            row.setdefault("id", str(uuid.uuid4()))
            rows.append(row)
            self._write(table, rows)
        return row

    def upsert(self, table, row):
        check_columns(table, row)
        keys = NATURAL_KEYS.get(table)
        if not keys:
            return self.insert(table, row)
        where = {k: row.get(k) for k in keys}
        with self._lock:
            rows = self._read(table)
            for i, existing in enumerate(rows):
                if self._match(existing, where):
                    merged = {**existing, **row}
                    rows[i] = merged
                    self._write(table, rows)
                    return merged
            new = dict(row)
            new.setdefault("id", str(uuid.uuid4()))
            rows.append(new)
            self._write(table, rows)
            return new

    def update(self, table, where, patch):
        check_columns(table, patch)
        n = 0
        with self._lock:
            rows = self._read(table)
            for i, r in enumerate(rows):
                if self._match(r, where):
                    rows[i] = {**r, **patch}
                    n += 1
            if n:
                self._write(table, rows)
        return n

    def delete(self, table, where):
        with self._lock:
            rows = self._read(table)
            keep = [r for r in rows if not self._match(r, where)]
            n = len(rows) - len(keep)
            if n:
                self._write(table, keep)
        return n
