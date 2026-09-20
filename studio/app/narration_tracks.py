"""Generate one approved narration-only track without touching picture.

Director cuts may be assembled outside the episode pipeline from already-paid
takes.  Re-running the voice stage would also reassemble the old picture, so
this deliberately narrow job reads only the narrator lines, makes one MP3,
and stores it privately for download.  Its digest and unique job key make a
double click harmless.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import PIPELINE_DIR, settings
from .ingest import history
from .preview import _r2
from .store import store

from serial import providers
from serial.config import Config


class NarrationTrackError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _script(series_id: str, episode_id: str) -> tuple[str, int]:
    paragraphs = []
    for scene in store.list("scenes", {"series_id": series_id, "episode_id": episode_id},
                            order="sequence"):
        lines = []
        for line in scene.get("dialogue") or []:
            speaker = str(line.get("speaker") or line.get("character_id") or "").lower()
            if speaker == "narrator":
                text = " ".join(str(line.get("text") or "").split())
                if text:
                    lines.append(text)
        if lines:
            paragraphs.append(" ".join(lines))
    text = "\n\n".join(paragraphs)
    if not text:
        raise NarrationTrackError("This episode has no narrator lines.")
    if len(text) > 5000:
        raise NarrationTrackError("The narration is longer than the approved single-track limit.")
    return text, len(paragraphs)


def _spec(series_id: str, episode_id: str) -> dict:
    series = store.get("series", {"id": series_id})
    episode = store.get("episodes", {"series_id": series_id, "episode_id": episode_id})
    voice = store.get("voices", {"series_id": series_id, "character_id": "narrator"})
    if not series or not episode:
        raise NarrationTrackError("Episode not found.")
    if not voice:
        raise NarrationTrackError("Assign the narrator voice before generating its track.")
    text, paragraphs = _script(series_id, episode_id)
    public = {
        "series_id": series_id,
        "episode_id": episode_id,
        "text": text,
        "paragraphs": paragraphs,
        "language": series.get("language") or "en-US",
        "voice_env": voice.get("voice_env") or "ELEVENLABS_VOICE_ID_NARRATOR",
        "model_id": voice.get("model_id") or "eleven_v3",
    }
    public["digest"] = hashlib.sha256(
        json.dumps(public, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return public


def status(series_id: str, episode_id: str) -> dict:
    try:
        spec = _spec(series_id, episode_id)
    except NarrationTrackError:
        return {"state": "unavailable"}
    job = store.get("production_jobs", {
        "idempotency_key": f"narration-track:{series_id}:{episode_id}:{spec['digest']}"
    })
    return {"state": "idle", "digest": spec["digest"]} if not job else {
        "state": job.get("state"), "digest": spec["digest"],
        "error": job.get("error") or "",
    }


def start(series_id: str, episode_id: str, actor: str) -> dict:
    if not settings.allow_paid:
        raise NarrationTrackError("Live provider calls are disabled.")
    spec = _spec(series_id, episode_id)
    key = f"narration-track:{series_id}:{episode_id}:{spec['digest']}"
    existing = store.get("production_jobs", {"idempotency_key": key})
    if existing:
        return existing
    row = {
        "id": str(uuid.uuid4()), "series_id": series_id, "episode_id": episode_id,
        "stages": ["narration_track"], "state": "queued", "mode": "live",
        "requested_by": actor, "idempotency_key": key, "force": [],
        "progress": {"digest": spec["digest"], "paragraphs": spec["paragraphs"]},
        "log": "Narration track queued.", "created_at": _now(),
    }
    try:
        job = store.insert("production_jobs", row)
    except Exception:
        job = store.get("production_jobs", {"idempotency_key": key})
        if not job:
            raise
        return job

    def run() -> None:
        store.update("production_jobs", {"id": job["id"]}, {
            "state": "running", "started_at": _now(), "log": "Generating narration only."
        })
        try:
            _generate(spec)
        except Exception as exc:  # noqa: BLE001 - persisted for the operator
            message = str(exc)[:400]
            store.update("production_jobs", {"id": job["id"]}, {
                "state": "failed", "error": message, "finished_at": _now(),
                "log": "Narration generation failed; no automatic retry was made.",
            })
            history(series_id, episode_id, "narration.track_failed", entity_type="job",
                    entity_id=job["id"], actor=actor, detail={"error": message})
        else:
            store.update("production_jobs", {"id": job["id"]}, {
                "state": "done", "finished_at": _now(),
                "log": "Narration track ready. Picture was not changed.",
            })
            history(series_id, episode_id, "narration.track_generated", entity_type="job",
                    entity_id=job["id"], actor=actor,
                    detail={"digest": spec["digest"], "paragraphs": spec["paragraphs"],
                            "language": spec["language"]})

    threading.Thread(target=run, daemon=True,
                     name=f"narration-{series_id}-{episode_id}").start()
    return job


def _generate(spec: dict) -> None:
    cfg = Config.load(PIPELINE_DIR, live=True)
    if not cfg.allow_paid_env or not cfg.elevenlabs_api_key:
        raise NarrationTrackError("ElevenLabs live generation is not configured.")
    voice_id = os.environ.get(spec["voice_env"], "").strip()
    if not voice_id and spec["voice_env"].startswith("ELEVENLABS_VOICE_ID_"):
        voice_id = cfg.voice_ids.get(spec["voice_env"][20:].lower(), "")
    if not voice_id:
        raise NarrationTrackError(f"The configured narrator voice {spec['voice_env']} is unavailable.")
    storage = _r2()
    r2_key = (f"series/{spec['series_id']}/episodes/{spec['episode_id']}/post/"
              f"narration/{spec['digest']}.mp3")
    try:
        storage.client.head_object(Bucket=storage.cfg.r2_bucket, Key=r2_key)
        return
    except Exception:
        pass
    with tempfile.TemporaryDirectory(prefix="narration-track-") as tmp:
        path = Path(tmp) / "narration.mp3"
        providers.tts(
            cfg, lambda _message: None, spec["text"],
            "warm intimate mature Russian fairy-tale narration; calm, restrained, natural pauses",
            voice_id, path, model_id=spec["model_id"], language_code=spec["language"],
        )
        if not path.exists() or not path.stat().st_size:
            raise NarrationTrackError("The voice provider returned an empty track.")
        storage.put(path, r2_key)


def object_key(series_id: str, episode_id: str, digest: str) -> str:
    state = status(series_id, episode_id)
    if state.get("state") != "done" or state.get("digest") != digest:
        raise NarrationTrackError("This narration track is not ready.")
    return f"series/{series_id}/episodes/{episode_id}/post/narration/{digest}.mp3"
