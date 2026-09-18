"""Storage driver interface.

Every studio record goes through this narrow interface so the Supabase driver
and the local JSON driver stay interchangeable. Tables and column names match
supabase/migrations/0001_studio.sql exactly.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

MIGRATION = Path(__file__).resolve().parents[2] / "supabase" / "migrations" / "0001_studio.sql"


@lru_cache(maxsize=1)
def columns() -> dict[str, set[str]]:
    """Column names per table, read from the migration Postgres actually runs.

    The local driver used to accept any field, so a write of a column the
    database does not have passed every test and failed only in production.
    One schema, read from one place, keeps both drivers honest.
    """
    out: dict[str, set[str]] = {}
    try:
        sql = MIGRATION.read_text(encoding="utf-8")
    except OSError:
        return out
    for block in re.finditer(r"create table if not exists\s+(\w+)\s*\((.*?)\n\);", sql, re.S):
        table, body = block.group(1), block.group(2)
        names = set()
        for line in body.splitlines():
            line = line.strip()
            match = re.match(r"([a-z_][a-z0-9_]*)\s+\S", line)
            if match and match.group(1) not in ("unique", "primary", "foreign", "check", "constraint"):
                names.add(match.group(1))
        out[table] = names
    return out


def check_columns(table: str, row: dict) -> None:
    """Refuse a field Postgres would refuse, wherever the driver is running."""
    known = columns().get(table)
    if not known:
        return
    unknown = sorted(set(row) - known)
    if unknown:
        raise ValueError(f"{table}: no such column(s): {', '.join(unknown)}. "
                         f"Add them to supabase/migrations/ before writing them.")

TABLES = [
    "studio_admins", "series", "seasons", "episodes", "scripts", "characters", "clothing",
    "voices", "locations", "props", "relationships", "secrets_bible", "knowledge_state",
    "scenes", "reference_assets", "takes", "episode_manifests", "production_jobs",
    "approvals", "costs", "generation_history", "integration_status",
]

# Natural keys used for upserts by the local driver and for conflict targets by
# the Supabase driver.
NATURAL_KEYS: dict[str, list[str]] = {
    "series": ["id"],
    "seasons": ["series_id", "season_id"],
    "episodes": ["series_id", "episode_id"],
    "characters": ["series_id", "character_id"],
    "clothing": ["series_id", "character_id", "variant_id"],
    "voices": ["series_id", "character_id"],
    "locations": ["series_id", "location_id"],
    "props": ["series_id", "prop_id"],
    "relationships": ["series_id", "rel_id"],
    "secrets_bible": ["series_id", "secret_id"],
    "knowledge_state": ["series_id", "episode_id", "kind", "subject_id"],
    "scenes": ["series_id", "episode_id", "scene_id"],
    "takes": ["series_id", "take_id"],
    "production_jobs": ["idempotency_key"],
    "integration_status": ["provider"],
    "studio_admins": ["email"],
}


class Driver(Protocol):
    name: str

    def list(self, table: str, where: dict[str, Any] | None = None,
             order: str | None = None, desc: bool = False, limit: int | None = None,
             offset: int = 0) -> list[dict]: ...

    def get(self, table: str, where: dict[str, Any]) -> dict | None: ...

    def insert(self, table: str, row: dict) -> dict: ...

    def upsert(self, table: str, row: dict) -> dict: ...

    def update(self, table: str, where: dict[str, Any], patch: dict) -> int: ...

    def delete(self, table: str, where: dict[str, Any]) -> int: ...
