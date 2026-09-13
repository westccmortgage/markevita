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


def cookie_is_secure(request) -> bool:
    """Whether to mark the session cookie Secure.

    Derived from the scheme the BROWSER used, not the interface the service
    binds to. Behind Netlify and Render the app itself speaks plain HTTP, so
    the bind address says nothing about the browser's connection — reading it
    would either drop the Secure flag on a public site or set it on a local
    HTTP one, where the cookie is then silently never sent back.
    """
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    scheme = forwarded or request.url.scheme
    return scheme == "https"

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

    if supabase_enabled():
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
    import httpx
    try:
        response = httpx.post(_auth_url("/token?grant_type=password"),
                              headers=_auth_headers(),
                              json={"email": email, "password": password}, timeout=20)
    except httpx.HTTPError as e:
        raise AuthError(f"Supabase could not be reached: {e}") from e
    if response.status_code >= 400:
        detail = _auth_error(response)
        if response.status_code in (400, 401):
            raise AuthError("Invalid email or password.")
        raise AuthError(f"Supabase rejected the sign-in: {detail}")
    user = (response.json() or {}).get("user") or {}
    return {"email": (user.get("email") or email).lower(), "backend": "supabase"}


def _local_sign_in(email: str, password: str) -> dict:
    if not settings.admin_email or not settings.admin_password:
        raise AuthError(
            "No administrator is configured. Set STUDIO_ADMIN_EMAIL and "
            f"STUDIO_ADMIN_PASSWORD in {settings.env_hint}, or configure Supabase Auth."
        )
    # Compare as bytes: compare_digest rejects non-ASCII str, and an accented
    # email or password would otherwise raise instead of failing cleanly.
    ok_email = hmac.compare_digest(email.encode(), settings.admin_email.strip().lower().encode())
    ok_password = hmac.compare_digest(password.encode(), settings.admin_password.encode())
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
    """Which backend verifies passwords.

    Depends only on whether Supabase Auth is configured. It is deliberately
    NOT tied to STUDIO_STORE: where the series records live says nothing about
    who may sign in, and coupling them made a degraded store silently fall
    back to local sign-in.
    """
    return "Supabase Auth" if settings.supabase_auth_configured else "Local administrator"


def supabase_enabled() -> bool:
    return settings.supabase_auth_configured


# ── Supabase Auth over its REST API ────────────────────────────────────────
# Called directly rather than through the SDK so that failures carry the
# provider's own message, and so the recovery flow does not depend on SDK
# session state that a stateless request cannot hold.

def _auth_url(path: str) -> str:
    return f"{settings.supabase_url.rstrip('/')}/auth/v1{path}"


def _auth_key() -> str:
    return settings.supabase_anon_key or settings.supabase_service_key


def _auth_headers() -> dict:
    return {"apikey": _auth_key(), "Content-Type": "application/json"}


def _auth_error(response) -> str:
    """The provider's own wording, which is more useful than ours."""
    try:
        body = response.json()
    except Exception:
        return f"HTTP {response.status_code}"
    for key in ("error_description", "msg", "message", "error"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return value
    return f"HTTP {response.status_code}"


def request_password_reset(email: str, redirect_to: str) -> None:
    """Send a recovery email.

    Always returns without error, even for an address that has no account:
    reporting which emails exist would turn this page into an account
    enumeration oracle.
    """
    if not supabase_enabled():
        raise AuthError(
            "Password recovery needs Supabase Auth. The local administrator "
            f"password is set directly in {settings.env_hint}."
        )
    import httpx
    try:
        # GoTrue reads ``redirect_to`` from the query string.  Putting it in
        # the JSON body is silently ignored, which makes recovery emails fall
        # back to the project's Site URL and strand the access token on the
        # public homepage instead of the studio reset form.
        httpx.post(_auth_url("/recover"), headers=_auth_headers(),
                   params={"redirect_to": redirect_to},
                   json={"email": email}, timeout=20)
    except httpx.HTTPError:
        # A transport failure is still not reported back to the browser, for
        # the same reason: the response must not vary with the address.
        pass


def verify_recovery_token(token_hash: str) -> str:
    """Exchange a recovery token_hash for an access token (server-side flow)."""
    import httpx
    response = httpx.post(_auth_url("/verify"), headers=_auth_headers(),
                          json={"type": "recovery", "token_hash": token_hash}, timeout=20)
    if response.status_code >= 400:
        raise AuthError(f"This recovery link is not valid or has expired. {_auth_error(response)}")
    token = (response.json() or {}).get("access_token")
    if not token:
        raise AuthError("This recovery link is not valid or has expired.")
    return token


def update_password(access_token: str, new_password: str) -> str:
    """Set a new password using a recovery access token. Returns the email."""
    if len(new_password or "") < 8:
        raise AuthError("The new password must be at least 8 characters.")
    import httpx
    response = httpx.put(
        _auth_url("/user"),
        headers={**_auth_headers(), "Authorization": f"Bearer {access_token}"},
        json={"password": new_password}, timeout=20,
    )
    if response.status_code >= 400:
        raise AuthError(f"The password was not changed. {_auth_error(response)}")
    email = ((response.json() or {}).get("email") or "").lower()
    store.insert("generation_history", {
        "event": "admin.password_reset", "actor": email or "unknown",
        "entity_type": "session", "detail": {}, "created_at": _now(),
    })
    return email
