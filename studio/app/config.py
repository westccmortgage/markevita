"""Studio configuration.

Reads the backend environment contract. Secret VALUES stay in this process:
nothing here is ever serialised to the browser or written to Supabase.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from . import PIPELINE_DIR

STUDIO_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = STUDIO_DIR.parent


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
    session_secret_ephemeral: bool = False
    store_fallback_reason: str = ""
    public_url: str = ""
    # Public path prefix the panel is served under, e.g. "/studio" when a CDN
    # proxies markevita.com/studio to this service. Empty when served at root.
    base_path: str = ""
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
            base_path=_env("STUDIO_BASE_PATH", ""),
            admin_email=_env("STUDIO_ADMIN_EMAIL"),
            admin_password=_env("STUDIO_ADMIN_PASSWORD"),
            supabase_url=_env("SUPABASE_URL"),
            supabase_anon_key=_env("SUPABASE_ANON_KEY"),
            supabase_service_key=_env("SUPABASE_SERVICE_ROLE_KEY"),
            public_url=_env("STUDIO_PUBLIC_URL").rstrip("/"),
        )
        if not s.session_secret:
            # Fallback: a per-process secret. Acceptable locally, but it means
            # every restart invalidates every session, so it is recorded and
            # reported rather than hidden.
            import secrets as _secrets
            s.session_secret = _secrets.token_hex(32)
            s.session_secret_ephemeral = True
        if s.store_driver == "supabase" and not (s.supabase_url and s.supabase_service_key):
            missing = [n for n, v in (("SUPABASE_URL", s.supabase_url),
                                      ("SUPABASE_SERVICE_ROLE_KEY", s.supabase_service_key)) if not v]
            s.store_fallback_reason = "missing " + ", ".join(missing)
            s.store_driver = "local"
        # Normalise: no trailing slash, always a leading slash when set.
        s.base_path = "/" + s.base_path.strip().strip("/") if s.base_path.strip().strip("/") else ""
        s.data_dir.mkdir(parents=True, exist_ok=True)
        s.package_dir.mkdir(parents=True, exist_ok=True)
        return s

    @property
    def deployed(self) -> bool:
        """True when this looks like a hosted deployment rather than a laptop:
        served under a public path prefix, or bound to a public interface."""
        return bool(self.base_path) or not self.host.startswith("127.")

    @property
    def env_hint(self) -> str:
        """Where an operator should actually set variables, for error text."""
        return ("the service's environment variables" if self.deployed
                else "studio/.env")

    def config_problems(self) -> list[dict]:
        """Configuration faults an operator needs to know about.

        Each is something that silently degrades the studio rather than
        failing loudly: the kind of fault you only discover by being locked
        out at an inconvenient moment.
        """
        problems: list[dict] = []
        can_auth = self.supabase_auth_configured or bool(self.admin_email and self.admin_password)
        if not can_auth:
            problems.append({
                "level": "fatal",
                "what": "Nobody can sign in.",
                "why": "No local administrator is configured and Supabase Auth is not set up.",
                "fix": f"Set STUDIO_ADMIN_EMAIL and STUDIO_ADMIN_PASSWORD in {self.env_hint}, "
                       "or configure SUPABASE_URL, SUPABASE_ANON_KEY and "
                       "SUPABASE_SERVICE_ROLE_KEY with STUDIO_STORE=supabase.",
            })
        if self.session_secret_ephemeral and self.deployed:
            problems.append({
                "level": "fatal",
                "what": "Everyone is signed out whenever the service restarts.",
                "why": "STUDIO_SESSION_SECRET is not set, so a new signing key is "
                       "generated on every boot. Hosts that sleep when idle "
                       "restart often, which makes this constant.",
                "fix": f"Set STUDIO_SESSION_SECRET in {self.env_hint} to a fixed random "
                       'value: python -c "import secrets;print(secrets.token_hex(32))"',
            })
        if self.store_fallback_reason:
            problems.append({
                "level": "fatal",
                "what": "STUDIO_STORE=supabase was requested but the local store is in use.",
                "why": f"Supabase is incompletely configured: {self.store_fallback_reason}.",
                "fix": f"Set the missing variable(s) in {self.env_hint} and redeploy.",
            })
        if self.supabase_auth_configured and not self.supabase_anon_key:
            problems.append({
                "level": "warning",
                "what": "Supabase Auth is running on the service role key.",
                "why": "SUPABASE_ANON_KEY is not set, so the privileged key is used for "
                       "sign-in calls that only need the public key.",
                "fix": f"Set SUPABASE_ANON_KEY in {self.env_hint}.",
            })
        if self.deployed and self.store_driver == "local" and not self.store_fallback_reason:
            problems.append({
                "level": "fatal",
                "what": "All records are lost when the service restarts.",
                "why": "STUDIO_STORE=local keeps series, episodes, scripts and jobs on "
                       "the container filesystem, which hosted platforms discard.",
                "fix": f"Apply supabase/migrations/0001_studio.sql, then set STUDIO_STORE=supabase "
                       f"with SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in {self.env_hint}.",
            })
        return problems

    def url(self, path: str = "/") -> str:
        """A browser-facing URL: the public prefix plus an internal path."""
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_path}{path}" if self.base_path else path

    @property
    def supabase_configured(self) -> bool:
        """Supabase usable as the RECORD STORE (server-side, service role)."""
        return bool(self.supabase_url and self.supabase_service_key)

    @property
    def supabase_auth_configured(self) -> bool:
        """Supabase usable for AUTHENTICATION.

        Deliberately independent of the record store: which database holds the
        series has nothing to do with who may sign in. Tying the two together
        made a degraded store silently downgrade authentication.
        """
        return bool(self.supabase_url and (self.supabase_anon_key or self.supabase_service_key))

    @property
    def mode(self) -> str:
        """The studio runs in mock mode unless paid calls are explicitly enabled."""
        return "live" if self.allow_paid else "mock"


settings = Settings.load()
