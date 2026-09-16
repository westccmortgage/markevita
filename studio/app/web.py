"""Admin panel (server-rendered HTML).

Everything the producer needs: create a series, add seasons and episodes, paste
or upload scripts, manage characters, clothing, voices, locations,
relationships and secrets, review generated scenes and takes, approve
references and finished episodes, watch costs, and start/pause/resume
production.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import quote
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth, authoring, integrations, runner, scripts as scriptmod
from . import live_jobs
from . import i18n
from .preview_web import _check_form, _csrf_token
from .config import settings
from .deps import current_admin, require_admin, templates
from .ingest import history
from .packaging import DEFAULT_LIMITS
from .store import store

router = APIRouter()

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
# A saved fal request that was refused while being READ back. Only a request id
# this job itself reported may be retired, so the form cannot name another one.
REFUSED_REQUEST_RE = re.compile(r"HTTP 40[123] . request ([a-fA-F0-9]{8}-[a-fA-F0-9-]{27,40})")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")
    return (s or "series")[:64]


def _jsonlist(raw: str) -> list:
    """Accept either a JSON array or a comma/newline separated list."""
    raw = (raw or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            v = json.loads(raw)
            if isinstance(v, list):
                return v
        except json.JSONDecodeError:
            pass
    return [p.strip() for p in re.split(r"[\n,]", raw) if p.strip()]


def _redirect(path: str, ok: str = "", err: str = "") -> RedirectResponse:
    """Redirect the browser. `path` is an internal path; the public prefix
    (STUDIO_BASE_PATH) is added here so one call site cannot forget it."""
    sep = "&" if "?" in path else "?"
    if ok:
        path = f"{path}{sep}ok={ok}"
    elif err:
        path = f"{path}{sep}err={err}"
    return RedirectResponse(settings.url(path), status_code=303)


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("admin", current_admin(request))
    ctx.setdefault("ok", request.query_params.get("ok"))
    ctx.setdefault("err", request.query_params.get("err"))
    ctx.setdefault("all_series", store.list("series", order="title"))
    ctx.setdefault("mode", settings.mode)
    if settings.allow_paid and "This build is mock-only" in str(ctx.get("err") or ""):
        ctx["err"] = None
    response = templates.TemplateResponse(request, template, ctx)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer" if request.url.path in ("/reset", "/forgot") else "same-origin"
    return response


# ── auth ───────────────────────────────────────────────────────────────────

@router.post("/ui-language")
def set_ui_language(request: Request, language: str = Form(...), next: str = Form("")):
    if language not in i18n.LANGUAGES:
        raise HTTPException(400, "Unsupported interface language")
    response = RedirectResponse(i18n.safe_return(next, settings.base_path), status_code=303)
    response.set_cookie(i18n.COOKIE, language, max_age=365 * 24 * 60 * 60,
                        path=settings.base_path or "/", httponly=True, samesite="lax",
                        secure=settings.public_url.startswith("https://") or request.url.scheme == "https")
    response.headers["Cache-Control"] = "no-store"
    return response

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_admin(request):
        return RedirectResponse(settings.url("/"), status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "err": request.query_params.get("err"),
        "ok": request.query_params.get("ok"),
        "next": request.query_params.get("next") or settings.url("/"),
        "admin": None, "all_series": [],
        # Without this an operator locked out by configuration sees only
        # "invalid password" and has nowhere to look.
        "problems": settings.config_problems(),
    })


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          next: str = Form("/")):
    try:
        session = auth.sign_in(email, password)
    except auth.AuthError as e:
        return _redirect("/login", err=str(e))
    # `next` arrives already public-prefixed from the sign-in form.
    response = RedirectResponse(next or settings.url("/"), status_code=303)
    response.set_cookie(
        auth.COOKIE, auth.serialize(session), max_age=auth.MAX_AGE,
        httponly=True, samesite="lax", path=settings.base_path or "/",
        secure=auth.cookie_is_secure(request),
    )
    return response


def _public_origin(request: Request) -> str:
    """The origin the BROWSER used, for building recovery links.

    Behind Netlify the service sees Render's own host, so the forwarded
    headers are the only source of markevita.com. STUDIO_PUBLIC_URL overrides
    both when a platform does not forward them.
    """
    if settings.public_url:
        return settings.public_url
    headers = request.headers
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").split(",")[0].strip()
    scheme = (headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    return f"{scheme}://{host}" if host else str(request.base_url).rstrip("/")


@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request):
    return templates.TemplateResponse(request, "forgot.html", {
        "err": request.query_params.get("err"), "ok": request.query_params.get("ok"),
        "supabase": auth.supabase_enabled(), "admin": None, "all_series": [],
    })


@router.post("/forgot")
def forgot(request: Request, email: str = Form(...)):
    reset_url = _public_origin(request) + settings.url("/reset")
    try:
        auth.request_password_reset(email.strip().lower(), reset_url)
    except auth.AuthError as e:
        return _redirect("/forgot", err=str(e))
    # The same answer regardless of whether the address has an account.
    return _redirect("/forgot", ok="If that address has an account, a recovery link is on its way.")


@router.get("/reset", response_class=HTMLResponse)
def reset_form(request: Request):
    """Landing page for a recovery link.

    Supabase returns either `?token_hash=` (verified server-side) or an
    implicit-flow `#access_token=` fragment, which never reaches the server —
    a little JavaScript moves it into the form.
    """
    return templates.TemplateResponse(request, "reset.html", {
        "err": request.query_params.get("err"),
        "token_hash": request.query_params.get("token_hash", ""),
        "admin": None, "all_series": [],
    })


@router.post("/reset")
def reset(request: Request, password: str = Form(...), confirm: str = Form(""),
          token_hash: str = Form(""), access_token: str = Form("")):
    if password != confirm:
        return _redirect("/reset", err="The two passwords do not match.")
    try:
        if token_hash:
            access_token = auth.verify_recovery_token(token_hash)
        if not access_token:
            return _redirect("/reset", err="This page needs a valid recovery link. Request a new one.")
        email = auth.update_password(access_token, password)
    except auth.AuthError as e:
        return _redirect("/reset", err=str(e))
    if email and not auth.is_admin(email):
        return _redirect("/login", err="Password updated, but this account is not a studio administrator.")
    return _redirect("/login", ok="Password updated. Sign in with the new password.")


@router.get("/logout")
def logout():
    response = RedirectResponse(settings.url("/login"), status_code=303)
    response.delete_cookie(auth.COOKIE, path=settings.base_path or "/")
    return response


# ── dashboard ──────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    require_admin(request)
    series = store.list("series", order="title")
    episodes = store.list("episodes", order="number")
    active, seen_live = [], set()
    for job in store.list('production_jobs', order='created_at', desc=True, limit=60):
        if job.get('stages') == ['runtime_lease']:
            continue
        if job.get('mode') == 'live' and job.get('stages') != ['clip_preview']:
            key = (job.get('series_id'), job.get('episode_id'))
            if key in seen_live:
                continue
            seen_live.add(key)
        if job.get('state') in ('queued', 'running', 'paused', 'pausing', 'submitted', 'storage_pending', 'submission_unknown'):
            active.append(job)
    costs = store.list("costs")
    preview_costs = [c for c in costs if c.get("stage") == "clip_preview"]
    simulated_costs = [c for c in costs if c.get("stage") != "clip_preview" and not c.get("stage", "").startswith("live/")]
    live_costs = [c for c in costs if c.get("stage", "").startswith("live/")]
    providers = integrations.status_all()
    return render(request, "dashboard.html",
                  series=series, episodes=episodes, active_jobs=active,
                  total_cost=round(sum(float(c.get("actual_usd") or 0) for c in simulated_costs), 2),
                  live_estimate=round(sum(float(c.get("actual_usd") or 0) for c in live_costs), 2),
                  preview_estimate=round(sum(float(c.get("estimated_usd") or 0) for c in preview_costs), 2),
                  has_preview_cost=bool(preview_costs),
                  providers=providers,
                  missing=[p for p in providers if not p["connected"]],
                  recent=store.list("generation_history", order="created_at", desc=True, limit=12))


# ── series ─────────────────────────────────────────────────────────────────

@router.post("/series")
def create_series(request: Request, title: str = Form(...), series_id: str = Form(""),
                  logline: str = Form(""), genre: str = Form(""), language: str = Form("en-US"),
                  aspect_ratio: str = Form("9:16")):
    a = require_admin(request)
    sid = _slug(series_id or title)
    if not ID_RE.match(sid):
        return _redirect("/", err="Series id must be lowercase letters, digits and underscores.")
    if store.get("series", {"id": sid}):
        return _redirect(f"/series/{sid}", err="A series with that id already exists.")
    width, height = (1080, 1920) if aspect_ratio == "9:16" else (1920, 1080)
    store.upsert("series", {
        "id": sid, "title": title, "logline": logline, "genre": genre, "language": language,
        "format": {"aspect_ratio": aspect_ratio, "width": width, "height": height, "captions": "both"},
        "production_limits": dict(DEFAULT_LIMITS), "approval": {"status": "draft"},
        "style": {"style_sentence": ""}, "status": "draft",
        "created_at": _now(), "updated_at": _now(),
    })
    store.upsert("seasons", {"series_id": sid, "season_id": "s01", "number": 1,
                             "title": "Season 1", "arc": "", "episode_order": []})
    history(sid, "", "series.created", entity_type="series", entity_id=sid, actor=a["email"])
    return _redirect(f"/series/{sid}", ok="Series created.")


@router.get("/series/{series_id}", response_class=HTMLResponse)
def series_page(request: Request, series_id: str):
    require_admin(request)
    s = store.get("series", {"id": series_id})
    if not s:
        raise HTTPException(404, "series not found")
    validation = runner.validate_series(series_id)
    return render(request, "series.html", s=s, setup=authoring.setup_problems(series_id),
                  fill=authoring.fill_state(series_id),
                  seasons=store.list("seasons", {"series_id": series_id}, order="number"),
                  episodes=store.list("episodes", {"series_id": series_id}, order="number"),
                  characters=store.list("characters", {"series_id": series_id}, order="character_id"),
                  locations=store.list("locations", {"series_id": series_id}, order="location_id"),
                  relationships=store.list("relationships", {"series_id": series_id}),
                  secrets=store.list("secrets_bible", {"series_id": series_id}),
                  validation=validation, **_video_model_choices())


SUBTITLES = {"both": "on the video, plus a separate file", "burned": "on the video",
             "srt": "separate file only", "none": "off"}


def _video_model_choices() -> dict:
    """What the producer picks between, with the rate each one bills at."""
    from serial.config import DEFAULT_VIDEO_MODEL, VIDEO_MODELS
    from serial import costs
    rates = {}
    for endpoint in VIDEO_MODELS:
        silent = costs.video_cost(1, False, "1080p", endpoint)
        audio = costs.video_cost(1, True, "1080p", endpoint)
        rates[endpoint] = f"${silent:.2f}/s, ${audio:.2f}/s with native audio"
    return {"video_models": sorted(VIDEO_MODELS.items(), key=lambda kv: kv[1]),
            "video_model_default": DEFAULT_VIDEO_MODEL, "video_model_rates": rates,
            "picture_levels": list(live_jobs.PICTURE)}


@router.post("/series/{series_id}/settings")
def series_settings(request: Request, series_id: str, title: str = Form(...),
                    logline: str = Form(""), genre: str = Form(""), language: str = Form("en-US"),
                    captions: str = Form("both"), budget: str = Form("50"),
                    regenerations: str = Form("2"), min_scenes: str = Form("12"),
                    max_scenes: str = Form("18"), min_seconds: str = Form(""),
                    max_seconds: str = Form(""), style_sentence: str = Form(""),
                    camera_rules: str = Form(""), color_rules: str = Form(""),
                    negative_image: str = Form(""), negative_video: str = Form(""),
                    video_model: str = Form(""), picture: str = Form("")):
    a = require_admin(request)
    s = store.get("series", {"id": series_id})
    if not s:
        raise HTTPException(404, "series not found")
    fmt = dict(s.get("format") or {})
    if captions not in ("both", "burned", "srt", "none"):
        return _redirect(f"/series/{series_id}", err="Choose one of the subtitle options.")
    fmt["captions"] = captions
    limits = {**DEFAULT_LIMITS, **(s.get("production_limits") or {})}
    from serial.config import VIDEO_MODELS
    if video_model:
        if video_model not in VIDEO_MODELS:
            return _redirect(f"/series/{series_id}", err="Choose one of the supported video models.")
        limits["video_model"] = video_model
    if picture:
        if picture not in live_jobs.PICTURE:
            return _redirect(f"/series/{series_id}", err="Choose one of the picture settings.")
        limits["picture"] = picture
    try:
        limits["maximum_episode_budget_usd"] = float(budget)
        limits["maximum_regenerations_per_scene"] = int(regenerations)
        limits["min_scenes"] = int(min_scenes)
        limits["max_scenes"] = int(max_scenes)
        if min_seconds.strip():
            limits["min_episode_seconds"] = int(min_seconds)
        if max_seconds.strip():
            limits["max_episode_seconds"] = int(max_seconds)
        if not (1 <= limits["min_scenes"] <= limits["max_scenes"] and
                1 <= limits["min_episode_seconds"] <= limits["max_episode_seconds"]):
            raise ValueError("Invalid range")
    except ValueError:
        return _redirect(f"/series/{series_id}", err="Use positive scene counts and durations; minimum must not exceed maximum.")
    store.update("series", {"id": series_id}, {
        "title": title, "logline": logline, "genre": genre, "language": language,
        "format": fmt, "production_limits": limits,
        "style": {"style_sentence": style_sentence, "camera_rules": camera_rules,
                  "color_rules": color_rules, "negative_image": negative_image,
                  "negative_video": negative_video},
        "updated_at": _now(),
    })
    history(series_id, "", "series.updated", entity_type="series", entity_id=series_id, actor=a["email"])
    return _redirect(f"/series/{series_id}", ok="Series saved.")


@router.post("/series/{series_id}/seasons")
def add_season(request: Request, series_id: str, season_id: str = Form(...),
               number: str = Form("1"), title: str = Form(""), arc: str = Form("")):
    require_admin(request)
    sid = _slug(season_id)
    if not ID_RE.match(sid):
        return _redirect(f"/series/{series_id}", err="Invalid season id.")
    store.upsert("seasons", {"series_id": series_id, "season_id": sid,
                             "number": int(number or 1), "title": title, "arc": arc,
                             "episode_order": []})
    return _redirect(f"/series/{series_id}", ok="Season added.")


@router.post("/series/{series_id}/episodes")
def add_episode(request: Request, series_id: str, episode_id: str = Form(...),
                season_id: str = Form("s01"), number: str = Form("1"),
                title: str = Form(""), logline: str = Form("")):
    a = require_admin(request)
    eid = _slug(episode_id)
    if not ID_RE.match(eid):
        return _redirect(f"/series/{series_id}", err="Invalid episode id.")
    if store.get("episodes", {"series_id": series_id, "episode_id": eid}):
        return _redirect(f"/series/{series_id}", err="That episode already exists.")
    limits = (store.get("series", {"id": series_id}) or {}).get("production_limits") or {}
    store.upsert("episodes", {
        "series_id": series_id, "episode_id": eid, "season_id": season_id,
        "number": int(number or 1), "title": title, "logline": logline, "status": "draft",
        "budget_usd": float(limits.get("maximum_episode_budget_usd") or 50),
        "spent_usd": 0, "created_at": _now(), "updated_at": _now(),
    })
    season = store.get("seasons", {"series_id": series_id, "season_id": season_id})
    if season:
        order = list(season.get("episode_order") or [])
        if eid not in order:
            order.append(eid)
            store.update("seasons", {"series_id": series_id, "season_id": season_id},
                         {"episode_order": order})
    history(series_id, eid, "episode.created", entity_type="episode", entity_id=eid, actor=a["email"])
    return _redirect(f"/series/{series_id}/episodes/{eid}", ok="Episode created.")


# ── characters, clothing, voices ───────────────────────────────────────────

# ── the screen the producer actually works on ──────────────────────────────

@router.post("/series/{series_id}/next-episode")
def open_next_episode(request: Request, series_id: str):
    a = require_admin(request)
    try:
        episode = authoring.next_episode(series_id, a["email"])
    except authoring.AuthoringError as e:
        return _redirect(f"/series/{series_id}", err=str(e))
    note = ("Continuing where you left off." if episode.get("reused")
            else f"Episode {episode['number']} opened.")
    return _redirect(f"/series/{series_id}/episodes/{episode['episode_id']}/studio", ok=note)


def _estimate(series_id: str, episode_id: str, audio_mode: str = "native") -> dict:
    """Length, an estimated cost and the cap. The figure is an estimate."""
    from serial.config import Config, VIDEO_MODELS
    from serial.package import SeriesPackage, estimate_first_pass, validate_episode
    from .config import PIPELINE_DIR
    from .live_jobs import video_model
    from .packaging import materialize
    try:
        cfg = Config.load(PIPELINE_DIR, live=False)
        pkg = SeriesPackage(materialize(series_id))
        # The figure must be the chosen model's, not the server default's:
        # Veo 3.1 bills about twice what Fast does per second of video.
        cfg.fal_video_model = video_model(pkg)
        for field, value in live_jobs.PICTURE[live_jobs.picture(pkg)].items():
            setattr(cfg, field, value)
        # Native scene audio is what the form offers first, and Veo bills more
        # for it. Estimating silent here would understate the usual run.
        cfg.video_generate_audio = audio_mode != "voices"
        norm = validate_episode(pkg, pkg.load_episode(episode_id), None, cfg)
        refs = store.list("reference_assets", {"series_id": series_id})
        est = estimate_first_pass(norm, pkg, cfg, not refs)
        return {"ok": True, "seconds": norm["total_seconds"], "clips": len(norm["scenes"]),
                "estimate_usd": est["total_first_pass"], "cap_usd": est["budget_cap"],
                "model": VIDEO_MODELS[cfg.fal_video_model],
                "warnings": norm.get("warnings") or []}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@router.get("/series/{series_id}/episodes/{episode_id}/studio", response_class=HTMLResponse)
def episode_studio(request: Request, series_id: str, episode_id: str):
    require_admin(request)
    s = store.get("series", {"id": series_id})
    ep = store.get("episodes", {"series_id": series_id, "episode_id": episode_id})
    if not s or not ep:
        raise HTTPException(404, "episode not found")
    scenes = store.list("scenes", {"series_id": series_id, "episode_id": episode_id},
                        order="sequence")
    memory = authoring.series_memory(series_id, episode_id)
    runtime = runner.episode_runtime(series_id, episode_id)
    # What stops this episode being written at all. The producer used to type a
    # wish, press the button and only then learn the series has no cast — and
    # the refusal gave no way to go and fix it.
    blockers = authoring.setup_problems(series_id)
    # One screen: the plain-words half above, the technical half from the old
    # episode page folded in below it behind a disclosure.
    context = _episode_context(request, series_id, episode_id)
    context.update(
        blockers=blockers, memory=memory, fill=authoring.fill_state(series_id),
        writing=authoring.work_state(series_id, "script", episode_id),
        script=authoring.readable(scenes, memory),
        estimate=_estimate(series_id, episode_id, runtime.get("audio_mode", "native"))
                 if scenes else None,
        subtitles=i18n.translate(
            SUBTITLES.get((s.get("format") or {}).get("captions", "both"), SUBTITLES["both"]),
            i18n.language(request)),
        voiceless=[c["id"] for c in memory["characters"]
                   if c["on_camera"] and not c["has_voice"]])
    return render(request, "authoring.html", **context)


@router.post("/series/{series_id}/episodes/{episode_id}/draft")
def draft_script(request: Request, series_id: str, episode_id: str, wish: str = Form("")):
    a = require_admin(request)
    back = f"/series/{series_id}/episodes/{episode_id}/studio"
    try:
        started = authoring.start_draft(series_id, episode_id, wish, a["email"])
    except authoring.AuthoringError as e:
        # The wish is kept so nothing typed is lost.
        return _redirect(f"{back}?wish={quote(wish[:2000])}", err=str(e))
    return _redirect(back, ok="Already writing. This page refreshes itself."
                     if started.get("already") else
                     "Writing the episode. It takes a minute or two; this page refreshes itself.")


@router.post("/series/{series_id}/episodes/{episode_id}/revise")
def revise_script(request: Request, series_id: str, episode_id: str, instruction: str = Form("")):
    a = require_admin(request)
    back = f"/series/{series_id}/episodes/{episode_id}/studio"
    try:
        started = authoring.start_revise(series_id, episode_id, instruction, a["email"])
    except authoring.AuthoringError as e:
        return _redirect(f"{back}?instruction={quote(instruction[:2000])}", err=str(e))
    return _redirect(back, ok="Already applying a change. This page refreshes itself."
                     if started.get("already") else
                     "Applying the change. It takes a minute or two; this page refreshes itself.")


@router.get("/series/{series_id}/characters", response_class=HTMLResponse)
def characters_page(request: Request, series_id: str):
    require_admin(request)
    s = store.get("series", {"id": series_id})
    if not s:
        raise HTTPException(404, "series not found")
    chars = store.list("characters", {"series_id": series_id}, order="character_id")
    for c in chars:
        c["_clothing"] = store.list("clothing", {"series_id": series_id,
                                                 "character_id": c["character_id"]}, order="variant_id")
        c["_voice"] = store.get("voices", {"series_id": series_id, "character_id": c["character_id"]})
    return render(request, "characters.html", s=s, characters=chars,
                  props=store.list("props", {"series_id": series_id}, order="prop_id"),
                  voice_slots=authoring.voice_choices(series_id),
                  fill=authoring.fill_state(series_id),
                  drafted=authoring.drafted_ids(series_id))


@router.get("/selfcheck", response_class=HTMLResponse)
def selfcheck_page(request: Request):
    """Fetch every screen against this server's own database and report."""
    require_admin(request)
    from fastapi.testclient import TestClient
    from . import selfcheck
    from .main import app
    client = TestClient(app, base_url=str(request.base_url).rstrip("/"),
                        raise_server_exceptions=False)
    client.cookies.set(auth.COOKIE, request.cookies.get(auth.COOKIE, ""))
    prefix = settings.base_path.rstrip("/")

    class _Prefixed:
        def get(self, path, **kw):
            return client.get(prefix + path, **kw)

    results = selfcheck.run(_Prefixed())
    return render(request, "selfcheck.html", results=results,
                  settings_in_force=selfcheck.settings_in_force(),
                  failures=[r for r in results if not r["ok"]])


@router.post("/series/{series_id}/fill-bible")
def fill_bible(request: Request, series_id: str, back: str = Form("")):
    """Complete the cast and places from what the series already established."""
    a = require_admin(request)
    target = back if back.startswith(f"/series/{series_id}") else f"/series/{series_id}/characters"
    try:
        started = authoring.start_fill(series_id, a["email"])
    except authoring.AuthoringError as e:
        return _redirect(target, err=str(e))
    except Exception as e:
        logging.getLogger(__name__).exception("Filling the bible failed")
        return _redirect(target, err=f"The studio could not start writing the bible: {e}")
    return _redirect(target, ok="Already writing. This page refreshes itself."
                     if started.get("already") else
                     "Writing the cast and places. It takes a minute or two; "
                     "this page refreshes itself.")


@router.post("/series/{series_id}/characters")
def save_character(request: Request, series_id: str, character_id: str = Form(...),
                   name: str = Form(...), visual: str = Form("yes"), role: str = Form(""),
                   age: str = Form(""), appearance: str = Form(""), behavior: str = Form(""),
                   immutable: str = Form("")):
    a = require_admin(request)
    cid = _slug(character_id)
    if not ID_RE.match(cid):
        return _redirect(f"/series/{series_id}/characters", err="Invalid character id.")
    store.upsert("characters", {
        "series_id": series_id, "character_id": cid, "name": name,
        "visual": visual == "yes", "role": role, "age": age,
        "appearance": appearance, "behavior": behavior,
        "immutable": _jsonlist(immutable), "props": [], "seed_assets": [],
        "updated_at": _now(),
    })
    slot = authoring.next_free_slot(series_id)
    if slot and not store.get("voices", {"series_id": series_id, "character_id": cid}):
        store.upsert("voices", {
            "series_id": series_id, "character_id": cid, "provider": "elevenlabs",
            "voice_env": slot, "model_id": "eleven_v3",
            "language": (store.get("series", {"id": series_id}) or {}).get("language", "en-US"),
            "style_notes": "", "phone_fx": False, "locked": False,
        })
    history(series_id, "", "character.saved", entity_type="character", entity_id=cid, actor=a["email"])
    return _redirect(f"/series/{series_id}/characters", ok=f"Character '{cid}' saved.")


@router.post("/series/{series_id}/characters/{character_id}/delete")
def delete_character(request: Request, series_id: str, character_id: str):
    require_admin(request)
    store.delete("characters", {"series_id": series_id, "character_id": character_id})
    store.delete("clothing", {"series_id": series_id, "character_id": character_id})
    store.delete("voices", {"series_id": series_id, "character_id": character_id})
    return _redirect(f"/series/{series_id}/characters", ok="Character removed.")


@router.post("/series/{series_id}/characters/{character_id}/clothing")
def save_clothing(request: Request, series_id: str, character_id: str,
                  variant_id: str = Form(...), description: str = Form(""),
                  is_default: str = Form(""), immutable: str = Form("")):
    require_admin(request)
    vid = _slug(variant_id)
    if not ID_RE.match(vid):
        return _redirect(f"/series/{series_id}/characters", err="Invalid wardrobe id.")
    if is_default == "yes":
        for v in store.list("clothing", {"series_id": series_id, "character_id": character_id}):
            store.update("clothing", {"series_id": series_id, "character_id": character_id,
                                      "variant_id": v["variant_id"]}, {"is_default": False})
    store.upsert("clothing", {
        "series_id": series_id, "character_id": character_id, "variant_id": vid,
        "is_default": is_default == "yes", "description": description,
        "immutable": _jsonlist(immutable),
    })
    return _redirect(f"/series/{series_id}/characters", ok=f"Wardrobe '{vid}' saved.")


@router.post("/series/{series_id}/characters/{character_id}/clothing/{variant_id}/delete")
def delete_clothing(request: Request, series_id: str, character_id: str, variant_id: str):
    require_admin(request)
    store.delete("clothing", {"series_id": series_id, "character_id": character_id,
                              "variant_id": variant_id})
    return _redirect(f"/series/{series_id}/characters", ok="Wardrobe variant removed.")


@router.post("/series/{series_id}/characters/{character_id}/voice")
def save_voice(request: Request, series_id: str, character_id: str,
               voice_env: str = Form(""), model_id: str = Form("eleven_v3"),
               language: str = Form("en-US"), style_notes: str = Form(""),
               phone_fx: str = Form(""), locked: str = Form("")):
    a = require_admin(request)
    env_name = (voice_env or f"ELEVENLABS_VOICE_ID_{character_id.upper()}").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]+", env_name):
        return _redirect(f"/series/{series_id}/characters",
                         err="The voice variable name may contain only A-Z, 0-9 and underscores.")
    if not authoring.voice_env_is_known(env_name):
        return _redirect(f"/series/{series_id}/characters",
                         err="Choose one of this server's voice slots.")
    store.upsert("voices", {
        "series_id": series_id, "character_id": character_id, "provider": "elevenlabs",
        "voice_env": env_name, "model_id": model_id, "language": language,
        "style_notes": style_notes, "phone_fx": phone_fx == "yes", "locked": locked == "yes",
    })
    history(series_id, "", "voice.assigned", entity_type="voice", entity_id=character_id,
            actor=a["email"], detail={"voice_env": env_name})
    ready = any(slot["env"] == env_name and slot["configured"]
                for slot in authoring.voice_slots())
    return _redirect(f"/series/{series_id}/characters",
                     ok=f"Voice bound to {env_name}." if ready else
                        f"Voice bound to {env_name}, but that slot is empty on this server.")


# ── locations and props ────────────────────────────────────────────────────

@router.get("/series/{series_id}/locations", response_class=HTMLResponse)
def locations_page(request: Request, series_id: str):
    require_admin(request)
    s = store.get("series", {"id": series_id})
    if not s:
        raise HTTPException(404, "series not found")
    return render(request, "locations.html", s=s,
                  locations=store.list("locations", {"series_id": series_id}, order="location_id"),
                  props=store.list("props", {"series_id": series_id}, order="prop_id"))


@router.post("/series/{series_id}/locations")
def save_location(request: Request, series_id: str, location_id: str = Form(...),
                  name: str = Form(...), description: str = Form(""),
                  lighting_states: str = Form(""), marks: str = Form(""),
                  immutable: str = Form("")):
    require_admin(request)
    lid = _slug(location_id)
    if not ID_RE.match(lid):
        return _redirect(f"/series/{series_id}/locations", err="Invalid location id.")
    states: dict[str, str] = {}
    for line in (lighting_states or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            states[_slug(k) or "default"] = v.strip()
    states.setdefault("default", states.get("default", ""))
    store.upsert("locations", {
        "series_id": series_id, "location_id": lid, "name": name, "description": description,
        "lighting_states": states, "marks": marks, "immutable": _jsonlist(immutable),
        "seed_assets": [],
    })
    return _redirect(f"/series/{series_id}/locations", ok=f"Location '{lid}' saved.")


@router.post("/series/{series_id}/locations/{location_id}/delete")
def delete_location(request: Request, series_id: str, location_id: str):
    require_admin(request)
    store.delete("locations", {"series_id": series_id, "location_id": location_id})
    return _redirect(f"/series/{series_id}/locations", ok="Location removed.")


@router.post("/series/{series_id}/props")
def save_prop(request: Request, series_id: str, prop_id: str = Form(...),
              description: str = Form("")):
    require_admin(request)
    pid = _slug(prop_id)
    if not ID_RE.match(pid):
        return _redirect(f"/series/{series_id}/locations", err="Invalid prop id.")
    store.upsert("props", {"series_id": series_id, "prop_id": pid, "description": description})
    return _redirect(f"/series/{series_id}/locations", ok=f"Prop '{pid}' saved.")


@router.post("/series/{series_id}/props/{prop_id}/delete")
def delete_prop(request: Request, series_id: str, prop_id: str):
    require_admin(request)
    store.delete("props", {"series_id": series_id, "prop_id": prop_id})
    return _redirect(f"/series/{series_id}/locations", ok="Prop removed.")


# ── relationships, secrets, knowledge ──────────────────────────────────────

@router.get("/series/{series_id}/world", response_class=HTMLResponse)
def world_page(request: Request, series_id: str):
    require_admin(request)
    s = store.get("series", {"id": series_id})
    if not s:
        raise HTTPException(404, "series not found")
    return render(request, "world.html", s=s,
                  characters=store.list("characters", {"series_id": series_id}, order="character_id"),
                  relationships=store.list("relationships", {"series_id": series_id}, order="rel_id"),
                  secrets=store.list("secrets_bible", {"series_id": series_id}, order="secret_id"),
                  knowledge=store.list("knowledge_state", {"series_id": series_id}, order="episode_id"),
                  episodes=store.list("episodes", {"series_id": series_id}, order="number"))


@router.post("/series/{series_id}/relationships")
def save_relationship(request: Request, series_id: str, rel_id: str = Form(...),
                      a_char: str = Form(...), b_char: str = Form(...), type: str = Form(""),
                      state: str = Form(""), allowed_states: str = Form(""),
                      is_public: str = Form(""), note: str = Form("")):
    require_admin(request)
    rid = _slug(rel_id)
    if not ID_RE.match(rid):
        return _redirect(f"/series/{series_id}/world", err="Invalid relationship id.")
    store.upsert("relationships", {
        "series_id": series_id, "rel_id": rid, "a": _slug(a_char), "b": _slug(b_char),
        "type": type, "state": state, "is_public": is_public == "yes",
        "allowed_states": _jsonlist(allowed_states), "note": note,
    })
    return _redirect(f"/series/{series_id}/world", ok=f"Relationship '{rid}' saved.")


@router.post("/series/{series_id}/relationships/{rel_id}/delete")
def delete_relationship(request: Request, series_id: str, rel_id: str):
    require_admin(request)
    store.delete("relationships", {"series_id": series_id, "rel_id": rel_id})
    return _redirect(f"/series/{series_id}/world", ok="Relationship removed.")


@router.post("/series/{series_id}/secrets")
def save_secret(request: Request, series_id: str, secret_id: str = Form(...),
                description: str = Form(""), holders_initial: str = Form(""),
                stakes: str = Form("")):
    require_admin(request)
    sid = _slug(secret_id)
    if not ID_RE.match(sid):
        return _redirect(f"/series/{series_id}/world", err="Invalid secret id.")
    store.upsert("secrets_bible", {
        "series_id": series_id, "secret_id": sid, "description": description,
        "holders_initial": [_slug(h) for h in _jsonlist(holders_initial)], "stakes": stakes,
    })
    return _redirect(f"/series/{series_id}/world", ok=f"Secret '{sid}' saved.")


@router.post("/series/{series_id}/secrets/{secret_id}/delete")
def delete_secret(request: Request, series_id: str, secret_id: str):
    require_admin(request)
    store.delete("secrets_bible", {"series_id": series_id, "secret_id": secret_id})
    return _redirect(f"/series/{series_id}/world", ok="Secret removed.")


# ── episode workspace ──────────────────────────────────────────────────────

@router.get("/series/{series_id}/episodes/{episode_id}", response_class=HTMLResponse)
def episode_page(request: Request, series_id: str, episode_id: str):
    """Kept so older links still work; the episode has one screen now."""
    require_admin(request)
    if any(job.get("stages") == ["clip_preview"] for job in
           store.list("production_jobs", {"series_id": series_id, "episode_id": episode_id})):
        return _redirect("/clip-preview")
    return _redirect(f"/series/{series_id}/episodes/{episode_id}/studio")


def _episode_context(request: Request, series_id: str, episode_id: str) -> dict:
    """Everything the technical half of the episode screen needs."""
    episode_jobs = store.list("production_jobs", {"series_id": series_id, "episode_id": episode_id})
    s = store.get("series", {"id": series_id})
    ep = store.get("episodes", {"series_id": series_id, "episode_id": episode_id})
    if not s or not ep:
        raise HTTPException(404, "episode not found")
    scripts_list = store.list("scripts", {"series_id": series_id, "episode_id": episode_id},
                              order="version", desc=True)
    runtime = runner.episode_runtime(series_id, episode_id)
    # Resume lives on this page, so a saved request that must be retired first
    # has to be visible here. Otherwise Resume just repeats the same refusal.
    blocked_request, blocked_job = None, None
    for job in sorted(episode_jobs, key=lambda j: j.get("created_at") or "", reverse=True):
        if job.get("state") != "failed":
            continue
        match = REFUSED_REQUEST_RE.search(job.get("error") or "")
        if match and not store.list("approvals", {
                "series_id": series_id, "episode_id": episode_id,
                "subject_type": "fal_request_unreachable", "subject_id": match.group(1)}):
            blocked_request, blocked_job = match.group(1), job["id"]
        break
    from serial.package import word_budget
    takes = store.list("takes", {"series_id": series_id, "episode_id": episode_id}, order="take_id")
    by_scene: dict[str, list] = {}
    for t in takes:
        by_scene.setdefault(t.get("scene_id", ""), []).append(t)
    return dict(s=s, ep=ep, mode=settings.mode,
                  blocked_request=blocked_request, blocked_job=blocked_job,
                  word_budget=word_budget,
                  scenes=store.list("scenes", {"series_id": series_id, "episode_id": episode_id},
                                    order="sequence"),
                  scripts=scripts_list,
                  latest_script=scripts_list[0] if scripts_list else None,
                  runtime=runtime,
                  production_digest=live_jobs.review(series_id, episode_id) if settings.allow_paid else "",
                  csrf_token=_csrf_token(request, require_admin(request)),
                  jobs=runner.jobs.jobs_for(series_id, episode_id, limit=10),
                  active_job=runner.jobs.active_job(series_id, episode_id),
                  takes_by_scene=by_scene,
                  costs=store.list("costs", {"series_id": series_id, "episode_id": episode_id}),
                  all_stages=runner.STAGES,
                  default_stages=runner.DEFAULT_STAGES,
                  validation=runner.validate_series(series_id),
                  history=store.list("generation_history",
                                     {"series_id": series_id, "episode_id": episode_id},
                                     order="created_at", desc=True, limit=25))


@router.post("/series/{series_id}/episodes/{episode_id}/settings")
def episode_settings(request: Request, series_id: str, episode_id: str,
                     title: str = Form(""), logline: str = Form(""), number: str = Form("1"),
                     cliff_scene: str = Form(""), cliff_hook: str = Form(""),
                     cliff_resolves: str = Form("tbd")):
    require_admin(request)
    patch: dict = {"title": title, "logline": logline, "updated_at": _now()}
    try:
        patch["number"] = int(number or 1)
    except ValueError:
        pass
    if cliff_scene or cliff_hook:
        patch["cliffhanger"] = {"scene_id": _slug(cliff_scene), "hook": cliff_hook,
                                "resolves_in": cliff_resolves or "tbd"}
    store.update("episodes", {"series_id": series_id, "episode_id": episode_id}, patch)
    return _redirect(f"/series/{series_id}/episodes/{episode_id}", ok="Episode saved.")


@router.post("/series/{series_id}/episodes/{episode_id}/script")
async def post_script(request: Request, series_id: str, episode_id: str,
                      content: str = Form(""), upload: UploadFile | None = File(None)):
    a = require_admin(request)
    filename, source = "", "paste"
    if upload is not None and upload.filename:
        raw = await upload.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _redirect(f"/series/{series_id}/episodes/{episode_id}",
                             err="The uploaded file is not UTF-8 text.")
        filename, source = upload.filename, "upload"
    back = f"/series/{series_id}/episodes/{episode_id}/studio"
    try:
        result = scriptmod.save_script(series_id, episode_id, content,
                                       source=source, filename=filename, actor=a["email"])
    except scriptmod.ScriptError as e:
        # Plain prose belongs here as much as the engine's own markup. Rather
        # than refusing it over a missing SCENE header, hand it to the studio,
        # which is what the wish field does with the same text.
        if (content or "").strip():
            try:
                authoring.start_draft(series_id, episode_id, content, a["email"])
            except authoring.AuthoringError as reason:
                return _redirect(back, err=str(reason))
            return _redirect(back, ok="That is not the engine's own format, so the studio "
                                      "is writing it into scenes. This page refreshes itself.")
        return _redirect(back, err=str(e))
    return _redirect(back, ok=f"Script v{result['version']} saved — {result['scenes']} scenes.")


@router.post("/series/{series_id}/episodes/{episode_id}/scenes/{scene_id}/dialogue")
async def reword_scene(request: Request, series_id: str, episode_id: str, scene_id: str):
    """Edit the words of one scene's lines, in place.

    Re-pasting the whole script was the only way to change a line, which is
    impractical on a phone and left an episode stranded when the voice stage
    asked for a shorter line.
    """
    a = require_admin(request)
    form = await request.form()
    texts = [form[key] for key in sorted(k for k in form if k.startswith("line_"))]
    back = f"/series/{series_id}/episodes/{episode_id}"
    try:
        result = scriptmod.reword_scene(series_id, episode_id, scene_id, texts, a["email"])
    except scriptmod.ScriptError as e:
        return _redirect(back, err=str(e))
    return _redirect(back, ok=f"{scene_id}: lines saved as script v{result['version']}. "
                              "Resume regenerates only the speech.")


@router.post("/series/{series_id}/episodes/{episode_id}/production")
def production_control(request: Request, series_id: str, episode_id: str,
                       action: str = Form(...), stages: list[str] = Form(default=[]),
                       force: str = Form(""), csrf_token: str = Form(""),
                       approve_live: str = Form(""), approved_digest: str = Form(""),
                       audio_mode: str = Form("native")):
    a = require_admin(request)
    back = f"/series/{series_id}/episodes/{episode_id}"
    approval = {}
    if settings.allow_paid:
        _check_form(request, a, csrf_token)
        approval = dict(approve_live=approve_live == "yes", approved_digest=approved_digest, audio_mode=audio_mode)
    try:
        if action == "check" and settings.allow_paid:
            live_jobs.check_configuration(series_id, episode_id, stages or runner.DEFAULT_STAGES, audio_mode)
            return _redirect(back, ok="Local production checks passed. Provider access and saved-request recovery are checked separately; no generation was started.")
        if action == "start":
            job = runner.jobs.start(series_id, episode_id, stages or None, a["email"],
                              [f.strip() for f in force.split(",") if f.strip()], **approval)
            return _redirect(f"/jobs/{job['id']}", ok=f"Production started in {settings.mode} mode. Open Jobs for progress.")
        if action == "pause":
            job = runner.jobs.active_job(series_id, episode_id)
            if not job:
                return _redirect(back, err="Nothing is running.")
            runner.jobs.pause(job["id"], a["email"])
            return _redirect(back, ok="Pausing at the next stage boundary.")
        if action == "resume":
            job = runner.jobs.resume(series_id, episode_id, a["email"], **approval)
            return _redirect(f"/jobs/{job['id']}", ok="Resumed. Completed stages are skipped.")
        if action == "cancel":
            job = runner.jobs.active_job(series_id, episode_id)
            if not job:
                return _redirect(back, err="Nothing is running.")
            runner.jobs.cancel(job["id"], a["email"])
            return _redirect(back, ok="Cancelling.")
    except (ValueError, PermissionError, runner.PackageError) as e:
        return _redirect(back, err=str(e))
    except Exception as exc:
        # Storage/lease failures can occur before a job exists. Do not show a
        # raw 500 (or leak an SDK exception containing a signed URL or token).
        import logging
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)[-5:]
        logging.getLogger(__name__).error('Production control failed (%s): %s', type(exc).__name__,
            ' -> '.join(f'{Path(f.filename).name}:{f.lineno} ({f.name})' for f in frames))
        return _redirect(back, err="Production could not be started or updated. Check Jobs before retrying; saved requests have not been cleared.")
    return _redirect(back, err=f"Unknown action '{action}'.")


@router.post("/series/{series_id}/episodes/{episode_id}/approve")
def approve_episode(request: Request, series_id: str, episode_id: str, note: str = Form("")):
    a = require_admin(request)
    back = f"/series/{series_id}/episodes/{episode_id}"
    try:
        runner.approve_publish(series_id, episode_id, a["email"], note)
    except ValueError as e:
        return _redirect(back, err=str(e))
    return _redirect(back, ok="Episode approved. Publishing stays disabled in this build.")


@router.post("/series/{series_id}/episodes/{episode_id}/override")
def episode_override(request: Request, series_id: str, episode_id: str,
                     key: str = Form("MAX_EPISODE_BUDGET_USD"), value: str = Form(...),
                     reason: str = Form("")):
    a = require_admin(request)
    back = f"/series/{series_id}/episodes/{episode_id}"
    try:
        runner.record_override(series_id, episode_id, key, value, a["email"], reason)
    except ValueError as e:
        return _redirect(back, err=str(e))
    return _redirect(back, ok=f"Override recorded: {key}={value}.")


@router.post("/series/{series_id}/takes/{take_id}/select")
def select_take_form(request: Request, series_id: str, take_id: str, back: str = Form("/")):
    a = require_admin(request)
    take = store.get("takes", {"series_id": series_id, "take_id": take_id})
    if not take:
        return _redirect(back, err="Take not found.")
    for sibling in store.list("takes", {"series_id": series_id, "episode_id": take["episode_id"],
                                        "scene_id": take["scene_id"], "stage": take["stage"]}):
        store.update("takes", {"series_id": series_id, "take_id": sibling["take_id"]},
                     {"selected": sibling["take_id"] == take_id})
    history(series_id, take["episode_id"], "take.selected", entity_type="take",
            entity_id=take_id, actor=a["email"])
    return _redirect(back, ok=f"Take {take_id} selected.")


# ── references ─────────────────────────────────────────────────────────────

@router.get("/series/{series_id}/references", response_class=HTMLResponse)
def references_page(request: Request, series_id: str):
    require_admin(request)
    s = store.get("series", {"id": series_id})
    if not s:
        raise HTTPException(404, "series not found")
    refs = store.list("reference_assets", {"series_id": series_id}, order="owner_id")
    grouped: dict[str, list] = {}
    for r in refs:
        grouped.setdefault(r.get("kind", "other"), []).append(r)
    return render(request, "references.html", s=s, grouped=grouped, total=len(refs),
                  csrf_token=_csrf_token(request, require_admin(request)),
                  approvals=store.list("approvals", {"series_id": series_id,
                                                     "subject_type": "references"}))


@router.post("/series/{series_id}/references/approve")
def approve_refs(request: Request, series_id: str, note: str = Form(""), csrf_token: str = Form("")):
    a = require_admin(request)
    back = f"/series/{series_id}/references"
    if settings.allow_paid:
        _check_form(request, a, csrf_token)
    try:
        result = runner.approve_references(series_id, a["email"], note)
    except (ValueError, KeyError) as e:
        return _redirect(back, err=str(e))
    return _redirect(back, ok=f"Reference pack for bible {result['bible_version']} approved.")


# ── costs, jobs, integrations ──────────────────────────────────────────────

@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, series_id: str | None = None):
    require_admin(request)
    where = {"series_id": series_id} if series_id else None
    rows = [{**r, "is_preview": r.get("stage") == "clip_preview", "is_live": r.get("stage", "").startswith("live/")}
            for r in store.list("costs", where)]
    by_episode: dict[str, dict] = {}
    for r in rows:
        # Keep a real preview estimate out of the simulated episode ledger,
        # even if both kinds of record refer to the same episode identifier.
        key = f"{r['series_id']}/{r['episode_id']}/{'preview' if r['is_preview'] else ('live' if r['is_live'] else 'mock')}"
        agg = by_episode.setdefault(key, {"key": key, "series_id": r["series_id"],
                                          "episode_id": r["episode_id"],
                                          "is_preview": r["is_preview"], "is_live": r["is_live"],
                                          "estimated_usd": 0.0,
                                          "actual_usd": None if r["is_preview"] else 0.0,
                                          "calls": 0})
        agg["estimated_usd"] += float(r.get("estimated_usd") or 0)
        if not r["is_preview"]:
            agg["actual_usd"] += float(r.get("actual_usd") or 0)
        agg["calls"] += 1
    return render(request, "costs.html", rows=rows[:500],
                  by_episode=sorted(by_episode.values(), key=lambda x: -x["estimated_usd"]),
                  total=round(sum(float(r.get("actual_usd") or 0) for r in rows if not r["is_preview"] and not r["is_live"]), 4),
                  live_estimate=round(sum(float(r.get("actual_usd") or 0) for r in rows if r["is_live"]), 4),
                  preview_estimate=round(sum(float(r.get("estimated_usd") or 0) for r in rows if r["is_preview"]), 4),
                  has_preview_cost=any(r["is_preview"] for r in rows),
                  episodes=store.list("episodes", where, order="number"),
                  selected=series_id)


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    require_admin(request)
    return render(request, "jobs.html",
                  jobs=[j for j in store.list("production_jobs", order="created_at", desc=True, limit=100) if j.get("stages") != ["runtime_lease"]])


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    require_admin(request)
    job = store.get("production_jobs", {"id": job_id})
    if not job:
        raise HTTPException(404, "job not found")
    if job.get("stages") == ["clip_preview"]:
        return _redirect("/clip-preview")
    progress = job.get("progress") or {}
    # Older paused jobs have the reason only in their log. Recognize that exact
    # marker so the next action is visible without restarting or changing them.
    reference_review = job.get("state") == "paused" and job.get("mode") == "live" and (
        progress.get("waiting_for") == "reference_approval"
        or (progress.get("stage") == "references" and live_jobs.REFERENCE_APPROVAL_MESSAGE
            in (job.get("log") or "").splitlines())
    )
    completed_stages = list(progress.get("done") or [])
    if reference_review and "references" in (job.get("stages") or []) and "references" not in completed_stages:
        completed_stages.append("references")
    match = REFUSED_REQUEST_RE.search(job.get("error") or "") if job.get("state") == "failed" else None
    refused_request = match.group(1) if match else None
    already_released = bool(refused_request and store.list("approvals", {
        "series_id": job["series_id"], "episode_id": job["episode_id"],
        "subject_type": "fal_request_unreachable", "subject_id": refused_request}))
    return render(request, "job.html", job=job, reference_review=reference_review,
                  completed_stages=completed_stages, refused_request=refused_request,
                  already_released=already_released,
                  s=store.get("series", {"id": job["series_id"]}))


@router.post("/jobs/{job_id}/release-request")
def release_saved_request(request: Request, job_id: str, request_id: str = Form(...),
                          note: str = Form("")):
    """Record that a saved fal request cannot be read back.

    This does not delete the request id and does not relax the resubmission
    guard anywhere else: the next run retires exactly this one take, charges
    its estimate as possibly-billed, and submits that single take again.
    """
    a = require_admin(request)
    job = store.get("production_jobs", {"id": job_id})
    if not job:
        raise HTTPException(404, "job not found")
    back = f"/jobs/{job_id}"
    match = REFUSED_REQUEST_RE.search(job.get("error") or "")
    if not match or match.group(1) != request_id.strip():
        return _redirect(back, err="This job did not report that request as refused.")
    if store.list("approvals", {"series_id": job["series_id"], "episode_id": job["episode_id"],
                                "subject_type": "fal_request_unreachable", "subject_id": request_id}):
        return _redirect(back, ok="That request is already recorded as unreachable.")
    store.insert("approvals", {
        "series_id": job["series_id"], "episode_id": job["episode_id"],
        "subject_type": "fal_request_unreachable", "subject_id": request_id,
        "decision": "released", "actor": a["email"], "note": note, "created_at": _now()})
    history(job["series_id"], job["episode_id"], "fal.request_released",
            entity_type="fal_request", entity_id=request_id, actor=a["email"], detail={"note": note})
    return _redirect(back, ok="Recorded. Open the episode and click Resume to generate that one take again.")


@router.get("/integrations", response_class=HTMLResponse)
def integrations_page(request: Request):
    require_admin(request)
    return render(request, "integrations.html", providers=integrations.status_all())


@router.post("/integrations/{provider}/test")
def test_integration_form(request: Request, provider: str):
    a = require_admin(request)
    try:
        result = integrations.test_connection(provider)
    except KeyError:
        return _redirect("/integrations", err=f"Unknown provider '{provider}'.")
    history("", "", "integration.test", entity_type="integration", entity_id=provider,
            actor=a["email"], detail={"connected": result["connected"]})
    if result["connected"]:
        return _redirect("/integrations", ok=f"{result['label']}: {result['state']}")
    return _redirect("/integrations", err=f"{result['label']}: {result['last_error']}")


@router.post("/series/{series_id}/episode-duration")
def episode_duration(request: Request, series_id: str, minimum: int = Form(...), maximum: int = Form(...),
                     episode_id: str = Form(...), csrf_token: str = Form("")):
    admin = require_admin(request)
    _check_form(request, admin, csrf_token)
    series = store.get("series", {"id": series_id})
    if not series or not ID_RE.fullmatch(episode_id):
        raise HTTPException(404, "Series or episode not found")
    if not 1 <= minimum <= maximum <= 3600:
        raise HTTPException(400, "Invalid duration range")
    limits = {**DEFAULT_LIMITS, **(series.get("production_limits") or {})}
    limits.update(min_episode_seconds=minimum, max_episode_seconds=maximum)
    store.update("series", {"id": series_id}, {"production_limits": limits, "updated_at": _now()})
    history(series_id, episode_id, "duration.updated", actor=admin["email"], detail={"minimum": minimum, "maximum": maximum})
    return _redirect(f"/series/{series_id}/episodes/{episode_id}", ok="Episode duration range saved.")


@router.get("/series/{series_id}/references/{asset_id}/image")
def reference_image(request: Request, series_id: str, asset_id: str):
    require_admin(request)
    asset = store.get("reference_assets", {"id": asset_id, "series_id": series_id}) or {}
    key = asset.get("r2_key", "")
    if not key.startswith(f"series/{series_id}/bible/"):
        raise HTTPException(404, "Reference image not found")
    from .preview import _r2
    response = RedirectResponse(_r2().presign(key, 900), status_code=303)
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    return response


@router.get("/series/{series_id}/episodes/{episode_id}/master/{version}")
def episode_master(request: Request, series_id: str, episode_id: str, version: str):
    require_admin(request)
    if not ID_RE.fullmatch(series_id) or not ID_RE.fullmatch(episode_id) or not re.fullmatch(r"v[0-9]+", version):
        raise HTTPException(404, "Master not found")
    runtime = runner.episode_runtime(series_id, episode_id)
    if not any(m["version"] == version for m in runtime["masters"]):
        raise HTTPException(404, "Master not found")
    from .preview import _r2
    key = f"series/{series_id}/episodes/{episode_id}/masters/{version}/episode.mp4"
    storage = _r2()
    try:
        storage.client.head_object(Bucket=storage.cfg.r2_bucket, Key=key)
    except Exception:
        raise HTTPException(409, "Master is not delivered yet") from None
    response = RedirectResponse(storage.presign(key, 900), status_code=303)
    response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    return response
