"""MarkeVita AI Series Studio — application entry point.

Mounts the admin panel (HTML) and the backend API (JSON) over the v0.3
production engine. Full episodes remain simulated. A separate first-clip
preview router requires its own enablement and explicit spending approval.
"""
from __future__ import annotations

import html
import logging
import os
import re
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .config import STUDIO_DIR, settings
from .preview_web import router as preview_router
from .web import router as web_router
from .notification_web import router as notification_router
from .notifications import Dispatcher

app = FastAPI(
    title="MarkeVita AI Series Studio",
    description="Admin panel and production backend for the content-agnostic v0.3 series engine.",
    version="0.3.0",
)

app.mount("/static", StaticFiles(directory=str(STUDIO_DIR / "app" / "static")), name="static")
app.include_router(api_router)
app.include_router(web_router)
app.include_router(preview_router)
app.include_router(notification_router)

notification_dispatcher = Dispatcher()


@app.on_event('startup')
async def start_notifications():
    if settings.store_driver == 'supabase':
        notification_dispatcher.start()


@app.on_event('shutdown')
async def stop_notifications():
    notification_dispatcher.stop.set()


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """Say what broke, instead of a blank "Internal Server Error".

    A white page with two words costs a round trip through the operator to
    read the host's log, and this panel is only ever seen by a signed-in
    administrator. The exception and where it was raised go to the page; the
    full traceback goes to the log. Values are never included — only types,
    messages and code locations.
    """
    reference = uuid.uuid4().hex[:8]
    logging.getLogger(__name__).exception("Unhandled request error [%s] %s %s",
                                          reference, request.method, request.url.path)
    frames = [f for f in traceback.extract_tb(exc.__traceback__)
              if "/app/" in f.filename or "/serial/" in f.filename]
    where = " -> ".join(f"{Path(f.filename).name}:{f.lineno} ({f.name})" for f in frames[-4:])
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": type(exc).__name__, "reference": reference}, status_code=500)
    body = (f"<h1>Something in the studio broke</h1>"
            f"<p><b>{html.escape(type(exc).__name__)}</b>: {html.escape(str(exc)[:500])}</p>"
            f"<p><code>{html.escape(where)}</code></p>"
            f"<p>Reference <code>{reference}</code>. Nothing was changed by this request.</p>"
            f"<p><a href=\"{html.escape(settings.url('/'))}\">Back to the studio</a></p>")
    return HTMLResponse(f"<!doctype html><meta charset=utf-8>"
                        f"<style>body{{font:15px/1.6 system-ui;margin:40px;max-width:760px}}"
                        f"code{{background:#eee;padding:2px 5px;border-radius:4px}}</style>{body}",
                        status_code=500)


@app.exception_handler(401)
async def unauthorized(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Sign in required"}, status_code=401)
    target = settings.url(request.url.path)
    return RedirectResponse(settings.url(f"/login?next={target}"), status_code=303)


@app.on_event("startup")
async def report_configuration() -> None:
    """Print a configuration report at boot.

    Hosted platforms show this in their log stream, which is the only place an
    operator can see a fault that otherwise only appears as a failed sign-in.
    """
    problems = settings.config_problems()
    print(f"[studio] mode={settings.mode} store={settings.store_driver} "
          f"base_path={settings.base_path or '/'} deployed={settings.deployed}")
    from serial.fal_auth import key_id_prefix, key_problem
    fal_value = os.getenv('FAL_KEY')
    print(f"[studio] fal key ID prefix={key_id_prefix(fal_value) or 'unavailable'}; "
          f"format={key_problem(fal_value) or 'OK'}; provider access not checked")
    if not problems:
        print("[studio] configuration OK")
        return
    print(f"[studio] {len(problems)} CONFIGURATION PROBLEM(S):")
    for p in problems:
        print(f"[studio]   ! {p['what']}")
        print(f"[studio]     {p['why']}")
        print(f"[studio]     fix: {p['fix']}")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    commit = os.environ.get("RENDER_GIT_COMMIT", "")
    return {
        "ok": True,
        "mode": settings.mode,
        "store": settings.store_driver,
        "paid_calls_enabled": settings.allow_paid,
        # Count only: the detail is for the operator on the sign-in page and in
        # the logs, not for anonymous callers of a public endpoint.
        "configuration_problems": len(settings.config_problems()),
        "clip_preview_available": True,
        "episode_live_available": True,
        "episode_duration_configurable": True,
        "ui_languages": ["en", "ru"],
        "asset_download_recovery": True,
        "web_push_available": True,
        # Identify the deployed build without exposing configuration values.
        "build_commit": commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None,
    }
