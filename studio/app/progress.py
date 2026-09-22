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


PAGE = 1000

# The engine groups a pack by "characters"; a stored row calls the same thing a
# "character". Comparing the two words as if they were one made every image
# look missing: 0 of 66 with fifty-eight of them on the screen underneath.
SINGULAR = {"characters": "character", "locations": "location", "props": "prop"}


def every(table: str, where: dict, order: str = "created_at") -> list[dict]:
    """Every matching row, not the page the database felt like returning.

    PostgREST caps a reply, and without an order the rows in it are arbitrary,
    so a total summed from one call came out different on every refresh. A
    number nobody can trust is worse than no number: it gets believed.
    """
    rows: list[dict] = []
    while True:
        page = store.list(table, where, order=order, limit=PAGE, offset=len(rows))
        rows += page
        if len(page) < PAGE:
            return rows


def _moment(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# Rebuilding a series package means writing it out and parsing it again, and
# the status page asks every thirty seconds while the worker holds the
# database. Answering from a few seconds ago is indistinguishable on a screen
# that refreshes at that rate, and it is the difference between a status page
# and a gateway timeout.
_RECENT: dict[str, tuple[float, tuple[int, int]]] = {}
FRESH_SECONDS = 20


def _pack(series_id: str) -> tuple[int, int]:
    """Reference images made for the current bible, and how many it wants."""
    import time as _time
    at, answer = _RECENT.get(series_id, (0.0, None))
    if answer is not None and _time.time() - at < FRESH_SECONDS:
        return answer
    answer = _count_pack(series_id)
    _RECENT[series_id] = (_time.time(), answer)
    return answer


def _count_pack(series_id: str) -> tuple[int, int]:
    from serial import reference_reuse
    from serial.package import SeriesPackage
    from .packaging import materialize
    pkg = SeriesPackage(materialize(series_id))
    version = pkg.reference_version
    made = {(r.get("kind"), r.get("owner_id"), r.get("name"))
            for r in every("reference_assets", {"series_id": series_id})
            if (r.get("bible_version") or "") == version}
    needed = [(SINGULAR.get(kind, kind), owner, name)
              for kind, owners in reference_reuse.expected(pkg).items()
              for owner, names in owners.items() for name in names]
    return len([k for k in needed if k in made]), len(needed)


# fal returns no price in its response, so the engine records its own estimate
# at the provider's published rate and says so on every take. Nothing inside
# this studio is a provider-confirmed charge; only the provider's own billing
# is. Saying "actual" without saying that is how an estimate becomes a bill.
PROVIDER_PRICE_NOTE = ("fal does not return a price, so every amount here is this studio's own "
                       "estimate at the provider's published rate. The provider's billing page "
                       "is the only confirmed figure.")


def _live_rows(series_id: str, episode_id: str) -> list[dict]:
    return [r for r in every("costs", {"series_id": series_id, "episode_id": episode_id})
            if str(r.get("stage") or "").startswith("live/")]


def ledger(series_id: str, episode_id: str) -> dict:
    """One provable breakdown of what this episode has cost, by kind.

    A single number was shown as spend and it was not one number: it summed
    each row's actual OR, where there was no actual, its estimate — so an
    amount nobody had been charged was added to the amount they had. The two
    screens then disagreed with each other and with the engine's own ledger,
    and there was no way to tell which was the bill.

    A take settles once. A second row for the same take is a re-ingest of the
    same request, so the later row stands and the earlier one is counted as
    discarded rather than added.
    """
    by_take: dict[str, dict] = {}
    superseded, superseded_usd = 0, 0.0
    for row in _live_rows(series_id, episode_id):
        key = str(row.get("take_id") or f"row:{row.get('id')}")
        if key in by_take:
            superseded += 1
            superseded_usd += float(by_take[key].get("actual_usd") or 0.0)
        by_take[key] = row
    kept = list(by_take.values())
    internal_actual = round(sum(float(r.get("actual_usd") or 0.0) for r in kept), 2)
    # An estimate on a row that never settled to an amount is not spend. It is
    # what a call was expected to cost, or what an interrupted one may have
    # cost without our being told.
    estimates_only = round(sum(float(r.get("estimated_usd") or 0.0) for r in kept
                               if not float(r.get("actual_usd") or 0.0)), 2)
    episode = store.get("episodes", {"series_id": series_id, "episode_id": episode_id}) or {}
    recorded = round(float(episode.get("spent_usd") or 0.0), 2)
    return {
        # The provider confirms nothing back to us, so this stays empty rather
        # than being filled with our own arithmetic.
        "provider_confirmed": None,
        "internal_actual": internal_actual,
        "engine_recorded": recorded,
        "agrees": abs(internal_actual - recorded) < 0.01,
        "estimates_only": estimates_only,
        "unverified": internal_actual,
        "superseded_rows": superseded,
        "superseded_usd": round(superseded_usd, 2),
        "calls": len(kept),
        "note": PROVIDER_PRICE_NOTE,
    }


def spend(series_id: str, episode_id: str) -> float:
    """What this episode's settled calls add up to, live only.

    Only amounts that settled. An estimate on a call that never settled is
    reported by ledger() under its own name and is not added here.
    """
    return ledger(series_id, episode_id)["internal_actual"]


def budget(series_id: str, episode_id: str) -> float:
    from serial.config import Config
    from serial.package import SeriesPackage
    from .config import PIPELINE_DIR
    from .packaging import materialize
    pkg = SeriesPackage(materialize(series_id))
    return float(pkg.limits(Config.load(PIPELINE_DIR, live=True), episode_id)["budget"])


def redone(series_id: str, episode_id: str) -> int:
    """Frames the quality check sent back. Each one is a picture paid for twice."""
    return len([t for t in every("takes", {"series_id": series_id, "episode_id": episode_id})
                if int(t.get("attempt") or 0) > 0])


def retry_policy() -> dict:
    """What a miss costs, before it is spent rather than after.

    A scene may be generated more than once when the check marks it down, and
    nothing said so anywhere: the producer met the policy as a bill. These are
    the numbers that decide it.
    """
    from serial.config import Config
    from serial.costs import PRICE
    from .config import PIPELINE_DIR
    out = {"attempts": 1, "pass_score": 7.0, "close_enough": 1.0,
           "per_clip": 0.0, "per_frame": 0.0, "worst_case": None}
    try:
        cfg = Config.load(PIPELINE_DIR, live=True)
    except Exception:                                              # noqa: BLE001
        return out
    out["attempts"] = int(getattr(cfg, "max_scene_regenerations", 2)) + 1
    out["pass_score"] = float(getattr(cfg, "qc_pass_score", 7.0))
    out["close_enough"] = float(getattr(cfg, "qc_close_enough", 1.0))
    try:
        seconds = 7  # a scene is six to eight; this is what the arithmetic is for
        rate = PRICE["veo31_fast_per_sec_audio" if getattr(cfg, "video_generate_audio", True)
                     else "veo31_fast_per_sec_silent"]
        out["per_clip"] = round(float(rate) * seconds, 2)
    except Exception:                                              # noqa: BLE001
        pass
    try:
        out["per_frame"] = round(float(PRICE["nano_banana_2_image_1k"]), 2)
    except Exception:                                              # noqa: BLE001
        pass
    return out


def report(series_id: str, episode_id: str, job: dict | None = None) -> dict:
    """Everything a screen needs about a run in flight. Never raises."""
    out: dict = {"made": 0, "needed": 0, "left": 0, "spent": 0.0, "budget": 0.0,
                 "redone": 0, "minutes_left": None, "cost_left": None,
                 "ledger": None, "retries": retry_policy()}
    try:
        out["ledger"] = ledger(series_id, episode_id)
        out["spent"] = out["ledger"]["internal_actual"]
    except Exception:                                              # noqa: BLE001
        pass
    try:
        out["budget"] = budget(series_id, episode_id)
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
        done_here = max(1, len([t for t in every(
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
