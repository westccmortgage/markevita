"""Retiring a fal request that cannot be read back.

Production case: keyframes for miami/s01e01_v2 stopped on request
01a09d10-… with HTTP 403 while COLLECTING. The queue had accepted the
request, the fal key was valid with credits, and removing the SDK from the
polling path did not change it. A request cannot be read back by a key that
did not create it, and the fal account holds exactly one key, created after
production had already begun — so the saved request is unreadable for good.

Polling it forever cannot succeed. These tests pin the narrow escape: retire
that one request, keep its id, charge it as possibly billed, submit that take
once more, and leave every other guard exactly as it was.
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

from app.live_providers import DurableFal  # noqa: E402
from app.provider_errors import ProviderFailure  # noqa: E402
from serial.costs import Budget  # noqa: E402
from serial.state import State  # noqa: E402

OLD_KEY = "11111111-2222-4333-8444-555555555555:OLD_SECRET"
NEW_KEY = "15f4e479-59db-4111-8222-333333333333:NEW_SECRET"
RID = "01a09d10-d4ec-7c22-879c-9feea760da68"
ENDPOINT = "fal-ai/nano-banana-2/edit"


def _resp(status, body):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body).encode()
    r.headers["content-type"] = "application/json"
    return r


def _fal(state, key=NEW_KEY, released=()):
    return DurableFal(SimpleNamespace(fal_key=key), lambda m: None, state,
                      Budget(10, state), None, released)


def _stuck(tmp_path, key_id=None):
    """A take exactly as production left it: accepted, then refused on read."""
    state = State(tmp_path)
    state.data["reserved_usd"] = 1
    take = {"status": "submitted", "request_id": RID, "endpoint": ENDPOINT,
            "estimated_cost": 1, "provider": "fal.ai",
            "provider_error": {"provider": "fal.ai", "phase": "collect", "http_status": 403,
                               "message": "fal.ai HTTP 403"}}
    if key_id:
        take["key_id"] = key_id
    state.data["takes"]["t1"] = take
    state.save()
    return state


# ── without a release, nothing changes ─────────────────────────────────────

def test_a_refused_request_is_still_never_resubmitted_on_its_own(tmp_path, monkeypatch):
    """The default stays exactly as before: poll, fail, never pay twice."""
    state = _stuck(tmp_path)
    monkeypatch.setattr("app.live_providers.requests.post",
                        lambda *a, **k: pytest.fail("resubmitted without a release"))
    monkeypatch.setattr("app.live_providers.requests.get",
                        lambda *a, **k: _resp(403, {"detail": "denied"}))
    with pytest.raises(ProviderFailure, match="HTTP 403"):
        _fal(state).run(ENDPOINT, {}, "t1", 1, "keyframe", None)
    saved = State(tmp_path).data
    assert saved["takes"]["t1"]["request_id"] == RID
    assert saved["spent_usd"] == 0


def test_a_release_for_another_request_id_is_ignored(tmp_path, monkeypatch):
    state = _stuck(tmp_path)
    monkeypatch.setattr("app.live_providers.requests.post",
                        lambda *a, **k: pytest.fail("resubmitted on an unrelated release"))
    monkeypatch.setattr("app.live_providers.requests.get",
                        lambda *a, **k: _resp(403, {"detail": "denied"}))
    fal = _fal(state, released={"99999999-1111-2222-3333-444444444444"})
    with pytest.raises(ProviderFailure):
        fal.run(ENDPOINT, {}, "t1", 1, "keyframe", None)
    assert State(tmp_path).data["takes"]["t1"]["request_id"] == RID


# ── an explicit release retires exactly one request ────────────────────────

def _accepting(calls):
    def post(url, **kw):
        calls.append(kw.get("headers", {}).get("Authorization"))
        return _resp(200, {"request_id": "22222222-3333-4444-5555-666666666666"})
    return post


def test_release_retires_the_request_keeps_its_id_and_submits_once(tmp_path, monkeypatch):
    state = _stuck(tmp_path)
    calls = []
    monkeypatch.setattr("app.live_providers.requests.post", _accepting(calls))
    fal = _fal(state, released={RID})
    monkeypatch.setattr(fal, "_wait", lambda endpoint, rid: {"images": [{"url": "https://cdn/a.png"}]})

    fal.run(ENDPOINT, {}, "t1", 1, "keyframe", None)

    take = State(tmp_path).data["takes"]["t1"]
    retired = take["unreachable_requests"]
    assert len(retired) == 1
    # The id is retained, never deleted.
    assert retired[0]["request_id"] == RID
    assert retired[0]["http_status"] == 403 and retired[0]["phase"] == "collect"
    assert retired[0]["charged_usd"] == 1
    # Exactly one new submission, with the current key.
    assert calls == ["Key " + NEW_KEY]
    assert take["request_id"] == "22222222-3333-4444-5555-666666666666"
    assert take["status"] == "succeeded"


def test_the_retired_request_is_charged_as_possibly_billed(tmp_path, monkeypatch):
    """The queue ran it; releasing the reservation at zero would understate
    the episode's spend."""
    state = _stuck(tmp_path)
    monkeypatch.setattr("app.live_providers.requests.post", _accepting([]))
    fal = _fal(state, released={RID})
    monkeypatch.setattr(fal, "_wait", lambda endpoint, rid: {"images": []})
    fal.run(ENDPOINT, {}, "t1", 1, "keyframe", None)
    saved = State(tmp_path).data
    # 1 for the unreachable request + 1 for the new take.
    assert saved["spent_usd"] == 2
    assert saved["reserved_usd"] == 0
    assert any("unreachable" in entry["what"] for entry in saved["cost_log"])


def test_a_release_is_spent_once_not_every_run(tmp_path, monkeypatch):
    """Resuming again must not retire-and-resubmit a second time."""
    state = _stuck(tmp_path)
    posts = []
    monkeypatch.setattr("app.live_providers.requests.post", _accepting(posts))
    fal = _fal(state, released={RID})
    monkeypatch.setattr(fal, "_wait", lambda endpoint, rid: {"images": []})
    fal.run(ENDPOINT, {}, "t1", 1, "keyframe", None)

    again = State(tmp_path)
    fal2 = _fal(again, released={RID})
    monkeypatch.setattr("app.live_providers.requests.post",
                        lambda *a, **k: pytest.fail("submitted twice for one release"))
    fal2.run(ENDPOINT, {}, "t1", 1, "keyframe", None)
    assert len(posts) == 1
    assert State(tmp_path).data["spent_usd"] == 2


# ── a rotated key retires the request without asking ───────────────────────

def test_a_request_from_a_replaced_key_is_retired_automatically(tmp_path, monkeypatch):
    """Once takes record which key created them, this needs no operator step."""
    state = _stuck(tmp_path, key_id="11111111")
    calls = []
    monkeypatch.setattr("app.live_providers.requests.post", _accepting(calls))
    fal = _fal(state, key=NEW_KEY)          # no release recorded
    monkeypatch.setattr(fal, "_wait", lambda endpoint, rid: {"images": []})
    fal.run(ENDPOINT, {}, "t1", 1, "keyframe", None)
    retired = State(tmp_path).data["takes"]["t1"]["unreachable_requests"][0]
    assert retired["request_id"] == RID
    assert retired["key_id"] == "11111111" and retired["current_key_id"] == "15f4e479"
    assert "no longer holds" in retired["reason"]
    assert len(calls) == 1


def test_the_same_key_is_never_treated_as_rotated(tmp_path, monkeypatch):
    """A refusal under the key that created the request is a different problem
    and must stay blocked."""
    state = _stuck(tmp_path, key_id="15f4e479")
    monkeypatch.setattr("app.live_providers.requests.post",
                        lambda *a, **k: pytest.fail("resubmitted under the same key"))
    monkeypatch.setattr("app.live_providers.requests.get",
                        lambda *a, **k: _resp(403, {"detail": "denied"}))
    with pytest.raises(ProviderFailure):
        _fal(state, key=NEW_KEY).run(ENDPOINT, {}, "t1", 1, "keyframe", None)
    assert State(tmp_path).data["takes"]["t1"]["request_id"] == RID


def test_a_submit_phase_refusal_is_not_treated_as_a_rotated_key(tmp_path, monkeypatch):
    state = _stuck(tmp_path, key_id="11111111")
    state.data["takes"]["t1"]["provider_error"]["phase"] = "submit"
    state.save()
    monkeypatch.setattr("app.live_providers.requests.post",
                        lambda *a, **k: pytest.fail("resubmitted on a submit-phase refusal"))
    monkeypatch.setattr("app.live_providers.requests.get",
                        lambda *a, **k: _resp(403, {"detail": "denied"}))
    with pytest.raises(ProviderFailure):
        _fal(state, key=NEW_KEY).run(ENDPOINT, {}, "t1", 1, "keyframe", None)


def test_new_submissions_record_the_key_that_created_them(tmp_path, monkeypatch):
    state = State(tmp_path)
    monkeypatch.setattr("app.live_providers.requests.post", _accepting([]))
    fal = _fal(state, key=NEW_KEY)
    monkeypatch.setattr(fal, "_wait", lambda endpoint, rid: {"images": []})
    fal.run(ENDPOINT, {}, "t1", 1, "keyframe", None)
    assert State(tmp_path).data["takes"]["t1"]["key_id"] == "15f4e479"


def test_a_mismatched_reservation_blocks_the_release(tmp_path, monkeypatch):
    """Budget accounting must be sound before any new paid call."""
    state = _stuck(tmp_path)
    state.data["reserved_usd"] = 0          # reservation lost
    state.save()
    monkeypatch.setattr("app.live_providers.requests.post",
                        lambda *a, **k: pytest.fail("submitted with unreconciled budget"))
    with pytest.raises(RuntimeError, match="reconciliation"):
        _fal(state, released={RID}).run(ENDPOINT, {}, "t1", 1, "keyframe", None)
