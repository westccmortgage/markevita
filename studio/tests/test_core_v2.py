"""Measured Decision Core V2 starts in evidence-only Shadow Mode."""
from __future__ import annotations

from app import core_v2
from app.store.local import LocalDriver


def _seed(tmp_path, monkeypatch):
    store = LocalDriver(tmp_path / "store")
    monkeypatch.setattr(core_v2, "store", store)
    store.insert("series", {"id": "wild", "title": "Wild"})
    store.insert("episodes", {
        "series_id": "wild", "episode_id": "s01e04", "season_id": "s01",
        "number": 4, "title": "Road", "status": "failed_qa",
        "brief": {"video_routing": {"mode": "manual_after_qc", "scenes": {
            "sc01": {"mode": "motion_still"},
            "sc02": {"mode": "video", "primary": "veo", "fallbacks": ["kling"]},
            "sc03": {"mode": "video", "primary": "veo", "fallbacks": ["kling"]},
            "sc04": {"mode": "video", "primary": "veo", "fallbacks": ["kling"]},
        }}},
        "opening_state": {}, "cliffhanger": {}, "budget_usd": 35,
        "spent_usd": 10.39,
    })
    for i, (action, lens) in enumerate((
        ("They leave the trees.", "35mm"),
        ("The hunter walks through the gorge.", "35mm"),
        ("The wildcat holds an intelligent gaze.", "85mm"),
        ("The wildcat runs up the ridge.", "50mm"),
    ), 1):
        store.insert("scenes", {
            "series_id": "wild", "episode_id": "s01e04", "scene_id": f"sc{i:02d}",
            "sequence": i, "duration_seconds": 6, "location": "ridge",
            "characters_in_frame": [], "wardrobe": {}, "action": action,
            "dialogue": [], "lens": lens,
        })
    store.insert("takes", {
        "series_id": "wild", "episode_id": "s01e04", "scene_id": "sc02",
        "take_id": "live:s01e04_sc02_vid_00", "stage": "vid", "endpoint": "veo",
        "actual_usd": 1.1, "selected": True,
        "qc": {"pass": True, "score": 7, "issues": ["minor blur"]},
    })
    store.insert("takes", {
        "series_id": "wild", "episode_id": "s01e04", "scene_id": "sc03",
        "take_id": "live:s01e04_sc03_vid_00", "stage": "vid", "endpoint": "veo",
        "actual_usd": 1.2, "selected": True,
        "qc": {"pass": False, "score": 5,
               "issues": ["wildcat face and tail anatomy drift"]},
    })
    store.insert("takes", {
        "series_id": "wild", "episode_id": "s01e04", "scene_id": "sc04",
        "take_id": "live:s01e04_sc04_vid_00", "stage": "vid", "endpoint": "veo",
        "actual_usd": 1.3, "selected": True,
        "qc": {"pass": False, "score": 5,
               "issues": ["camera smear and body scale drift"]},
    })
    return store


def test_shadow_mode_makes_scene_decisions_without_production_changes(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)

    report = core_v2.run_shadow_analysis("wild", "s01e04", actor="owner@example.test")

    assert report["core"] == "measured-decision-core-v2"
    assert report["mode"] == "shadow"
    assert report["summary"]["ready"] == 2
    assert report["summary"]["repair"] == 2
    assert report["summary"]["projected_repair_usd"] == 1.3
    decisions = {row["scene_id"]: row for row in report["decisions"]}
    assert decisions["sc01"]["recommended_action"] == "keep_motion_still"
    assert decisions["sc02"]["recommended_action"] == "accept_existing_take"
    assert decisions["sc03"]["recommended_action"] == "convert_to_motion_still"
    assert decisions["sc04"]["recommended_action"] == "simplify_action_then_retry"
    assert decisions["sc04"]["evidence"]["next_provider"] == "kling"
    assert report["guardrails"] == {
        "paid_calls": False,
        "take_selection_changes": False,
        "automatic_approvals": False,
        "publication": False,
    }
    assert len(store.list("takes")) == 3
    assert len(store.list("approvals")) == 0


def test_shadow_report_is_idempotent_for_the_same_evidence(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)

    first = core_v2.run_shadow_analysis("wild", "s01e04")
    second = core_v2.run_shadow_analysis("wild", "s01e04")

    assert first["input_digest"] == second["input_digest"]
    assert second["reused"] is True
    assert len(store.list("generation_history", {"event": core_v2.EVENT})) == 1


def test_new_qc_evidence_creates_a_new_shadow_report(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    first = core_v2.run_shadow_analysis("wild", "s01e04")
    store.update("takes", {"series_id": "wild", "take_id": "live:s01e04_sc04_vid_00"},
                 {"qc": {"pass": True, "score": 8, "issues": []}})

    second = core_v2.run_shadow_analysis("wild", "s01e04")

    assert first["input_digest"] != second["input_digest"]
    assert second["summary"]["ready"] == 3
    assert len(store.list("generation_history", {"event": core_v2.EVENT})) == 2


def test_old_video_qc_is_recovered_from_the_immutable_job_log(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    store.update("takes", {"series_id": "wild", "take_id": "live:s01e04_sc02_vid_00"},
                 {"qc": {}})
    store.insert("production_jobs", {
        "series_id": "wild", "episode_id": "s01e04", "state": "failed",
        "mode": "live", "idempotency_key": "legacy-run", "created_at": "2026-09-22T00:00:00Z",
        "log": "[00:01:00] video: sc02 veo QC 7 OK ['minor blur']",
    })

    report = core_v2.run_shadow_analysis("wild", "s01e04")

    decision = next(row for row in report["decisions"] if row["scene_id"] == "sc02")
    assert decision["verdict"] == "ready"
    assert decision["evidence"]["qc_score"] == 7


def test_supervised_plan_uses_same_engine_retry_and_free_motion_still(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    report = core_v2.run_shadow_analysis("wild", "s01e04", actor="owner@example.test")

    force = core_v2.supervised_force(
        report,
        store.get("episodes", {"series_id": "wild", "episode_id": "s01e04"})["brief"],
    )

    assert force == ["motion_still:sc03", "video_retry:sc04:r0"]
    assert not any(token.startswith("video:sc04:r1") for token in force)


def test_supervised_repair_records_exact_tokens_and_starts_narrow_job(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    report = core_v2.run_shadow_analysis("wild", "s01e04", actor="owner@example.test")
    from app import live_jobs, packaging, runner
    from serial import package as serial_package

    calls = []
    monkeypatch.setattr(packaging, "materialize", lambda series_id: tmp_path / series_id)
    monkeypatch.setattr(serial_package, "SeriesPackage", lambda path: object())
    monkeypatch.setattr(live_jobs, "package_digest", lambda package, episode_id: "package-digest")
    monkeypatch.setattr(runner.jobs, "start", lambda *args, **kwargs:
                        calls.append((args, kwargs)) or {"id": "repair-job"})

    result = core_v2.approve_supervised_repair(
        "wild", "s01e04", actor="owner@example.test",
        input_digest=report["input_digest"], max_incremental_usd=1.3,
    )

    assert result["job"]["id"] == "repair-job"
    assert result["force"] == ["motion_still:sc03", "video_retry:sc04:r0"]
    args, kwargs = calls[0]
    assert args[2] == ["video", "voice", "lipsync", "assemble", "qa", "deliver"]
    assert args[4] == result["force"]
    assert kwargs == {"approved_digest": "package-digest", "approve_live": True,
                      "audio_mode": "voices"}
    token_rows = store.list("approvals", {"subject_type": "core_v2_repair_token"})
    assert {row["subject_id"] for row in token_rows} == set(result["force"])
    event = store.list("generation_history", {"event": core_v2.SUPERVISED_EVENT})[0]
    assert event["detail"]["automatic_fallback"] is False
    assert event["detail"]["publication"] is False


def test_supervised_repair_rejects_stale_or_underfunded_confirmation(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    report = core_v2.run_shadow_analysis("wild", "s01e04")

    import pytest
    with pytest.raises(ValueError, match="report changed"):
        core_v2.approve_supervised_repair(
            "wild", "s01e04", actor="owner@example.test",
            input_digest="stale", max_incremental_usd=1.3,
        )
    with pytest.raises(ValueError, match="Approve at least"):
        core_v2.approve_supervised_repair(
            "wild", "s01e04", actor="owner@example.test",
            input_digest=report["input_digest"], max_incremental_usd=1.29,
        )


def test_live_admission_accepts_only_recorded_core_repair_tokens(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    from app import live_jobs, runner
    import pytest

    monkeypatch.setattr(runner, "store", store)
    marker = RuntimeError("force tokens validated")
    monkeypatch.setattr(live_jobs, "materialize", lambda series_id: (_ for _ in ()).throw(marker))
    force = ["video_retry:sc04:r0", "motion_still:sc03"]

    with pytest.raises(PermissionError, match="recorded approval"):
        live_jobs.start(object(), "wild", "s01e04", ["video"],
                        "owner@example.test", force, "digest", True, "voices")

    for token in force:
        store.insert("approvals", {
            "series_id": "wild", "episode_id": "s01e04",
            "subject_type": "core_v2_repair_token", "subject_id": token,
            "decision": "approved", "actor": "owner@example.test", "note": "test",
        })
    with pytest.raises(RuntimeError, match="force tokens validated"):
        live_jobs.start(object(), "wild", "s01e04", ["video"],
                        "owner@example.test", force, "digest", True, "voices")


def test_two_same_engine_failures_stop_paid_retries(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    store.insert("takes", {
        "series_id": "wild", "episode_id": "s01e04", "scene_id": "sc04",
        "take_id": "live:s01e04_sc04_vid_r0_01", "stage": "vid", "endpoint": "veo",
        "actual_usd": 1.3, "selected": True,
        "qc": {"pass": False, "score": 4,
               "issues": ["camera smear and wildcat anatomy drift again"]},
    })

    report = core_v2.run_shadow_analysis("wild", "s01e04")
    decision = next(row for row in report["decisions"] if row["scene_id"] == "sc04")

    assert decision["recommended_action"] == "convert_to_motion_still"
    assert decision["estimated_incremental_usd"] == 0
    assert decision["evidence"]["same_engine_stop"] is True
    assert decision["evidence"]["failed_attempts"] == 2
    assert "Stop buying" in decision["reason"]


def test_completed_supervised_motion_still_is_ready_even_if_original_route_was_video(tmp_path, monkeypatch):
    store = _seed(tmp_path, monkeypatch)
    store.insert("production_jobs", {
        "series_id": "wild", "episode_id": "s01e04", "state": "failed",
        "mode": "live", "idempotency_key": "supervised", "created_at": "2026-09-22T10:00:00Z",
        "force": ["motion_still:sc04"],
        "log": "video: sc04 editorial motion-still; no paid video provider",
    })

    report = core_v2.run_shadow_analysis("wild", "s01e04")
    decision = next(row for row in report["decisions"] if row["scene_id"] == "sc04")

    assert decision["verdict"] == "ready"
    assert decision["recommended_action"] == "keep_motion_still"
    assert decision["evidence"]["evidence_source"] == "production_job_log"
