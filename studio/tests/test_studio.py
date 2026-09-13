"""Studio tests.

These cover the layer the studio adds on top of the v0.3 engine: the record
store, the Supabase→package bridge, script parsing, mock-mode enforcement,
secret containment, the job lifecycle, and an end-to-end mock production run
driven exactly as the admin panel drives it.

The engine's own 23 tests live in ../pipeline/tests and are not repeated here.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))

# Isolate every test run from the developer's own data.
_TMP = tempfile.mkdtemp(prefix="studio-test-")
os.environ.setdefault("STUDIO_STORE", "local")
os.environ["STUDIO_ALLOW_PAID"] = "false"
os.environ["STUDIO_SESSION_SECRET"] = "test-secret-not-used-anywhere-real"
os.environ["STUDIO_ADMIN_EMAIL"] = "admin@example.test"
os.environ["STUDIO_ADMIN_PASSWORD"] = "test-password"

from app import auth, ingest, integrations, packaging, runner, scripts  # noqa: E402
from app.config import settings  # noqa: E402
from app.store.local import LocalDriver  # noqa: E402

SERIES = "test_series"


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch):
    """Point every module's store at a throwaway directory."""
    driver = LocalDriver(Path(tempfile.mkdtemp(prefix="studio-store-")))
    # Every module binds `store` at import time, so each one must be redirected.
    for module in (packaging, scripts, integrations, auth, ingest, runner):
        monkeypatch.setattr(module, "store", driver, raising=False)
    monkeypatch.setattr("app.store.store", driver, raising=False)
    yield driver


# ── store ──────────────────────────────────────────────────────────────────

def test_upsert_is_idempotent_on_the_natural_key(isolated_store):
    isolated_store.upsert("series", {"id": SERIES, "title": "First"})
    isolated_store.upsert("series", {"id": SERIES, "title": "Second"})
    rows = isolated_store.list("series")
    assert len(rows) == 1 and rows[0]["title"] == "Second"


def test_delete_and_filter(isolated_store):
    isolated_store.upsert("scenes", {"series_id": SERIES, "episode_id": "e1", "scene_id": "sc01"})
    isolated_store.upsert("scenes", {"series_id": SERIES, "episode_id": "e2", "scene_id": "sc01"})
    assert len(isolated_store.list("scenes", {"episode_id": "e1"})) == 1
    isolated_store.delete("scenes", {"episode_id": "e1"})
    assert len(isolated_store.list("scenes")) == 1


def test_unknown_table_is_rejected(isolated_store):
    with pytest.raises(KeyError):
        isolated_store.list("definitely_not_a_table")


# ── script parsing ─────────────────────────────────────────────────────────

def test_structured_script_parses_every_field():
    parsed = scripts.parse(
        "SCENE sc01 | 8s | shore | night\n"
        "CHARACTERS: lead_a\n"
        "WARDROBE: lead_a=w_default\n"
        "SHOT: medium shot | 50mm | slow push-in\n"
        "ACTION: She reads the list.\n"
        "IN: list folded\nOUT: list open\n"
        "PROPS: passenger_list=in her hand\n"
        "LEARNS: lead_a gains secret_one via overhears\n"
        "REL: rel_a_b -> suspicious\n"
        "CLIFFHANGER\n"
        "lead_a (quiet): Your name is on it.\n"
        "vo narrator: Nobody left.\n"
    )
    scene = parsed["scenes"][0]
    assert scene["duration_seconds"] == 8
    assert scene["location"] == "shore" and scene["lighting_state"] == "night"
    assert scene["wardrobe"] == {"lead_a": "w_default"}
    assert scene["lens"] == "50mm" and scene["camera_motion"] == "slow push-in"
    assert scene["props"] == [{"prop_id": "passenger_list", "state": "in her hand"}]
    assert scene["knowledge_gained"][0]["secret"] == "secret_one"
    assert scene["relationship_changes"] == [{"id": "rel_a_b", "state": "suspicious"}]
    assert scene["is_cliffhanger"] is True
    assert scene["dialogue"][0] == {"speaker": "lead_a", "text": "Your name is on it.", "delivery": "quiet"}
    assert scene["dialogue"][1]["voice_over"] is True
    assert parsed["cliffhanger"]["scene_id"] == "sc01"


def test_script_without_action_is_rejected():
    with pytest.raises(scripts.ScriptError, match="no ACTION"):
        scripts.parse("SCENE sc01 | 6s | shore\nlead_a: A line.")


def test_content_before_a_scene_header_is_rejected():
    with pytest.raises(scripts.ScriptError, match="before the first SCENE"):
        scripts.parse("lead_a: A line with no scene.")


def test_json_brief_is_accepted():
    parsed = scripts.parse('{"scenes": [{"scene_id": "sc01", "action": "x"}], "title": "T"}')
    assert parsed["format"] == "json" and parsed["title"] == "T"


def test_empty_script_is_rejected():
    with pytest.raises(scripts.ScriptError):
        scripts.parse("   ")


# ── packaging bridge ───────────────────────────────────────────────────────

def _minimal_series(driver):
    driver.upsert("series", {"id": SERIES, "title": "Test", "language": "en-US",
                             "style": {"style_sentence": "A style sentence."}})
    driver.upsert("seasons", {"series_id": SERIES, "season_id": "s01", "number": 1,
                              "episode_order": ["s01e01"]})
    driver.upsert("characters", {"series_id": SERIES, "character_id": "lead_a", "name": "Lead A",
                                 "visual": True, "appearance": "Appearance text."})
    driver.upsert("clothing", {"series_id": SERIES, "character_id": "lead_a",
                               "variant_id": "w_default", "is_default": True,
                               "description": "Wardrobe text."})
    driver.upsert("voices", {"series_id": SERIES, "character_id": "lead_a",
                             "provider": "elevenlabs", "voice_env": "ELEVENLABS_VOICE_ID_LEAD_A"})
    driver.upsert("locations", {"series_id": SERIES, "location_id": "shore", "name": "Shore",
                                "description": "Location text.", "lighting_states": {"default": "day"}})


def test_package_matches_the_engine_contract(isolated_store, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "package_dir", tmp_path)
    _minimal_series(isolated_store)
    root = packaging.materialize(SERIES)

    import json
    series = json.loads((root / "series.json").read_text())
    assert series["schema_version"] == "2.0"
    assert series["seasons"][0]["episodes"] == ["s01e01"]

    characters = json.loads((root / "bible" / "characters.json").read_text())
    assert characters[0]["wardrobe"]["default"] == "w_default"
    assert characters[0]["voice"]["voice_env"] == "ELEVENLABS_VOICE_ID_LEAD_A"


def test_package_never_contains_a_voice_id(isolated_store, monkeypatch, tmp_path):
    """The bible stores the NAME of the variable, never the id it holds."""
    monkeypatch.setattr(settings, "package_dir", tmp_path)
    monkeypatch.setenv("ELEVENLABS_VOICE_ID_LEAD_A", "super-secret-voice-id")
    _minimal_series(isolated_store)
    root = packaging.materialize(SERIES)
    everything = " ".join(p.read_text() for p in root.rglob("*.json"))
    assert "super-secret-voice-id" not in everything


def test_optional_bible_files_are_omitted_when_empty(isolated_store, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "package_dir", tmp_path)
    _minimal_series(isolated_store)
    root = packaging.materialize(SERIES)
    # Empty arrays would fail the engine's schema, so these must not be written.
    assert not (root / "bible" / "props.json").exists()
    assert not (root / "bible" / "secrets.json").exists()


# ── mock-mode enforcement and secret containment ───────────────────────────

def test_studio_is_in_mock_mode():
    assert settings.allow_paid is False
    assert settings.mode == "mock"


def test_starting_live_job_requires_reviewed_approval(monkeypatch):
    from app import runner
    monkeypatch.setattr(runner.settings, "allow_paid", True)
    with pytest.raises(PermissionError, match="approve"):
        runner.jobs.start(SERIES, "s01e01", ["intake"], "tester")


def test_integration_status_never_returns_a_secret(monkeypatch, isolated_store):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-do-not-leak-me")
    status = integrations.status("anthropic")
    assert status["connected"] is True
    assert "sk-ant-do-not-leak-me" not in repr(status)
    assert set(status) >= {"connected", "state", "model", "last_test_at", "last_error"}


def test_connection_test_is_offline_by_default(monkeypatch, isolated_store):
    """With tests offline, a present credential passes without any network call."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    monkeypatch.setattr(settings, "allow_connection_tests", False)

    def explode(*a, **k):
        raise AssertionError("a connection test must not touch the network")

    monkeypatch.setattr(integrations, "_probe", explode)
    result = integrations.test_connection("elevenlabs")
    assert result["connected"] is True and result["last_error"] is None


def test_missing_credential_is_reported_not_raised(isolated_store, monkeypatch):
    monkeypatch.delenv("FAL_KEY", raising=False)
    result = integrations.test_connection("fal")
    assert result["state"] == "Missing" and "FAL_KEY" in result["last_error"]


# ── deployment configuration faults ────────────────────────────────────────

def _deployed_settings(monkeypatch, **env):
    """Settings as they would load on a hosted platform."""
    from app.config import Settings
    for key in ("STUDIO_ADMIN_EMAIL", "STUDIO_ADMIN_PASSWORD", "STUDIO_SESSION_SECRET",
                "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "STUDIO_STORE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STUDIO_BASE_PATH", "/studio")
    monkeypatch.setenv("STUDIO_HOST", "0.0.0.0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Ignore any developer .env so the test describes the hosted environment.
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    return Settings.load()


def test_unconfigured_deployment_reports_every_fault(monkeypatch):
    """The exact state the Render deployment was in: nobody could sign in,
    sessions died on restart, and records lived on an ephemeral disk."""
    problems = _deployed_settings(monkeypatch).config_problems()
    faults = " ".join(p["what"] for p in problems)
    assert "Nobody can sign in" in faults
    assert "signed out whenever the service restarts" in faults
    assert "records are lost" in faults
    assert all(p["level"] == "fatal" for p in problems)


def test_a_correctly_configured_deployment_reports_nothing(monkeypatch):
    s = _deployed_settings(
        monkeypatch,
        STUDIO_ADMIN_EMAIL="admin@example.test", STUDIO_ADMIN_PASSWORD="pw",
        STUDIO_SESSION_SECRET="a-fixed-secret",
        STUDIO_STORE="supabase", SUPABASE_URL="https://x.supabase.co",
        SUPABASE_ANON_KEY="anon-key", SUPABASE_SERVICE_ROLE_KEY="service-key",
    )
    assert s.config_problems() == []


def test_local_use_is_not_treated_as_a_deployment(monkeypatch):
    """A laptop with no session secret is fine; only hosted use is a fault."""
    from app.config import Settings
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    for key in ("STUDIO_BASE_PATH", "STUDIO_SESSION_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STUDIO_HOST", "127.0.0.1")
    monkeypatch.setenv("STUDIO_ADMIN_EMAIL", "admin@example.test")
    monkeypatch.setenv("STUDIO_ADMIN_PASSWORD", "pw")
    s = Settings.load()
    assert s.deployed is False
    assert s.config_problems() == []
    assert s.env_hint == "studio/.env"


def test_env_hint_points_at_the_platform_when_deployed(monkeypatch):
    """The old message told a Render operator to edit studio/.env, which does
    not exist there."""
    assert _deployed_settings(monkeypatch).env_hint == "the service's environment variables"


def test_supabase_auth_does_not_depend_on_the_record_store(monkeypatch):
    """The production bug: Supabase was fully configured, but because the
    record store had fallen back to local the panel silently reported
    'Local administrator' and refused every sign-in."""
    s = _deployed_settings(monkeypatch, SUPABASE_URL="https://x.supabase.co",
                           SUPABASE_ANON_KEY="anon-key", STUDIO_STORE="local")
    assert s.store_driver == "local"
    assert s.supabase_auth_configured is True
    assert not any("Nobody can sign in" in p["what"] for p in s.config_problems())


def test_a_degraded_store_is_reported_rather_than_hidden(monkeypatch):
    """STUDIO_STORE=supabase quietly became local; that must be visible."""
    s = _deployed_settings(monkeypatch, STUDIO_STORE="supabase",
                           SUPABASE_URL="https://x.supabase.co", SUPABASE_ANON_KEY="anon-key")
    assert s.store_driver == "local"
    assert "SUPABASE_SERVICE_ROLE_KEY" in s.store_fallback_reason
    assert any("local store is in use" in p["what"] for p in s.config_problems())


def test_auth_backend_follows_supabase_config_only(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://x.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-key")
    monkeypatch.setattr(settings, "store_driver", "local")
    assert auth.auth_backend() == "Supabase Auth"
    monkeypatch.setattr(settings, "supabase_url", "")
    assert auth.auth_backend() == "Local administrator"


# ── password recovery ──────────────────────────────────────────────────────

def test_recovery_needs_supabase(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "")
    with pytest.raises(auth.AuthError, match="Supabase Auth"):
        auth.request_password_reset("someone@example.test", "https://markevita.com/studio/reset")


def test_recovery_does_not_reveal_whether_an_account_exists(monkeypatch, isolated_store):
    """The response must not vary with the address, or the page becomes an
    account-enumeration oracle."""
    monkeypatch.setattr(settings, "supabase_url", "https://x.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-key")
    sent = []

    class _Resp:
        status_code = 400
        def json(self): return {"msg": "User not found"}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (sent.append(k), _Resp())[1])
    auth.request_password_reset("nobody@example.test", "https://markevita.com/studio/reset")
    assert sent, "the recovery request is still sent"
    # No exception, no distinguishing return value.


def test_recovery_transport_failure_is_swallowed(monkeypatch, isolated_store):
    monkeypatch.setattr(settings, "supabase_url", "https://x.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-key")
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx, "post", boom)
    auth.request_password_reset("someone@example.test", "https://markevita.com/studio/reset")


def test_short_new_password_is_rejected_before_any_call(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "put", lambda *a, **k: pytest.fail("must not call Supabase"))
    with pytest.raises(auth.AuthError, match="at least 8"):
        auth.update_password("token", "short")


def test_invalid_recovery_token_is_reported(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://x.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-key")

    class _Resp:
        status_code = 401
        def json(self): return {"msg": "Token has expired"}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(auth.AuthError, match="expired"):
        auth.verify_recovery_token("bad-token")


def test_sign_in_reports_bad_credentials_plainly(monkeypatch, isolated_store):
    monkeypatch.setattr(settings, "supabase_url", "https://x.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon-key")

    class _Resp:
        status_code = 400
        def json(self): return {"error_description": "Invalid login credentials"}

    import httpx
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    with pytest.raises(auth.AuthError, match="Invalid email or password"):
        auth.sign_in("someone@example.test", "wrong")


# ── session cookie ─────────────────────────────────────────────────────────

class _FakeRequest:
    def __init__(self, scheme, forwarded=None):
        from types import SimpleNamespace
        self.url = SimpleNamespace(scheme=scheme)
        self.headers = {"x-forwarded-proto": forwarded} if forwarded else {}


def test_cookie_is_secure_behind_an_https_proxy():
    """The app speaks HTTP behind Netlify and Render; only the forwarded
    scheme reveals that the browser is on HTTPS."""
    assert auth.cookie_is_secure(_FakeRequest("http", "https")) is True
    assert auth.cookie_is_secure(_FakeRequest("http", "https, http")) is True


def test_cookie_is_not_secure_on_plain_local_http():
    """A Secure cookie over local HTTP is never sent back, which looks like a
    login that silently fails."""
    assert auth.cookie_is_secure(_FakeRequest("http")) is False


def test_cookie_is_secure_on_direct_https():
    assert auth.cookie_is_secure(_FakeRequest("https")) is True


def test_non_ascii_credentials_fail_cleanly(monkeypatch, isolated_store):
    """compare_digest rejects non-ASCII str, which would raise instead of
    returning a normal 'invalid password'."""
    monkeypatch.setattr(settings, "admin_email", "admin@example.test")
    monkeypatch.setattr(settings, "admin_password", "test-password")
    with pytest.raises(auth.AuthError):
        auth.sign_in("админ@example.test", "пароль")


# ── public path prefix (markevita.com/studio) ──────────────────────────────

def test_base_path_is_normalised():
    from app.config import Settings
    for raw, expected in [("/studio/", "/studio"), ("studio", "/studio"),
                          ("", ""), ("   ", ""), ("/", "")]:
        os.environ["STUDIO_BASE_PATH"] = raw
        assert Settings.load().base_path == expected, raw
    os.environ.pop("STUDIO_BASE_PATH", None)


def test_url_helper_prefixes_only_when_configured():
    from app.config import Settings
    os.environ["STUDIO_BASE_PATH"] = "/studio"
    prefixed = Settings.load()
    assert prefixed.url("/login") == "/studio/login"
    assert prefixed.url("/") == "/studio/"
    os.environ.pop("STUDIO_BASE_PATH", None)
    assert Settings.load().url("/login") == "/login"


def test_every_template_url_carries_the_prefix():
    """A hardcoded internal link would break the panel under /studio."""
    import re
    for f in (STUDIO / "app" / "templates").glob("*.html"):
        bare = re.findall(r'(?:href|action)="/(?!/)[^"]*', f.read_text())
        assert not bare, f"{f.name} has unprefixed URLs: {bare}"


# ── auth ───────────────────────────────────────────────────────────────────

def test_session_cookie_round_trips_and_rejects_tampering():
    token = auth.serialize({"email": "admin@example.test", "role": "owner"})
    assert auth.deserialize(token)["email"] == "admin@example.test"
    assert auth.deserialize(token[:-4] + "aaaa") is None


def test_wrong_password_is_rejected(isolated_store):
    with pytest.raises(auth.AuthError):
        auth.sign_in("admin@example.test", "not-the-password")


def test_correct_local_password_is_accepted(isolated_store):
    session = auth.sign_in("admin@example.test", "test-password")
    assert session["email"] == "admin@example.test"


# ── end to end, exactly as the panel drives it ─────────────────────────────

@pytest.mark.slow
def test_full_mock_production_run(isolated_store, tmp_path, monkeypatch):
    """Seed → validate → run to the approval gate → approve → finish complete.

    Uses the real engine in mock mode: no provider is contacted and no money
    is spent, but every stage really executes, including ffmpeg assembly.
    """
    import time

    monkeypatch.setattr(settings, "package_dir", tmp_path / "packages")
    runs = tmp_path / "runs"
    monkeypatch.setattr(runner, "RUNS_ROOT", runs)

    sys.path.insert(0, str(STUDIO / "seed"))
    import island_of_no_witnesses as seed_module
    monkeypatch.setattr(seed_module, "store", isolated_store)
    seed_module.seed()
    sid, eid = seed_module.SERIES_ID, seed_module.EPISODE_ID

    result = runner.validate_series(sid)
    assert result["ok"], result
    assert result["episodes"][0]["clips"] == 12
    assert 90 <= result["episodes"][0]["seconds"] <= 120

    def run_to_rest(stages):
        job = runner.jobs.start(sid, eid, stages, "tester")
        for _ in range(600):
            time.sleep(1)
            current = isolated_store.get("production_jobs", {"id": job["id"]})
            if current["state"] in ("done", "failed", "cancelled", "paused"):
                return current
        raise AssertionError("job did not settle in time")

    # The references gate must stop the run rather than spending on video.
    blocked = run_to_rest(["intake", "direction", "references", "keyframes"])
    assert blocked["state"] == "failed"
    assert "References page" in (blocked["error"] or "")

    runner.approve_references(sid, "tester", "test approval")
    finished = run_to_rest(runner.DEFAULT_STAGES)
    assert finished["state"] == "done", finished.get("error")

    runtime = runner.episode_runtime(sid, eid)
    assert runtime["status"] == "complete"
    assert runtime["spent_usd"] > 0
    assert runtime["qa"] and runtime["qa"][0]["pass"] is True

    master = runs / sid / eid / "out" / "masters" / "v1" / "episode.mp4"
    assert master.exists() and master.stat().st_size > 100_000
    assert (master.parent / "episode.srt").exists()
    assert (master.parent / "manifest.json").exists()

    # Approval is recorded; publishing stays off.
    runner.approve_publish(sid, eid, "tester", "verified")
    assert runner.episode_runtime(sid, eid)["approvals"]["publish"]["approved"] is True

    # Re-running costs nothing more: completed stages are skipped.
    before = runner.episode_runtime(sid, eid)["spent_usd"]
    again = run_to_rest(runner.DEFAULT_STAGES)
    assert again["state"] == "done"
    assert runner.episode_runtime(sid, eid)["spent_usd"] == before
