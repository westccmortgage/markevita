"""Measured Decision Core V2 adapter for the Series Studio.

Production Official analysis is deliberately read-only: it examines the full
episode contract, existing takes, QC evidence, motion-still placeholders and
spend, then records the exact dynamic-video repair plan. It never calls a
media provider, changes a selected take or approves a retry. Final production
is dynamic video through fal.ai; editorial motion-stills remain review aids.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .store import store

CORE = "measured-decision-core-v2"
POLICY_VERSION = "studio-production-official-1"
EVENT = "core_v2.production_official_analysis_completed"
SUPERVISED_EVENT = "core_v2.supervised_repair_started"
SUPERVISED_APPROVAL = "core_v2_repair_plan"
RETRY_ACTIONS = {"generate_dynamic_video", "switch_engine_then_retry"}
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


def _providers_for(brief: dict, scene_id: str) -> list[str]:
    """Return the route in the same order the production runner will use."""
    route = _route_for(brief, scene_id)
    configured = list(brief.get("video_route") or [])
    primary = route.get("primary") or (configured[0] if configured else None)
    return list(dict.fromkeys(
        provider for provider in [primary, *(route.get("fallbacks") or []), *configured]
        if provider
    ))


def _video_estimate(scene: dict, provider: str | None) -> float | None:
    """Published-rate estimate for one clip, or None when no rate is known.

    An engine missing from the price list used to raise out of here and take
    the whole report down with it. Pricing it at zero would be worse: it would
    sit inside an approved ceiling that never allowed for it. So it is unknown,
    said so by name, and the approval refuses until it is priced.
    """
    if not provider:
        return 0.0
    from serial.costs import UnknownVideoModel, video_cost

    try:
        return round(video_cost(
            int(scene.get("duration_seconds") or 0), False, "1080p", provider,
        ), 4)
    except UnknownVideoModel:
        return None


def _failed_engines(takes: list[dict]) -> set[str]:
    """Every engine that already produced a take QC marked down for this scene.

    Takes with no verdict at all are left out: nobody judged them, so they
    are not evidence that the engine fails this shot.
    """
    failed: set[str] = set()
    for take in takes:
        qc = take.get("qc") or {}
        if not qc:
            continue
        if not bool(qc.get("pass")) and _number(qc.get("score"), -1) < 7:
            engine = take.get("endpoint") or take.get("provider")
            if engine:
                failed.add(engine)
    return failed


def _next_untried(providers: list[str], failed: set[str]) -> tuple[int, str | None]:
    """The first engine on the route this scene has not already failed on.

    Choosing only past the engine of the best take let a scene that failed on
    Veo and then on Kling be offered Kling again whenever the Veo take scored
    higher — buying a failure already paid for once.
    """
    for index, provider in enumerate(providers):
        if provider not in failed:
            return index, provider
    return len(providers), None


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
    providers = _providers_for(brief, scene_id)
    supervised_motion_still = scene_id in (completed_motion_stills or set())
    take = _best_take(takes)
    if route.get("mode") == "motion_still" or supervised_motion_still:
        used = ((take or {}).get("endpoint") or (take or {}).get("provider") or "")
        tried = _failed_engines(takes)
        if supervised_motion_still and used:
            # A supervised still replaced a clip that failed; that engine is
            # spent for this shot even if its verdict never reached the take.
            tried.add(used)
        target_index, target = _next_untried(providers, tried)
        if not target:
            return {
                "scene_id": scene_id, "verdict": "insufficient_evidence",
                "recommended_action": "review_exhausted_dynamic_route",
                "reason": "The motion-still cannot enter the master and no untried dynamic engine remains.",
                "evidence": {"routing_mode": "motion_still", "provider": used,
                             "dynamic_final_required": True},
                "risk": "high", "confidence": 1.0,
                "estimated_incremental_usd": 0.0, "requires_human": True,
            }
        return {
            "scene_id": scene_id,
            "verdict": "repair",
            "recommended_action": "generate_dynamic_video",
            "reason": ("The current clip is an editorial motion-still. Production Official "
                       "requires a full dynamic video beat; the still remains only as a visual reference."),
            "evidence": {"routing_mode": "motion_still", "qc_score": None, "issues": [],
                         "evidence_source": ("production_job_log" if supervised_motion_still
                                             else "episode_plan"),
                         "provider": used or None, "target_provider": target,
                         "target_route_index": target_index,
                         "dynamic_final_required": True},
            "risk": "medium",
            "confidence": 1.0,
            "estimated_incremental_usd": _video_estimate(scene, target),
            "requires_human": True,
        }

    if not take:
        target = providers[0] if providers else None
        if target:
            return {
                "scene_id": scene_id,
                "verdict": "repair",
                "recommended_action": "generate_dynamic_video",
                "reason": "No dynamic video take exists; generate the single approved story beat.",
                "evidence": {"routing_mode": route.get("mode") or "video", "qc_score": None,
                             "issues": ["missing dynamic video take"],
                             "target_provider": target, "target_route_index": 0,
                             "dynamic_final_required": True},
                "risk": "medium",
                "confidence": 1.0,
                "estimated_incremental_usd": _video_estimate(scene, target),
                "requires_human": True,
            }
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
    used = take.get("endpoint") or take.get("provider") or ""
    tried = _failed_engines(takes) | ({used} if used else set())
    target_index, target = _next_untried(providers, tried)
    if not target:
        return {
            "scene_id": scene_id,
            "verdict": "insufficient_evidence",
            "recommended_action": "review_exhausted_dynamic_route",
            "reason": "The failed provider cannot be repeated and no untried dynamic engine remains.",
            "evidence": {**evidence, "categories": categories,
                         "failed_attempts": len(failed_takes),
                         "same_engine_stop": repeated_same_engine,
                         "dynamic_final_required": True},
            "risk": "high", "confidence": 1.0,
            "estimated_incremental_usd": 0.0, "requires_human": True,
        }
    reason = ("The existing dynamic take failed visual QC. Simplify the beat to one motivated "
              "action and switch to the next approved fal.ai video engine; do not buy the same "
              "engine again and do not substitute a motion-still.")
    return {
        "scene_id": scene_id,
        "verdict": "repair",
        "recommended_action": "switch_engine_then_retry",
        "reason": reason,
        "evidence": {**evidence, "categories": categories,
                     "next_provider": target, "target_provider": target,
                     "target_route_index": target_index,
                     "failed_attempts": len(failed_takes),
                     "same_engine_stop": repeated_same_engine,
                     "dynamic_final_required": True},
        "risk": "high" if score < 6 or "identity_anatomy" in categories else "medium",
        "confidence": 0.9 if categories != ["unclassified_visual_qc"] else 0.68,
        "estimated_incremental_usd": _video_estimate(scene, target),
        # Analysis never authorizes spending or a creative substitution.
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
    """Create an idempotent, no-spend Production Official decision report."""
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
    unpriced = [d["scene_id"] for d in decisions
                if d.get("verdict") == "repair" and d.get("estimated_incremental_usd") is None]
    projected = round(sum(_number(d.get("estimated_incremental_usd")) for d in decisions
                          if d.get("estimated_incremental_usd") is not None), 4)
    spent = _number(snapshot["episode"].get("spent_usd"))
    budget = _number(snapshot["episode"].get("budget_usd"))
    report = {
        "core": CORE,
        "policy_version": POLICY_VERSION,
        "mode": "production_official_preview",
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
            # Not in the total above, and the approval refuses while any are
            # listed: an amount nobody can state cannot be approved.
            "unpriced_scenes": unpriced,
            "dynamic_ready": ready,
            "dynamic_repair": repair,
            "motion_stills_allowed_in_master": 0,
            "ready_for_supervised_mode": repair == 0 and missing == 0,
        },
        "decisions": decisions,
        "guardrails": {
            "paid_calls": False,
            "take_selection_changes": False,
            "automatic_approvals": False,
            "dynamic_video_only": True,
            "motion_stills_in_master": False,
            "provider_gateway": "fal.ai",
            "repeat_failed_engine": False,
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
        if action == "accept_existing_take":
            continue
        if action in RETRY_ACTIONS:
            evidence = decision.get("evidence") or {}
            route_index = int(evidence.get("target_route_index") or 0)
            force.append(f"video:{scene_id}:r{route_index}")
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

    This is intentionally separate from read-only analysis. It creates a
    receipt for the report digest, never enables publication, and gives the
    live runner only the reviewed scene/provider one-shot tokens.
    """
    from . import live_jobs, runner
    from .packaging import materialize
    from serial.package import SeriesPackage

    report = latest_shadow_report(series_id, episode_id)
    if not report or report.get("input_digest") != input_digest:
        raise ValueError("The Core V2 report changed. Run Production Official analysis again and review it.")
    summary = report.get("summary") or {}
    if int(summary.get("insufficient_evidence") or 0):
        raise ValueError("The repair plan needs more evidence before it can run.")
    if summary.get("unpriced_scenes"):
        raise ValueError("No published price is known for the engine chosen for "
                         + ", ".join(summary["unpriced_scenes"])
                         + "; the ceiling cannot cover an amount nobody can state.")
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
                 "reviewed dynamic provider switch only; no motion-stills; no publication."),
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
                   "automatic_fallback": False, "dynamic_video_only": True,
                   "motion_stills_in_master": False},
        "created_at": timestamp,
    })
    return {"job": job, "report": report, "force": force, "reused": False}


# ── finishing preflight ────────────────────────────────────────────────────
#
# Video repair and finishing are different pieces of work with different
# money attached, and the authorization for one must never carry the other.
# This computes what finishing would cost and what it would reuse. It calls
# no provider, writes no take and starts nothing.

FINISHING_APPROVAL = "core_v2_finishing_plan"
FINISHING_EVENT = "core_v2.finishing_plan_computed"
FINISHING_STAGES = ("voice", "lipsync", "assemble", "qa", "deliver")
FINISHING_POLICY = "studio-finishing-1"


def _latest_job(series_id: str, episode_id: str) -> dict:
    rows = store.list("production_jobs", {"series_id": series_id, "episode_id": episode_id},
                      order="created_at", desc=True, limit=1)
    return rows[0] if rows else {}


def _finished_stages(series_id: str, episode_id: str) -> set[str]:
    """Stages the runner has recorded as done on the most recent job."""
    progress = (_latest_job(series_id, episode_id).get("progress") or {})
    return {str(stage) for stage in (progress.get("done") or [])}


def _spoken_scenes(scenes: list[dict], stills: set[str]) -> list[dict]:
    """Scenes with a line to say. A held frame has no lips to sync."""
    return [s for s in scenes
            if (s.get("dialogue") or []) and str(s.get("scene_id")) not in stills]


def finishing_plan(series_id: str, episode_id: str) -> dict:
    """What finishing would run, cost and reuse — without running any of it.

    Read-only by construction: it reads the store and the published price
    list, and touches no provider. The repair authorization cannot pay for
    any of this; finishing carries its own digest, its own ceiling and its
    own confirmation, and publication is not part of either.
    """
    from serial import costs as costmod
    from serial.config import Config
    from .config import PIPELINE_DIR
    from . import progress as progressmod

    snapshot = _snapshot(series_id, episode_id)
    scenes, stills = snapshot["scenes"], set(snapshot["completed_motion_stills"])
    cfg = Config.load(PIPELINE_DIR, live=True)
    done = _finished_stages(series_id, episode_id)

    characters = sum(len(str(line.get("text") or ""))
                     for scene in scenes for line in (scene.get("dialogue") or []))
    voice_rate = float(costmod.PRICE.get("elevenlabs_per_1k_chars_estimate") or 0.0)
    spoken = _spoken_scenes(scenes, stills)
    lipsync_seconds = sum(float(s.get("duration_seconds") or 0) for s in spoken)

    stages = [
        # Speech is paid for whatever the price list says. The published
        # per-character rate is not in it, so the amount is unknown rather
        # than nothing: showing it as free would put a real charge inside a
        # ceiling that never allowed for it.
        {"stage": "voice", "needed": "voice" not in done and bool(characters),
         "paid": True, "provider": "ElevenLabs",
         "estimated_usd": round(characters / 1000.0 * voice_rate, 2) if voice_rate > 0 else None,
         "cost_known": voice_rate > 0,
         "cost_note": None if voice_rate > 0 else
             ("PRICE_ELEVENLABS_PER_1K_CHARS_ESTIMATE is not set, so this amount is unknown. "
              "It is charged against the ElevenLabs plan, not against this ceiling."),
         "reuses": "Speech already recorded for a line that has not changed.",
         "on_failure": "The line is left unvoiced and the episode stops before lipsync; nothing recorded is discarded.",
         "resumable": True, "unit": f"{characters} characters"},
        {"stage": "lipsync", "needed": "lipsync" not in done and bool(spoken),
         "paid": True, "provider": cfg.fal_lipsync_model,
         "estimated_usd": round(costmod.lipsync_cost(lipsync_seconds, cfg.lipsync_variant), 2),
         "reuses": "The clip and the speech for each scene; held frames are skipped entirely.",
         "on_failure": "That scene keeps its unsynced clip and the episode stops; the clip is not regenerated.",
         "resumable": True, "unit": f"{len(spoken)} scene(s), {lipsync_seconds:.0f}s"},
        {"stage": "assemble", "needed": "assemble" not in done,
         "paid": False, "provider": "local ffmpeg",
         "estimated_usd": 0.0,
         "reuses": "Every finished scene, the subtitle cues and any music bed already in the package.",
         "on_failure": "No master is written and the previous one is left untouched.",
         "resumable": True, "unit": f"{len(scenes)} scene(s)"},
        {"stage": "qa", "needed": "qa" not in done,
         "paid": False, "provider": "local ffprobe",
         "estimated_usd": 0.0,
         "reuses": "The assembled master; nothing is generated to check it.",
         "on_failure": "The report names each failed check and the master is kept for review.",
         "resumable": True, "unit": "13 checks"},
        {"stage": "deliver", "needed": "deliver" not in done,
         "paid": False, "provider": "Cloudflare R2",
         "estimated_usd": 0.0,
         "reuses": "The master and its QA report as they stand.",
         "on_failure": "Nothing is published; the master stays where it is and delivery can be retried.",
         "resumable": True, "unit": "1 master"},
    ]
    for stage in stages:
        stage["needs_confirmation"] = True
        stage.setdefault("cost_known", True)
        stage.setdefault("cost_note", None)

    total = round(sum(s["estimated_usd"] for s in stages
                      if s["needed"] and s["cost_known"]), 2)
    unknown = [s["stage"] for s in stages if s["needed"] and not s["cost_known"]]
    ledger = progressmod.ledger(series_id, episode_id)
    budget = progressmod.budget(series_id, episode_id)
    plan = {
        "core": CORE, "policy": FINISHING_POLICY,
        "series_id": series_id, "episode_id": episode_id,
        "input_digest": _digest(snapshot),
        "stages": stages,
        "paid_stages": [s["stage"] for s in stages if s["needed"] and s["paid"]],
        "free_stages": [s["stage"] for s in stages if s["needed"] and not s["paid"]],
        "skipped_stages": [s["stage"] for s in stages if not s["needed"]],
        "projected_finishing_usd": total,
        # Named rather than folded into the total, because a number that
        # quietly omits a charge is worse than one that says what it omits.
        "cost_unknown_stages": unknown,
        "spent_usd": ledger["internal_actual"],
        "budget_usd": budget,
        "remaining_budget_usd": round(max(0.0, budget - ledger["internal_actual"]), 2),
        # Publication is not a finishing stage and no finishing approval
        # covers it. It is listed so the screen can say so out loud.
        "publication": {"stage": "publish", "included": False, "enabled": False,
                        "needs_separate_confirmation": True,
                        "note": "Publication is never part of a finishing approval."},
        "automatic_fallback": False,
        "repair_authorization_applies": False,
        "not_run": ["video", "keyframes", "references", "publish"],
        "computed_at": _now(),
    }
    plan["video_repair_complete"] = "video" in done
    plan["blocked"] = not plan["video_repair_complete"]
    return plan


def approve_finishing(series_id: str, episode_id: str, *, actor: str,
                      input_digest: str, max_incremental_usd: float) -> dict:
    """Record a separate human approval and run only the finishing stages.

    Deliberately not reachable from the repair receipt. The repair plan was
    priced and approved as video work; carrying that authorization into voice
    and lipsync would spend money against a ceiling nobody was shown. So this
    takes its own digest, its own ceiling and its own confirmation, and it
    never includes publication.
    """
    from . import live_jobs, runner
    from .packaging import materialize
    from serial.package import SeriesPackage

    plan = finishing_plan(series_id, episode_id)
    if plan["input_digest"] != input_digest:
        raise ValueError("The episode changed since this plan was shown. Reload it and review again.")
    if plan["blocked"]:
        raise ValueError("Video repair is not complete, so finishing cannot start yet.")
    running = [s["stage"] for s in plan["stages"] if s["needed"]]
    if not running:
        raise ValueError("Every finishing stage is already done for this episode.")
    projected = _number(plan["projected_finishing_usd"])
    cap = _number(max_incremental_usd, -1)
    if cap < projected or projected < 0:
        raise ValueError(f"Approve at least the projected finishing estimate of ${projected:.2f}.")
    if projected > _number(plan["remaining_budget_usd"]):
        raise ValueError("The projected finishing estimate exceeds the remaining episode budget.")

    timestamp = _now()
    approval = store.insert("approvals", {
        "series_id": series_id, "episode_id": episode_id,
        "subject_type": FINISHING_APPROVAL, "subject_id": input_digest,
        "decision": "approved", "actor": actor,
        "note": (f"Core V2 finishing; stages {', '.join(running)}; maximum incremental "
                 f"estimate ${cap:.2f}; no video regeneration; no fallback; no publication."),
        "created_at": timestamp,
    })
    package = SeriesPackage(materialize(series_id))
    digest = live_jobs.package_digest(package, episode_id)
    previous = store.list("production_jobs", {"series_id": series_id, "episode_id": episode_id},
                          order="created_at", desc=True, limit=1)
    audio_mode = (((previous[0].get("progress") or {}).get("audio_mode"))
                  if previous else None) or "voices"
    # Never "publish", and never "video": an approval for finishing must not
    # be able to buy another clip.
    job = runner.jobs.start(series_id, episode_id, list(FINISHING_STAGES), actor, [],
                            approved_digest=digest, approve_live=True, audio_mode=audio_mode)
    store.insert("generation_history", {
        "series_id": series_id, "episode_id": episode_id,
        "entity_type": "job", "entity_id": job["id"],
        "event": FINISHING_EVENT, "actor": actor,
        "detail": {"plan_digest": input_digest, "approval_id": approval.get("id"),
                   "stages": running, "projected_finishing_usd": projected,
                   "max_incremental_usd": cap, "cost_unknown_stages": plan["cost_unknown_stages"],
                   "publication": False, "automatic_fallback": False,
                   "video_regeneration": False},
        "created_at": timestamp,
    })
    return {"job": job, "plan": plan, "stages": running}
