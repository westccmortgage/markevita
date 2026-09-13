"""Shared request helpers: session, current admin, template environment."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from .config import STUDIO_DIR, settings
from . import auth

templates = Jinja2Templates(directory=str(STUDIO_DIR / "app" / "templates"))


def current_admin(request: Request) -> dict | None:
    token = request.cookies.get(auth.COOKIE)
    return auth.deserialize(token) if token else None


def require_admin(request: Request) -> dict:
    admin = current_admin(request)
    if not admin:
        raise HTTPException(status_code=401, detail="Sign in required")
    return admin


def fmt_money(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def fmt_time(v) -> str:
    if not v:
        return "—"
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return str(v)


templates.env.filters["money"] = fmt_money
templates.env.filters["time"] = fmt_time
templates.env.globals["base"] = settings.base_path
templates.env.globals["mode"] = settings.mode
templates.env.globals["store_driver"] = settings.store_driver
templates.env.globals["auth_backend"] = auth.auth_backend()
