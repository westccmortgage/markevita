"""MarkeVita AI Series Studio — application entry point.

Mounts the admin panel (HTML) and the backend API (JSON) over the v0.3
production engine. Full episodes remain simulated. A separate first-clip
preview router requires its own enablement and explicit spending approval.
"""
from __future__ import annotations

import os
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .config import STUDIO_DIR, settings
from .preview_web import router as preview_router
from .web import router as web_router

app = FastAPI(
    title="MarkeVita AI Series Studio",
    description="Admin panel and production backend for the content-agnostic v0.3 series engine.",
    version="0.3.0",
)

app.mount("/static", StaticFiles(directory=str(STUDIO_DIR / "app" / "static")), name="static")
app.include_router(api_router)
app.include_router(web_router)
app.include_router(preview_router)


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
        # Identify the deployed build without exposing configuration values.
        "build_commit": commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None,
    }
