"""Backend API.

Every production action the admin panel can take is available here as JSON, so
the pipeline is controllable by the panel, by a script, or by a future worker
without a second implementation. All routes require an administrator session.

Secret values are never returned by any route.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from . import integrations, runner, scripts as scriptmod
from .config import settings
from .deps import require_admin
from .ingest import history
from .packaging import materialize
from .store import store

router = APIRouter(prefix="/api", tags=["studio"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def admin(request: Request) -> dict:
    return require_admin(request)


# ── series ─────────────────────────────────────────────────────────────────

@router.get("/series")
def list_series(a: dict = Depends(admin)):
    return {"series": store.list("series", order="title")}


@router.get("/series/{series_id}")
def get_series(series_id: str, a: dict = Depends(admin)):
    s = store.get("series", {"id": series_id})
    if not s:
        raise HTTPException(404, "series not found")
    return {
        "series": s,
        "seasons": store.list("seasons", {"series_id": series_id}, order="number"),
        "episodes": store.list("episodes", {"series_id": series_id}, order="number"),
        "characters": store.list("characters", {"series_id": series_id}, order="character_id"),
        "locations": store.list("locations", {"series_id": series_id}, order="location_id"),
        "relationships": store.list("relationships", {"series_id": series_id}, order="rel_id"),
        "secrets": store.list("secrets_bible", {"series_id": series_id}, order="secret_id"),
    }


@router.post("/series/{series_id}/validate")
def validate(series_id: str, a: dict = Depends(admin)):
    """Free: schema, continuity, knowledge ledger, cliffhanger, budget projection."""
    return runner.validate_series(series_id)


@router.post("/series/{series_id}/package")
def build_package(series_id: str, a: dict = Depends(admin)):
    return {"package_dir": str(materialize(series_id, clean=True))}


# ── episodes and scripts ───────────────────────────────────────────────────

@router.get("/series/{series_id}/episodes/{episode_id}")
def get_episode(series_id: str, episode_id: str, a: dict = Depends(admin)):
    ep = store.get("episodes", {"series_id": series_id, "episode_id": episode_id})
    if not ep:
        raise HTTPException(404, "episode not found")
    return {
        "episode": ep,
        "scenes": store.list("scenes", {"series_id": series_id, "episode_id": episode_id}, order="sequence"),
        "runtime": runner.episode_runtime(series_id, episode_id),
        "jobs": runner.jobs.jobs_for(series_id, episode_id, limit=20),
        "takes": store.list("takes", {"series_id": series_id, "episode_id": episode_id}, order="take_id"),
        "costs": store.list("costs", {"series_id": series_id, "episode_id": episode_id}),
    }


@router.post("/series/{series_id}/episodes/{episode_id}/script")
def put_script(series_id: str, episode_id: str, payload: dict = Body(...), a: dict = Depends(admin)):
    try:
        return scriptmod.save_script(
            series_id, episode_id, payload.get("content", ""),
            source=payload.get("source", "paste"), filename=payload.get("filename", ""),
            actor=a["email"],
        )
    except scriptmod.ScriptError as e:
        raise HTTPException(400, str(e)) from e


# ── production control ─────────────────────────────────────────────────────

@router.post("/series/{series_id}/episodes/{episode_id}/start")
def start(series_id: str, episode_id: str, payload: dict = Body(default={}), a: dict = Depends(admin)):
    try:
        job = runner.jobs.start(
            series_id, episode_id,
            stages=payload.get("stages") or None,
            requested_by=a["email"],
            force=payload.get("force") or [],
            approved_digest=payload.get("approved_digest", ""),
            approve_live=payload.get("approve_live") is True,
            audio_mode=payload.get("audio_mode", "native"),
        )
    except (ValueError, PermissionError) as e:
        raise HTTPException(400, str(e)) from e
    return {"job": job}


@router.post("/series/{series_id}/episodes/{episode_id}/pause")
def pause(series_id: str, episode_id: str, a: dict = Depends(admin)):
    job = runner.jobs.active_job(series_id, episode_id)
    if not job:
        raise HTTPException(409, "no active job for this episode")
    runner.jobs.pause(job["id"], a["email"])
    return {"job_id": job["id"], "state": "pausing"}


@router.post("/series/{series_id}/episodes/{episode_id}/resume")
def resume(series_id: str, episode_id: str, a: dict = Depends(admin)):
    return {"job": runner.jobs.resume(series_id, episode_id, a["email"])}


@router.post("/series/{series_id}/episodes/{episode_id}/cancel")
def cancel(series_id: str, episode_id: str, a: dict = Depends(admin)):
    job = runner.jobs.active_job(series_id, episode_id)
    if not job:
        raise HTTPException(409, "no active job for this episode")
    runner.jobs.cancel(job["id"], a["email"])
    return {"job_id": job["id"], "state": "cancelling"}


@router.get("/jobs/{job_id}")
def job_detail(job_id: str, a: dict = Depends(admin)):
    job = store.get("production_jobs", {"id": job_id})
    if not job:
        raise HTTPException(404, "job not found")
    return {"job": job}


# ── approvals, takes, overrides ────────────────────────────────────────────

@router.post("/series/{series_id}/approve/references")
def approve_references(series_id: str, payload: dict = Body(default={}), a: dict = Depends(admin)):
    try:
        return runner.approve_references(series_id, a["email"], payload.get("note", ""))
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/series/{series_id}/episodes/{episode_id}/approve/publish")
def approve_publish(series_id: str, episode_id: str, payload: dict = Body(default={}), a: dict = Depends(admin)):
    try:
        return runner.approve_publish(series_id, episode_id, a["email"], payload.get("note", ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/series/{series_id}/takes/{take_id}/select")
def select_take(series_id: str, take_id: str, a: dict = Depends(admin)):
    take = store.get("takes", {"series_id": series_id, "take_id": take_id})
    if not take:
        raise HTTPException(404, "take not found")
    # One selected take per (episode, scene, stage).
    for sibling in store.list("takes", {
        "series_id": series_id, "episode_id": take["episode_id"],
        "scene_id": take["scene_id"], "stage": take["stage"],
    }):
        store.update("takes", {"series_id": series_id, "take_id": sibling["take_id"]},
                     {"selected": sibling["take_id"] == take_id})
    history(series_id, take["episode_id"], "take.selected", entity_type="take",
            entity_id=take_id, actor=a["email"])
    return {"take_id": take_id, "selected": True}


@router.post("/series/{series_id}/episodes/{episode_id}/override")
def override(series_id: str, episode_id: str, payload: dict = Body(...), a: dict = Depends(admin)):
    try:
        return runner.record_override(
            series_id, episode_id, payload["key"], str(payload["value"]),
            a["email"], payload.get("reason", ""),
        )
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from e


# ── costs and history ──────────────────────────────────────────────────────

@router.get("/costs")
def costs(series_id: str | None = None, a: dict = Depends(admin)):
    where = {"series_id": series_id} if series_id else None
    rows = store.list("costs", where)
    by_episode: dict[str, dict] = {}
    for r in rows:
        key = f"{r['series_id']}/{r['episode_id']}"
        agg = by_episode.setdefault(key, {"series_id": r["series_id"], "episode_id": r["episode_id"],
                                          "estimated_usd": 0.0, "actual_usd": 0.0, "calls": 0})
        agg["estimated_usd"] += float(r.get("estimated_usd") or 0)
        agg["actual_usd"] += float(r.get("actual_usd") or 0)
        agg["calls"] += 1
    return {
        "rows": rows,
        "by_episode": list(by_episode.values()),
        "total_actual_usd": round(sum(float(r.get("actual_usd") or 0) for r in rows), 4),
        "mode": settings.mode,
        "note": "Mock mode: these are simulated costs. No paid call has been made.",
    }


@router.get("/history")
def get_history(series_id: str | None = None, episode_id: str | None = None,
                limit: int = 200, a: dict = Depends(admin)):
    where = {}
    if series_id:
        where["series_id"] = series_id
    if episode_id:
        where["episode_id"] = episode_id
    return {"history": store.list("generation_history", where or None,
                                  order="created_at", desc=True, limit=limit)}


# ── integrations ───────────────────────────────────────────────────────────

@router.get("/integrations")
def get_integrations(a: dict = Depends(admin)):
    """Connected / Missing, selected model, last test, last error. No secrets."""
    return {"providers": integrations.status_all(),
            "mode": settings.mode,
            "connection_tests_enabled": settings.allow_connection_tests}


@router.post("/integrations/{provider}/test")
def test_integration(provider: str, a: dict = Depends(admin)):
    try:
        result = integrations.test_connection(provider)
    except KeyError as e:
        raise HTTPException(404, f"unknown provider {provider}") from e
    history("", "", "integration.test", entity_type="integration", entity_id=provider,
            actor=a["email"], detail={"connected": result["connected"]})
    return result
