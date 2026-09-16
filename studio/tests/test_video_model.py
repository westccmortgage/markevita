"""Choosing Veo 3.1 or Veo 3.1 Fast once, for a whole series.

The two endpoints take the same request and differ in price and quality. What
has to hold is that the choice is made once per series and inherited, that the
figure shown before a run is the chosen model's figure, that a saved provider
request is collected from the model that accepted it, and that assigned
character voices are never laid under Veo's own speech.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from serial import costs  # noqa: E402
from serial.config import DEFAULT_VIDEO_MODEL, VIDEO_MODELS, Config  # noqa: E402

FAST = "fal-ai/veo3.1/fast/image-to-video"
FULL = "fal-ai/veo3.1/image-to-video"


# ── price ──────────────────────────────────────────────────────────────────

def test_both_models_are_offered():
    assert set(VIDEO_MODELS) == {FAST, FULL}
    assert DEFAULT_VIDEO_MODEL == FAST


@pytest.mark.parametrize("audio,resolution,fast,full", [
    (False, "1080p", 0.10, 0.20),
    (True, "1080p", 0.15, 0.40),
    (False, "4k", 0.30, 0.40),
    (True, "4k", 0.35, 0.60),
])
def test_each_model_is_billed_at_its_own_published_rate(audio, resolution, fast, full):
    assert costs.video_cost(1, audio, resolution, FAST) == pytest.approx(fast)
    assert costs.video_cost(1, audio, resolution, FULL) == pytest.approx(full)


def test_the_full_model_is_never_priced_at_the_fast_rate():
    """Half the real price would put a run over an approved budget."""
    for seconds in (4, 6, 8):
        assert costs.video_cost(seconds, False, "1080p", FULL) == pytest.approx(
            2 * costs.video_cost(seconds, False, "1080p", FAST))


def test_an_unpriced_model_is_refused_rather_than_billed_as_fast():
    with pytest.raises(costs.UnknownVideoModel, match="No published price"):
        costs.video_cost(8, False, "1080p", "fal-ai/some/other-model")


def test_an_unsupported_model_in_the_environment_is_refused(monkeypatch):
    monkeypatch.setenv("FAL_VIDEO_MODEL", "fal-ai/veo3.1/text-to-video")
    with pytest.raises(ValueError, match="not supported"):
        Config.load(STUDIO.parent / "pipeline", live=False)


# ── the choice belongs to the series ───────────────────────────────────────

def _package(series_limits):
    from types import SimpleNamespace
    return SimpleNamespace(series={"production_limits": series_limits})


def test_the_series_choice_is_what_a_run_uses():
    from app import live_jobs
    assert live_jobs.video_model(_package({"video_model": FULL})) == FULL


def test_a_series_that_never_chose_keeps_the_fast_model():
    from app import live_jobs
    assert live_jobs.video_model(_package({})) == DEFAULT_VIDEO_MODEL


def test_the_choice_is_part_of_the_package_schema():
    """So switching model changes the digest the producer approves."""
    from serial.schema import SERIES
    allowed = SERIES["properties"]["production_limits"]["properties"]["video_model"]["enum"]
    assert set(allowed) == {FAST, FULL}


def test_the_fast_model_stays_the_fallback_when_a_series_never_chose():
    """New series start on the full model; a package without the setting — an
    older one, or one built elsewhere — still runs rather than failing."""
    from app import live_jobs
    from types import SimpleNamespace
    assert live_jobs.video_model(SimpleNamespace(series={})) == DEFAULT_VIDEO_MODEL
    assert DEFAULT_VIDEO_MODEL == FAST


def test_the_estimate_follows_the_model_and_not_the_server_default():
    """A figure priced at the Fast rate would understate a Veo 3.1 run."""
    from serial.package import estimate_first_pass
    from types import SimpleNamespace

    norm = {"scenes": [{"duration": 8}, {"duration": 8}], "limits": {"budget": 50}}
    pkg = SimpleNamespace(characters={}, locations={}, props={})

    def cfg(model):
        return SimpleNamespace(video_generate_audio=False, video_resolution="1080p",
                               fal_video_model=model, image_resolution="1K",
                               lipsync_variant="lipsync-2")

    fast = estimate_first_pass(norm, pkg, cfg(FAST), False)
    full = estimate_first_pass(norm, pkg, cfg(FULL), False)
    assert full["video"] == pytest.approx(2 * fast["video"])
    assert full["total_first_pass"] > fast["total_first_pass"]


# ── assigned voices are never laid under Veo's own speech ──────────────────

@pytest.mark.parametrize("audio_mode,generates", [("native", True), ("voices", False)])
def test_assigned_voices_switch_off_the_models_own_speech(monkeypatch, audio_mode, generates):
    from app import live_jobs
    from app.config import settings
    monkeypatch.setattr(settings, "allow_paid", True)
    monkeypatch.setattr(settings, "store_driver", "supabase")
    monkeypatch.setenv("PIPELINE_ALLOW_PAID", "true")
    cfg = live_jobs.configuration(audio_mode, FULL)
    assert cfg.fal_video_model == FULL
    assert cfg.video_generate_audio is generates


def test_a_series_set_to_an_unknown_model_does_not_start(monkeypatch):
    from app import live_jobs
    from app.config import settings
    monkeypatch.setattr(settings, "allow_paid", True)
    monkeypatch.setattr(settings, "store_driver", "supabase")
    monkeypatch.setenv("PIPELINE_ALLOW_PAID", "true")
    with pytest.raises(ValueError, match="unsupported video model"):
        live_jobs.configuration("native", "fal-ai/veo3.1/text-to-video")


# ── a saved request belongs to the model that accepted it ──────────────────

def test_a_saved_request_is_collected_from_the_model_that_accepted_it():
    """Switching the series must not orphan a request already paid for."""
    from serial.providers import Fal
    from types import SimpleNamespace

    takes = {"sc01/video": {"status": "submitted", "request_id": "r-1", "endpoint": FAST}}
    state = SimpleNamespace(take=takes.get, save=lambda: None,
                            data={}, set_status=lambda *a: None)
    fal = Fal(SimpleNamespace(dry_run=False, fal_key="k"), lambda *a: None, state,
              SimpleNamespace(reserve=lambda *a: None, settle=lambda *a: None), None)
    polled = {}

    def _wait(endpoint, request_id):
        polled["endpoint"] = endpoint
        return {"video": {"url": "u"}}

    fal._wait = _wait
    fal.run(FULL, {"prompt": "p"}, "sc01/video", 1.0, "video sc01", stub=dict)
    assert polled["endpoint"] == FAST
    assert takes["sc01/video"]["endpoint"] == FAST


def test_the_live_transport_also_polls_the_original_model():
    """The queue path a real run uses, not only the mock one."""
    from app.live_providers import DurableFal
    from types import SimpleNamespace

    takes = {"sc01/video": {"status": "submitted", "request_id": "r-1", "endpoint": FAST,
                            "key_id": "abc"}}
    settled = []
    state = SimpleNamespace(take=takes.get, save=lambda: None, data={},
                            set_status=lambda *a: None)
    fal = DurableFal(SimpleNamespace(dry_run=False, fal_key="abc:secret"), lambda *a: None,
                     state, SimpleNamespace(reserve=lambda *a: None,
                                            settle=lambda *a: settled.append(a)), None)
    polled = {}
    fal._wait = lambda endpoint, request_id: polled.setdefault("endpoint", endpoint) or {"video": {}}
    fal.run(FULL, {"prompt": "p"}, "sc01/video", 1.0, "video sc01", stub=dict)
    assert polled["endpoint"] == FAST
    assert takes["sc01/video"]["endpoint"] == FAST


# ── picture quality, and what it actually changes ──────────────────────────

def _pkg(limits):
    from types import SimpleNamespace
    return SimpleNamespace(series={"production_limits": limits})


def test_the_series_chooses_the_picture(monkeypatch):
    from app import live_jobs
    assert live_jobs.picture(_pkg({"picture": "maximum"})) == "maximum"
    assert live_jobs.picture(_pkg({})) == "standard"
    assert live_jobs.picture(_pkg({"picture": "cinema"})) == "standard"


@pytest.mark.parametrize("level,video,image,lipsync", [
    ("standard", "1080p", "1K", "lipsync-2"),
    ("high", "1080p", "2K", "lipsync-2-pro"),
    ("maximum", "4k", "2K", "lipsync-2-pro"),
])
def test_each_level_sets_every_knob(monkeypatch, level, video, image, lipsync):
    from app import live_jobs
    from app.config import settings
    monkeypatch.setattr(settings, "allow_paid", True)
    monkeypatch.setattr(settings, "store_driver", "supabase")
    monkeypatch.setenv("PIPELINE_ALLOW_PAID", "true")
    cfg = live_jobs.configuration("native", FULL, level)
    assert (cfg.video_resolution, cfg.image_resolution, cfg.lipsync_variant) == (video, image, lipsync)


def test_an_unknown_picture_level_does_not_start(monkeypatch):
    from app import live_jobs
    from app.config import settings
    monkeypatch.setattr(settings, "allow_paid", True)
    monkeypatch.setattr(settings, "store_driver", "supabase")
    monkeypatch.setenv("PIPELINE_ALLOW_PAID", "true")
    with pytest.raises(ValueError, match="unknown picture quality"):
        live_jobs.configuration("native", FULL, "cinema")


def test_maximum_costs_more_than_standard_on_the_same_episode():
    """The producer is told what the choice buys before approving it."""
    from serial import costs
    from app.live_jobs import PICTURE
    def episode(level, endpoint):
        p = PICTURE[level]
        return (costs.video_cost(180, True, p["video_resolution"], endpoint)
                + 23 * costs.image_cost(False, p["image_resolution"]))
    assert episode("maximum", FULL) > episode("high", FULL) > episode("standard", FAST)


def test_three_minutes_is_what_a_new_series_asks_for():
    from app.packaging import DEFAULT_LIMITS
    assert DEFAULT_LIMITS["min_episode_seconds"] >= 180
    # Enough scenes to fill it at the longest clip the models make.
    assert DEFAULT_LIMITS["max_scenes"] * 8 >= DEFAULT_LIMITS["max_episode_seconds"]
    assert DEFAULT_LIMITS["min_scenes"] * 8 >= DEFAULT_LIMITS["min_episode_seconds"]


def test_the_language_model_is_priced_from_the_model_that_answered():
    from serial.costs import UnknownLanguageModel, anthropic_rates
    assert anthropic_rates("claude-opus-5") == (5.0, 25.0)
    assert anthropic_rates("claude-sonnet-5") == (2.0, 10.0)
    with pytest.raises(UnknownLanguageModel, match="No published price"):
        anthropic_rates("claude-imaginary-9")


def test_a_new_series_is_set_up_for_the_best_picture():
    """Asked for, explicitly: maximum, and a budget that can pay for it."""
    from app.packaging import DEFAULT_LIMITS
    from app.live_jobs import PICTURE
    assert DEFAULT_LIMITS["video_model"] == FULL
    assert DEFAULT_LIMITS["picture"] == "maximum"
    assert PICTURE[DEFAULT_LIMITS["picture"]]["video_resolution"] == "4k"
    # A three-minute 4K episode costs well over a hundred; the series budget
    # must not be the thing that stops it.
    from serial import costs
    seconds = DEFAULT_LIMITS["min_episode_seconds"]
    video = costs.video_cost(seconds, True, "4k", FULL)
    assert DEFAULT_LIMITS["maximum_episode_budget_usd"] >= video


# ── preflight must accept every model the studio can bill and send ─────────

def _config(video_model):
    """A real Config, not a hand-rolled stand-in: the gate reads fields a fake
    keeps forgetting, which is how the fast-only check survived unnoticed."""
    from serial.config import Config
    cfg = Config(fal_video_model=video_model, fal_key="", elevenlabs_api_key="x",
                  image_resolution="2K", video_resolution="4k",
                  lipsync_variant="lipsync-2-pro",
                  r2_account_id="a", r2_access_key_id="b", r2_secret_access_key="c",
                  r2_bucket="d", anthropic_api_key="e")
    cfg.native_dialogue = True   # set by the studio at run time, not a Config field
    return cfg


def _series_package():
    from types import SimpleNamespace
    return SimpleNamespace(series={"format": {"aspect_ratio": "9:16"}},
                           limits=lambda cfg: {"budget": 150.0})


@pytest.mark.parametrize("model", [FAST, FULL])
def test_preflight_accepts_both_veo_endpoints(model):
    """Choosing Veo 3.1 in the settings was refused at the gate by a check
    that still named only the fast endpoint."""
    from app import preflight
    errors = preflight.problems(_config(model), ["video"], _series_package())
    assert not [e for e in errors if "FAL_VIDEO_MODEL" in e], errors


def test_preflight_still_refuses_a_model_with_no_adapter():
    from app import preflight
    errors = preflight.problems(_config("fal-ai/some/other-model"), ["video"], _series_package())
    assert any("FAL_VIDEO_MODEL" in e for e in errors)
