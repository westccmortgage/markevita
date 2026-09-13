"""Storage driver interface.

Every studio record goes through this narrow interface so the Supabase driver
and the local JSON driver stay interchangeable. Tables and column names match
supabase/migrations/0001_studio.sql exactly.
"""
from __future__ import annotations

from typing import Any, Protocol

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
             order: str | None = None, desc: bool = False, limit: int | None = None) -> list[dict]: ...

    def get(self, table: str, where: dict[str, Any]) -> dict | None: ...

    def insert(self, table: str, row: dict) -> dict: ...

    def upsert(self, table: str, row: dict) -> dict: ...

    def update(self, table: str, where: dict[str, Any], patch: dict) -> int: ...

    def delete(self, table: str, where: dict[str, Any]) -> int: ...
