"""Admin panel (server-rendered HTML).

Everything the producer needs: create a series, add seasons and episodes, paste
or upload scripts, manage characters, clothing, voices, locations,
relationships and secrets, review generated scenes and takes, approve
references and finished episodes, watch costs, and start/pause/resume
production.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse

from . import auth, integrations, runner, scripts as scriptmod
from .config import settings
from .deps import current_admin, require_admin, templates
from .ingest import history
from .packaging import DEFAULT_LIMITS
from .store import store

router = APIRouter()

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


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
    return templates.TemplateResponse(request, template, ctx)


# ── auth ───────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_admin(request):
        return RedirectResponse(settings.url("/"), status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "err": request.query_params.get("err"),
        "next": request.query_params.get("next") or settings.url("/"),
        "admin": None, "all_series": [],
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
        secure=not settings.host.startswith("127."),
    )
    return response


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
    active = [j for j in store.list("production_jobs", order="created_at", desc=True, limit=60)
              if j.get("state") in ("queued", "running", "paused", "pausing")]
    costs = store.list("costs")
    providers = integrations.status_all()
    return render(request, "dashboard.html",
                  series=series, episodes=episodes, active_jobs=active,
                  total_cost=round(sum(float(c.get("actual_usd") or 0) for c in costs), 2),
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
    return render(request, "series.html", s=s,
                  seasons=store.list("seasons", {"series_id": series_id}, order="number"),
                  episodes=store.list("episodes", {"series_id": series_id}, order="number"),
                  characters=store.list("characters", {"series_id": series_id}, order="character_id"),
                  locations=store.list("locations", {"series_id": series_id}, order="location_id"),
                  relationships=store.list("relationships", {"series_id": series_id}),
                  secrets=store.list("secrets_bible", {"series_id": series_id}),
                  validation=validation)


@router.post("/series/{series_id}/settings")
def series_settings(request: Request, series_id: str, title: str = Form(...),
                    logline: str = Form(""), genre: str = Form(""), language: str = Form("en-US"),
                    captions: str = Form("both"), budget: str = Form("50"),
                    regenerations: str = Form("2"), min_scenes: str = Form("12"),
                    max_scenes: str = Form("18"), style_sentence: str = Form(""),
                    camera_rules: str = Form(""), color_rules: str = Form(""),
                    negative_image: str = Form(""), negative_video: str = Form("")):
    a = require_admin(request)
    s = store.get("series", {"id": series_id})
    if not s:
        raise HTTPException(404, "series not found")
    fmt = dict(s.get("format") or {})
    fmt["captions"] = captions
    limits = {**DEFAULT_LIMITS, **(s.get("production_limits") or {})}
    try:
        limits["maximum_episode_budget_usd"] = float(budget)
        limits["maximum_regenerations_per_scene"] = int(regenerations)
        limits["min_scenes"] = int(min_scenes)
        limits["max_scenes"] = int(max_scenes)
    except ValueError:
        return _redirect(f"/series/{series_id}", err="Limits must be numbers.")
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
                  props=store.list("props", {"series_id": series_id}, order="prop_id"))


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
    if not store.get("voices", {"series_id": series_id, "character_id": cid}):
        store.upsert("voices", {
            "series_id": series_id, "character_id": cid, "provider": "elevenlabs",
            "voice_env": f"ELEVENLABS_VOICE_ID_{cid.upper()}", "model_id": "eleven_v3",
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
    store.upsert("voices", {
        "series_id": series_id, "character_id": character_id, "provider": "elevenlabs",
        "voice_env": env_name, "model_id": model_id, "language": language,
        "style_notes": style_notes, "phone_fx": phone_fx == "yes", "locked": locked == "yes",
    })
    history(series_id, "", "voice.assigned", entity_type="voice", entity_id=character_id,
            actor=a["email"], detail={"voice_env": env_name})
    return _redirect(f"/series/{series_id}/characters",
                     ok=f"Voice bound to {env_name}. Put the id in studio/.env under that name.")


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
    require_admin(request)
    s = store.get("series", {"id": series_id})
    ep = store.get("episodes", {"series_id": series_id, "episode_id": episode_id})
    if not s or not ep:
        raise HTTPException(404, "episode not found")
    scripts_list = store.list("scripts", {"series_id": series_id, "episode_id": episode_id},
                              order="version", desc=True)
    runtime = runner.episode_runtime(series_id, episode_id)
    takes = store.list("takes", {"series_id": series_id, "episode_id": episode_id}, order="take_id")
    by_scene: dict[str, list] = {}
    for t in takes:
        by_scene.setdefault(t.get("scene_id", ""), []).append(t)
    return render(request, "episode.html", s=s, ep=ep,
                  scenes=store.list("scenes", {"series_id": series_id, "episode_id": episode_id},
                                    order="sequence"),
                  scripts=scripts_list,
                  latest_script=scripts_list[0] if scripts_list else None,
                  runtime=runtime,
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
    try:
        result = scriptmod.save_script(series_id, episode_id, content,
                                       source=source, filename=filename, actor=a["email"])
    except scriptmod.ScriptError as e:
        return _redirect(f"/series/{series_id}/episodes/{episode_id}", err=str(e))
    return _redirect(f"/series/{series_id}/episodes/{episode_id}",
                     ok=f"Script v{result['version']} saved — {result['scenes']} scenes.")


@router.post("/series/{series_id}/episodes/{episode_id}/production")
def production_control(request: Request, series_id: str, episode_id: str,
                       action: str = Form(...), stages: list[str] = Form(default=[]),
                       force: str = Form("")):
    a = require_admin(request)
    back = f"/series/{series_id}/episodes/{episode_id}"
    try:
        if action == "start":
            runner.jobs.start(series_id, episode_id, stages or None, a["email"],
                              [f.strip() for f in force.split(",") if f.strip()])
            return _redirect(back, ok="Production started in mock mode.")
        if action == "pause":
            job = runner.jobs.active_job(series_id, episode_id)
            if not job:
                return _redirect(back, err="Nothing is running.")
            runner.jobs.pause(job["id"], a["email"])
            return _redirect(back, ok="Pausing at the next stage boundary.")
        if action == "resume":
            runner.jobs.resume(series_id, episode_id, a["email"])
            return _redirect(back, ok="Resumed. Completed stages are skipped.")
        if action == "cancel":
            job = runner.jobs.active_job(series_id, episode_id)
            if not job:
                return _redirect(back, err="Nothing is running.")
            runner.jobs.cancel(job["id"], a["email"])
            return _redirect(back, ok="Cancelling.")
    except (ValueError, PermissionError) as e:
        return _redirect(back, err=str(e))
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
                  approvals=store.list("approvals", {"series_id": series_id,
                                                     "subject_type": "references"}))


@router.post("/series/{series_id}/references/approve")
def approve_refs(request: Request, series_id: str, note: str = Form("")):
    a = require_admin(request)
    back = f"/series/{series_id}/references"
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
    rows = store.list("costs", where)
    by_episode: dict[str, dict] = {}
    for r in rows:
        key = f"{r['series_id']}/{r['episode_id']}"
        agg = by_episode.setdefault(key, {"key": key, "series_id": r["series_id"],
                                          "episode_id": r["episode_id"],
                                          "estimated_usd": 0.0, "actual_usd": 0.0, "calls": 0})
        agg["estimated_usd"] += float(r.get("estimated_usd") or 0)
        agg["actual_usd"] += float(r.get("actual_usd") or 0)
        agg["calls"] += 1
    return render(request, "costs.html", rows=rows[:500],
                  by_episode=sorted(by_episode.values(), key=lambda x: -x["actual_usd"]),
                  total=round(sum(float(r.get("actual_usd") or 0) for r in rows), 4),
                  episodes=store.list("episodes", where, order="number"),
                  selected=series_id)


@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    require_admin(request)
    return render(request, "jobs.html",
                  jobs=store.list("production_jobs", order="created_at", desc=True, limit=100))


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str):
    require_admin(request)
    job = store.get("production_jobs", {"id": job_id})
    if not job:
        raise HTTPException(404, "job not found")
    return render(request, "job.html", job=job)


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
        return _redirect("/integrations", ok=f"{result['label']}: connected.")
    return _redirect("/integrations", err=f"{result['label']}: {result['last_error']}")
