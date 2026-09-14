"""Submission and polling share one credential and the queue's own URLs.

Background: a live keyframes job was accepted by the fal queue (request id
assigned) and then refused with HTTP 403 while collecting the result. The
Integrations check said the key authenticated. Polling went through the SDK,
which resolved its own key from the raw environment and rebuilt the request
URL; the submission used the normalised config key. These tests pin the
single-credential, queue-URL behaviour and the server-log-only explanation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from app.live_providers import DurableFal, _request_base  # noqa: E402
from app.provider_errors import ProviderFailure  # noqa: E402
from serial.costs import Budget  # noqa: E402
from serial.state import State  # noqa: E402

KEY = "11111111-2222-4333-8444-555555555555:PRIVATE_SECRET"
RID = "01a09d10-d4ec-7c22-879c-9feea760da68"
QUEUE = "https://queue.fal.run/"


def _resp(status, body, url=""):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.headers["content-type"] = "application/json"
    r.url = url
    return r


def _fal(tmp_path):
    state = State(tmp_path)
    return DurableFal(SimpleNamespace(fal_key=KEY), lambda m: None, state, Budget(10, state), None), state


def test_submission_stores_the_queue_request_urls(tmp_path, monkeypatch):
    fal, state = _fal(tmp_path)
    accepted = {"request_id": RID,
                "status_url": f"{QUEUE}fal-ai/nano-banana-2/requests/{RID}/status",
                "response_url": f"{QUEUE}fal-ai/nano-banana-2/requests/{RID}"}
    monkeypatch.setattr("app.live_providers.requests.post", lambda *a, **k: _resp(200, accepted))
    monkeypatch.setattr(fal, "_wait", lambda endpoint, rid: {"images": []})
    fal.run("fal-ai/nano-banana-2/edit", {}, "t1", 1, "keyframe", None)
    take = State(tmp_path).data["takes"]["t1"]
    assert take["request_id"] == RID
    assert take["status_url"] == accepted["status_url"]
    assert take["response_url"] == accepted["response_url"]


def test_foreign_request_urls_are_not_trusted(tmp_path, monkeypatch):
    """Only queue.fal.run addresses may be polled with the credential."""
    fal, _ = _fal(tmp_path)
    accepted = {"request_id": RID, "status_url": "https://evil.test/steal", "response_url": "https://evil.test/x"}
    monkeypatch.setattr("app.live_providers.requests.post", lambda *a, **k: _resp(200, accepted))
    monkeypatch.setattr(fal, "_wait", lambda endpoint, rid: {"images": []})
    fal.run("fal-ai/nano-banana-2/edit", {}, "t1", 1, "keyframe", None)
    take = State(tmp_path).data["takes"]["t1"]
    assert "status_url" not in take and "response_url" not in take


def test_polling_uses_stored_urls_and_the_submission_credential(tmp_path, monkeypatch):
    fal, state = _fal(tmp_path)
    status_url = f"{QUEUE}fal-ai/nano-banana-2/requests/{RID}/status"
    response_url = f"{QUEUE}fal-ai/nano-banana-2/requests/{RID}"
    state.data["takes"]["t1"] = {"status": "submitted", "request_id": RID,
                                 "status_url": status_url, "response_url": response_url}
    state.save()
    seen = []

    def get(url, headers=None, **kw):
        seen.append((url, headers["Authorization"]))
        if url == status_url:
            return _resp(200, {"status": "COMPLETED"})
        assert url == response_url
        return _resp(200, {"images": [{"url": "https://cdn.test/a.png"}]})

    monkeypatch.setattr("app.live_providers.requests.get", get)
    monkeypatch.setattr("app.live_providers.time.sleep", lambda s: None)
    # The SDK must be out of the loop entirely.
    import fal_client
    monkeypatch.setattr(fal_client, "status", lambda *a, **k: pytest.fail("SDK polling used"))
    monkeypatch.setattr(fal_client, "result", lambda *a, **k: pytest.fail("SDK polling used"))

    result = fal._wait("fal-ai/nano-banana-2/edit", RID)
    assert result["images"][0]["url"].endswith("a.png")
    assert [u for u, _ in seen] == [status_url, response_url]
    assert all(auth == "Key " + KEY for _, auth in seen)


def test_polling_waits_until_completed(tmp_path, monkeypatch):
    fal, state = _fal(tmp_path)
    state.data["takes"]["t1"] = {"status": "submitted", "request_id": RID,
                                 "status_url": "https://queue.fal.run/s", "response_url": "https://queue.fal.run/r"}
    state.save()
    answers = iter([{"status": "IN_QUEUE"}, {"status": "IN_PROGRESS"}, {"status": "COMPLETED"}])
    monkeypatch.setattr("app.live_providers.requests.get",
                        lambda url, **k: _resp(200, next(answers) if url.endswith("/s") else {"video": {"url": "x"}}))
    slept = []
    monkeypatch.setattr("app.live_providers.time.sleep", slept.append)
    assert fal._wait("fal-ai/test", RID) == {"video": {"url": "x"}}
    assert len(slept) == 2


def test_legacy_take_without_urls_polls_the_request_address(tmp_path, monkeypatch):
    """A take saved before URLs were stored (the failing production job) must
    resume at owner/alias/requests/<id>, without the endpoint's sub-path."""
    fal, state = _fal(tmp_path)
    state.data["takes"]["t1"] = {"status": "submitted", "request_id": RID}
    state.save()
    seen = []
    monkeypatch.setattr("app.live_providers.requests.get",
                        lambda url, **k: (seen.append(url), _resp(200, {"status": "COMPLETED", "ok": 1}))[1])
    fal._wait("fal-ai/nano-banana-2/edit", RID)
    assert seen[0] == f"{QUEUE}fal-ai/nano-banana-2/requests/{RID}/status"
    assert seen[1] == f"{QUEUE}fal-ai/nano-banana-2/requests/{RID}"
    assert "/edit/" not in seen[0]


def test_request_base_handles_namespaces():
    assert _request_base("fal-ai/veo3.1/fast/image-to-video", RID) == f"{QUEUE}fal-ai/veo3.1/requests/{RID}"
    assert _request_base("workflows/acme/flow", RID) == f"{QUEUE}workflows/acme/flow/requests/{RID}"


def test_collect_refusal_explains_itself_in_the_logs(tmp_path, monkeypatch, capsys):
    """The error banner stays scrubbed; the reason reaches the logs."""
    fal, state = _fal(tmp_path)
    state.data["reserved_usd"] = 1
    state.data["takes"]["t1"] = {"status": "submitted", "request_id": RID,
                                 "status_url": "https://queue.fal.run/s", "response_url": "https://queue.fal.run/r"}
    state.save()
    body = {"detail": f"User is locked. Reason: PRIVATE_REASON. See https://fal.ai/billing?k={KEY}"}
    monkeypatch.setattr("app.live_providers.requests.get", lambda url, **k: _resp(403, body, url))

    with pytest.raises(ProviderFailure) as excinfo:
        fal.run("fal-ai/nano-banana-2/edit", {}, "t1", 1, "keyframe", None)

    public = str(excinfo.value)
    assert "HTTP 403" in public and RID in public
    assert "PRIVATE_REASON" not in public and "https://" not in public
    saved = State(tmp_path).data["takes"]["t1"]
    assert "PRIVATE_REASON" not in json.dumps(saved) and "PRIVATE_SECRET" not in json.dumps(saved)

    out = capsys.readouterr().out
    assert f"[fal] HTTP 403 request {RID}: User is locked. Reason: PRIVATE_REASON." in out
    assert "<url>" in out and "PRIVATE_SECRET" not in out and "https://" not in out


def test_the_reason_reaches_the_job_log_operators_actually_read(tmp_path, monkeypatch, capsys):
    """Asking an operator to open the host's log stream did not work in
    practice. The job log is on the page they are already looking at."""
    fal, state = _fal(tmp_path)
    state.data["reserved_usd"] = 1
    state.data["takes"]["t1"] = {"status": "submitted", "request_id": RID,
                                 "status_url": "https://queue.fal.run/s", "response_url": "https://queue.fal.run/r"}
    state.save()
    lines = []
    fal.log = lines.append
    monkeypatch.setattr("app.live_providers.requests.get",
                        lambda url, **k: _resp(403, {"detail": f"Exhausted balance. See https://fal.ai/billing?k={KEY}"}, url))
    with pytest.raises(ProviderFailure):
        fal.run("fal-ai/nano-banana-2/edit", {}, "t1", 1, "keyframe", None)
    assert any("Exhausted balance." in line for line in lines)
    assert not any("PRIVATE_SECRET" in line or "https://" in line for line in lines)


def test_a_failing_job_log_does_not_mask_the_provider_error(tmp_path, monkeypatch):
    """The log sink is best-effort; the ProviderFailure must still surface."""
    fal, state = _fal(tmp_path)
    state.data["reserved_usd"] = 1
    state.data["takes"]["t1"] = {"status": "submitted", "request_id": RID,
                                 "status_url": "https://queue.fal.run/s", "response_url": "https://queue.fal.run/r"}
    state.save()

    def broken(_):
        raise RuntimeError("log sink down")

    fal.log = broken
    monkeypatch.setattr("app.live_providers.requests.get",
                        lambda url, **k: _resp(403, {"detail": "denied"}, url))
    with pytest.raises(ProviderFailure, match="HTTP 403"):
        fal.run("fal-ai/nano-banana-2/edit", {}, "t1", 1, "keyframe", None)


def test_submit_refusal_is_also_logged(tmp_path, monkeypatch, capsys):
    fal, _ = _fal(tmp_path)

    def post(*a, **k):
        r = _resp(403, {"detail": "Forbidden: PRIVATE_SUBMIT_REASON"})
        r.raise_for_status()

    monkeypatch.setattr("app.live_providers.requests.post", post)
    with pytest.raises(ProviderFailure):
        fal.run("fal-ai/nano-banana-2/edit", {}, "t1", 1, "keyframe", None)
    out = capsys.readouterr().out
    assert "[fal] HTTP 403 request -: Forbidden: PRIVATE_SUBMIT_REASON" in out
