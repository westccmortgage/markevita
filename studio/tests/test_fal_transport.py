"""Submission and retrieval must use one explicitly-bound fal credential.

Background: the module-level fal_client functions share a client that resolves
its credential from the environment once and caches it. A process that assigns
FAL_KEY after that first resolution submits with one key and reads results with
another. The queue accepts the submission, then refuses the status/result read
with HTTP 403 — which looks exactly like a bad key and sends an operator off to
rotate a key that was never the problem.

These tests pin the binding and the wording that keeps the two apart.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

import fal_client  # noqa: E402
from app.provider_errors import fal_diagnostic  # noqa: E402
from serial.providers import Fal, InputPublisher, fal_client_for  # noqa: E402

KEY = "11111111-2222-4333-8444-555555555555:PRIVATE_SECRET"
STALE = "99999999-8888-4777-8666-555555555555:STALE_SECRET"
RID = "01a09d10-d4ec-7c22-879c-9feea760da68"


def _fal(key=KEY):
    return Fal(SimpleNamespace(fal_key=key, dry_run=False), lambda m: None, None, None, None)


def test_client_is_bound_to_the_configured_key():
    client = _fal().client
    assert client._auth.header_value == "Key " + KEY


def test_client_is_reused_not_rebuilt_per_call():
    fal = _fal()
    assert fal.client is fal.client


def test_two_configs_do_not_share_one_credential():
    """The regression in one sentence: a second config must not inherit the
    first one's cached credential."""
    assert _fal(KEY).client._auth.header_value != _fal(STALE).client._auth.header_value


def test_wait_never_touches_the_module_level_client(monkeypatch):
    """The whole point of the binding: polling goes through the bound client."""
    for name in ("status", "result", "submit"):
        monkeypatch.setattr(fal_client, name,
                            lambda *a, **k: pytest.fail("module-level fal_client used"))
    calls = []

    def status(app, rid, with_logs=False):
        calls.append(("status", app, rid))
        return fal_client.Completed(logs=None, metrics={})

    def result(app, rid):
        calls.append(("result", app, rid))
        return {"images": []}

    fal = _fal()
    # SyncClient is a frozen dataclass, so the stub is injected as the client.
    fal._client = SimpleNamespace(status=status, result=result)
    assert fal._wait("fal-ai/nano-banana-2/edit", RID) == {"images": []}
    assert calls == [("status", "fal-ai/nano-banana-2/edit", RID),
                     ("result", "fal-ai/nano-banana-2/edit", RID)]


def test_input_upload_uses_the_configured_key(monkeypatch, tmp_path):
    """An input uploaded with a different credential than the request that
    consumes it is the same failure one step earlier."""
    monkeypatch.setattr(fal_client, "upload_file",
                        lambda *a, **k: pytest.fail("module-level upload_file used"))
    source = tmp_path / "frame.png"
    source.write_bytes(b"pixels")
    publisher = InputPublisher(
        SimpleNamespace(fal_key=KEY, dry_run=False, provider_input_mode="fal_storage"),
        lambda m: None, None, "prefix")
    seen = {}

    def build(key):
        seen["key"] = key
        return SimpleNamespace(upload_file=lambda p: "https://fal.storage/x.png")

    monkeypatch.setattr("serial.providers.fal_client_for", build)
    assert publisher.url(source) == "https://fal.storage/x.png"
    assert seen["key"] == KEY


def test_r2_mode_does_not_build_a_fal_client(monkeypatch, tmp_path):
    source = tmp_path / "frame.png"
    source.write_bytes(b"pixels")
    r2 = SimpleNamespace(enabled=True, put=lambda p, k: None,
                         presign=lambda k, s: "https://r2.test/signed")
    publisher = InputPublisher(
        SimpleNamespace(fal_key=KEY, dry_run=False, provider_input_mode="r2_presigned"),
        lambda m: None, r2, "prefix")
    monkeypatch.setattr("serial.providers.fal_client_for",
                        lambda key: pytest.fail("fal client built in R2 mode"))
    assert publisher.url(source) == "https://r2.test/signed"


def test_fal_client_for_returns_an_independent_client():
    a, b = fal_client_for(KEY), fal_client_for(STALE)
    assert a is not b
    assert a._auth.header_value == "Key " + KEY
    assert b._auth.header_value == "Key " + STALE


# ── wording: a refused read is not a refused submission ────────────────────

class _Exc(Exception):
    def __init__(self, response):
        self.response = response


def _response(status):
    import httpx
    return httpx.Response(status, json={"detail": "PRIVATE_PROVIDER_TEXT"})


def test_collect_403_says_the_request_was_already_accepted():
    message = fal_diagnostic(_Exc(_response(403)), RID, phase="collect")["message"]
    assert "HTTP 403" in message and RID in message
    assert "already accepted" in message
    assert "not proof that a new submission would be refused" in message
    assert "PRIVATE_PROVIDER_TEXT" not in message


def test_submit_403_points_at_the_balance_not_at_the_key():
    """A run that had billed a hundred and fifty images that day stopped here.

    The advice sent its producer to check key permissions, which the provider's
    own billing proved were fine, while the account sat at zero credit — which
    is what fal.ai refuses with this code. A refusal must send someone to the
    one place the answer is.
    """
    message = fal_diagnostic(_Exc(_response(403)), phase="submit")["message"]
    assert "balance" in message
    assert "permissions it needs" in message, "it still blames the key"
    assert "already accepted" not in message, "nothing was accepted; this is the submission"


def test_submit_403_has_a_russian_translation():
    ru = json.loads((STUDIO / "app" / "locales" / "ru.json").read_text())
    advice = fal_diagnostic(_Exc(_response(403)), phase="submit")["message"].split(": ", 1)[1]
    assert advice in ru
    assert "баланс" in ru[advice]


def test_collect_403_has_a_russian_translation():
    """The operator reading this in Russian must get the same distinction."""
    ru = json.loads((STUDIO / "app" / "locales" / "ru.json").read_text())
    advice = fal_diagnostic(_Exc(_response(403)), RID, phase="collect")["message"].split(": ", 1)[1]
    assert advice in ru
    assert "не доказывает" in ru[advice]


def test_other_statuses_are_unchanged_by_phase():
    for status in (401, 402, 429, 422):
        collect = fal_diagnostic(_Exc(_response(status)), RID, phase="collect")["message"]
        submit = fal_diagnostic(_Exc(_response(status)), RID, phase="submit")["message"]
        assert collect == submit
