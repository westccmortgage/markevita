"""Production job queue and worker.

Turns the v0.3 command-line pipeline into a controllable service. Each job runs
the engine's stages one at a time in a background worker, so the panel can:

* start a run for a set of stages,
* pause it (the worker stops at the next stage boundary),
* resume it (the engine skips completed stages and reuses finished takes),
* cancel it.

Resumability and idempotency come from the engine itself: a stage marked done
is skipped, a take that already succeeded is not re-requested, and an
in-flight provider request is collected by request id rather than resubmitted.
The studio adds an idempotency key per job so a double-clicked button cannot
start a second run.

Mock jobs use the original local engine path. Reviewed live jobs use the same
engine with private checkpoints, a durable worker lease and paid-call tracking.
"""
from __future__ import annotations

import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .config import PIPELINE_DIR, settings
from .ingest import history, ingest_episode, ingest_series_state
from .packaging import materialize, package_dir
from .store import store

sys.path.insert(0, str(PIPELINE_DIR))

from serial.config import Config           # noqa: E402
from serial.package import PackageError, SeriesPackage, validate_episode  # noqa: E402
from serial.pipeline import STAGES, Pipeline, SeriesState  # noqa: E402
from serial.state import State             # noqa: E402

RUNS_ROOT = PIPELINE_DIR / "runs"
# Every stage except publish. Publishing is a separate, explicitly approved act.
DEFAULT_STAGES = [s for s in STAGES if s != "publish"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobCancelled(RuntimeError):
    pass


# The engine is a command-line tool and phrases its blocking errors as CLI
# instructions. Inside the panel that advice is wrong, so known blockers are
# restated as the action the producer can actually take here.
_GUIDANCE = [
    ("QA failed", "The assembled video failed final quality checks. The job log names the failed checks; generated scenes are saved."),
    ("Audio normalization", "The soundtrack could not be normalized. Source media is saved; review the audio before continuing."),
    ("ChunkedEncodingError", "The download of an already generated file was interrupted. Resume this episode to retrieve the saved result; do not force regeneration."),
    ("не прошёл QC", "A reference image did not pass quality control. Review the job log and reference images before continuing."),
    ("submission outcome", "A provider submission needs reconciliation; do not force or repeat generation."),
    ("interrupted request", "A paid request was interrupted before its response was saved. Reconcile it with the provider before another attempt."),
    ("approval референсов", "Approve the reference pack on the series' References page, then resume."),
    ("approval 'publish'", "Approve the finished episode on its page, then run the publish stage."),
    ("series.json approval.status", "Approve the current script and budget in the production form."),
    ("PIPELINE_ALLOW_PAID", "Enable PIPELINE_ALLOW_PAID in the service environment to use live production."),
    ("needs_budget_override", "The episode hit its budget cap. Record an override with a reason to continue."),
    ("budget:", "The episode hit its budget cap. Record an override with a reason to continue."),
    ("не помещаются", "A spoken line is too long for its clip. Shorten it in the script and save again."),
    ("нет voice id", "A character has no voice id in the environment. Check the Characters page for the variable name."),
    ("не прошли QC", "Generated material failed quality control. Force those scenes to regenerate, or accept them."),
    ("не прошло QC", "Generated material failed quality control. Force those scenes to regenerate, or accept them."),
]


def explain(error: str) -> str:
    """Panel-facing guidance for a known engine blocker, or '' when there is none."""
    for needle, advice in _GUIDANCE:
        if needle in error:
            return advice
    return ""


class _Control:
    def __init__(self):
        self.pause = threading.Event()
        self.cancel = threading.Event()


class JobManager:
    """Owns the worker threads. One active job per episode."""

    def __init__(self):
        self._controls: dict[str, _Control] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    # ── queries ────────────────────────────────────────────────────────────

    def active_job(self, series_id: str, episode_id: str) -> dict | None:
        for job in store.list("production_jobs", {"series_id": series_id, "episode_id": episode_id},
                              order="created_at", desc=True):
            if job.get("state") in ("queued", "running", "paused"):
                return job
        return None

    def jobs_for(self, series_id: str, episode_id: str | None = None, limit: int = 50) -> list[dict]:
        where = {"series_id": series_id}
        if episode_id:
            where["episode_id"] = episode_id
        return store.list("production_jobs", where, order="created_at", desc=True, limit=limit)

    # ── control ────────────────────────────────────────────────────────────

    def start(self, series_id: str, episode_id: str, stages: list[str] | None = None,
              requested_by: str = "", force: list[str] | None = None, *,
              approved_digest: str = "", approve_live: bool = False, audio_mode: str = "native") -> dict:
        episode = store.get("episodes", {"series_id": series_id, "episode_id": episode_id}) or {}
        if episode.get("status") == "preview" or (episode.get("brief") or {}).get("kind") == "clip_preview":
            raise ValueError("Open First clip to manage this preview; episode production cannot restart it.")
        stages = stages or DEFAULT_STAGES
        unknown = [s for s in stages if s not in STAGES]
        if unknown:
            raise ValueError(f"unknown stages: {unknown}")
        if "publish" in stages:
            raise PermissionError("Publishing is not available from episode production.")
        stages = [stage for stage in DEFAULT_STAGES if stage in stages]
        if settings.allow_paid:
            from .live_jobs import start
            return start(self, series_id, episode_id, stages, requested_by, force or [],
                         approved_digest, approve_live, audio_mode)

        with self._lock:
            existing = self.active_job(series_id, episode_id)
            if existing and existing["state"] in ("queued", "running"):
                return existing  # idempotent: the run is already going

            signature = f"{series_id}:{episode_id}:{','.join(stages)}:{','.join(sorted(force or []))}"
            job = store.insert("production_jobs", {
                "series_id": series_id, "episode_id": episode_id, "stages": stages,
                "state": "queued", "mode": settings.mode, "requested_by": requested_by,
                "idempotency_key": f"{signature}@{_now()}",
                "force": force or [], "progress": {"stage": None, "done": [], "total": len(stages)},
                "log": "", "created_at": _now(),
            })
            control = _Control()
            self._controls[job["id"]] = control
            thread = threading.Thread(target=self._run, args=(job["id"], control), daemon=True,
                                      name=f"job-{job['id'][:8]}")
            self._threads[job["id"]] = thread
            thread.start()
        history(series_id, episode_id, "job.start", entity_type="job", entity_id=job["id"],
                detail={"stages": stages, "mode": settings.mode}, actor=requested_by or "system")
        return job

    def pause(self, job_id: str, actor: str = "") -> None:
        job = store.get("production_jobs", {"id": job_id}) or {}
        if job.get("stages") == ["clip_preview"]:
            raise ValueError("Use First clip to check this request; preview collection cannot be paused here.")
        control = self._controls.get(job_id)
        if control:
            control.pause.set()
        job = store.get("production_jobs", {"id": job_id}) or {}
        store.update("production_jobs", {"id": job_id}, {"state": "pausing"})
        history(job.get("series_id", ""), job.get("episode_id", ""), "job.pause",
                entity_type="job", entity_id=job_id, actor=actor or "system")

    def cancel(self, job_id: str, actor: str = "") -> None:
        job = store.get("production_jobs", {"id": job_id}) or {}
        if job.get("stages") == ["clip_preview"]:
            raise ValueError("This preview has already been submitted; use First clip to retrieve its result.")
        control = self._controls.get(job_id)
        if control:
            control.cancel.set()
        job = store.get("production_jobs", {"id": job_id}) or {}
        store.update("production_jobs", {"id": job_id}, {"state": "cancelling"})
        history(job.get("series_id", ""), job.get("episode_id", ""), "job.cancel",
                entity_type="job", entity_id=job_id, actor=actor or "system")

    def resume(self, series_id: str, episode_id: str, requested_by: str = "", **approval) -> dict:
        """Resume production. Completed stages are skipped by the engine and
        finished takes are reused, so this never repeats paid work."""
        paused = None
        for job in store.list("production_jobs", {"series_id": series_id, "episode_id": episode_id},
                              order="created_at", desc=True):
            if job.get("state") in ("paused", "failed", "cancelled", "interrupted", "running", "queued"):
                paused = job
                break
        stages = list(paused.get("stages") or DEFAULT_STAGES) if paused else DEFAULT_STAGES
        force = list(paused.get("force") or []) if paused else []
        return self.start(series_id, episode_id, stages, requested_by, force, **approval)

    # ── worker ─────────────────────────────────────────────────────────────

    def _run(self, job_id: str, control: _Control) -> None:
        job = store.get("production_jobs", {"id": job_id})
        if not job:
            return
        series_id, episode_id = job["series_id"], job["episode_id"]
        stages: list[str] = list(job["stages"])
        lines: list[str] = []

        def log(msg: str) -> None:
            lines.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
            store.update("production_jobs", {"id": job_id}, {"log": "\n".join(lines[-400:])})

        store.update("production_jobs", {"id": job_id}, {"state": "running", "started_at": _now()})
        done: list[str] = []
        pipeline = None
        try:
            root = materialize(series_id)
            log(f"package materialised at {root}")
            pkg = SeriesPackage(root)
            cfg = Config.load(PIPELINE_DIR, live=False)  # mock is enforced, never inherited
            cfg.mode = "mock"

            ep_dir = RUNS_ROOT / series_id / episode_id
            ep_dir.mkdir(parents=True, exist_ok=True)
            # Honour a recorded budget override exactly as the CLI does.
            for override in State(ep_dir).data.get("overrides", []):
                if override.get("key") == "MAX_EPISODE_BUDGET_USD":
                    cfg.max_episode_budget_usd = float(override["new"])
                    log(f"budget override in effect: ${cfg.max_episode_budget_usd:.2f}")

            pipeline = Pipeline(cfg, pkg, episode_id, RUNS_ROOT, force=set(job.get("force") or []))
            for stage in stages:
                if control.cancel.is_set():
                    raise JobCancelled("cancelled by operator")
                if control.pause.is_set():
                    store.update("production_jobs", {"id": job_id}, {
                        "state": "paused", "progress": {"stage": stage, "done": done, "total": len(stages)},
                    })
                    log(f"paused before stage '{stage}' — resume continues from here")
                    history(series_id, episode_id, "job.paused", entity_type="job", entity_id=job_id,
                            detail={"next_stage": stage})
                    return
                store.update("production_jobs", {"id": job_id}, {
                    "progress": {"stage": stage, "done": done, "total": len(stages)}})
                log(f"stage {stage}: start")
                getattr(pipeline, f"stage_{stage}")()
                done.append(stage)
                log(f"stage {stage}: done")
                # Project state after every stage so the panel stays current.
                ingest_episode(series_id, episode_id, RUNS_ROOT)
                ingest_series_state(series_id, RUNS_ROOT)

            store.update("production_jobs", {"id": job_id}, {
                "state": "done", "finished_at": _now(),
                "progress": {"stage": None, "done": done, "total": len(stages)},
            })
            log("run complete")
            history(series_id, episode_id, "job.done", entity_type="job", entity_id=job_id,
                    detail={"stages": done})

        except JobCancelled as e:
            store.update("production_jobs", {"id": job_id},
                         {"state": "cancelled", "finished_at": _now(), "error": str(e)})
            log(f"cancelled: {e}")
        except Exception as e:  # engine raised: record it, keep the run resumable
            detail = f"{type(e).__name__}: {e}"
            advice = explain(str(e))
            if advice:
                detail = f"{detail}\n\nWhat to do: {advice}"
            store.update("production_jobs", {"id": job_id}, {
                "state": "failed", "finished_at": _now(), "error": detail,
                "progress": {"stage": None, "done": done, "total": len(stages)},
            })
            log("FAILED: " + detail)
            log(traceback.format_exc(limit=6))
            history(series_id, episode_id, "job.failed", entity_type="job", entity_id=job_id,
                    detail={"error": detail, "done": done})
        finally:
            try:
                ingest_episode(series_id, episode_id, RUNS_ROOT)
                ingest_series_state(series_id, RUNS_ROOT)
            except Exception:
                pass
            with self._lock:
                self._controls.pop(job_id, None)
                self._threads.pop(job_id, None)
            if pipeline is not None:
                try:
                    pipeline.logf.close()
                except Exception:
                    pass


jobs = JobManager()


# ── free operations (no worker, no provider traffic) ───────────────────────

def validate_series(series_id: str) -> dict:
    """Validate the whole package and every written brief. Costs nothing."""
    root = materialize(series_id)
    cfg = Config.load(PIPELINE_DIR, live=False)
    result: dict = {"package": None, "episodes": [], "ok": False, "package_dir": str(root)}
    try:
        pkg = SeriesPackage(root)
    except PackageError as e:
        result["package"] = str(e)
        return result
    result["package"] = "ok"
    result["bible_version"] = pkg.bible_version
    prev_end = None
    all_ok = True
    for _season_id, ep_id in pkg.episode_order():
        brief = pkg.episode_dir(ep_id) / "brief.json"
        if not brief.exists():
            result["episodes"].append({"episode_id": ep_id, "ok": None, "message": "No brief yet — add scenes."})
            prev_end = None
            continue
        try:
            ep = pkg.load_episode(ep_id)
            norm = validate_episode(pkg, ep, prev_end, cfg)
            result["episodes"].append({
                "episode_id": ep_id, "ok": True,
                "clips": len(norm["scenes"]), "seconds": norm["total_seconds"],
                "warnings": norm.get("warnings") or [],
                "prompts": "package" if pkg.load_production_prompts(ep_id) else "llm",
            })
            prev_end = norm["end_state"]
        except PackageError as e:
            result["episodes"].append({"episode_id": ep_id, "ok": False, "message": str(e)})
            all_ok = False
            prev_end = None
    result["ok"] = all_ok
    return result


def approve_references(series_id: str, actor: str, note: str = "") -> dict:
    """Record approval of the reference pack for the current bible version.
    Mirrors `run_episode.py --approve references`."""
    root = materialize(series_id)
    pkg = SeriesPackage(root)
    if settings.allow_paid:
        from .live_jobs import approve_references as approve_live_references
        return approve_live_references(series_id, actor, note)
    ss = SeriesState(RUNS_ROOT / series_id / "series_state.json")
    if ss.data.get("bible_version") != pkg.bible_version or not ss.data["references"]["characters"]:
        raise ValueError(
            f"No reference pack exists for bible version {pkg.bible_version}. "
            "Run the 'references' stage first."
        )
    ss.data["approvals"]["references"] = {
        "approved": True, "bible_version": pkg.bible_version, "by": actor, "at": _now(), "note": note,
    }
    for group in ss.data["references"].values():
        for pack in group.values():
            records = [pack] if "path" in pack else list(pack.values())
            for rec in records:
                if isinstance(rec, dict):
                    rec["approval"] = "approved"
    ss.save()
    store.insert("approvals", {
        "series_id": series_id, "episode_id": "", "subject_type": "references",
        "subject_id": pkg.bible_version, "decision": "approved", "actor": actor,
        "note": note, "created_at": _now(),
    })
    ingest_series_state(series_id, RUNS_ROOT)
    history(series_id, "", "approval.references", entity_type="references",
            entity_id=pkg.bible_version, actor=actor, detail={"note": note})
    return {"bible_version": pkg.bible_version, "by": actor}


def approve_publish(series_id: str, episode_id: str, actor: str, note: str = "") -> dict:
    """Record final approval of a finished episode. Mirrors
    `run_episode.py --approve publish`. Publishing itself stays disabled."""
    ep_dir = runtime_root() / series_id / episode_id
    st = State(ep_dir)
    if st.data.get("status") != "complete" and not st.data.get("delivered"):
        raise ValueError(
            f"Episode is '{st.data.get('status')}', not complete. There is nothing to approve yet."
        )
    st.data["approvals"]["publish"] = {"approved": True, "by": actor, "at": _now(), "note": note}
    st.save()
    store.insert("approvals", {
        "series_id": series_id, "episode_id": episode_id, "subject_type": "publish",
        "subject_id": episode_id, "decision": "approved", "actor": actor,
        "note": note, "created_at": _now(),
    })
    history(series_id, episode_id, "approval.publish", entity_type="episode",
            entity_id=episode_id, actor=actor, detail={"note": note})
    return {"episode_id": episode_id, "by": actor}


def record_override(series_id: str, episode_id: str, key: str, value: str,
                    actor: str, reason: str) -> dict:
    """Record an explicit limit override (actor, time, old, new, reason), as
    the specification requires. No agent can raise a limit by itself."""
    if not (actor and reason):
        raise ValueError("An override requires both an actor and a reason.")
    import os
    ep_dir = RUNS_ROOT / series_id / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    st = State(ep_dir)
    st.data["overrides"].append({
        "key": key, "old": os.environ.get(key), "new": value,
        "reason": reason, "by": actor, "at": _now(),
    })
    if st.data.get("status") == "needs_budget_override":
        st.data["status"] = "validated"
    st.save()
    history(series_id, episode_id, "override.recorded", entity_type="episode",
            entity_id=episode_id, actor=actor, detail={"key": key, "new": value, "reason": reason})
    return {"key": key, "new": value}


def runtime_root() -> Path:
    return RUNS_ROOT / "live" if settings.allow_paid else RUNS_ROOT


def episode_runtime(series_id: str, episode_id: str) -> dict:
    """Live view of an episode's engine state, for the panel."""
    ep_dir = runtime_root() / series_id / episode_id
    st = State(ep_dir)
    if settings.allow_paid and not st.path.exists():
        from .live_jobs import saved_state
        try:
            st.data.update(saved_state(series_id, episode_id))
        except Exception:
            st.data["status"] = "checkpoint_unavailable"
    log_path = ep_dir / "log.txt"
    tail = ""
    if log_path.exists():
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:])
    masters = []
    out = ep_dir / "out" / "masters"
    if out.exists():
        for v in sorted(out.iterdir(), key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else -1):
            masters.append({"version": v.name, "files": sorted(f.name for f in v.iterdir() if f.is_file())})
    if not masters and st.data.get("master_version"):
        masters = [{"version": "v" + str(st.data["master_version"]), "files": ["episode.mp4", "episode.srt", "poster.jpg"]}]
    qa_reports = []
    qa_dir = ep_dir / "out" / "qa"
    if qa_dir.exists():
        import json
        for v in sorted(qa_dir.iterdir()):
            report = v / "report.json"
            if report.exists():
                try:
                    qa_reports.append({"version": v.name, **json.loads(report.read_text(encoding="utf-8"))})
                except json.JSONDecodeError:
                    pass
    return {
        "status": st.data.get("status", "draft"),
        "stages": st.data.get("stages", {}),
        "spent_usd": float(st.data.get("spent_usd") or 0.0),
        "reserved_usd": float(st.data.get("reserved_usd") or 0.0),
        "approvals": st.data.get("approvals", {}),
        "overrides": st.data.get("overrides", []),
        "takes": st.data.get("takes", {}),
        "cost_log": st.data.get("cost_log", []),
        "masters": masters,
        "qa": qa_reports,
        "log": tail,
        "run_dir": str(ep_dir),
    }
