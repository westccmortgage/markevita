"""Authenticated, explicitly approved first-clip preview.

GET routes never submit or poll a provider request. Every state-changing form
requires same-origin provenance and a timed token bound to the signed admin
session. The backend additionally locks the one permitted generation.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadData, URLSafeTimedSerializer

from . import auth, preview
from .config import settings
from .deps import require_admin, templates

router = APIRouter()
_CSRF_AGE = 30 * 60
_STATUS = {
    "queued": "Preparing the approved request",
    "submission_unknown": "Submission needs reconciliation; no new request will be sent",
    "submitted": "Generating your clip",
    "storage_pending": "Generation finished; saving the clip",
    "done": "Your clip is ready",
    "failed": "The preview needs attention; automatic regeneration is disabled",
}


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="studio-clip-preview-csrf")


def _session_fingerprint(request: Request) -> str:
    cookie = request.cookies.get(auth.COOKIE, "")
    return hashlib.sha256(cookie.encode()).hexdigest()


def _csrf_token(request: Request, admin: dict) -> str:
    return _signer().dumps({
        "session": _session_fingerprint(request),
        "email": admin["email"], "nonce": secrets.token_urlsafe(24),
    })


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def _check_form(request: Request, admin: dict, token: str) -> None:
    # A configured public origin wins over the upstream Render host. When it
    # is absent (local development), use the actual request host, never an
    # arbitrary x-forwarded-host supplied by the caller.
    expected = _origin(settings.public_url or str(request.base_url))
    supplied = request.headers.get("origin") or request.headers.get("referer") or ""
    if not expected or _origin(supplied) != expected:
        raise HTTPException(403, "Open the preview page and submit the form from this studio.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Open the preview page and submit the form from this studio.")
    try:
        payload = _signer().loads(token, max_age=_CSRF_AGE)
        valid = (
            isinstance(payload, dict)
            and payload.get("email") == admin["email"]
            and isinstance(payload.get("session"), str)
            and hmac.compare_digest(payload["session"], _session_fingerprint(request))
            and bool(payload.get("nonce"))
        )
    except (BadData, TypeError, ValueError):
        valid = False
    if not valid:
        raise HTTPException(403, "This form expired. Refresh the preview page and try again.")


def _private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _page(request: Request, *, error: str = "", status_code: int = 200):
    admin = require_admin(request)
    spec = preview.definition()
    problems = preview.problems()
    job = preview.current_job()
    state = str((job or {}).get("state", ""))
    # Never pass the full job to the template: it contains internal provider
    # request URLs and asset URLs that are not part of this public view.
    job_view = None if not job else {
        "id": job["id"], "state": state,
        "label": _STATUS.get(state, "Awaiting status"),
        "may_poll": state in {"queued", "submitted", "storage_pending", "submission_unknown"},
    }
    response = templates.TemplateResponse(request, "clip_preview.html", {
        "admin": admin, "spec": spec, "problems": problems,
        "job": job_view, "csrf_token": _csrf_token(request, admin),
        "ok": None, "err": error or None,
    }, status_code=status_code)
    return _private(response)


def _current_job(job_id: str) -> dict:
    job = preview.current_job()
    if not job or str(job.get("id", "")) != job_id:
        raise HTTPException(404, "This preview job was not found.")
    return job


@router.get("/clip-preview", response_class=HTMLResponse)
def clip_preview(request: Request):
    return _page(request)


@router.post("/clip-preview/start", response_class=HTMLResponse)
def start_clip_preview(request: Request, csrf_token: str = Form(""),
                       approved_digest: str = Form(""), approved_max_usd: str = Form(""),
                       approve: str = Form("")):
    admin = require_admin(request)
    _check_form(request, admin, csrf_token)
    if approve != "yes":
        return _page(request, error="Approve the displayed scene and spending limit before generating.", status_code=400)
    try:
        preview.start(actor=admin["email"], approved_digest=approved_digest,
                      approved_max_usd=approved_max_usd)
    except PermissionError:
        return _page(request, error="Generation is not enabled for this preview. Review the setup messages below.", status_code=403)
    except ValueError:
        return _page(request, error="The scene or spending approval changed. Review this page and approve the current preview.", status_code=400)
    except RuntimeError:
        return _page(request, error="The preview could not start. Check its status below before taking further action.", status_code=409)
    return _private(RedirectResponse(settings.url("/clip-preview"), status_code=303))


@router.post("/clip-preview/poll", response_class=HTMLResponse)
def poll_clip_preview(request: Request, job_id: str = Form(""), csrf_token: str = Form("")):
    admin = require_admin(request)
    _check_form(request, admin, csrf_token)
    _current_job(job_id)
    try:
        # refresh may only inspect the existing request and store its result.
        # It must never create a replacement request, even after a failure.
        preview.refresh(job_id)
    except (RuntimeError, ValueError, PermissionError):
        return _page(request, error="Status could not be updated. No replacement generation was requested.", status_code=409)
    return _private(RedirectResponse(settings.url("/clip-preview"), status_code=303))


@router.get("/clip-preview/media")
def clip_preview_media(request: Request, job_id: str = ""):
    require_admin(request)
    job = _current_job(job_id)
    if job.get("state") != "done":
        raise HTTPException(409, "The clip is not ready yet.")
    try:
        url = preview.media_url(job_id)
    except (RuntimeError, ValueError, PermissionError):
        raise HTTPException(503, "The saved clip is temporarily unavailable.") from None
    if not isinstance(url, str) or not _origin(url) or urlsplit(url).scheme != "https":
        raise HTTPException(503, "The saved clip is temporarily unavailable.")
    return _private(RedirectResponse(url, status_code=303))
