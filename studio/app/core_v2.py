"""Measured Decision Core V2 adapter for the Series Studio.

Shadow mode is deliberately read-only with respect to production: it examines
the episode contract, existing takes, QC evidence and spend, then records a
decision report.  It never calls a media provider, changes a selected take or
approves a retry.  This makes the first rollout useful without giving a new
decision layer permission to spend money.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .store import store

CORE = "measured-decision-core-v2"
POLICY_VERSION = "studio-shadow-1"
EVENT = "core_v2.shadow_analysis_completed"
SUPERVISED_EVENT = "core_v2.supervised_repair_started"
SUPERVISED_APPROVAL = "core_v2_repair_plan"
RETRY_ACTIONS = {"simplify_action_then_retry"}
QC_LOG_LINE = re.compile(
    r"video:\s+(sc\d+)\b.*?\bQC\s+([0-9]+(?:\.[0-9]+)?)\s+(OK|FAIL)\s*(.*)$",
    re.MULTILINE,
)
MOTION_STILL_LOG_LINE = re.compile(
    r"video:\s+(sc\d+)\s+editorial motion-still; no paid video provider",
    re.MULTILINE,
)

IDENTITY_WORDS = (
    "anatom", "identity", "species", "domestic", "tabby", "dog-like",
    "face", "body proportion", "size", "scale", "tail", "ear notch",
    "coat", "wildcat", "кошк", "анатом", "морда", "хвост", "размер",
)
CAMERA_WORDS = (
    "camera", "drift", "smear", "blur", "flicker", "jump", "zoom",
    "motion", "камер", "смаз", "дрейф", "скач",
)
CONTINUITY_WORDS = (
    "wardrobe", "garment", "bow", "quiver", "prop", "bridge", "background",
    "location", "continuity", "одежд", "лук", "колчан", "мост", "фон",
)
STATIC_BEATS = (
    "gaze", "looks", "looked", "realizes", "remains", "holds her ground",
    "does not move", "взгляд", "смотр", "понима", "остаётся", "стоит",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "; ".join(_text(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_text(item)}" for key, item in value.items())
    return str(value or "")


def _categories(issues: str) -> list[str]:
    lowered = issues.lower()
    categories = []
    if any(word in lowered for word in IDENTITY_WORDS):
        categories.append("identity_anatomy")
    if any(word in lowered for word in CAMERA_WORDS):
        categories.append("camera_motion")
    if any(word in lowered for word in CONTINUITY_WORDS):
        categories.append("continuity_props")
    return categories or ["unclassified_visual_qc"]


def _route_for(brief: dict, scene_id: str) -> dict:
    routing = brief.get("video_routing") or {}
    return dict((routing.get("scenes") or {}).get(scene_id) or {})


def _next_provider(route: dict, endpoint: str) -> str | None:
    providers = [route.get("primary"), *(route.get("fallbacks") or [])]
    providers = [item for item in providers if item]
    if not providers:
        return None
    try:
        index = providers.index(endpoint)
    except ValueError:
        return providers[0]
    return providers[index + 1] if index + 1 < len(providers) else None


def _best_take(takes: list[dict]) -> dict | None:
    if not takes:
        return None
    return sorted(
        takes,
        key=lambda take: (
            bool(take.get("selected")),
            _number((take.get("qc") or {}).get("score"), -1),
            take.get("created_at") or "",
        ),
        reverse=True,
    )[0]


def _qc_from_job_logs(jobs: list[dict]) -> dict[str, dict]:
    """Recover old video QC that was logged but not projected into ``takes``.

    Earlier ingestion looked only for ``take.qc`` while the video stage writes
    ``take.qa``. The complete verdict still exists in the immutable job log,
    so Shadow Mode can evaluate old episodes without regenerating anything.
    New ingestion writes the field correctly; this is a compatibility bridge.
    """
    recovered: dict[str, dict] = {}
    for job in sorted(jobs, key=lambda row: row.get("created_at") or ""):
        for match in QC_LOG_LINE.finditer(job.get("log") or ""):
            scene_id, score, verdict, issues = match.groups()
            recovered[scene_id] = {
                "pass": verdict == "OK",
                "score": _number(score),
                "issues": [issues.strip()] if issues.strip() else [],
                "fix_hint": "",
                "evidence_source": "production_job_log",
            }
    return recovered


def _scene_decision(scene: dict, takes: list[dict], brief: dict,
                    completed_motion_stills: set[str] | None = None) -> dict:
    scene_id = scene["scene_id"]
    route = _route_for(brief, scene_id)
    supervised_motion_still = scene_id in (completed_motion_stills or set())
    if route.get("mode") == "motion_still" or supervised_motion_still:
        return {
            "scene_id": scene_id,
            "verdict": "ready",
            "recommended_action": "keep_motion_still",
            "reason": ("The supervised repair already replaced this failed clip with a zero-cost "
                       "editorial motion-still." if supervised_motion_still else
                       "The approved production plan already uses a zero-cost editorial motion-still."),
            "evidence": {"routing_mode": "motion_still", "qc_score": None, "issues": [],
                         "evidence_source": ("production_job_log" if supervised_motion_still
                                             else "episode_plan")},
            "risk": "low",
            "confidence": 0.99,
            "estimated_incremental_usd": 0.0,
            "requires_human": False,
        }

    take = _best_take(takes)
    if not take:
        return {
            "scene_id": scene_id,
            "verdict": "insufficient_evidence",
            "recommended_action": "inspect_missing_take",
            "reason": "No generated video take is available for visual judgement.",
            "evidence": {"routing_mode": route.get("mode") or "video", "qc_score": None,
                         "issues": ["missing video take"]},
            "risk": "high",
            "confidence": 1.0,
            "estimated_incremental_usd": 0.0,
            "requires_human": True,
        }

    qc = take.get("qc") or {}
    score = _number(qc.get("score"), -1)
    issues_text = _text(qc.get("issues") or [])
    fix_hint = _text(qc.get("fix_hint") or "")
    passed = bool(qc.get("pass")) or score >= 7
    evidence = {
        "take_id": take.get("take_id"),
        "provider": take.get("endpoint") or take.get("provider"),
        "qc_score": None if score < 0 else score,
        "issues": qc.get("issues") or [],
        "fix_hint": qc.get("fix_hint") or "",
    }
    if passed:
        return {
            "scene_id": scene_id,
            "verdict": "ready",
            "recommended_action": "accept_existing_take",
            "reason": "The existing take meets the configured visual QC threshold.",
            "evidence": evidence,
            "risk": "low" if not issues_text else "medium",
            "confidence": 0.96 if score >= 8 else 0.86,
            "estimated_incremental_usd": 0.0,
            "requires_human": False,
        }

    failed_takes = []
    for candidate in takes:
        candidate_qc = candidate.get("qc") or {}
        candidate_score = _number(candidate_qc.get("score"), -1)
        if not bool(candidate_qc.get("pass")) and candidate_score < 7:
            failed_takes.append(candidate)
    provider_failures: dict[str, int] = {}
    for candidate in failed_takes:
        provider = candidate.get("endpoint") or candidate.get("provider") or "unknown"
        provider_failures[provider] = provider_failures.get(provider, 0) + 1
    repeated_same_engine = max(provider_failures.values(), default=0) >= 2

    categories = _categories(f"{issues_text}; {fix_hint}")
    action_text = (scene.get("action") or "").lower()
    static_friendly = (str(scene.get("lens") or "").lower() == "85mm"
                       or any(word in action_text for word in STATIC_BEATS))
    if repeated_same_engine:
        action = "convert_to_motion_still"
        reason = ("Two paid attempts on the same engine failed visual QC. Stop buying the same "
                  "failure pattern and preserve the story beat as a controlled editorial motion-still.")
        incremental = 0.0
    elif "identity_anatomy" in categories and static_friendly:
        action = "convert_to_motion_still"
        reason = ("Identity or anatomy is unstable in a beat that can retain its story meaning "
                  "as a controlled editorial motion-still.")
        incremental = 0.0
    elif "continuity_props" in categories and "identity_anatomy" not in categories:
        action = "recompose_keyframe_then_retry"
        reason = ("The failure is concentrated in props or location continuity; lock those in a "
                  "new first frame before buying another animation.")
        incremental = _number(take.get("actual_usd") or take.get("estimated_usd"))
    else:
        action = "simplify_action_then_retry"
        reason = ("The shot combines identity or camera instability with motion. Reduce it to one "
                  "controlled action before any approved retry.")
        incremental = _number(take.get("actual_usd") or take.get("estimated_usd"))
    return {
        "scene_id": scene_id,
        "verdict": "repair",
        "recommended_action": action,
        "reason": reason,
        "evidence": {**evidence, "categories": categories,
                     "next_provider": _next_provider(route, take.get("endpoint") or ""),
                     "failed_attempts": len(failed_takes),
                     "same_engine_stop": repeated_same_engine},
        "risk": "high" if score < 6 or "identity_anatomy" in categories else "medium",
        "confidence": 0.9 if categories != ["unclassified_visual_qc"] else 0.68,
        "estimated_incremental_usd": round(incremental, 4),
        # Shadow mode never authorizes spending or a creative substitution.
        "requires_human": True,
    }


def _snapshot(series_id: str, episode_id: str) -> dict:
    episode = store.get("episodes", {"series_id": series_id, "episode_id": episode_id})
    if not episode:
        raise ValueError("episode not found")
    scenes = store.list("scenes", {"series_id": series_id, "episode_id": episode_id},
                        order="sequence")
    takes = store.list("takes", {"series_id": series_id, "episode_id": episode_id},
                       order="created_at")
    costs = store.list("costs", {"series_id": series_id, "episode_id": episode_id})
    jobs = store.list("production_jobs", {"series_id": series_id, "episode_id": episode_id},
                      order="created_at")
    completed_motion_stills = {
        match.group(1)
        for job in jobs
        for match in MOTION_STILL_LOG_LINE.finditer(job.get("log") or "")
    }
    return {"episode": episode, "scenes": scenes, "takes": takes, "costs": costs,
            "log_qc": _qc_from_job_logs(jobs),
            "completed_motion_stills": completed_motion_stills}


def _digest(snapshot: dict) -> str:
    relevant = {
        "policy": POLICY_VERSION,
        "brief": (snapshot["episode"].get("brief") or {}),
        "status": snapshot["episode"].get("status"),
        "spent_usd": snapshot["episode"].get("spent_usd"),
        "scenes": [{key: scene.get(key) for key in
                    ("scene_id", "sequence", "action", "lens", "status", "qa")}
                   for scene in snapshot["scenes"]],
        "takes": [{key: take.get(key) for key in
                   ("take_id", "scene_id", "stage", "endpoint", "actual_usd", "qc", "selected")}
                  for take in snapshot["takes"]],
        "legacy_log_qc": snapshot.get("log_qc") or {},
        "completed_motion_stills": sorted(snapshot.get("completed_motion_stills") or []),
    }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def latest_shadow_report(series_id: str, episode_id: str) -> dict | None:
    rows = store.list("generation_history", {
        "series_id": series_id, "episode_id": episode_id, "event": EVENT,
    }, order="created_at", desc=True, limit=1)
    return dict((rows[0].get("detail") or {})) if rows else None


def run_shadow_analysis(series_id: str, episode_id: str, actor: str = "system") -> dict:
    """Create an idempotent, no-spend Core V2 decision report."""
    snapshot = _snapshot(series_id, episode_id)
    digest = _digest(snapshot)
    previous = latest_shadow_report(series_id, episode_id)
    if previous and previous.get("input_digest") == digest:
        return {**previous, "reused": True}

    brief = snapshot["episode"].get("brief") or {}
    by_scene: dict[str, list[dict]] = {}
    for take in snapshot["takes"]:
        if (take.get("stage") or "").startswith("vid"):
            candidate = dict(take)
            scene_id = candidate.get("scene_id") or ""
            if not candidate.get("qc") and scene_id in snapshot["log_qc"]:
                candidate["qc"] = snapshot["log_qc"][scene_id]
            by_scene.setdefault(scene_id, []).append(candidate)
    decisions = [_scene_decision(scene, by_scene.get(scene["scene_id"], []), brief,
                                 snapshot.get("completed_motion_stills"))
                 for scene in snapshot["scenes"]]
    ready = sum(decision["verdict"] == "ready" for decision in decisions)
    repair = sum(decision["verdict"] == "repair" for decision in decisions)
    missing = len(decisions) - ready - repair
    projected = round(sum(_number(d.get("estimated_incremental_usd")) for d in decisions), 4)
    spent = _number(snapshot["episode"].get("spent_usd"))
    budget = _number(snapshot["episode"].get("budget_usd"))
    report = {
        "core": CORE,
        "policy_version": POLICY_VERSION,
        "mode": "shadow",
        "series_id": series_id,
        "episode_id": episode_id,
        "input_digest": digest,
        "created_at": _now(),
        "summary": {
            "scene_count": len(decisions),
            "ready": ready,
            "repair": repair,
            "insufficient_evidence": missing,
            "spent_usd": round(spent, 4),
            "budget_usd": round(budget, 4),
            "remaining_budget_usd": round(max(0.0, budget - spent), 4),
            "projected_repair_usd": projected,
            "ready_for_supervised_mode": repair == 0 and missing == 0,
        },
        "decisions": decisions,
        "guardrails": {
            "paid_calls": False,
            "take_selection_changes": False,
            "automatic_approvals": False,
            "publication": False,
        },
        "reused": False,
    }
    store.insert("generation_history", {
        "series_id": series_id,
        "episode_id": episode_id,
        "entity_type": "episode",
        "entity_id": episode_id,
        "event": EVENT,
        "detail": report,
        "actor": actor,
        "created_at": report["created_at"],
    })
    return report


def supervised_force(report: dict, brief: dict) -> list[str]:
    """Translate a reviewed report into narrow, auditable production tokens."""
    force: list[str] = []
    unsupported: list[str] = []
    for decision in report.get("decisions") or []:
        scene_id = decision.get("scene_id") or ""
        action = decision.get("recommended_action") or ""
        if action in ("accept_existing_take", "keep_motion_still"):
            continue
        if action == "convert_to_motion_still":
            force.append(f"motion_still:{scene_id}")
            continue
        if action in RETRY_ACTIONS:
            route = _route_for(brief, scene_id)
            providers = [route.get("primary"), *(route.get("fallbacks") or [])]
            providers = [provider for provider in providers if provider]
            used = (decision.get("evidence") or {}).get("provider")
            route_index = providers.index(used) if used in providers else 0
            force.append(f"video_retry:{scene_id}:r{route_index}")
            continue
        if decision.get("verdict") != "ready":
            unsupported.append(f"{scene_id}:{action}")
    if unsupported:
        raise ValueError("Core V2 cannot safely execute these recommendations yet: " +
                         ", ".join(unsupported))
    return force


def approve_supervised_repair(series_id: str, episode_id: str, *, actor: str,
                              input_digest: str, max_incremental_usd: float) -> dict:
    """Record human approval and start only the exact reviewed repair plan.

    This is intentionally separate from Shadow Mode.  It creates a receipt for
    the report digest, never enables publication, never advances to a fallback
    engine, and gives the live runner only scene-scoped one-shot tokens.
    """
    from . import live_jobs, runner
    from .packaging import materialize
    from serial.package import SeriesPackage

    report = latest_shadow_report(series_id, episode_id)
    if not report or report.get("input_digest") != input_digest:
        raise ValueError("The Core V2 report changed. Run Shadow Analysis again and review it.")
    summary = report.get("summary") or {}
    if int(summary.get("insufficient_evidence") or 0):
        raise ValueError("The repair plan needs more evidence before it can run.")
    projected = _number(summary.get("projected_repair_usd"))
    cap = _number(max_incremental_usd, -1)
    if cap < projected or projected < 0:
        raise ValueError(f"Approve at least the reviewed repair estimate of ${projected:.2f}.")
    if projected > _number(summary.get("remaining_budget_usd")):
        raise ValueError("The reviewed repair estimate exceeds the remaining episode budget.")

    episode = store.get("episodes", {"series_id": series_id, "episode_id": episode_id}) or {}
    force = supervised_force(report, episode.get("brief") or {})
    if not force:
        raise ValueError("The current Core V2 report has no repair work to start.")
    existing = next((job for job in store.list(
        "production_jobs", {"series_id": series_id, "episode_id": episode_id},
        order="created_at", desc=True,
    ) if set(job.get("force") or []) == set(force)), None)
    if existing:
        return {"job": existing, "report": report, "force": force, "reused": True}

    timestamp = _now()
    approval = store.insert("approvals", {
        "series_id": series_id,
        "episode_id": episode_id,
        "subject_type": SUPERVISED_APPROVAL,
        "subject_id": input_digest,
        "decision": "approved",
        "actor": actor,
        "note": (f"Core V2 supervised repair; maximum incremental estimate ${cap:.2f}; "
                 "same-engine retries only; no fallback; no publication."),
        "created_at": timestamp,
    })
    for token in force:
        store.insert("approvals", {
            "series_id": series_id,
            "episode_id": episode_id,
            "subject_type": "core_v2_repair_token",
            "subject_id": token,
            "decision": "approved",
            "actor": actor,
            "note": f"Bound to Core V2 report {input_digest}; one-shot repair token.",
            "created_at": timestamp,
        })
    package = SeriesPackage(materialize(series_id))
    digest = live_jobs.package_digest(package, episode_id)
    previous = store.list("production_jobs", {
        "series_id": series_id, "episode_id": episode_id,
    }, order="created_at", desc=True, limit=1)
    audio_mode = (((previous[0].get("progress") or {}).get("audio_mode"))
                  if previous else None) or "voices"
    job = runner.jobs.start(
        series_id, episode_id,
        ["video"],
        actor, force,
        approved_digest=digest, approve_live=True, audio_mode=audio_mode,
    )
    store.insert("generation_history", {
        "series_id": series_id, "episode_id": episode_id,
        "entity_type": "job", "entity_id": job["id"],
        "event": SUPERVISED_EVENT, "actor": actor,
        "detail": {"report_digest": input_digest, "approval_id": approval.get("id"),
                   "force": force, "projected_repair_usd": projected,
                   "max_incremental_usd": cap, "publication": False,
                   "automatic_fallback": False},
        "created_at": timestamp,
    })
    return {"job": job, "report": report, "force": force, "reused": False}
