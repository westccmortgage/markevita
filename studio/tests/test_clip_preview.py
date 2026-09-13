"""Offline proofs for the single paid preview, including restart boundaries.

The fake store retains independent serialized records and enforces PostgreSQL
unique keys. Provider and storage calls are intercepted; no real credentials,
provider requests, database connection, or video generation are used here.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
os.environ.setdefault("STUDIO_STORE", "local")
os.environ.setdefault("STUDIO_ALLOW_PAID", "false")
os.environ.setdefault("PIPELINE_ALLOW_PAID", "false")
os.environ.setdefault("STUDIO_SESSION_SECRET", "offline-preview-session-secret")
# Match the established studio suite before Settings is first imported. Test
# collection can import this file first; later environment changes alone do
# not refresh the settings singleton used by local-auth regression tests.
os.environ["STUDIO_ADMIN_EMAIL"] = "admin@example.test"
os.environ["STUDIO_ADMIN_PASSWORD"] = "test-password"

from app import auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.store.base import NATURAL_KEYS  # noqa: E402
from app.store.supa import SupabaseDriver  # noqa: E402


class StrictStore(SupabaseDriver):
    """Model durable unique inserts, including jobs shared by new processes."""

    def __init__(self):
        self.rows = {}
        self.lock = threading.RLock()

    def list(self, table, where=None, order=None, desc=False, limit=None):
        with self.lock:
            rows = [copy.deepcopy(r) for r in self.rows.get(table, [])
                    if all(r.get(k) == v for k, v in (where or {}).items())]
        if order:
            rows.sort(key=lambda r: str(r.get(order) or ""), reverse=desc)
        return rows[:limit] if limit else rows

    def get(self, table, where):
        rows = self.list(table, where)
        return rows[0] if rows else None

    def insert(self, table, row):
        with self.lock:
            row = copy.deepcopy(row)
            row.setdefault("id", str(uuid.uuid4()))
            constraints = [["id"]]
            if NATURAL_KEYS.get(table):
                constraints.append(NATURAL_KEYS[table])
            for keys in constraints:
                if all(row.get(k) is not None for k in keys):
                    if self.get(table, {k: row[k] for k in keys}):
                        raise RuntimeError("duplicate key violates unique constraint")
            self.rows.setdefault(table, []).append(row)
            return copy.deepcopy(row)

    def upsert(self, table, row):
        with self.lock:
            keys = NATURAL_KEYS.get(table) or ["id"]
            old = (self.get(table, {k: row[k] for k in keys})
                   if all(k in row for k in keys) else None)
            if old:
                self.update(table, {"id": old["id"]}, row)
                return self.get(table, {"id": old["id"]})
            return self.insert(table, row)

    def update(self, table, where, patch):
        with self.lock:
            count = 0
            for row in self.rows.get(table, []):
                if all(row.get(k) == v for k, v in where.items()):
                    row.update(copy.deepcopy(patch))
                    count += 1
            return count

    def delete(self, table, where):
        with self.lock:
            old = self.rows.get(table, [])
            keep = [r for r in old if not all(r.get(k) == v for k, v in where.items())]
            self.rows[table] = keep
            return len(old) - len(keep)


def _form(html):
    return dict(re.findall(r'name="([^"]+)" value="([^"]*)"', html))


@pytest.fixture
def ui(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app import preview, preview_web

    spec = json.loads((STUDIO / "previews/first_clip.json").read_text())
    spec["digest"] = "offline-approved-digest"
    state = SimpleNamespace(job=None, starts=[], polls=[], media=[])
    monkeypatch.setattr(settings, "base_path", "/studio")
    monkeypatch.setattr(settings, "public_url", "https://markevita.com")
    monkeypatch.setitem(preview_web.templates.env.globals, "base", "/studio")
    monkeypatch.setattr(preview, "definition", lambda: copy.deepcopy(spec))
    monkeypatch.setattr(preview, "problems", lambda: [])
    monkeypatch.setattr(preview, "current_job", lambda: copy.deepcopy(state.job))
    monkeypatch.setattr(preview, "start", lambda **kw: state.starts.append(kw))
    monkeypatch.setattr(preview, "refresh", lambda job_id: state.polls.append(job_id))
    monkeypatch.setattr(preview, "media_url", lambda job_id: (
        state.media.append(job_id) or "https://private-media.example.test/clip?signature=offline"))
    app = FastAPI()
    # Netlify strips this public prefix on the real backend. Mounting it here
    # exercises browser-facing form actions and redirect targets together.
    app.include_router(preview_web.router, prefix="/studio")
    client = TestClient(app, base_url="https://markevita.com", follow_redirects=False)
    client.cookies.set(auth.COOKIE, auth.serialize({"email": "admin@example.test", "role": "owner"}))
    state.client = client
    state.spec = spec
    yield state
    client.close()


def test_preview_ui_requires_signed_in_admin(ui):
    ui.client.cookies.clear()
    assert ui.client.get("/studio/clip-preview").status_code == 401
    assert ui.client.post("/studio/clip-preview/start").status_code == 401
    assert ui.client.get("/studio/clip-preview/media?job_id=anything").status_code == 401
    assert ui.starts == ui.polls == ui.media == []


def test_preview_get_has_prefixed_approval_form_and_never_calls_provider(ui):
    page = ui.client.get("/studio/clip-preview")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    # A normal HTML POST under no-referrer sends Origin:null in browsers,
    # so the form must retain same-origin provenance for the CSRF check.
    assert page.headers["referrer-policy"] == "same-origin"
    assert 'action="/studio/clip-preview/start"' in page.text
    assert 'href="/studio/' in page.text
    assert _form(page.text)["approved_digest"] == ui.spec["digest"]
    assert ui.spec["dialogue"] in page.text
    assert ui.starts == ui.polls == ui.media == []


@pytest.mark.parametrize("change,expected", [
    ("unchecked", 400), ("wrong_checkbox", 400),
    ("bad_csrf", 403), ("missing_origin", 403), ("null_origin", 403),
    ("foreign_origin", 403), ("different_session", 403),
])
def test_preview_submit_requires_exact_approval_and_same_session_origin(ui, change, expected):
    form = _form(ui.client.get("/studio/clip-preview").text)
    form["approve"] = "yes"
    headers = {"Origin": "https://markevita.com"}
    if change == "unchecked":
        form.pop("approve")
    elif change == "wrong_checkbox":
        form["approve"] = "true"
    elif change == "bad_csrf":
        form["csrf_token"] = "forged"
    elif change == "missing_origin":
        headers = {}
    elif change == "null_origin":
        headers["Origin"] = "null"
    elif change == "foreign_origin":
        headers["Origin"] = "https://markevita.com.evil.example"
    elif change == "different_session":
        ui.client.cookies.set(auth.COOKIE, auth.serialize({"email": "other@example.test", "role": "owner"}))
    response = ui.client.post("/studio/clip-preview/start", data=form, headers=headers)
    assert response.status_code == expected
    assert not ui.starts


def test_preview_valid_ui_approval_passes_exact_spec_budget_and_actor(ui):
    form = _form(ui.client.get("/studio/clip-preview").text)
    form["approve"] = "yes"
    response = ui.client.post("/studio/clip-preview/start", data=form,
                              headers={"Origin": "https://markevita.com"})
    assert response.status_code == 303
    assert response.headers["location"] == "/studio/clip-preview"
    assert ui.starts == [{"actor": "admin@example.test",
                          "approved_digest": ui.spec["digest"],
                          "approved_max_usd": str(ui.spec["max_usd"])}]


def test_preview_existing_job_get_never_polls_or_exposes_provider_url(ui):
    ui.job = {"id": "allowed-job", "state": "submitted", "progress": {
        "provider_url": "https://provider.example/private-secret-url"}}
    page = ui.client.get("/studio/clip-preview")
    assert page.headers["referrer-policy"] == "same-origin"
    assert 'action="/studio/clip-preview/poll"' in page.text
    assert "private-secret-url" not in page.text
    assert ui.starts == ui.polls == ui.media == []


def test_preview_arbitrary_job_ids_cannot_poll_or_get_media(ui):
    ui.job = {"id": "allowed-job", "state": "done"}
    form = _form(ui.client.get("/studio/clip-preview").text)
    # A done page has no form; get a session-bound token from an earlier state.
    ui.job["state"] = "submitted"
    form = _form(ui.client.get("/studio/clip-preview").text)
    form["job_id"] = "other-job"
    response = ui.client.post("/studio/clip-preview/poll", data=form,
                              headers={"Origin": "https://markevita.com"})
    assert response.status_code == 404
    ui.job["state"] = "done"
    assert ui.client.get("/studio/clip-preview/media?job_id=other-job").status_code == 404
    assert ui.starts == ui.polls == ui.media == []


def test_preview_only_ready_job_has_authenticated_private_playback(ui):
    ui.job = {"id": "allowed-job", "state": "submitted"}
    assert ui.client.get("/studio/clip-preview/media?job_id=allowed-job").status_code == 409
    ui.job["state"] = "done"
    response = ui.client.get("/studio/clip-preview/media?job_id=allowed-job")
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["location"].startswith("https://private-media.example.test/")
    assert ui.media == ["allowed-job"]


@pytest.fixture
def dashboard_ui(ui, monkeypatch):
    from app import web

    db = StrictStore()
    monkeypatch.setattr(web, "store", db)
    monkeypatch.setattr(web.integrations, "status_all", lambda: [])
    ui.client.app.include_router(web.router, prefix="/studio")
    db.insert("costs", {"series_id": "preview-series", "episode_id": "preview01",
                        "stage": "clip_preview", "provider": "fal.ai",
                        "estimated_usd": 1.2, "actual_usd": 0})
    db.insert("costs", {"series_id": "preview-series", "episode_id": "preview01",
                        "stage": "video", "provider": "mock",
                        "estimated_usd": 0.35, "actual_usd": 0.35})
    ui.db = db
    return ui


def test_preview_estimate_is_separate_from_simulated_spend_and_unverified_on_cost_page(dashboard_ui):
    home = dashboard_ui.client.get("/studio/")
    assert home.status_code == 200
    assert home.context["total_cost"] == 0.35
    assert home.context["preview_estimate"] == 1.2
    assert home.context["has_preview_cost"] is True
    costs = dashboard_ui.client.get("/studio/costs")
    assert costs.status_code == 200
    assert costs.context["total"] == 0.35
    assert costs.context["preview_estimate"] == 1.2
    assert len(costs.context["by_episode"]) == 2
    preview_cost = next(r for r in costs.context["by_episode"] if r["is_preview"])
    assert preview_cost["actual_usd"] is None
    assert "Unverified" in costs.text
    assert "Real preview estimate reserved" in costs.text


def test_preview_job_and_episode_links_open_preview_without_generic_runner_controls(dashboard_ui, monkeypatch):
    from app import web

    monkeypatch.setattr(web.runner, "episode_runtime", lambda *a: pytest.fail("Preview must not read mock engine state"))
    dashboard_ui.db.insert("production_jobs", {
        "id": "preview-job", "idempotency_key": "preview-job-key",
        "series_id": "preview-series", "episode_id": "preview01", "stages": ["clip_preview"],
        "state": "submitted", "mode": "live",
    })
    for path in ("/studio/jobs/preview-job", "/studio/series/preview-series/episodes/preview01"):
        response = dashboard_ui.client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/studio/clip-preview"


def test_preview_records_cannot_enter_episode_package_or_restart_through_mock_runner(monkeypatch):
    from app import packaging, runner

    db = StrictStore()
    monkeypatch.setattr(packaging, "store", db)
    monkeypatch.setattr(runner, "store", db)
    monkeypatch.setattr(settings, "allow_paid", False)
    monkeypatch.setattr(runner.threading, "Thread", lambda *a, **kw: pytest.fail("Preview must not start an episode worker"))
    series = {"id": "mixed-series", "title": "Mixed production"}
    db.insert("series", series)
    db.insert("seasons", {"series_id": series["id"], "season_id": "previews", "number": 0,
                          "episode_order": ["preview_kind", "preview_status"]})
    db.insert("seasons", {"series_id": series["id"], "season_id": "s01", "number": 1,
                          "episode_order": ["ep02", "preview_mixed", "ep01"]})
    for episode_id, status, brief in (
        ("preview_kind", "done", {"kind": "clip_preview"}),
        ("preview_status", "preview", {}),
        ("preview_mixed", "done", {"kind": "clip_preview"}),
        ("ep01", "draft", {}), ("ep02", "draft", {}),
    ):
        db.insert("episodes", {"series_id": series["id"], "episode_id": episode_id,
                               "status": status, "brief": brief})
    seasons = packaging.build_series_json(series)["seasons"]
    assert len(seasons) == 1
    assert seasons[0]["season_id"] == "s01"
    assert seasons[0]["episodes"] == ["ep02", "ep01"]
    # The stable kind marker must protect the clip even after status changes.
    with pytest.raises(ValueError, match="preview"):
        runner.jobs.start(series["id"], "preview_kind", ["intake"], "admin@example.test")
    assert not db.list("production_jobs")


class OfflineR2:
    def __init__(self):
        self.client = self
        self.cfg = SimpleNamespace(r2_bucket="offline-preview-bucket")
        self.objects = {}
        self.uploads = []
        self.fail_uploads = 0
        self.fail_preflight = False

    def head_bucket(self, **kwargs):
        if self.fail_preflight:
            raise RuntimeError("offline simulated bucket access failure")
        return {}

    def put_object(self, *, Key, Body, **kwargs):
        self.objects[Key] = Body
        return {}

    def head_object(self, *, Key, **kwargs):
        return {"ContentLength": len(self.objects[Key])}

    def put(self, path, key):
        self.uploads.append(key)
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise RuntimeError("offline simulated upload interruption")
        self.objects[key] = path.read_bytes()
        return key

    def presign(self, key, expires):
        assert key in self.objects
        assert expires == 900
        return "https://offline-account.r2.cloudflarestorage.com/" + key + "?signature=offline"


@pytest.fixture
def backend(monkeypatch):
    from app import preview

    db = StrictStore()
    r2 = OfflineR2()
    state = SimpleNamespace(
        db=db, r2=r2, preview=preview, requests=[], status="IN_PROGRESS", timeout=False,
        video_url="https://v3.fal.media/files/offline-preview.mp4",
        probe={"streams": [{"codec_type": "video", "width": 1080, "height": 1920},
                            {"codec_type": "audio"}],
               "format": {"duration": "8.0", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}},
    )
    for name in ("STUDIO_ALLOW_PAID", "PIPELINE_ALLOW_PAID"):
        monkeypatch.setenv(name, "true")
    for name in ("FAL_KEY", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.setenv(name, "offline-test-" + name.lower())
    monkeypatch.setattr(settings, "allow_paid", True)
    monkeypatch.setattr(settings, "store_driver", "supabase")
    monkeypatch.setattr(preview, "store", db)
    monkeypatch.setattr(preview, "_r2", lambda: r2)
    monkeypatch.setattr(preview.shutil, "which", lambda _name: "/offline/ffprobe")

    def probe(*args, **kwargs):
        assert args[0][0] == "ffprobe"
        assert Path(args[0][-1]).read_bytes() == b"offline-mp4-content"
        return SimpleNamespace(stdout=json.dumps(state.probe).encode(), returncode=0)

    monkeypatch.setattr(preview.subprocess, "run", probe)

    def transport(request):
        state.requests.append(request)
        if request.method == "POST" and str(request.url) == "https://queue.fal.run/fal-ai/veo3.1/fast":
            if state.timeout:
                raise httpx.ReadTimeout("offline simulated ambiguous submit", request=request)
            return httpx.Response(202, json={
                "request_id": "offline-request-1",
                "status_url": "https://queue.fal.run/fal-ai/veo3.1/requests/offline-request-1/status",
                "response_url": "https://queue.fal.run/fal-ai/veo3.1/requests/offline-request-1",
            })
        if request.method == "GET" and request.url.host == "queue.fal.run":
            assert "/requests/offline-request-1" in request.url.path
            if request.url.path.endswith("/status"):
                return httpx.Response(200, json={"status": state.status})
            return httpx.Response(200, json={"video": {"url": state.video_url}})
        if request.method == "GET" and str(request.url) == state.video_url:
            assert "authorization" not in request.headers, "Provider key must not be sent to CDN"
            return httpx.Response(200, content=b"offline-mp4-content")
        pytest.fail(f"Unexpected offline request: {request.method} {request.url.host}{request.url.path}")

    client = httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(preview.httpx, "post", lambda url, **kwargs: client.post(url, **kwargs))
    monkeypatch.setattr(preview.httpx, "get", lambda url, **kwargs: client.get(url, **kwargs))
    monkeypatch.setattr(preview.httpx, "stream", lambda method, url, **kwargs: client.stream(method, url, **kwargs))

    def start(module=None, **overrides):
        current = module or preview
        spec = current.definition()
        args = {"actor": "admin@example.test", "approved_digest": spec["digest"],
                "approved_max_usd": "1.20"}
        args.update(overrides)
        return current.start(**args)

    def fresh_worker():
        # New module globals, same durable database and provider transports;
        # no state or in-process lock is carried over from the first worker.
        name = "app._offline_preview_worker_" + uuid.uuid4().hex
        definition = importlib.util.spec_from_file_location(name, preview.__file__)
        worker = importlib.util.module_from_spec(definition)
        definition.loader.exec_module(worker)
        worker.store = db
        worker._r2 = lambda: r2
        return worker

    state.start = start
    state.fresh_worker = fresh_worker
    state.submissions = lambda: [r for r in state.requests if r.method == "POST"]
    yield state
    client.close()


@pytest.mark.parametrize("blocker", [
    "studio_flag", "pipeline_flag", "settings_flag", "fal_key", "r2_key", "local_backend",
])
def test_preview_no_submission_unless_all_paid_and_durable_storage_gates_pass(backend, monkeypatch, blocker):
    if blocker == "studio_flag":
        monkeypatch.setenv("STUDIO_ALLOW_PAID", "false")
    elif blocker == "pipeline_flag":
        monkeypatch.setenv("PIPELINE_ALLOW_PAID", "false")
    elif blocker == "settings_flag":
        monkeypatch.setattr(settings, "allow_paid", False)
    elif blocker == "fal_key":
        monkeypatch.delenv("FAL_KEY")
    elif blocker == "r2_key":
        monkeypatch.delenv("R2_SECRET_ACCESS_KEY")
    else:
        # Configuration claims Supabase, but the effective driver is local.
        monkeypatch.setattr(backend.db, "name", "local")
    with pytest.raises(RuntimeError):
        backend.start()
    assert not backend.requests
    assert not backend.db.list("production_jobs")
    assert not backend.r2.objects


@pytest.mark.parametrize("overrides", [
    {"approved_digest": "stale-spec-hash"}, {"approved_max_usd": "1.21"},
    {"approved_max_usd": "NaN"}, {"approved_max_usd": "Infinity"},
    {"approved_max_usd": "-Infinity"}, {"approved_max_usd": "-1"},
])
def test_preview_approval_hash_and_finite_exact_budget_are_required(backend, overrides):
    with pytest.raises(RuntimeError):
        backend.start(**overrides)
    assert not backend.requests
    assert not backend.db.list("production_jobs")


def test_preview_storage_write_preflight_fails_before_billable_request(backend):
    backend.r2.fail_preflight = True
    with pytest.raises(RuntimeError, match="preflight"):
        backend.start()
    assert not backend.requests
    assert not backend.db.list("production_jobs")
    backend.r2.fail_preflight = False
    assert backend.start()["state"] == "submitted"
    assert len(backend.submissions()) == 1


def test_preview_exact_payload_one_cost_reservation_and_no_duplicate_after_restart(backend):
    first = backend.start()
    again = backend.start()
    worker = backend.fresh_worker()
    restarted = backend.start(worker)
    assert first["state"] == "submitted"
    assert first["id"] == again["id"] == restarted["id"]
    assert len(backend.submissions()) == 1
    payload = json.loads(backend.submissions()[0].content)
    assert payload == {"prompt": backend.preview.definition()["prompt"], "duration": "8s",
                       "aspect_ratio": "9:16", "resolution": "1080p",
                       "generate_audio": True, "auto_fix": False}
    assert len(backend.db.list("costs")) == 1
    assert len(backend.db.list("production_jobs")) == 1
    assert first["progress"]["approval"]["actor"] == "admin@example.test"
    assert first["progress"]["cost"]["reserved_usd"] == 1.2
    assert first["progress"]["cost"]["actual_usd"] is None
    assert "offline-test-fal_key" not in json.dumps(backend.db.rows)


def test_preview_concurrent_start_has_one_durable_submission_winner(backend, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)
    preflight = backend.preview._preflight

    def simultaneous_preflight():
        preflight()
        barrier.wait(timeout=5)

    monkeypatch.setattr(backend.preview, "_preflight", simultaneous_preflight)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(lambda _: backend.start(), range(2)))
    assert jobs[0]["id"] == jobs[1]["id"]
    assert len(backend.submissions()) == 1
    assert len(backend.db.list("production_jobs")) == len(backend.db.list("costs")) == 1


def test_preview_ambiguous_timeout_never_submits_again_even_after_restart(backend):
    backend.timeout = True
    first = backend.start()
    assert first["state"] == "submission_unknown"
    backend.timeout = False
    worker = backend.fresh_worker()
    assert backend.start(worker)["id"] == first["id"]
    assert worker.refresh(first["id"])["state"] == "submission_unknown"
    assert len(backend.requests) == len(backend.submissions()) == 1
    assert first["progress"]["cost"]["reserved_usd"] == 1.2


def test_preview_saved_request_polling_resumes_and_archives_without_regeneration(backend):
    job = backend.start()
    worker = backend.fresh_worker()
    pending = worker.refresh(job["id"])
    assert pending["state"] == "submitted"
    assert pending["progress"]["request"]["request_id"] == "offline-request-1"
    backend.status = "COMPLETED"
    done = worker.refresh(job["id"])
    assert done["state"] == "done"
    assert done["progress"]["result"]["has_audio"] is True
    assert len(backend.submissions()) == 1
    assert len(backend.db.list("costs")) == len(backend.db.list("takes")) == 1
    assert backend.db.list("takes")[0]["selected"] is False
    assert "invoice" in backend.db.list("takes")[0]["params"]["cost_basis"]
    count = len(backend.requests)
    assert worker.refresh(job["id"])["state"] == "done"
    assert len(backend.requests) == count
    assert worker.media_url(job["id"]).startswith("https://offline-account.r2.cloudflarestorage.com/")


def test_preview_r2_failure_retries_same_completed_asset_and_does_not_duplicate_cost_or_take(backend):
    job = backend.start()
    backend.status = "COMPLETED"
    backend.r2.fail_uploads = 1
    first = backend.preview.refresh(job["id"])
    assert first["state"] == "storage_pending"
    assert first["progress"]["request"]["request_id"] == "offline-request-1"
    provider_gets = len([r for r in backend.requests if r.url.host == "queue.fal.run"])
    worker = backend.fresh_worker()
    done = worker.refresh(job["id"])
    assert done["state"] == "done"
    assert len([r for r in backend.requests if r.url.host == "queue.fal.run"]) == provider_gets
    assert len(backend.submissions()) == 1
    assert len(backend.db.list("costs")) == len(backend.db.list("takes")) == 1
    assert len(backend.r2.uploads) == 2
    assert backend.r2.uploads[0] == backend.r2.uploads[1]


def test_preview_retry_after_take_record_survives_failed_completion_write(backend, monkeypatch):
    job = backend.start()
    backend.status = "COMPLETED"
    update = backend.db.update
    failures = [True]

    def interrupted_update(table, where, patch):
        if table == "production_jobs" and patch.get("state") == "done" and failures:
            failures.pop()
            raise RuntimeError("offline simulated database interruption")
        return update(table, where, patch)

    monkeypatch.setattr(backend.db, "update", interrupted_update)
    assert backend.preview.refresh(job["id"])["state"] == "storage_pending"
    assert len(backend.db.list("takes")) == 1
    assert backend.fresh_worker().refresh(job["id"])["state"] == "done"
    assert len(backend.submissions()) == 1
    assert len(backend.db.list("costs")) == len(backend.db.list("takes")) == 1


@pytest.mark.parametrize("invalid", ["foreign_cdn", "missing_audio", "landscape"])
def test_preview_invalid_media_is_never_archived_or_regenerated(backend, invalid):
    job = backend.start()
    backend.status = "COMPLETED"
    if invalid == "foreign_cdn":
        backend.video_url = "https://fal.media.evil.example/clip.mp4"
    elif invalid == "missing_audio":
        backend.probe["streams"] = backend.probe["streams"][:1]
    else:
        backend.probe["streams"][0].update(width=1920, height=1080)
    rejected = backend.preview.refresh(job["id"])
    assert rejected["state"] != "done"
    assert rejected["error"]
    assert not backend.db.list("takes")
    assert not backend.r2.uploads
    assert len(backend.submissions()) == 1
    if invalid == "foreign_cdn":
        assert all(r.url.host == "queue.fal.run" for r in backend.requests)
