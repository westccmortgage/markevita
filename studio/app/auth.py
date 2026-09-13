"""Administrator authentication.

Two backends, same session:

* Supabase Auth (production). Email + password go to Supabase; the returned
  user must also appear in the `studio_admins` allow-list.
* Local administrator (development). Credentials come from STUDIO_ADMIN_EMAIL
  and STUDIO_ADMIN_PASSWORD so the panel can be opened without cloud setup.

The session is a signed, HttpOnly cookie. No provider secret and no Supabase
key is ever placed in it.
"""
from __future__ import annotations

import hmac
from datetime import datetime, timezone

from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import settings
from .store import store

COOKIE = "markevita_studio_session"
MAX_AGE = 60 * 60 * 12  # 12 hours

_serializer = URLSafeTimedSerializer(settings.session_secret, salt="studio-session")


class AuthError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_admin(email: str) -> bool:
    """True when the email is on the allow-list, or when the allow-list is
    empty and this is the configured local administrator (first-run)."""
    if not email:
        return False
    if store.get("studio_admins", {"email": email.lower()}):
        return True
    admins = store.list("studio_admins")
    if not admins and settings.admin_email and email.lower() == settings.admin_email.lower():
        store.upsert("studio_admins", {"email": email.lower(), "role": "owner", "created_at": _now()})
        return True
    return False


def sign_in(email: str, password: str) -> dict:
    """Verify credentials and return the session payload."""
    email = (email or "").strip().lower()
    if not email or not password:
        raise AuthError("Email and password are required.")

    if settings.supabase_configured and settings.store_driver == "supabase":
        user = _supabase_sign_in(email, password)
    else:
        user = _local_sign_in(email, password)

    if not is_admin(user["email"]):
        raise AuthError("This account is not an administrator of the studio.")
    record = store.get("studio_admins", {"email": user["email"]}) or {}
    session = {"email": user["email"], "role": record.get("role", "producer"), "at": _now()}
    store.insert("generation_history", {
        "event": "admin.sign_in", "actor": user["email"], "entity_type": "session",
        "detail": {"backend": user["backend"]}, "created_at": _now(),
    })
    return session


def _supabase_sign_in(email: str, password: str) -> dict:
    from supabase import create_client
    key = settings.supabase_anon_key or settings.supabase_service_key
    try:
        client = create_client(settings.supabase_url, key)
        res = client.auth.sign_in_with_password({"email": email, "password": password})
    except Exception as e:
        raise AuthError(f"Supabase rejected the sign-in: {e}") from e
    if not getattr(res, "user", None):
        raise AuthError("Invalid email or password.")
    return {"email": (res.user.email or email).lower(), "backend": "supabase"}


def _local_sign_in(email: str, password: str) -> dict:
    if not settings.admin_email or not settings.admin_password:
        raise AuthError(
            "No administrator is configured. Set STUDIO_ADMIN_EMAIL and "
            "STUDIO_ADMIN_PASSWORD in studio/.env, or configure Supabase Auth."
        )
    ok_email = hmac.compare_digest(email, settings.admin_email.strip().lower())
    ok_password = hmac.compare_digest(password, settings.admin_password)
    if not (ok_email and ok_password):
        raise AuthError("Invalid email or password.")
    return {"email": email, "backend": "local"}


def serialize(session: dict) -> str:
    return _serializer.dumps(session)


def deserialize(token: str) -> dict | None:
    try:
        return _serializer.loads(token, max_age=MAX_AGE)
    except BadSignature:
        return None
    except Exception:
        return None


def auth_backend() -> str:
    if settings.supabase_configured and settings.store_driver == "supabase":
        return "Supabase Auth"
    return "Local administrator"
