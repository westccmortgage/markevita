"""One explicitly approved, bounded clip; independent of the mock episode runner.

The durable job is also the submission lock. Once submission might have begun,
no code path sends it again. Refresh only collects the saved provider request.
Provider credentials and presigned URLs never enter the job log.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import PIPELINE_DIR, STUDIO_DIR, settings
from .store import store

sys.path.insert(0, str(PIPELINE_DIR))
from serial.config import Config  # noqa: E402
from serial.storage import R2  # noqa: E402

SPEC_PATH = STUDIO_DIR / "previews" / "first_clip.json"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
STORAGE_LEASE_SECONDS = 180
_FIXED = {
    "id": "first_clip_v1", "series_id": "island_of_no_witnesses",
    "episode_id": "preview01", "endpoint": "fal-ai/veo3.1/fast",
    "duration_seconds": 8, "aspect_ratio": "9:16", "resolution": "1080p",
    "generate_audio": True, "auto_fix": False,
}


class PreviewError(RuntimeError):
    """Safe operator-facing error; never contains provider response bodies."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def definition() -> dict:
    raw = SPEC_PATH.read_bytes()
    spec = json.loads(raw)
    if not isinstance(spec, dict) or any(
        spec.get(k) != v or type(spec.get(k)) is not type(v) for k, v in _FIXED.items()
    ):
        raise PreviewError("The preview definition is outside the approved model and clip limits.")
    if any(not isinstance(spec.get(k), str) or not spec[k].strip()
           for k in ("prompt", "title", "series_title", "summary", "dialogue")):
        raise PreviewError("The preview definition is incomplete.")
    if len(spec["prompt"]) > 12000:
        raise PreviewError("The preview prompt is too long.")
    for key in ("max_usd", "estimated_usd"):
        if isinstance(spec.get(key), bool) or str(spec.get(key)) not in ("1.2", "1.20"):
            raise PreviewError("The preview budget must remain $1.20.")
    return {**spec, "digest": hashlib.sha256(raw).hexdigest()}


def problems() -> list[str]:
    result = []
    if not settings.allow_paid or not _enabled("STUDIO_ALLOW_PAID"):
        result.append("STUDIO_ALLOW_PAID must be enabled for this approved preview.")
    if not _enabled("PIPELINE_ALLOW_PAID"):
        result.append("PIPELINE_ALLOW_PAID must be enabled for this approved preview.")
    if store.name != "supabase":
        result.append("The preview requires the durable Supabase store; local storage cannot submit paid work.")
    for name in ("FAL_KEY", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        value = os.getenv(name, "").strip()
        if not value or value.lower() in ("your_key", "your-key", "changeme", "placeholder"):
            result.append(f"{name} is not configured.")
    if not shutil.which("ffprobe"):
        result.append("ffprobe must be installed before the clip can be generated and validated.")
    return result


def _key(spec: dict) -> str:
    return f"clip-preview:{spec['id']}:{spec['digest']}"


def current_job() -> dict | None:
    return store.get("production_jobs", {"idempotency_key": _key(definition())})


def _revision(state: str) -> str:
    return f"Preview {state}. Revision {uuid.uuid4().hex}."


def _save(job: dict, state: str, progress: dict, error: str | None = None) -> tuple[dict, bool]:
    patch = {"state": state, "progress": progress, "error": error, "log": _revision(state)}
    if state in ("done", "failed"):
        patch["finished_at"] = _now()
    # Atomic compare-and-swap on scalar columns supported by both existing
    # drivers. The random revision also prevents a stale refresh overwriting
    # a later provider result or a storage lease.
    changed = store.update("production_jobs", {
        "id": job["id"], "state": job["state"], "log": job["log"],
    }, patch)
    latest = store.get("production_jobs", {"id": job["id"]})
    if not latest:
        raise PreviewError("The durable preview job is unavailable.")
    return latest, bool(changed)


def _id(job: dict, kind: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{job['idempotency_key']}:{kind}"))


def _insert_once(table: str, row: dict, where: dict) -> dict:
    existing = store.get(table, where)
    if existing:
        return existing
    try:
        return store.insert(table, row)
    except Exception:
        # Only a now-visible matching row makes an insert error a harmless
        # concurrent winner. Network or schema errors still stop the operation.
        existing = store.get(table, where)
        if existing:
            return existing
        raise PreviewError(f"Could not persist the preview {table} record.") from None


def _r2() -> R2:
    cfg = Config.load(PIPELINE_DIR, live=True)
    expected = f"https://{cfg.r2_account_id}.r2.cloudflarestorage.com"
    if not cfg.r2_configured or cfg.r2_s3_endpoint.rstrip("/") != expected:
        raise PreviewError("R2 credentials and the account's standard S3 endpoint are required.")
    return R2(cfg, lambda _message: None)


def _preflight() -> None:
    try:
        storage = _r2()
        storage.client.head_bucket(Bucket=storage.cfg.r2_bucket)
        # A bucket listing alone does not prove write access. This tiny probe
        # contains no secrets and is retained at one deterministic private key.
        payload = b'{"purpose":"studio-clip-preview-storage-preflight"}'
        key = "studio/preflight/first_clip_v1.json"
        storage.client.put_object(Bucket=storage.cfg.r2_bucket, Key=key,
                                  Body=payload, ContentType="application/json")
        head = storage.client.head_object(Bucket=storage.cfg.r2_bucket, Key=key)
        if int(head.get("ContentLength", -1)) != len(payload):
            raise PreviewError("R2 write verification failed. No generation request was sent.")
    except PreviewError:
        raise
    except Exception:
        raise PreviewError("R2 preflight failed. No generation request was sent.") from None


def _queue_url(value: object, request_id: str) -> str:
    if not isinstance(value, str):
        raise PreviewError("The provider did not return a usable request URL.")
    parsed = urlparse(value)
    parts = parsed.path.strip("/").split("/")
    is_request_path = (parts[-2:] == ["requests", request_id]
                       or parts[-3:] in (["requests", request_id, "status"],
                                         ["requests", request_id, "response"]))
    if (parsed.scheme != "https" or parsed.hostname != "queue.fal.run"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.fragment or parsed.query
            or not is_request_path or any(part in (".", "..", "") for part in parts)
            or "%" in parsed.path):
        raise PreviewError("The provider returned an unexpected request URL.")
    return value


def _headers() -> dict:
    key = os.getenv("FAL_KEY", "").strip()
    if not key:
        raise PreviewError("FAL_KEY is required to retrieve this existing request.")
    return {"Authorization": f"Key {key}"}


def _payload(spec: dict) -> dict:
    return {
        "prompt": spec["prompt"], "duration": "8s", "aspect_ratio": "9:16",
        "resolution": "1080p", "generate_audio": True, "auto_fix": False,
    }


def start(actor: str, approved_digest: str, approved_max_usd: object) -> dict:
    spec = definition()
    try:
        amount = Decimal(str(approved_max_usd))
    except (InvalidOperation, ValueError):
        raise PreviewError("Approve the displayed $1.20 maximum before starting.") from None
    if (not isinstance(actor, str) or not actor.strip() or not amount.is_finite() or amount != Decimal("1.20")
            or approved_digest != spec["digest"]):
        raise PreviewError("The approval must match this exact clip and its $1.20 maximum.")
    existing = current_job()
    if existing:
        return existing  # Includes failed/ambiguous jobs: never resubmit.
    blockers = problems()
    if blockers:
        raise PreviewError(" ".join(blockers))
    _preflight()  # No billable submission until durable output storage responds.
    _insert_once("series", {
        "id": spec["series_id"], "title": spec["series_title"],
        "logline": spec["summary"], "genre": "romantic thriller", "language": "en-US",
        "format": {"aspect_ratio": "9:16", "width": 1080, "height": 1920, "captions": "both"},
        "approval": {"status": "draft"}, "status": "draft", "created_at": _now(), "updated_at": _now(),
    }, {"id": spec["series_id"]})
    _insert_once("seasons", {
        "series_id": spec["series_id"], "season_id": "previews", "number": 0,
        "title": "Previews", "arc": "", "episode_order": [spec["episode_id"]],
    }, {"series_id": spec["series_id"], "season_id": "previews"})
    _insert_once("episodes", {
        "series_id": spec["series_id"], "season_id": "previews", "episode_id": spec["episode_id"],
        "number": 1, "title": spec["title"], "logline": spec["summary"], "status": "preview",
        "target_seconds": 8, "budget_usd": 1.2,
        "brief": {"kind": "clip_preview", "preview_digest": spec["digest"]},
        "created_at": _now(), "updated_at": _now(),
    }, {"series_id": spec["series_id"], "episode_id": spec["episode_id"]})
    progress = {
        "stage": "clip_preview", "done": [], "total": 1,
        "spec": spec, "digest": spec["digest"],
        "approval": {"actor": actor, "at": _now(), "max_usd": 1.2},
        "cost": {"estimated_usd": 1.2, "reserved_usd": 1.2, "actual_usd": None,
                 "invoice_verified": False, "basis": "published price estimate"},
        "request": {}, "result": {},
    }
    # Database UNIQUE(idempotency_key) arbitrates multiple processes. Only the
    # process that transitions queued -> submission_unknown can issue a POST.
    row = {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, _key(spec))),
        "series_id": spec["series_id"], "episode_id": spec["episode_id"],
        "stages": ["clip_preview"], "state": "queued", "mode": "live",
        "requested_by": actor, "idempotency_key": _key(spec), "force": [],
        "progress": progress, "log": _revision("queued"), "created_at": _now(), "started_at": _now(),
    }
    job = _insert_once("production_jobs", row, {"idempotency_key": row["idempotency_key"]})
    if job["log"] != row["log"]:
        return job
    try:
        _insert_once("costs", {
            "id": _id(job, "cost"), "series_id": spec["series_id"], "episode_id": spec["episode_id"],
            "stage": "clip_preview", "provider": "fal.ai", "endpoint": spec["endpoint"],
            "take_id": _id(job, "take"), "estimated_usd": 1.2,
            # Unknown invoice amount is never presented as a measured charge.
            "actual_usd": 0, "created_at": _now(),
        }, {"id": _id(job, "cost")})
    except Exception:
        job, _ = _save(job, "failed", progress, "Cost reservation could not be saved. No request was sent.")
        return job
    job, claimed = _save(job, "submission_unknown", progress,
                         "Submission may have begun. This job will never be submitted automatically again.")
    if not claimed:
        return job
    try:
        # httpx has zero application retries; redirects are never followed.
        response = httpx.post(
            f"https://queue.fal.run/{spec['endpoint']}",
            headers={**_headers(), "X-Fal-No-Retry": "1"},
            json=_payload(spec), timeout=30, follow_redirects=False,
        )
        if response.status_code not in (200, 201, 202):
            return _save(job, "submission_unknown", progress,
                         f"Provider submission returned HTTP {response.status_code}. Do not submit again; reconcile the fal dashboard.")[0]
        data = response.json()
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", request_id):
            raise PreviewError("The provider response did not contain a valid request ID.")
        # Persist the ID before inspecting any other provider field. If later
        # validation fails, an operator can still reconcile that exact request.
        progress = {**progress, "request": {"request_id": request_id, "status": "ACCEPTED"}}
        job, saved = _save(job, "submission_unknown", progress, "Request accepted; validating collection URLs.")
        if not saved:
            return job
        progress["request"] = {
            **progress["request"], "status_url": _queue_url(data.get("status_url"), request_id),
            "response_url": _queue_url(data.get("response_url"), request_id),
        }
        return _save(job, "submitted", progress)[0]
    except Exception:
        # A timeout, malformed response, or failed persistence is ambiguous:
        # leave the reservation intact and categorically prohibit resubmission.
        try:
            latest = store.get("production_jobs", {"id": job["id"]}) or job
            return _save(latest, "submission_unknown", latest.get("progress") or progress,
                         "Submission outcome needs reconciliation in fal. No automatic retry will be made.")[0]
        except Exception:
            raise PreviewError("Submission status could not be saved. Do not submit again; check this job and fal.") from None


def _media_url(value: object) -> str:
    if not isinstance(value, str):
        raise PreviewError("The completed provider result has no MP4 URL.")
    parsed = urlparse(value)
    hostname = parsed.hostname or ""
    if (parsed.scheme != "https" or not (hostname == "fal.media" or hostname.endswith(".fal.media"))
            or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment):
        raise PreviewError("The completed media URL is outside the approved fal CDN.")
    return value


def _download(url: str, destination: Path) -> None:
    url = _media_url(url)
    # The provider key is intentionally absent from media requests.
    for _hop in range(4):
        with httpx.stream("GET", url, timeout=60, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = _media_url(response.headers.get("location"))
                continue
            if response.status_code != 200:
                raise PreviewError(f"Media download returned HTTP {response.status_code}; refresh will retrieve the same clip.")
            length = response.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > MAX_DOWNLOAD_BYTES):
                raise PreviewError("The generated file exceeds the 64 MB download limit.")
            count = 0
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes(65536):
                    count += len(chunk)
                    if count > MAX_DOWNLOAD_BYTES:
                        raise PreviewError("The generated file exceeds the 64 MB download limit.")
                    handle.write(chunk)
            if not count:
                raise PreviewError("The provider returned an empty media file.")
            return
    raise PreviewError("The media download returned too many redirects.")


def _validate_media(path: Path) -> dict:
    try:
        process = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
        ], capture_output=True, timeout=20, check=True)
        probe = json.loads(process.stdout)
        video = next(s for s in probe["streams"] if s.get("codec_type") == "video")
        duration = float(probe["format"]["duration"])
        width, height = int(video["width"]), int(video["height"])
        audio = any(s.get("codec_type") == "audio" for s in probe["streams"])
        if (not math.isfinite(duration) or not 7.5 <= duration <= 8.5
                or width != 1080 or height != 1920 or not audio
                or "mp4" not in probe["format"].get("format_name", "").split(",")):
            raise ValueError("Unexpected media characteristics")
        return {"duration_seconds": duration, "width": width, "height": height, "has_audio": audio}
    except Exception:
        raise PreviewError("The clip failed validation: an approximately 8-second 1080×1920 MP4 with audio is required. No regeneration was started.") from None


def _collect(job: dict) -> dict:
    progress = job["progress"]
    lease = progress.get("storage_lease") or {}
    if lease:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(lease["at"])).total_seconds()
            if age < STORAGE_LEASE_SECONDS:
                return job
        except (KeyError, TypeError, ValueError):
            pass
    progress = {**progress, "storage_lease": {"at": _now(), "id": uuid.uuid4().hex}}
    job, claimed = _save(job, "storage_pending", progress)
    if not claimed:
        return job
    try:
        with tempfile.TemporaryDirectory(prefix="studio-preview-") as directory:
            path = Path(directory) / "preview.mp4"
            _download(progress["result"]["provider_url"], path)
            metadata = _validate_media(path)
            with path.open("rb") as handle:
                checksum = hashlib.file_digest(handle, "sha256").hexdigest()
            spec = progress["spec"]
            take_id = _id(job, "take")
            r2_key = (f"series/{job['series_id']}/episodes/{job['episode_id']}/"
                      f"scenes/preview/takes/{take_id}/preview.mp4")
            storage = _r2()
            storage.put(path, r2_key)
            # Do not mark completed until R2 confirms the uploaded object size.
            head = storage.client.head_object(Bucket=storage.cfg.r2_bucket, Key=r2_key)
            if int(head.get("ContentLength", -1)) != path.stat().st_size:
                raise PreviewError("R2 did not confirm the complete media file.")
            _insert_once("takes", {
                "id": take_id, "series_id": job["series_id"], "episode_id": job["episode_id"],
                "scene_id": "preview", "take_id": take_id, "stage": "clip_preview", "attempt": 0,
                "provider": "fal.ai", "endpoint": spec["endpoint"],
                "request_id": progress["request"]["request_id"], "prompt": spec["prompt"],
                "params": {**_payload(spec), "preview_digest": progress["digest"], "cost_basis": "estimate; invoice unverified"},
                "r2_key": r2_key, "checksum": checksum, "duration_seconds": metadata["duration_seconds"],
                "estimated_usd": spec["estimated_usd"], "actual_usd": 0,
                "qc": {**metadata, "technical_validation": "passed", "creative_review": "pending"},
                "selected": False, "forced": False, "created_at": _now(),
            }, {"id": take_id})
            progress = {**progress, "storage_lease": None, "done": ["clip_preview"], "stage": None,
                        "result": {**progress["result"], **metadata, "r2_key": r2_key, "checksum": checksum},
                        "cost": {**progress["cost"], "estimated_spent_usd": spec["estimated_usd"]}}
            return _save(job, "done", progress)[0]
    except Exception as error:
        progress = {**progress, "storage_lease": None}
        message = str(error) if isinstance(error, PreviewError) else "Media collection or R2 storage failed. Refresh retrieves the existing clip without generating again."
        return _save(job, "storage_pending", progress, message)[0]


def refresh(job_id: str) -> dict:
    job = store.get("production_jobs", {"id": job_id})
    if not job or job.get("stages") != ["clip_preview"] or job.get("mode") != "live":
        raise PreviewError("Preview job not found.")
    if job["state"] in ("done", "failed", "queued", "submission_unknown"):
        return job
    if job["state"] == "storage_pending":
        return _collect(job)
    if job["state"] != "submitted":
        return job
    progress = job["progress"]
    request = progress.get("request") or {}
    try:
        rid = request["request_id"]
        status_response = httpx.get(_queue_url(request.get("status_url"), rid),
                                   headers=_headers(), timeout=20, follow_redirects=False)
        if not 200 <= status_response.status_code < 300:
            raise PreviewError(f"Provider status returned HTTP {status_response.status_code}. Refresh checks the same request.")
        status_data = status_response.json()
        status = status_data.get("status")
        if status not in ("IN_QUEUE", "IN_PROGRESS", "COMPLETED", "FAILED", "CANCELLED"):
            raise PreviewError("The provider returned an unknown job status. No new request was sent.")
        progress = {**progress, "request": {**request, "status": status}}
        if status == "COMPLETED" and (status_data.get("error") or status_data.get("error_type")):
            return _save(job, "failed", progress,
                         "The provider completed this request with an error. No regeneration will be started.")[0]
        if status in ("FAILED", "CANCELLED"):
            return _save(job, "failed", progress, "The provider did not produce the requested clip. No regeneration will be started.")[0]
        if status != "COMPLETED":
            return _save(job, "submitted", progress)[0]
        result_response = httpx.get(_queue_url(request.get("response_url"), rid),
                                   headers=_headers(), timeout=20, follow_redirects=False)
        if not 200 <= result_response.status_code < 300:
            raise PreviewError(f"Provider result returned HTTP {result_response.status_code}. Refresh retrieves the same request.")
        data = result_response.json()
        if data.get("error") or data.get("error_type"):
            return _save(job, "failed", progress,
                         "The provider returned an error for this completed clip. No regeneration will be started.")[0]
        provider_url = _media_url((data.get("video") or {}).get("url"))
        progress = {**progress, "result": {"provider_url": provider_url}}
        job, changed = _save(job, "storage_pending", progress)
        return _collect(job) if changed else job
    except Exception as error:
        message = str(error) if isinstance(error, PreviewError) else "Could not retrieve the saved provider request. Refresh will not generate another clip."
        return _save(job, "submitted", progress, message)[0]


def media_url(job_id: str) -> str:
    job = store.get("production_jobs", {"id": job_id})
    if not job or job.get("stages") != ["clip_preview"] or job.get("state") != "done":
        raise PreviewError("The preview is not ready to view.")
    key = ((job.get("progress") or {}).get("result") or {}).get("r2_key")
    expected = (f"series/{job['series_id']}/episodes/{job['episode_id']}/"
                f"scenes/preview/takes/{_id(job, 'take')}/preview.mp4")
    if key != expected:
        raise PreviewError("The saved preview object key is invalid.")
    try:
        return _r2().presign(key, expires=900)
    except Exception:
        raise PreviewError("The saved preview could not be opened from R2.") from None
