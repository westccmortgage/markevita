"""MarkeVita AI Series Studio — application entry point.

Mounts the admin panel (HTML) and the backend API (JSON) over the v0.3
production engine. Mock mode is enforced: no paid provider call is reachable
from this service while STUDIO_ALLOW_PAID is false.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .config import STUDIO_DIR, settings
from .web import router as web_router

app = FastAPI(
    title="MarkeVita AI Series Studio",
    description="Admin panel and production backend for the content-agnostic v0.3 series engine.",
    version="0.3.0",
)

app.mount("/static", StaticFiles(directory=str(STUDIO_DIR / "app" / "static")), name="static")
app.include_router(api_router)
app.include_router(web_router)


@app.exception_handler(401)
async def unauthorized(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Sign in required"}, status_code=401)
    target = settings.url(request.url.path)
    return RedirectResponse(settings.url(f"/login?next={target}"), status_code=303)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {
        "ok": True,
        "mode": settings.mode,
        "store": settings.store_driver,
        "paid_calls_enabled": settings.allow_paid,
    }
