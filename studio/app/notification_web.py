"""Authenticated notification setup; browser consent is initiated by a click."""
import json
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import i18n, notifications
from .config import STUDIO_DIR, settings
from .deps import require_admin
from .preview_web import _check_form, _csrf_token
from .store import store

router = APIRouter()


@router.get('/notifications-sw.js', include_in_schema=False)
def worker():
    # Fixed script only. No caching/interception of authenticated pages or media.
    return FileResponse(STUDIO_DIR / 'app/static/notifications-sw.js', media_type='application/javascript',
        headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': settings.url('/')})


@router.get('/notifications')
def page(request: Request):
    from .web import render
    admin = require_admin(request)
    return render(request, 'notifications.html', csrf_token=_csrf_token(request, admin))


@router.get('/notifications/key')
def public_key(request: Request):
    require_admin(request)
    try:
        value = notifications.repository().keypair()['public']
    except Exception:
        raise HTTPException(503, 'Notification setup is unavailable. Try again shortly.') from None
    return JSONResponse({'public_key': value}, headers={'Cache-Control': 'no-store'})


@router.post('/notifications/subscription')
def subscription(request: Request, action: str = Form(...), subscription: str = Form(...),
                 csrf_token: str = Form(...)):
    admin = require_admin(request)
    _check_form(request, admin, csrf_token)
    if action not in ('enable', 'disable', 'test', 'status') or len(subscription) > 4096:
        raise HTTPException(400, 'Invalid notification request.')
    try:
        value = notifications.validate_subscription(json.loads(subscription))
    except (ValueError, TypeError):
        raise HTTPException(400, 'This browser push subscription is not supported.') from None
    try:
        repo = notifications.repository()
        key = repo.subscription_key(value)
        saved, etag = repo.read(key)
        owned = saved and saved['actor'] == admin['email']
        if action == 'status':
            return JSONResponse({'enabled': bool(owned)}, headers={'Cache-Control': 'no-store'})
        if action == 'enable':
            repo.subscribe(value, admin['email'], i18n.language(request), notifications.jobs_for(store, admin['email']))
        elif action == 'disable':
            if owned:
                repo.delete(key)
        elif action == 'test':
            if not owned:
                raise HTTPException(409, 'Enable notifications first.')
            if time.time() - saved.get('last_test', 0) < 30:
                raise HTTPException(429, 'Wait 30 seconds before another test.')
            saved['last_test'] = time.time()
            repo.write(key, saved, etag)
            notifications.send(repo, value, {
                'title': 'MarkeVita', 'body': i18n.translate('Notifications are working.', i18n.language(request)),
                'url': settings.url('/notifications'), 'tag': 'studio-test'})
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, 'Notification setup is unavailable. Try again shortly.') from None
    return JSONResponse({'ok': True}, headers={'Cache-Control': 'no-store'})
