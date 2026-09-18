"""Reference identity survives episode edits, including recovery after partial runs."""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from make_fixture import build
from serial import prompts, reference_reuse
from serial.config import Config
from serial.package import SeriesPackage
from serial.pipeline import Pipeline
from serial.state import State, sha256


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def previous(tmp_path):
    source = build(tmp_path / "pkg", fast=True)
    for name in ("characters", "locations"):
        path = source / "bible" / f"{name}.json"
        rows = json.loads(path.read_text())
        for row in rows:
            row.pop("seed_assets", None)
        write(path, rows)
    pkg = SeriesPackage(source)
    root = tmp_path / "runs" / pkg.series["series_id"]
    refs = {"characters": {}, "locations": {}, "props": {}}
    def record(kind, owner, name):
        path = root / "references" / pkg.reference_version / kind / owner / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"original image {kind}/{owner}/{name}".encode())
        return {"path": str(path), "checksum": sha256(path), "approval": "approved",
                "bible_version": pkg.reference_version, "r2_key": f"original/{kind}/{owner}/{name}.png"}
    for cid, char in pkg.characters.items():
        if not char["visual"]:
            continue
        names = [k for k, _ in prompts.CHARACTER_PACK if not k.startswith("fullbody")]
        names += [f"{k}__{v}" for k, _ in prompts.CHARACTER_PACK if k.startswith("fullbody")
                  for v in char["wardrobe"]["variants"]]
        refs["characters"][cid] = {name: record("characters", cid, name) for name in names}
    for lid in pkg.locations:
        refs["locations"][lid] = {name: record("locations", lid, name) for name, _ in prompts.LOCATION_PACK}
    for pid in pkg.props:
        refs["props"][pid] = record("props", pid, pid)
    master = root / "s01e01" / "out" / "masters" / "v2"
    write(master / "provenance.json", {"bible_version": pkg.bible_version,
                                   "reference_version": pkg.reference_version, "references": refs})
    (master / "episode.mp4").write_bytes(b"original delivered movie")
    state = State(root / "s01e01")
    state.data.update(package_checksums=pkg.checksums, episode=dict(pkg.series["format"]),
                      master_dir=str(master), stages={"deliver": "done"})
    state.save()
    return source, root, pkg, refs, master


def new_episode(source):
    series = json.loads((source / "series.json").read_text())
    series["seasons"][0]["episodes"].append("s01e01_v2")
    series["language"] = "ru-RU"
    write(source / "series.json", series)
    return SeriesPackage(source)


def test_partial_new_pack_recovers_originals_without_generation_or_fabricated_approval(previous, monkeypatch):
    source, root, old, refs, master = previous
    original_provenance = (master / "provenance.json").read_bytes()
    pkg = new_episode(source)
    # The package moved — a new episode, a new spoken language — while nobody's
    # face did. The pack keeps its version; what has to be rescued here is a
    # partial pack left in series_state by a run that died part-way.
    assert pkg.bible_version != old.bible_version
    assert pkg.reference_version == old.reference_version
    cfg = Config.load(HERE.parent, live=False)
    pipeline = Pipeline(cfg, pkg, "s01e01_v2", root.parent)
    pipeline.sstate.data.update(bible_version=pkg.reference_version,
                                references={"characters": {"char_a": {}}, "locations": {}, "props": {}})
    monkeypatch.setattr(pipeline, "_gen_ref", lambda *a, **kw: pytest.fail("regenerated original actors"))
    pipeline.stage_references()
    recovered = pipeline.sstate.data["references"]
    for kind, owners in refs.items():
        for owner, pack in owners.items():
            for name, rec in ({owner: pack} if kind == "props" else pack).items():
                got = recovered[kind][owner] if kind == "props" else recovered[kind][owner][name]
                assert got["path"] == rec["path"] and got["checksum"] == rec["checksum"]
                assert got["r2_key"] == rec["r2_key"]
                assert got["source_bible_version"] == old.reference_version
                assert got["bible_version"] == pkg.reference_version and got["approval"] == "pending"
    with pytest.raises(RuntimeError, match="approval"):
        pipeline._require_references_approval()
    assert pipeline.state.stage_done("references")
    assert (master / "provenance.json").read_bytes() == original_provenance
    assert (master / "episode.mp4").read_bytes() == b"original delivered movie"
    pipeline.logf.close()


def test_nonvisual_edits_preserve_existing_approval(previous, monkeypatch):
    source, root, old, refs, _ = previous
    approval = {"approved": True, "by": "reviewer", "at": "original-time", "bible_version": old.reference_version}
    ss = {"bible_version": old.reference_version, "reference_pack_complete": old.reference_version,
          "reference_inputs_fingerprint": reference_reuse.fingerprint(old), "references": refs,
          "approvals": {"references": approval}}
    pkg = new_episode(source)
    path = source / "bible" / "characters.json"
    chars = json.loads(path.read_text())
    chars[0]["voice"]["language"] = "ru-RU"
    write(path, chars)
    pkg = SeriesPackage(source)
    result = reference_reuse.find_reusable(pkg, root, ss)
    assert result == (refs, old.reference_version, approval)
    assert result[2] is not approval
    pipeline = Pipeline(Config.load(HERE.parent, live=False), pkg, "s01e01_v2", root.parent)
    pipeline.sstate.data.update(ss)
    monkeypatch.setattr(pipeline, "_gen_ref", lambda *a, **kw: pytest.fail("unnecessary generation"))
    pipeline.stage_references()
    pipeline._require_references_approval()
    copied = pipeline.sstate.data["approvals"]["references"]
    assert copied["by"] == "reviewer" and copied["at"] == "original-time"
    assert copied["bible_version"] == pkg.reference_version
    pipeline.logf.close()


@pytest.mark.parametrize("change", ["appearance", "wardrobe", "location", "style", "format", "seed", "corrupt", "missing", "unapproved", "checksums"])
def test_legacy_recovery_requires_unchanged_visuals_and_verified_evidence(previous, change):
    source, root, old, refs, master = previous
    if change in ("appearance", "wardrobe", "seed"):
        path = source / "bible" / "characters.json"
        data = json.loads(path.read_text())
        if change == "appearance":
            data[0]["appearance"] = "A different person"
        elif change == "wardrobe":
            data[0]["wardrobe"]["variants"]["w_day"]["description"] = "A different outfit"
        else:
            data[0]["seed_assets"] = ["assets/char_a_seed.png"]
        write(path, data)
    elif change == "location":
        path = source / "bible" / "locations.json"
        data = json.loads(path.read_text()); data[0]["description"] = "Different architecture"
        write(path, data)
    elif change == "style":
        path = source / "bible" / "style.json"
        data = json.loads(path.read_text()); data["style_sentence"] = "Different style"
        write(path, data)
    elif change == "format":
        path = source / "series.json"
        data = json.loads(path.read_text()); data["format"]["width"] = 720
        write(path, data)
    elif change in ("corrupt", "missing"):
        path = Path(next(iter(refs["characters"]["char_a"].values()))["path"])
        path.write_bytes(b"corrupt") if change == "corrupt" else path.unlink()
    elif change == "unapproved":
        next(iter(refs["characters"]["char_a"].values()))["approval"] = "pending"
        write(master / "provenance.json", {"bible_version": old.bible_version,
                                   "reference_version": old.reference_version, "references": refs})
    else:
        state = State(root / "s01e01"); state.data.pop("package_checksums"); state.save()
    assert reference_reuse.find_reusable(new_episode(source), root, {}) is None


def test_visual_fingerprint_includes_seed_bytes(tmp_path):
    source = build(tmp_path / "pkg", fast=True)
    before = reference_reuse.fingerprint(SeriesPackage(source))
    (source / "assets" / "char_a_seed.png").write_bytes(b"changed image bytes")
    assert reference_reuse.fingerprint(SeriesPackage(source)) != before


def test_a_moved_target_says_what_moved(previous):
    """"bible изменился (X -> Y)" is not something a producer can act on."""
    source, root, old, _, _ = previous
    before = reference_reuse.fingerprint_parts(old)
    assert reference_reuse.changed_parts(old, before) == []
    # Opening an episode is not a change of anybody's look.
    assert reference_reuse.changed_parts(new_episode(source), before) == []

    path = source / "bible" / "style.json"
    style = json.loads(path.read_text())
    style["style_sentence"] = style["style_sentence"] + " Shot at dusk."
    write(path, style)
    assert reference_reuse.changed_parts(SeriesPackage(source), before) == ["style"]

    # A pack made before this was recorded says so rather than guessing.
    assert reference_reuse.changed_parts(SeriesPackage(source), None) == []


def test_the_pack_s_version_stands_still_while_the_story_moves(previous):
    """The one question that would have caught four defects in a row.

    Every one of them was the same shape: the studio chases a target that its
    own actions move. Production stops to ask for the reference pack to be
    approved; approving it answers "generate the pack for the current settings
    first"; generating it arrives at a version that has moved again. Three
    versions of one series moved that way in an evening, and none of the code
    read wrongly on its own — each function was correct and the loop was not.

    So this asks the loop's question directly: writing the series must not
    change what its characters look like. Only the look may do that.
    """
    source, _, old, _, _ = previous
    stable = old.reference_version

    def reopened():
        return SeriesPackage(source).reference_version

    # Opening an episode.
    series = json.loads((source / "series.json").read_text())
    series["seasons"][0]["episodes"].append("s01e01_v2")
    write(source / "series.json", series)
    assert reopened() == stable, "opening an episode redrew everybody"

    # Telling it in another language.
    series["language"] = "ru-RU"
    write(source / "series.json", series)
    assert reopened() == stable, "a spoken language redrew everybody"

    # Renaming the series.
    series["title"] = "Another Title Entirely"
    write(source / "series.json", series)
    assert reopened() == stable, "a title redrew everybody"

    # Casting a voice.
    path = source / "bible" / "characters.json"
    chars = json.loads(path.read_text())
    chars[0].setdefault("voice", {})["language"] = "ru-RU"
    write(path, chars)
    assert reopened() == stable, "a voice redrew everybody"

    # And the look still does move it, or none of the above means anything.
    style = json.loads((source / "bible" / "style.json").read_text())
    style["style_sentence"] = style["style_sentence"] + " Shot at dusk."
    write(source / "bible" / "style.json", style)
    assert reopened() != stable, "a change of style left the pack unchanged"


def test_a_half_built_pack_is_not_called_finished(previous, monkeypatch):
    """The stage said "already done" while approval said "not yet". Both true.

    The stage's own mark survives a change of bible, and nothing clears it.
    So when the bible moved, the pack was emptied, regeneration began, and the
    run died part-way — and the next run read that old mark, saw the handful
    of images it had managed, and declared the pack finished. It was never
    completed, so approval kept answering "generate the reference pack for the
    current series settings first", and no amount of running or approving
    could get past it.
    """
    source, root, old, refs, _ = previous
    pkg = SeriesPackage(source)
    pipeline = Pipeline(Config.load(HERE.parent, live=False), pkg, "s01e01_v2", root.parent)
    pipeline.state.mark_stage("references")
    pipeline.sstate.data.update(bible_version=pkg.reference_version, references=refs)
    pipeline.sstate.data.pop("reference_pack_complete", None)

    # Nothing to recover from elsewhere: this is about the half-built pack in
    # front of it, not about reuse.
    monkeypatch.setattr(reference_reuse, "find_reusable", lambda *a, **kw: None)
    pipeline.stage_references()
    # It carries on to the end of the stage and records the pack as complete,
    # which is the one thing approval asks for and the one thing the early
    # return never did.
    assert pipeline.sstate.data.get("reference_pack_complete") == pkg.reference_version

    # And a pack that really is complete is still left alone.
    again = Pipeline(Config.load(HERE.parent, live=False), pkg, "s01e01_v2", root.parent)
    again.state.mark_stage("references")
    again.sstate.data.update(bible_version=pkg.reference_version, references=refs,
                             reference_pack_complete=pkg.reference_version)
    monkeypatch.setattr(again, "_gen_ref", lambda *a, **kw: pytest.fail("regenerated a finished pack"))
    monkeypatch.setattr(reference_reuse, "find_reusable", lambda *a, **kw: None)
    again.stage_references()
    again.logf.close()
    pipeline.logf.close()
