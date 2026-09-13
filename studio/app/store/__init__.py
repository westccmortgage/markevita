"""Store facade: picks the configured driver and exposes it as `store`."""
from __future__ import annotations

from ..config import settings
from .base import NATURAL_KEYS, TABLES  # noqa: F401
from .local import LocalDriver


def _build():
    if settings.store_driver == "supabase" and settings.supabase_configured:
        try:
            from .supa import SupabaseDriver
            return SupabaseDriver(settings.supabase_url, settings.supabase_service_key)
        except Exception as e:  # pragma: no cover - falls back so the panel still opens
            print(f"[studio] Supabase driver unavailable ({e}); falling back to local store")
    return LocalDriver(settings.data_dir)


store = _build()
