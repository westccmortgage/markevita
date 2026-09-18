"""What a running episode has actually done, in numbers a producer can read.

A production run was visible only as a state word and a log that had to be
reloaded by hand. How many pictures were made, how many were still owed, what
it had cost against what was approved, how much of it was the quality check
asking for a frame again — none of that was on any screen, and the honest
answer to "how long will this take" was a guess. All of it is already
recorded; this reads it back in one place so every screen says the same thing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .store import store


def _moment(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _pack(series_id: str) -> tuple[int, int]:
    """Reference images made for the current bible, and how many it wants."""
    from serial import reference_reuse
    from serial.package import SeriesPackage
    from .packaging import materialize
    pkg = SeriesPackage(materialize(series_id))
    version = pkg.reference_version
    made = {(r.get("kind"), r.get("owner_id"), r.get("name"))
            for r in store.list("reference_assets", {"series_id": series_id})
            if (r.get("bible_version") or "") == version}
    needed = [(kind, owner, name)
              for kind, owners in reference_reuse.expected(pkg).items()
              for owner, names in owners.items() for name in names]
    return len([k for k in needed if k in made]), len(needed)


def spend(series_id: str, episode_id: str) -> float:
    """What this episode has actually been charged for, live only."""
    total = 0.0
    for row in store.list("costs", {"series_id": series_id, "episode_id": episode_id}):
        if str(row.get("stage") or "").startswith("live/"):
            total += float(row.get("actual_usd") or row.get("estimated_usd") or 0.0)
    return round(total, 2)


def budget(series_id: str) -> float:
    from serial.config import Config
    from serial.package import SeriesPackage
    from .config import PIPELINE_DIR
    from .packaging import materialize
    pkg = SeriesPackage(materialize(series_id))
    return float(pkg.limits(Config.load(PIPELINE_DIR, live=True))["budget"])


def redone(series_id: str, episode_id: str) -> int:
    """Frames the quality check sent back. Each one is a picture paid for twice."""
    return len([t for t in store.list("takes", {"series_id": series_id, "episode_id": episode_id})
                if int(t.get("attempt") or 0) > 0])


def report(series_id: str, episode_id: str, job: dict | None = None) -> dict:
    """Everything a screen needs about a run in flight. Never raises."""
    out: dict = {"made": 0, "needed": 0, "left": 0, "spent": 0.0, "budget": 0.0,
                 "redone": 0, "minutes_left": None, "cost_left": None}
    try:
        out["spent"] = spend(series_id, episode_id)
    except Exception:                                              # noqa: BLE001
        pass
    try:
        out["budget"] = budget(series_id)
    except Exception:                                              # noqa: BLE001
        pass
    try:
        out["redone"] = redone(series_id, episode_id)
    except Exception:                                              # noqa: BLE001
        pass
    try:
        out["made"], out["needed"] = _pack(series_id)
        out["left"] = max(0, out["needed"] - out["made"])
    except Exception:                                              # noqa: BLE001
        return out
    # An estimate from this run's own pace, not from a number someone guessed.
    # It is offered only once the run has made enough pictures for the pace to
    # mean anything, and only while it is running.
    started = _moment((job or {}).get("started_at"))
    if started and out["left"] and out["made"] >= 3 and (job or {}).get("state") == "running":
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        done_here = max(1, len([t for t in store.list(
            "takes", {"series_id": series_id, "episode_id": episode_id})
            if _moment(t.get("created_at")) and _moment(t["created_at"]) >= started]))
        each = elapsed / done_here
        if 1 <= each <= 900:
            out["minutes_left"] = max(1, round(out["left"] * each / 60))
    if out["left"] and out["made"]:
        try:
            from serial.costs import PRICE
            out["cost_left"] = round(out["left"] * float(PRICE["nano_banana_2_image_1k"]), 2)
        except Exception:                                          # noqa: BLE001
            pass
    return out
