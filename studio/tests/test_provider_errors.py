"""Public provider errors retain useful codes without exposing payloads or charging twice."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import requests
from fal_client.client import FalClientHTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import i18n
from app.live_providers import DurableFal
from app.provider_errors import ProviderFailure, fal_diagnostic
from serial.costs import Budget
from serial.state import State

RID = "3418ab11-f6e9-43f2-a46b-b595f0fb2012"


def test_saved_result_error_is_safe_and_resume_never_resubmits(tmp_path, monkeypatch):
    state = State(tmp_path)
    state.data.update(reserved_usd=1)
    state.data["takes"]["t1"] = {"status": "submitted", "request_id": RID}
    state.save()
    monkeypatch.setattr("app.live_providers.requests.post", lambda *a, **kw: pytest.fail("duplicate paid submit"))
    fal = DurableFal(SimpleNamespace(fal_key="PRIVATE_KEY"), lambda m: None, state, Budget(10, state), None)
    response = httpx.Response(422, json={"detail": [{"type": "file_download_error", "msg": "PRIVATE_PROVIDER_TEXT",
        "input": "https://private.test/image?token=PRIVATE_TOKEN"}]})
    exc = FalClientHTTPError("PRIVATE_PROVIDER_TEXT", 422, {}, response)
    def failed_wait(endpoint, rid):
        assert rid == RID
        raise exc
    monkeypatch.setattr(fal, "_wait", failed_wait)
    with pytest.raises(ProviderFailure) as caught:
        fal.run("fal-ai/test", {}, "t1", 1, "image", None)
    public = str(caught.value)
    assert "HTTP 422" in public and "file_download_error" in public and RID in public
    saved = State(tmp_path).data["takes"]["t1"]
    assert saved["request_id"] == RID and saved["status"] == "submitted"
    assert saved["provider_error"]["message"] == public
    assert "PRIVATE" not in json.dumps(saved["provider_error"])
    assert "https://" not in public
    ru = i18n.notice({"request": SimpleNamespace(cookies={i18n.COOKIE: "ru"})}, public)
    assert "не смог скачать" in ru and RID in ru and "HTTP 422" in ru
    monkeypatch.setattr(fal, "_wait", lambda *a: {"images": [{"url": "https://example.test/saved.png"}]})
    fal.run("fal-ai/test", {}, "t1", 1, "image", None)
    fal.run("fal-ai/test", {}, "t1", 1, "image", None)
    assert "provider_error" not in state.data["takes"]["t1"]
    assert state.data["spent_usd"] == 1 and state.data["reserved_usd"] == 0
    assert len(state.data["cost_log"]) == 1


def test_submit_http_error_is_recorded_and_no_unknown_request_repeated(tmp_path, monkeypatch):
    state = State(tmp_path)
    fal = DurableFal(SimpleNamespace(fal_key="PRIVATE_KEY"), lambda m: None, state, Budget(10, state), None)
    calls = []
    def denied(*a, **kw):
        calls.append(1)
        response = requests.Response(); response.status_code = 402
        response._content = b'{"detail":"PRIVATE_BILLING_DETAIL"}'
        return response
    monkeypatch.setattr("app.live_providers.requests.post", denied)
    with pytest.raises(ProviderFailure, match="HTTP 402"):
        fal.run("fal-ai/test", {}, "t1", 1, "image", None)
    with pytest.raises(RuntimeError, match="unknown"):
        fal.run("fal-ai/test", {}, "t1", 1, "image", None)
    info = State(tmp_path).data["takes"]["t1"]["provider_error"]
    assert info["phase"] == "submit" and "PRIVATE" not in json.dumps(info)
    assert len(calls) == 1


def test_unknown_response_and_request_id_are_never_echoed():
    response = httpx.Response(500, json={"error_type": "PRIVATE_ERROR", "detail": "PRIVATE_DETAIL"})
    exc = FalClientHTTPError("PRIVATE_MESSAGE", 500, {}, response, "PRIVATE_ERROR")
    diagnostic = fal_diagnostic(exc, "https://private.test/PRIVATE_TOKEN")
    assert diagnostic["http_status"] == 500 and diagnostic["error_type"] is None
    assert "PRIVATE" not in json.dumps(diagnostic)
