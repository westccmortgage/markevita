"""The release recorded in the panel must reach the worker.

This is the seam that unit tests on either side cannot cover: the job page
writes an approval row, and the next production run reads it back to decide
whether a saved request may be retired. A mismatch in the table, the filter
keys, or the decision value would fail silently — the operator records the
release, presses Resume, and sees the identical refusal with no explanation.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from app import auth, web  # noqa: E402
from app.config import settings  # noqa: E402
from app.store.local import LocalDriver  # noqa: E402

RID = "01a09d10-d4ec-7c22-879c-9feea760da68"
SERIES, EPISODE = "miami", "s01e01_v2"
ERROR = (f"fal.ai HTTP 403 · request {RID}: fal.ai denied access while reading a request "
         "it had already accepted.")


@pytest.fixture
def ui(monkeypatch, tmp_path):
    driver = LocalDriver(tmp_path / "store")
    from app import ingest, packaging, runner
    for module in (web, runner, ingest, packaging):
        monkeypatch.setattr(module, "store", driver, raising=False)
    # Package validation and the live digest need a materialised series on
    # disk; neither is what these tests are about.
    monkeypatch.setattr(runner, "validate_series",
                        lambda sid: {"package": "ok", "ok": True, "episodes": []})
    monkeypatch.setattr(web.live_jobs, "review", lambda sid, eid: "")
    monkeypatch.setattr(settings, "base_path", "/studio")
    monkeypatch.setitem(web.templates.env.globals, "base", "/studio")

    driver.upsert("series", {"id": SERIES, "title": "Miami"})
    job = driver.insert("production_jobs", {
        "series_id": SERIES, "episode_id": EPISODE, "state": "failed", "mode": "live",
        "stages": ["keyframes"], "error": ERROR, "idempotency_key": "live:miami:s01e01_v2:w",
        "progress": {"done": ["intake", "direction", "references"], "stage": "keyframes"}})

    app = FastAPI()
    app.include_router(web.router, prefix="/studio")
    client = TestClient(app, base_url="https://markevita.com", follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.serialize({"email": "admin@example.test", "role": "owner"}))
    yield SimpleNamespace(client=client, store=driver, job=job)
    client.close()


def _released(store, series_id=SERIES, episode_id=EPISODE):
    """Exactly the query live_jobs.run uses to build the released set."""
    return {a["subject_id"] for a in store.list("approvals", {
        "series_id": series_id, "episode_id": episode_id,
        "subject_type": "fal_request_unreachable"}) if a.get("decision") == "released"}


def test_the_job_page_offers_the_release_for_the_refused_request(ui):
    page = ui.client.get(f"/studio/jobs/{ui.job['id']}").text
    assert "refused a request it had already accepted" in page
    assert RID in page
    assert f'action="/studio/jobs/{ui.job["id"]}/release-request"' in page


def test_recording_a_release_reaches_the_worker_query(ui):
    """Record in the panel, then read it exactly as production does."""
    assert _released(ui.store) == set()
    response = ui.client.post(f"/studio/jobs/{ui.job['id']}/release-request",
                              data={"request_id": RID, "note": "orphaned by key rotation"})
    assert response.status_code == 303
    assert _released(ui.store) == {RID}

    recorded = ui.store.list("approvals", {"subject_type": "fal_request_unreachable"})[0]
    assert recorded["actor"] == "admin@example.test"
    assert recorded["note"] == "orphaned by key rotation"


def test_a_release_does_not_leak_to_another_episode(ui):
    """Scope matters: another episode's stuck request must stay protected."""
    ui.client.post(f"/studio/jobs/{ui.job['id']}/release-request", data={"request_id": RID})
    assert _released(ui.store, episode_id="s01e02") == set()
    assert _released(ui.store, series_id="other") == set()


def test_a_request_this_job_did_not_report_is_refused(ui):
    other = "99999999-1111-4222-8333-444444444444"
    response = ui.client.post(f"/studio/jobs/{ui.job['id']}/release-request",
                              data={"request_id": other})
    assert "did+not+report" in response.headers["location"].replace("%20", "+")
    assert _released(ui.store) == set()


def test_recording_twice_does_not_duplicate_the_row(ui):
    for _ in range(2):
        ui.client.post(f"/studio/jobs/{ui.job['id']}/release-request", data={"request_id": RID})
    assert len(ui.store.list("approvals", {"subject_type": "fal_request_unreachable"})) == 1


def test_the_page_then_shows_it_is_already_recorded(ui):
    ui.client.post(f"/studio/jobs/{ui.job['id']}/release-request", data={"request_id": RID})
    page = ui.client.get(f"/studio/jobs/{ui.job['id']}").text
    assert "Already recorded as unreachable" in page
    assert "Record as unreachable" not in page


def test_a_succeeding_job_offers_nothing(ui):
    done = ui.store.insert("production_jobs", {
        "series_id": SERIES, "episode_id": EPISODE, "state": "done", "mode": "live",
        "stages": ["keyframes"], "error": None, "idempotency_key": "live:miami:s01e01_v2:x",
        "progress": {"done": ["keyframes"]}})
    page = ui.client.get(f"/studio/jobs/{done['id']}").text
    assert "refused a request it had already accepted" not in page


def test_the_episode_page_warns_before_resume(ui):
    """Resume lives on the episode page, so the blocker must be visible there.
    Without this the operator presses Resume and gets the same refusal."""
    ui.store.upsert("episodes", {"series_id": SERIES, "episode_id": EPISODE,
                                 "number": 1, "title": "V2"})
    page = ui.client.get(f"/studio/series/{SERIES}/episodes/{EPISODE}").text
    assert "Resume will stop on the same refusal" in page
    assert RID in page
    assert f'/studio/jobs/{ui.job["id"]}' in page


def test_the_episode_warning_clears_once_recorded(ui):
    ui.store.upsert("episodes", {"series_id": SERIES, "episode_id": EPISODE,
                                 "number": 1, "title": "V2"})
    ui.client.post(f"/studio/jobs/{ui.job['id']}/release-request", data={"request_id": RID})
    page = ui.client.get(f"/studio/series/{SERIES}/episodes/{EPISODE}").text
    assert "Resume will stop on the same refusal" not in page


def test_the_job_page_shows_the_scene_ids_in_a_voice_overrun(ui):
    """End to end: the engine names the scenes, guidance keeps them, and the
    page prints them. The operator was editing eight scenes blind."""
    job = ui.store.insert("production_jobs", {
        "series_id": SERIES, "episode_id": EPISODE, "state": "failed", "mode": "live",
        "stages": ["voice"], "idempotency_key": "live:miami:s01e01_v2:voice",
        "progress": {"done": ["video"], "stage": "voice"},
        "error": "RuntimeError: A spoken line is too long for its clip. "
                 "Shorten it in the script and save again. (sc05 (4.8s > 3.6s))"})
    page = ui.client.get(f"/studio/jobs/{job['id']}").text
    assert "too long for its clip" in page
    assert "sc05 (4.8s &gt; 3.6s)" in page or "sc05 (4.8s > 3.6s)" in page


def test_long_error_text_is_set_to_wrap(ui):
    """A non-wrapping error scrolls sideways on a phone and hides its own
    detail; that is how the scene ids stayed invisible after being added."""
    css = (STUDIO / "app" / "static" / "studio.css").read_text()
    rule = css[css.index("\npre {"):]
    rule = rule[:rule.index("}")]
    assert "white-space: pre-wrap" in rule
    assert "overflow-wrap: anywhere" in rule


def test_signing_out_blocks_the_release(ui):
    ui.client.cookies.clear()
    response = ui.client.post(f"/studio/jobs/{ui.job['id']}/release-request",
                              data={"request_id": RID})
    assert response.status_code in (303, 401)
    assert _released(ui.store) == set()
