import copy
import json
import re
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from make_fixture import build  # noqa: E402
from serial import package as pkgmod  # noqa: E402
from serial import providers  # noqa: E402
from serial.config import Config  # noqa: E402
from serial.costs import BudgetExceeded  # noqa: E402
from serial.pipeline import Pipeline, STAGES, SeriesState  # noqa: E402
from serial.state import State, now  # noqa: E402


@pytest.fixture
def fx(tmp_path):
    root = build(tmp_path / "pkg", fast=True)
    return root


@pytest.fixture
def cfg():
    os.environ.pop("PIPELINE_ALLOW_PAID", None)
    return Config.load(HERE.parent, live=False)


def _pkg(root):
    return pkgmod.SeriesPackage(root)


def _ep(root, ep="s01e01"):
    return json.loads((root / "episodes" / ep / "brief.json").read_text())


def _write_ep(root, ep, obj):
    (root / "episodes" / ep / "brief.json").write_text(json.dumps(obj), encoding="utf-8")


# ---------------- package / validation ----------------

def test_package_loads_and_versions(fx):
    pkg = _pkg(fx)
    assert pkg.series["series_id"] == "fixture_series"
    assert len(pkg.bible_version) == 12
    assert pkg.previous_episode("s01e02") == "s01e01" and pkg.previous_episode("s01e01") is None


def test_split_two_visible_speakers(fx, cfg):
    pkg = _pkg(fx)
    norm = pkgmod.validate_episode(pkg, pkg.load_episode("s01e01"), None, cfg)
    ids = [s["scene_id"] for s in norm["scenes"]]
    assert "sc03a" in ids and "sc03b" in ids and "sc03" not in ids
    a = next(s for s in norm["scenes"] if s["scene_id"] == "sc03a")
    b = next(s for s in norm["scenes"] if s["scene_id"] == "sc03b")
    assert a["duration"] + b["duration"] == 8 and a["lipsync_speaker"] == "char_a" and b["lipsync_speaker"] == "char_b"
    assert b["split"] and "continues directly" in b["continuity_in"]


def test_voice_over_and_wardrobe_defaults(fx, cfg):
    pkg = _pkg(fx)
    norm = pkgmod.validate_episode(pkg, pkg.load_episode("s01e01"), None, cfg)
    vo = next(s for s in norm["scenes"] if s["scene_id"] == "sc04")
    assert vo["lipsync_speaker"] is None and vo["dialogue"][0]["voice_over"]
    assert vo["wardrobe"] == {"char_a": "w_day"}


def test_knowledge_ledger_and_end_state(fx, cfg):
    pkg = _pkg(fx)
    norm = pkgmod.validate_episode(pkg, pkg.load_episode("s01e01"), None, cfg)
    kinds = [(x["type"], x.get("character") or x.get("id")) for x in norm["ledger"]]
    assert ("knowledge", "char_a") in kinds and ("relationship", "rel_ab") in kinds
    assert norm["end_state"]["knowledge"]["secret_x"] == ["char_a", "char_b"]
    assert norm["end_state"]["relationships"]["rel_ab"] == "state_2"


@pytest.mark.parametrize("mutate,needle", [
    (lambda e: e["scenes"][0]["characters_in_frame"].append("nobody"), "unknown character"),
    (lambda e: e["scenes"][0]["characters_in_frame"].append("char_v"), "voice-only"),
    (lambda e: e["scenes"][0].update(location="loc_zzz"), "unknown location"),
    (lambda e: e["scenes"][0]["dialogue"].append({"speaker": "char_b", "text": " ".join(["word"] * 40)}), "too many for"),
    (lambda e: e["scenes"][3].update(knowledge_required=[{"character": "char_b", "secret": "secret_y"}]), "nobody told them yet"),
    (lambda e: e["cliffhanger"].update(scene_id="sc03"), "must be the last scene"),
    (lambda e: e["cliffhanger"].update(hook="  "), "hook is empty"),
    (lambda e: e["cliffhanger"].update(resolves_in="s01e01"), "same episode"),
    (lambda e: e["scenes"][4].update(relationship_changes=[{"id": "rel_ab", "state": "state_9"}]), "allowed_states"),
    (lambda e: e["scenes"][0].update(wardrobe={"char_a": "w_missing"}), "wardrobe"),
    (lambda e: e["scenes"][0].update(duration_seconds=4), "cannot be split"),
])
def test_episode_rejections(fx, cfg, mutate, needle):
    pkg = _pkg(fx)
    e = _ep(fx); mutate(e); _write_ep(fx, "s01e01", e)
    with pytest.raises(pkgmod.PackageError) as ei:
        pkgmod.validate_episode(pkg, pkg.load_episode("s01e01"), None, cfg)
    assert needle in str(ei.value)


def test_schema_rejects_bad_duration(fx, cfg):
    pkg = _pkg(fx)
    e = _ep(fx); e["scenes"][1]["duration_seconds"] = 5; _write_ep(fx, "s01e01", e)
    with pytest.raises(pkgmod.PackageError) as ei:
        pkg.load_episode("s01e01")
    assert "duration_seconds" in str(ei.value)


def test_cross_episode_opening_state(fx, cfg):
    pkg = _pkg(fx)
    e1 = pkgmod.validate_episode(pkg, pkg.load_episode("s01e01"), None, cfg)
    e2 = pkgmod.validate_episode(pkg, pkg.load_episode("s01e02"), e1["end_state"], cfg)   # matches
    assert e2["warnings"] == []
    with pytest.raises(pkgmod.PackageError) as ei:                                        # from bible initial state it does not
        pkgmod.validate_episode(pkg, pkg.load_episode("s01e02"), None, cfg)
    assert "carried state" in str(ei.value)


def test_bible_cross_check(fx):
    chars = json.loads((fx / "bible" / "characters.json").read_text())
    chars[0]["wardrobe"]["default"] = "w_nope"
    (fx / "bible" / "characters.json").write_text(json.dumps(chars))
    with pytest.raises(pkgmod.PackageError) as ei:
        _pkg(fx)
    assert "wardrobe.default" in str(ei.value)


# ---------------- mock pipeline ----------------

def _run(cfg, pkg, runs, ep="s01e01", stages=None, **kw):
    p = Pipeline(cfg, pkg, ep, runs, **kw)
    p.run(stages or [s for s in STAGES if s != "publish"])
    return p


def _approve_refs(runs, pkg):
    ss = SeriesState(runs / pkg.series["series_id"] / "series_state.json")
    ss.data["approvals"]["references"] = {"approved": True, "bible_version": pkg.bible_version, "by": "test", "at": now()}
    ss.save()


def test_e2e_mock_with_gates(fx, cfg, tmp_path):
    pkg = _pkg(fx); runs = tmp_path / "runs"
    _run(cfg, pkg, runs, stages=["intake", "direction", "references"])
    with pytest.raises(RuntimeError, match="approval"):                                  # gate before paid production
        _run(cfg, pkg, runs, stages=["keyframes"])
    _approve_refs(runs, pkg)
    p = _run(cfg, pkg, runs)
    st = State(runs / "fixture_series" / "s01e01")
    assert st.data["status"] == "complete" and st.data["delivered"]
    mdir = Path(st.data["master_dir"])
    assert (mdir / "episode.mp4").exists() and (mdir / "episode.srt").exists() and (mdir / "poster.jpg").exists()
    qa = json.loads((Path(st.data["qa_dir"]) / "report.json").read_text())
    assert qa["pass"], [c for c in qa["checks"] if not c["pass"]]
    assert (mdir / "episode.srt").read_text().count("-->") == 7
    assert all("request_id" in t and "checksum" in t and "estimated_cost" in t for t in st.data["takes"].values())
    assert 0 < st.data["spent_usd"] <= 50
    with pytest.raises(RuntimeError, match="publish"):                                   # gate before publication
        _run(cfg, pkg, runs, stages=["publish"])
    st.data["approvals"]["publish"] = {"approved": True, "by": "test", "at": now()}; st.save()
    _run(cfg, pkg, runs, stages=["publish"])
    st = State(runs / "fixture_series" / "s01e01")
    assert st.data["status"] == "published" and "public" in st.data
    ss = SeriesState(runs / "fixture_series" / "series_state.json")
    assert ss.data["episodes"]["s01e01"]["end_state"]["relationships"]["rel_ab"] == "state_2"


def test_idempotent_rerun_and_force_scene(fx, cfg, tmp_path):
    pkg = _pkg(fx); runs = tmp_path / "runs"
    _run(cfg, pkg, runs, stages=["intake", "direction", "references"]); _approve_refs(runs, pkg)
    _run(cfg, pkg, runs)
    st = State(runs / "fixture_series" / "s01e01"); n = len(st.data["takes"]); spent = st.data["spent_usd"]
    _run(cfg, pkg, runs)                                                                  # rerun: nothing regenerated
    st = State(runs / "fixture_series" / "s01e01")
    assert len(st.data["takes"]) == n and st.data["spent_usd"] == spent
    _run(cfg, pkg, runs, force={"sc05"})                                                  # one scene redone through all stages
    st = State(runs / "fixture_series" / "s01e01")
    sc05 = [t for t, v in st.data["takes"].items() if v.get("scene_id") == "sc05"]
    assert len(sc05) == 6 and st.data["spent_usd"] > spent                                # kf+vid+ls x2: forced redo = new takes, old kept
    assert st.data["master_version"] == 2


def test_resume_after_crash_mid_stage(fx, cfg, tmp_path, monkeypatch):
    pkg = _pkg(fx); runs = tmp_path / "runs"
    _run(cfg, pkg, runs, stages=["intake", "direction", "references"]); _approve_refs(runs, pkg)
    _run(cfg, pkg, runs, stages=["keyframes"])
    real = providers.gen_video; calls = {"n": 0}
    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated provider outage")
        return real(*a, **k)
    monkeypatch.setattr(providers, "gen_video", flaky)
    with pytest.raises(RuntimeError, match="outage"):
        _run(cfg, pkg, runs, stages=["video"])
    monkeypatch.setattr(providers, "gen_video", real)
    st = State(runs / "fixture_series" / "s01e01")
    done_before = [s for s, v in st.data["scenes"].items() if v.get("video")]
    assert len(done_before) == 2
    _run(cfg, pkg, runs, stages=["video"])
    st = State(runs / "fixture_series" / "s01e01")
    assert all(v.get("video") for v in st.data["scenes"].values())
    vids = [t for t in st.data["takes"].values() if t.get("endpoint") == cfg.fal_video_model]
    assert len(vids) == 8 and all(t["status"] == "succeeded" for t in vids)               # каждой сцене ровно один take, ничего не задвоилось


def test_budget_stop_sets_status(fx, cfg, tmp_path):
    s = json.loads((fx / "series.json").read_text()); s["production_limits"]["maximum_episode_budget_usd"] = 3
    (fx / "series.json").write_text(json.dumps(s))
    pkg = _pkg(fx); runs = tmp_path / "runs"
    with pytest.raises(BudgetExceeded):
        _run(cfg, pkg, runs, stages=["intake"])
    assert State(runs / "fixture_series" / "s01e01").data["status"] == "needs_budget_override"


def test_live_mode_guarded(fx, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x"); monkeypatch.setenv("FAL_KEY", "x")
    monkeypatch.setenv("PROVIDER_INPUT_MODE", "fal_storage"); monkeypatch.delenv("PIPELINE_ALLOW_PAID", raising=False)
    cfg = Config.load(HERE.parent, live=True)
    pkg = _pkg(fx); runs = tmp_path / "runs"
    _run(cfg, pkg, runs, stages=["intake"])                                               # бесплатно даже в live
    with pytest.raises(RuntimeError, match="PIPELINE_ALLOW_PAID"):
        _run(cfg, pkg, runs, stages=["references"])
    monkeypatch.setenv("PIPELINE_ALLOW_PAID", "true")
    cfg = Config.load(HERE.parent, live=True)
    with pytest.raises(RuntimeError, match="approval.status"):                            # пакет не утверждён
        _run(cfg, pkg, runs, stages=["references"])


# ---------------- video model ----------------

VEO_FAST = "fal-ai/veo3.1/fast/image-to-video"
VEO = "fal-ai/veo3.1/image-to-video"


def _video_takes(runs, series="fixture_series", ep="s01e01"):
    st = State(runs / series / ep)
    return {k: t for k, t in st.data["takes"].items() if t.get("what", "").startswith("video")}


@pytest.mark.parametrize("model", [VEO_FAST, VEO])
def test_both_video_models_run_through_the_whole_pipeline(fx, cfg, tmp_path, model):
    """Both endpoints take the same request; only price and quality differ."""
    cfg.fal_video_model = model
    pkg = _pkg(fx); runs = tmp_path / "runs"
    _run(cfg, pkg, runs, stages=["intake", "direction", "references"])
    _approve_refs(runs, pkg)
    _run(cfg, pkg, runs)
    st = State(runs / "fixture_series" / "s01e01")
    assert st.data["status"] == "complete" and st.data["delivered"]
    takes = _video_takes(runs)
    assert takes, "no video takes recorded"
    for take in takes.values():
        assert take["endpoint"] == model
        assert set(take["params"]) >= {"prompt", "aspect_ratio", "duration", "resolution",
                                       "generate_audio", "negative_prompt"}
        assert take["params"]["duration"] in ("4s", "6s", "8s")
        assert take["params"]["resolution"] in ("720p", "1080p", "4k")
        assert take["estimated_cost"] > 0


def test_the_chosen_model_is_what_each_video_take_is_billed_at(fx, cfg, tmp_path):
    """The same episode under each model, on identical scenes and prompts."""
    pkg = _pkg(fx)
    billed = {}
    for model in (VEO_FAST, VEO):
        cfg.fal_video_model = model
        runs = tmp_path / f"runs_{model.count('fast')}"
        _run(cfg, pkg, runs, stages=["intake", "direction", "references"])
        _approve_refs(runs, pkg)
        _run(cfg, pkg, runs)
        takes = _video_takes(runs)
        billed[model] = sum(t["estimated_cost"] for t in takes.values())
        assert {t["prompt"] for t in takes.values()}  # prompts recorded for comparison
    assert billed[VEO] == pytest.approx(2 * billed[VEO_FAST], rel=1e-6)


def test_a_saved_request_survives_a_change_of_model(fx, cfg, tmp_path):
    """Switching the series must not orphan a request already paid for."""
    cfg.fal_video_model = VEO_FAST
    pkg = _pkg(fx); runs = tmp_path / "runs"
    _run(cfg, pkg, runs, stages=["intake", "direction", "references"])
    _approve_refs(runs, pkg)
    _run(cfg, pkg, runs)
    before = _video_takes(runs)
    cfg.fal_video_model = VEO
    _run(cfg, pkg, runs)
    after = _video_takes(runs)
    assert {k: t["endpoint"] for k, t in after.items()} == {k: VEO_FAST for k in before}
    assert {k: t["request_id"] for k, t in after.items()} == {k: t["request_id"] for k, t in before.items()}


# ---------------- subtitles ----------------

def _deliverable(runs, series="fixture_series", ep="s01e01"):
    st = State(runs / series / ep)
    mdir = Path(st.data["master_dir"])
    return mdir, sorted(f.name for f in mdir.iterdir() if f.is_file())


@pytest.mark.parametrize("captions,shipped", [
    ("both", True), ("burned", True), ("srt", True), ("none", False),
])
def test_the_series_decides_whether_subtitles_ship(fx, cfg, tmp_path, captions, shipped):
    series = json.loads((fx / "series.json").read_text())
    series["format"]["captions"] = captions
    (fx / "series.json").write_text(json.dumps(series))
    pkg = _pkg(fx); runs = tmp_path / "runs"
    _run(cfg, pkg, runs, stages=["intake", "direction", "references"])
    _approve_refs(runs, pkg)
    _run(cfg, pkg, runs)
    mdir, files = _deliverable(runs)
    assert ("episode.srt" in files) is shipped
    assert "episode.mp4" in files
    meta = json.loads((mdir / "metadata.json").read_text())
    assert meta["captions"] == captions


def test_turning_subtitles_off_does_not_disable_the_subtitle_check(fx, cfg, tmp_path):
    """Otherwise 'no subtitles' would quietly also mean 'unverified timing'."""
    series = json.loads((fx / "series.json").read_text())
    series["format"]["captions"] = "none"
    (fx / "series.json").write_text(json.dumps(series))
    pkg = _pkg(fx); runs = tmp_path / "runs"
    _run(cfg, pkg, runs, stages=["intake", "direction", "references"])
    _approve_refs(runs, pkg)
    _run(cfg, pkg, runs)
    mdir, _ = _deliverable(runs)
    st = State(runs / "fixture_series" / "s01e01")
    report = json.loads((Path(st.data["qa_dir"]) / "report.json").read_text())
    check = next(c for c in report["checks"] if c["check"] == "subtitles_match_dialogue")
    assert check["pass"] and check["detail"].split("/")[0] != "-1"
    assert json.loads((mdir / "metadata.json").read_text())["subtitle_cues"] > 0
    # Delivery and publication must not look for a file that was never written.
    st.data["approvals"]["publish"] = {"approved": True, "by": "test", "at": now()}; st.save()
    _run(cfg, pkg, runs, stages=["publish"])
    published = State(runs / "fixture_series" / "s01e01").data["public"]
    assert "episode.mp4" in published and "episode.srt" not in published


# ---------------- language model transport ----------------

def _llm(monkeypatch, cfg, stop_reason="end_turn", text='{"ok": true}'):
    from serial.llm import LLM
    from types import SimpleNamespace
    seen = {}

    class _Stream:
        def __init__(self, kw):
            seen.update(kw)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                stop_reason=stop_reason,
                content=[SimpleNamespace(type="text", text=text)],
                model_dump=lambda mode=None: {
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 10, "output_tokens": 20}})

    class _Messages:
        def stream(self, **kw):
            return _Stream(kw)

        def create(self, **kw):
            raise AssertionError("a plain create() sits past the HTTP timeout at this size")

    llm = LLM(cfg, lambda *a: None)          # dry_run keeps the constructor offline
    llm.client = SimpleNamespace(messages=_Messages())
    return llm, seen


def test_the_engine_streams_its_model_calls(cfg, monkeypatch):
    """A plain create() at direction size died on APITimeoutError mid-run."""
    llm, seen = _llm(monkeypatch, cfg)
    assert llm._json("system", "content", max_tokens=48000) == {"ok": True}
    assert seen["max_tokens"] == 48000


def test_an_answer_cut_off_by_the_ceiling_stops_the_run(cfg):
    llm, _ = _llm(None, cfg, stop_reason="max_tokens")
    with pytest.raises(RuntimeError, match="ran out of room"):
        llm._create("system", "content", 800)


def test_every_stage_leaves_room_for_thinking_and_the_answer(cfg):
    """Thinking counts against the same ceiling as the reply, so a cap sized
    for the answer alone truncates before the answer starts."""
    import inspect
    from serial import llm as mod
    caps = [int(m) for m in re.findall(r"max_tokens=(\d+)\)", inspect.getsource(mod))]
    assert caps and min(caps) >= 4000, caps
