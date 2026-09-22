"""Engine run state → studio records.

The v0.3 engine is the source of truth while a run is in flight: it writes
`runs/<series>/<episode>/state.json` and `runs/<series>/series_state.json`.
After every stage the studio reads those files and projects them into the
database so the admin panel can show scenes, takes, costs, references,
approvals and history without reaching into the filesystem.

Only object keys, checksums and metadata are stored — the media itself stays
in R2 (or, in mock mode, on local disk under runs/).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .store import store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def history(series_id: str, episode_id: str, event: str, *, entity_type: str = "",
            entity_id: str = "", detail: dict | None = None, actor: str = "system") -> None:
    store.insert("generation_history", {
        "series_id": series_id, "episode_id": episode_id, "entity_type": entity_type,
        "entity_id": entity_id, "event": event, "detail": detail or {},
        "actor": actor, "created_at": _now(),
    })


def ingest_episode(series_id: str, episode_id: str, runs_root: Path) -> dict:
    """Project one episode's run state into the store. Returns a summary."""
    ep_dir = runs_root / series_id / episode_id
    state = _read(ep_dir / "state.json")
    if not state:
        return {"status": None, "takes": 0, "spent_usd": 0.0}

    store.update("episodes", {"series_id": series_id, "episode_id": episode_id}, {
        "status": state.get("status", "draft"),
        "spent_usd": round(float(state.get("spent_usd") or 0.0), 4),
        "updated_at": _now(),
    })

    _ingest_scenes(series_id, episode_id, state)
    n_takes = _ingest_takes(series_id, episode_id, state)
    _ingest_costs(series_id, episode_id, state)
    _ingest_manifests(series_id, episode_id, ep_dir)
    _ingest_ledger(series_id, episode_id, ep_dir)

    return {
        "status": state.get("status"),
        "takes": n_takes,
        "spent_usd": round(float(state.get("spent_usd") or 0.0), 4),
        "reserved_usd": round(float(state.get("reserved_usd") or 0.0), 4),
        "stages": state.get("stages", {}),
        "approvals": state.get("approvals", {}),
        "overrides": state.get("overrides", []),
    }


def _ingest_scenes(series_id: str, episode_id: str, state: dict) -> None:
    """Record per-scene production status. Scenes split by the engine
    (sc07 → sc07a/sc07b) appear here as their own rows."""
    episode = state.get("episode") or {}
    by_id = {s["scene_id"]: s for s in episode.get("scenes", [])}
    for scene_id, info in (state.get("scenes") or {}).items():
        existing = store.get("scenes", {"series_id": series_id, "episode_id": episode_id, "scene_id": scene_id})
        status = "complete" if info.get("lipsync") or info.get("video") else "pending"
        if info.get("qa_failed"):
            status = "failed_qa"
        patch = {"status": status, "qa": info.get("qc") or info.get("qa") or {}}
        if existing:
            store.update("scenes", {"series_id": series_id, "episode_id": episode_id, "scene_id": scene_id}, patch)
        elif scene_id in by_id:
            # A sub-shot created by the engine's two-speaker split.
            src = by_id[scene_id]
            store.upsert("scenes", {
                "series_id": series_id, "episode_id": episode_id, "scene_id": scene_id,
                "sequence": int(src.get("sequence") or 0),
                "duration_seconds": int(src.get("duration_seconds") or 0),
                "location": src.get("location", ""),
                "lighting_state": src.get("lighting_state", "default"),
                "characters_in_frame": src.get("characters_in_frame") or [],
                "wardrobe": src.get("wardrobe") or {},
                "action": src.get("action", ""),
                "dialogue": src.get("dialogue") or [],
                "shot_type": src.get("shot_type", ""),
                "lens": src.get("lens", ""),
                "camera_motion": src.get("camera_motion", ""),
                "continuity_in": src.get("continuity_in", ""),
                "continuity_out": src.get("continuity_out", ""),
                "props": src.get("props") or [],
                "is_cliffhanger": bool(src.get("is_cliffhanger")),
                **patch,
            })


def _ingest_takes(series_id: str, episode_id: str, state: dict) -> int:
    n = 0
    for take_id, t in (state.get("takes") or {}).items():
        parts = take_id.split("_")
        scene_id = parts[1] if len(parts) > 2 else ""
        stage = parts[2] if len(parts) > 3 else t.get("what", "")
        attempt = 0
        if parts and parts[-1].isdigit():
            attempt = int(parts[-1])
        store.upsert("takes", {
            "series_id": series_id, "episode_id": episode_id, "scene_id": scene_id,
            "take_id": ("live:" if state.get("mode") == "live" else "") + take_id, "stage": stage, "attempt": attempt,
            "provider": t.get("provider", ""), "endpoint": t.get("endpoint", ""),
            "request_id": t.get("request_id", ""),
            "prompt": (t.get("params") or {}).get("prompt", "") or t.get("prompt", ""),
            "negative": (t.get("params") or {}).get("negative_prompt", ""),
            "params": t.get("params") or {},
            "r2_key": t.get("r2_key", ""), "checksum": t.get("checksum", ""),
            "duration_seconds": t.get("duration_seconds"),
            "estimated_usd": float(t.get("estimated_cost") or 0.0),
            "actual_usd": float(t.get("actual_cost") or 0.0),
            # Image takes historically used ``qc`` while video takes use
            # ``qa``. Dropping the latter made passed video look unreviewed in
            # the database even though the immutable runtime and job log held
            # the verdict.
            "qc": t.get("qc") or t.get("qa") or {},
            "selected": t.get("status") == "succeeded",
            "forced": bool(t.get("forced_by_operator")),
            "created_at": t.get("created_at") or _now(),
        })
        n += 1
    return n


def _ingest_costs(series_id: str, episode_id: str, state: dict) -> None:
    """Replace the episode's cost rows with the engine's cost log (the log is
    append-only and authoritative, so a rewrite keeps the two in step)."""
    log = state.get("cost_log") or []
    if not log:
        return
    live = state.get("mode") == "live"
    for row in store.list("costs", {"series_id": series_id, "episode_id": episode_id}):
        if row.get("stage") != "clip_preview" and row.get("stage", "").startswith("live/") == live:
            store.delete("costs", {"id": row["id"]})
    takes = state.get("takes") or {}
    for entry in log:
        take_id = entry.get("take_id") or ""
        t = takes.get(take_id) or state.get("paid_operations", {}).get(take_id, {})
        store.insert("costs", {
            "series_id": series_id, "episode_id": episode_id,
            "stage": ("live/" if live else "") + (entry.get("what") or "").split(" ")[0],
            "provider": t.get("provider", ""), "endpoint": t.get("endpoint", ""),
            "take_id": ("live:" if live else "") + take_id,
            "estimated_usd": float(entry.get("estimated") or 0.0),
            "actual_usd": float(entry.get("actual") or 0.0),
            "created_at": _now(),
        })


def _ingest_manifests(series_id: str, episode_id: str, ep_dir: Path) -> None:
    masters = ep_dir / "out" / "masters"
    if not masters.exists():
        return
    for version_dir in sorted(masters.iterdir()):
        manifest = _read(version_dir / "manifest.json")
        if not manifest:
            continue
        previous = store.get("episode_manifests", {"series_id": series_id, "episode_id": episode_id, "version": version_dir.name})
        store.upsert("episode_manifests", {
            **({"id": previous["id"]} if previous else {}),
            "series_id": series_id, "episode_id": episode_id, "version": version_dir.name,
            "r2_key": manifest.get("r2_key", ""), "manifest": manifest, "created_at": _now(),
        })


def _ingest_ledger(series_id: str, episode_id: str, ep_dir: Path) -> None:
    """Carry the engine's knowledge/relationship end state into the store so
    the panel can show who knows which secret after this episode."""
    ledger = _read(ep_dir / "work" / "ledger.json")
    end_state = ledger.get("end_state") if isinstance(ledger, dict) else None
    if not isinstance(end_state, dict):
        return
    for secret_id, holders in (end_state.get("knowledge") or {}).items():
        store.upsert("knowledge_state", {
            "series_id": series_id, "episode_id": episode_id, "kind": "knowledge",
            "subject_id": secret_id, "value": holders, "recorded_at": _now(),
        })
    for rel_id, rel_state in (end_state.get("relationships") or {}).items():
        store.upsert("knowledge_state", {
            "series_id": series_id, "episode_id": episode_id, "kind": "relationship",
            "subject_id": rel_id, "value": rel_state, "recorded_at": _now(),
        })


def ingest_series_state(series_id: str, runs_root: Path) -> dict:
    """Project reference packs and series-level approvals into the store."""
    data = _read(runs_root / series_id / "series_state.json")
    if not data:
        return {"references": 0}
    bible_version = data.get("bible_version") or ""
    n = 0
    for kind, group in (data.get("references") or {}).items():
        singular = {"characters": "character", "locations": "location", "props": "prop"}.get(kind, kind)
        for owner_id, pack in (group or {}).items():
            # A pack is either {name: record} or a single record with "path".
            items = [(owner_id, pack)] if (isinstance(pack, dict) and "path" in pack) \
                else list(pack.items()) if isinstance(pack, dict) else []
            for name, rec in items:
                if not isinstance(rec, dict):
                    continue
                previous = store.get("reference_assets", {"series_id": series_id, "bible_version": rec.get("bible_version", bible_version),
                                                          "kind": singular, "owner_id": owner_id, "name": str(name)})
                store.upsert("reference_assets", {
                    **({"id": previous["id"]} if previous else {}),
                    "series_id": series_id, "bible_version": rec.get("bible_version", bible_version),
                    "kind": singular, "owner_id": owner_id, "name": str(name),
                    "r2_key": rec.get("r2_key", ""), "checksum": rec.get("checksum", ""),
                    "approval": rec.get("approval", "pending"),
                    "metadata": {"path": rec.get("path", "")},
                    "created_at": rec.get("created_at") or _now(),
                })
                n += 1
    store.update("series", {"id": series_id}, {"bible_version": bible_version, "updated_at": _now()})
    return {"references": n, "approvals": data.get("approvals", {}), "bible_version": bible_version}
