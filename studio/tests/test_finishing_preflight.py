"""What finishing would cost, computed without starting any of it.

Video repair and finishing are different work with different money attached.
The authorization for one must never carry the other, and the only way to know
that before paying is to be shown the plan first.
"""
import sys
from pathlib import Path

import pytest

STUDIO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from app import core_v2  # noqa: E402

SERIES, EPISODE = "island_of_no_witnesses", "s01e04"


def _scene(scene_id, sequence, seconds=6, lines=("a line",)):
    return {"series_id": SERIES, "episode_id": EPISODE, "scene_id": scene_id,
            "sequence": sequence, "duration_seconds": seconds, "status": "complete",
            "dialogue": [{"speaker": "nora", "text": t} for t in lines], "qa": {}}


class _Store:
    def __init__(self, scenes, done, spent=5.47):
        self._scenes, self._done, self._spent = scenes, done, spent

    def get(self, table, where):
        if table == "episodes":
            return {"series_id": SERIES, "episode_id": EPISODE, "brief": {},
                    "status": "failed_qa", "spent_usd": self._spent}
        return None

    def list(self, table, where=None, order=None, desc=False, limit=None, offset=0):
        if table == "scenes":
            return list(self._scenes)
        if table == "production_jobs":
            return [{"id": "5412a1da", "series_id": SERIES, "episode_id": EPISODE,
                     "state": "failed", "log": "",
                     "progress": {"done": list(self._done)}}]
        return []


@pytest.fixture
def plan_for(monkeypatch):
    def build(scenes, done, spent=5.47, budget=35.0):
        monkeypatch.setattr(core_v2, "store", _Store(scenes, done, spent))
        from app import progress as progressmod
        monkeypatch.setattr(progressmod, "ledger",
                            lambda s, e: {"internal_actual": spent})
        monkeypatch.setattr(progressmod, "budget", lambda s, e: budget)
        return core_v2.finishing_plan(SERIES, EPISODE)
    return build


SCENES = [_scene("sc01", 1), _scene("sc02", 2, seconds=8)]


def test_the_plan_starts_nothing_and_calls_no_provider(plan_for, monkeypatch):
    """The whole point of a preflight: it is safe to look at."""
    import serial.providers as providers
    for name in ("gen_video", "gen_image", "lipsync", "tts"):
        monkeypatch.setattr(providers, name, _forbidden(name), raising=False)
    plan = plan_for(SCENES, done=["intake", "direction", "references", "keyframes", "video"])
    assert plan["projected_finishing_usd"] >= 0


def _forbidden(name):
    def _raise(*a, **kw):
        raise AssertionError(f"{name} must not be called while planning")
    return _raise


def test_finishing_never_includes_publication(plan_for):
    plan = plan_for(SCENES, done=["video"])
    assert plan["publication"]["included"] is False
    assert plan["publication"]["enabled"] is False
    assert plan["publication"]["needs_separate_confirmation"] is True
    assert "publish" not in [s["stage"] for s in plan["stages"]]
    assert "publish" in plan["not_run"]


def test_the_repair_authorization_does_not_pay_for_finishing(plan_for):
    plan = plan_for(SCENES, done=["video"])
    assert plan["repair_authorization_applies"] is False
    assert plan["input_digest"]
    assert plan["automatic_fallback"] is False


def test_finishing_carries_its_own_digest_and_ceiling(plan_for):
    """A new digest and a new ceiling, or the repair receipt would cover work
    the producer never saw priced."""
    plan = plan_for(SCENES, done=["video"], spent=5.47, budget=35.0)
    assert plan["remaining_budget_usd"] == 29.53
    assert plan["projected_finishing_usd"] <= plan["remaining_budget_usd"]
    assert plan["policy"] == core_v2.FINISHING_POLICY


def test_every_stage_says_whether_it_is_needed_paid_and_resumable(plan_for):
    plan = plan_for(SCENES, done=["video"])
    assert [s["stage"] for s in plan["stages"]] == list(core_v2.FINISHING_STAGES)
    for stage in plan["stages"]:
        for key in ("needed", "paid", "provider", "estimated_usd", "reuses",
                    "on_failure", "resumable", "needs_confirmation"):
            assert key in stage, (stage["stage"], key)
        assert stage["needs_confirmation"] is True


def test_a_stage_already_done_is_not_offered_again(plan_for):
    plan = plan_for(SCENES, done=["video", "voice", "lipsync"])
    offered = {s["stage"] for s in plan["stages"] if s["needed"]}
    assert offered == {"assemble", "qa", "deliver"}
    assert set(plan["skipped_stages"]) >= {"voice", "lipsync"}
    assert plan["paid_stages"] == []


def test_assembly_qa_and_delivery_are_free_and_said_to_be(plan_for):
    plan = plan_for(SCENES, done=["video"])
    free = {s["stage"] for s in plan["stages"] if not s["paid"]}
    assert free == {"assemble", "qa", "deliver"}
    assert all(s["estimated_usd"] == 0.0 for s in plan["stages"] if not s["paid"])


def test_speech_is_paid_and_an_unset_rate_is_unknown_not_free(plan_for):
    """The published per-character rate is not in the price list. Calling the
    amount zero would put a real charge inside a ceiling that never allowed
    for it."""
    plan = plan_for(SCENES, done=["video"])
    voice = next(s for s in plan["stages"] if s["stage"] == "voice")
    assert voice["paid"] is True
    if not voice["cost_known"]:
        assert voice["estimated_usd"] is None
        assert "not set" in voice["cost_note"]
        assert "voice" in plan["cost_unknown_stages"]
    assert isinstance(plan["projected_finishing_usd"], float)


def test_a_held_frame_is_not_paid_to_have_its_lips_synced(plan_for, monkeypatch):
    """A motion-still has nothing moving. Paying to animate a mouth on it buys
    an uncanny face on a deliberate shot."""
    plan = plan_for(SCENES, done=["video"])
    with_still = dict(plan)
    monkeypatch.setattr(core_v2, "_snapshot", lambda s, e: {
        "episode": {"spent_usd": 0.0}, "scenes": SCENES, "takes": [], "costs": [],
        "log_qc": {}, "completed_motion_stills": {"sc02"}})
    held = core_v2.finishing_plan(SERIES, EPISODE)
    lip_all = next(s for s in with_still["stages"] if s["stage"] == "lipsync")
    lip_held = next(s for s in held["stages"] if s["stage"] == "lipsync")
    assert lip_held["estimated_usd"] < lip_all["estimated_usd"]


def test_finishing_is_blocked_until_video_repair_is_complete(plan_for):
    unfinished = plan_for(SCENES, done=["keyframes"])
    assert unfinished["video_repair_complete"] is False
    assert unfinished["blocked"] is True
    ready = plan_for(SCENES, done=["video"])
    assert ready["video_repair_complete"] is True and ready["blocked"] is False


# ── the boundary between repair money and finishing money ─────────────────

def test_finishing_never_starts_video_or_publish(monkeypatch):
    """An approval for finishing must not be able to buy another clip, and
    must not be able to publish."""
    import inspect
    src = inspect.getsource(core_v2.approve_finishing)
    assert "list(FINISHING_STAGES)" in src
    assert "publish" not in core_v2.FINISHING_STAGES
    assert "video" not in core_v2.FINISHING_STAGES


def test_a_repair_receipt_cannot_authorise_finishing():
    """Different subject types, so no repair approval row ever satisfies a
    finishing check."""
    assert core_v2.FINISHING_APPROVAL != core_v2.SUPERVISED_APPROVAL


def test_a_stale_plan_is_refused_rather_than_run(monkeypatch):
    monkeypatch.setattr(core_v2, "finishing_plan", lambda s, e: {
        "input_digest": "current", "blocked": False, "stages": [],
        "projected_finishing_usd": 0.0, "remaining_budget_usd": 10.0,
        "cost_unknown_stages": []})
    with pytest.raises(ValueError, match="changed since this plan was shown"):
        core_v2.approve_finishing(SERIES, EPISODE, actor="a@b.test",
                                  input_digest="stale", max_incremental_usd=10.0)


def test_finishing_is_refused_while_video_repair_is_incomplete(monkeypatch):
    monkeypatch.setattr(core_v2, "finishing_plan", lambda s, e: {
        "input_digest": "d", "blocked": True, "stages": [],
        "projected_finishing_usd": 0.0, "remaining_budget_usd": 10.0,
        "cost_unknown_stages": []})
    with pytest.raises(ValueError, match="Video repair is not complete"):
        core_v2.approve_finishing(SERIES, EPISODE, actor="a@b.test",
                                  input_digest="d", max_incremental_usd=10.0)


def test_a_ceiling_below_the_estimate_is_refused(monkeypatch):
    monkeypatch.setattr(core_v2, "finishing_plan", lambda s, e: {
        "input_digest": "d", "blocked": False,
        "stages": [{"stage": "lipsync", "needed": True}],
        "projected_finishing_usd": 4.00, "remaining_budget_usd": 29.53,
        "cost_unknown_stages": []})
    with pytest.raises(ValueError, match=r"at least the projected finishing estimate of \$4.00"):
        core_v2.approve_finishing(SERIES, EPISODE, actor="a@b.test",
                                  input_digest="d", max_incremental_usd=1.00)


def test_an_estimate_over_the_remaining_budget_is_refused(monkeypatch):
    monkeypatch.setattr(core_v2, "finishing_plan", lambda s, e: {
        "input_digest": "d", "blocked": False,
        "stages": [{"stage": "lipsync", "needed": True}],
        "projected_finishing_usd": 40.0, "remaining_budget_usd": 29.53,
        "cost_unknown_stages": []})
    with pytest.raises(ValueError, match="exceeds the remaining episode budget"):
        core_v2.approve_finishing(SERIES, EPISODE, actor="a@b.test",
                                  input_digest="d", max_incremental_usd=40.0)


# ── Resume must not quietly re-buy an accepted scene ──────────────────────

def test_resume_does_not_carry_a_repair_token_forward(monkeypatch):
    """The approval rows that admit a repair token are never retired, so a
    Resume that carried the previous job's force list would regenerate scenes
    the producer had since accepted by hand — at full price and without
    asking."""
    from app import runner

    jobs = [{"id": "5412a1da", "state": "failed", "stages": ["video"],
             "force": ["video_retry:sc02:r1", "motion_still:sc04", "sc12"]}]

    class _Store:
        def list(self, table, where=None, order=None, desc=False, limit=None, offset=0):
            return list(jobs) if table == "production_jobs" else []

    started = {}
    monkeypatch.setattr(runner, "store", _Store())
    monkeypatch.setattr(runner.JobManager, "start",
                        lambda self, s, e, stages, by, force, **kw: started.update(
                            stages=stages, force=force) or {"id": "new"})
    runner.JobManager().resume(SERIES, EPISODE, "a@b.test")
    assert started["force"] == [], "a repair token belongs to the job it was approved for"
    assert started["stages"] == ["video"]


def test_resume_keeps_a_force_item_that_is_not_a_repair_token(monkeypatch):
    from app import runner
    jobs = [{"id": "j", "state": "interrupted", "stages": ["video"], "force": ["references"]}]

    class _Store:
        def list(self, table, where=None, order=None, desc=False, limit=None, offset=0):
            return list(jobs) if table == "production_jobs" else []

    started = {}
    monkeypatch.setattr(runner, "store", _Store())
    monkeypatch.setattr(runner.JobManager, "start",
                        lambda self, s, e, stages, by, force, **kw: started.update(force=force) or {})
    runner.JobManager().resume(SERIES, EPISODE, "a@b.test")
    assert started["force"] == ["references"]
