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


def settings_in_force() -> list[dict]:
    """Numbers the operator sets on the server, read back from the server.

    Kept off /healthz on purpose — that endpoint is public and says nothing
    about configuration. Here it is behind the sign-in, where an administrator
    can confirm a change took effect instead of taking it on trust.
    """
    import os
    return [
        {"name": "MAX_EPISODE_BUDGET_USD",
         "value": os.getenv("MAX_EPISODE_BUDGET_USD") or "50 (default)",
         "means": "the hard ceiling on one episode; a series budget is clamped to it"},
        {"name": "ANTHROPIC_MODEL",
         "value": os.getenv("ANTHROPIC_MODEL") or "claude-opus-5 (default)",
         "means": "writes the scripts and the cast descriptions"},
        {"name": "VIDEO_GENERATE_AUDIO / picture",
         "value": "chosen per series",
         "means": "resolution, reference sharpness and lip-sync follow the series' Picture setting"},
        {"name": "voice slots",
         "value": ", ".join(s["env"] for s in _slots()) or "none configured",
         "means": "a character can only be given a voice that exists here"},
    ]


def _slots():
    from .authoring import voice_slots
    return voice_slots()


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


# Writing the series must not change what its characters look like. Every
# defect that kept an episode from being produced this evening was the same
# shape: the studio chases a target its own actions move. Production stops to
# ask for the reference pack to be approved; approving it answers "generate
# the pack for the current settings first"; generating it arrives at a version
# that has moved again. None of the code read wrongly on its own — each
# function was correct and the loop was not. So the loop's question is asked
# here, on the real series, where an operator can see the answer.
CONVERGENCE = [
    ("opening another episode", "episodes"),
    ("telling it in another language", "language"),
    ("renaming the series", "title"),
    ("casting a voice", "voice"),
]


def _bend(root, change):
    """Make the one edit, on a copy, without touching the real package."""
    import json
    if change in ("episodes", "language", "title"):
        path = root / "series.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if change == "episodes" and data.get("seasons"):
            data["seasons"][0].setdefault("episodes", []).append("selfcheck_probe")
        elif change == "language":
            data["language"] = "xx-XX" if data.get("language") != "xx-XX" else "yy-YY"
        else:
            data["title"] = (data.get("title") or "") + " (probe)"
    else:
        path = root / "bible" / "characters.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data:
            return False
        data[0].setdefault("voice", {})["language"] = "xx-XX"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return True


def convergence(series_id: str) -> list[dict]:
    """Confirm the reference pack's version stands still while the story moves."""
    import shutil
    import tempfile
    from pathlib import Path as _Path
    from serial.package import SeriesPackage
    from .packaging import materialize

    results = []
    try:
        source = materialize(series_id)
        stable = SeriesPackage(source).reference_version
    except Exception as exc:                                       # noqa: BLE001
        return [{"change": "reading the series", "ok": False,
                 "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}]
    for label, change in CONVERGENCE:
        work = _Path(tempfile.mkdtemp(prefix="selfcheck-converge-"))
        try:
            root = work / "package"
            shutil.copytree(source, root)
            if not _bend(root, change):
                continue
            moved = SeriesPackage(root).reference_version
            results.append({"change": label, "ok": moved == stable,
                            "detail": "" if moved == stable else
                                      f"redrew everybody: {stable} -> {moved}"})
        except Exception as exc:                                   # noqa: BLE001
            results.append({"change": label, "ok": False,
                            "detail": f"{type(exc).__name__}: {str(exc)[:160]}"})
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return results
