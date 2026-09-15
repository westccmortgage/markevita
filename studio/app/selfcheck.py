"""Walk every screen against the database this server actually uses.

Local tests run against a permissive local store, so a page can pass the whole
suite and still fail on Supabase — that happened three times in a row, and each
time the producer found it instead of the suite. This walks the real screens
with the real driver and reports what breaks, with the exception and the line,
so a deploy can be checked in one click instead of one screenshot at a time.

It performs no writes and starts no paid work: only pages are fetched.
"""
from __future__ import annotations

import traceback
from pathlib import Path

from .store import store


def _sample() -> dict:
    """One real series, season, episode and character to build paths from."""
    series = store.list("series", limit=1)
    sid = series[0]["id"] if series else ""
    episodes = store.list("episodes", {"series_id": sid}) if sid else []
    jobs = store.list("production_jobs", {"series_id": sid}, order="created_at",
                      desc=True, limit=1) if sid else []
    return {"series_id": sid,
            "episode_id": episodes[-1]["episode_id"] if episodes else "",
            "job_id": jobs[0]["id"] if jobs else ""}


def paths() -> list[str]:
    s = _sample()
    out = ["/", "/jobs", "/costs", "/integrations", "/notifications", "/clip-preview"]
    if s["series_id"]:
        sid = s["series_id"]
        out += [f"/series/{sid}"] + [f"/series/{sid}/{p}" for p in
                                     ("characters", "locations", "world", "references")]
        if s["episode_id"]:
            out.append(f"/series/{sid}/episodes/{s['episode_id']}/studio")
    if s["job_id"]:
        out.append(f"/jobs/{s['job_id']}")
    return out


def run(client) -> list[dict]:
    """Fetch each screen in-process and report how it answered."""
    results = []
    for path in paths():
        try:
            response = client.get(path, follow_redirects=False)
        except Exception as exc:                                   # noqa: BLE001
            frames = [f for f in traceback.extract_tb(exc.__traceback__)
                      if "/app/" in f.filename or "/serial/" in f.filename]
            results.append({"path": path, "status": 0, "ok": False,
                            "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                            "where": " -> ".join(f"{Path(f.filename).name}:{f.lineno}"
                                                 for f in frames[-3:])})
            continue
        ok = response.status_code in (200, 303, 304)
        results.append({"path": path, "status": response.status_code, "ok": ok,
                        "detail": "" if ok else response.text[:200], "where": ""})
    return results
