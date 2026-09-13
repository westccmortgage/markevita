"""Studio configuration.

Reads the backend environment contract. Secret VALUES stay in this process:
nothing here is ever serialised to the browser or written to Supabase.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

STUDIO_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = STUDIO_DIR.parent
PIPELINE_DIR = REPO_ROOT / "pipeline"


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None or v == "" else v.strip()


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8800
    session_secret: str = ""
    # Hard safety switch. While false the studio refuses every paid call.
    allow_paid: bool = False
    # Offline by default: connection tests check credential presence/shape only.
    allow_connection_tests: bool = False
    store_driver: str = "local"
    admin_email: str = ""
    admin_password: str = ""
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_key: str = ""
    data_dir: Path = field(default_factory=lambda: STUDIO_DIR / ".studio-data")
    package_dir: Path = field(default_factory=lambda: STUDIO_DIR / ".studio-packages")
    runs_dir: Path = field(default_factory=lambda: PIPELINE_DIR / "runs")

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(STUDIO_DIR / ".env")
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(PIPELINE_DIR / ".env")
        s = cls(
            host=_env("STUDIO_HOST", "127.0.0.1"),
            port=int(_env("STUDIO_PORT", "8800")),
            session_secret=_env("STUDIO_SESSION_SECRET"),
            allow_paid=_bool("STUDIO_ALLOW_PAID", False),
            allow_connection_tests=_bool("STUDIO_ALLOW_CONNECTION_TESTS", False),
            store_driver=_env("STUDIO_STORE", "local").lower(),
            admin_email=_env("STUDIO_ADMIN_EMAIL"),
            admin_password=_env("STUDIO_ADMIN_PASSWORD"),
            supabase_url=_env("SUPABASE_URL"),
            supabase_anon_key=_env("SUPABASE_ANON_KEY"),
            supabase_service_key=_env("SUPABASE_SERVICE_ROLE_KEY"),
        )
        if not s.session_secret:
            # Dev fallback: a per-process secret. Sessions end when the server
            # restarts, which is correct behaviour for an unconfigured studio.
            import secrets as _secrets
            s.session_secret = _secrets.token_hex(32)
        if s.store_driver == "supabase" and not (s.supabase_url and s.supabase_service_key):
            s.store_driver = "local"
        s.data_dir.mkdir(parents=True, exist_ok=True)
        s.package_dir.mkdir(parents=True, exist_ok=True)
        return s

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key)

    @property
    def mode(self) -> str:
        """The studio runs in mock mode unless paid calls are explicitly enabled."""
        return "live" if self.allow_paid else "mock"


settings = Settings.load()
