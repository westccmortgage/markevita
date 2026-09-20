"""Reuse complete reference images only when their visual inputs are proven equal.

The full package version still covers script/season/voice settings. It is not
the identity of a visual reference pack. Legacy delivered masters retain the
checksums and approved image records needed for conservative recovery.
"""
import copy
import hashlib
import json
from pathlib import Path

from . import prompts
from .state import sha256


def fingerprint(pkg):
    characters = [{k: v for k, v in c.items() if k != "voice"}
                  for c in pkg.characters.values() if c["visual"]]
    seeds = {name: sha256(pkg.root / name)
             for item in characters + list(pkg.locations.values())
             for name in item.get("seed_assets", [])}
    content = {"characters": characters, "locations": list(pkg.locations.values()),
               "props": list(pkg.props.values()), "style": pkg.style, "seeds": seeds,
               "format": {k: pkg.series["format"][k] for k in ("aspect_ratio", "width", "height")}}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def fingerprint_parts(pkg):
    """The same inputs as fingerprint(), itemised.

    fingerprint() answers "is this the same pack"; when it says no, that is
    the whole of what anyone is told, and a producer is left regenerating a
    pack against a version they cannot see moving. These are the pieces it is
    made of, so the run can name the one that changed.
    """
    parts = {}
    for cid, c in sorted(pkg.characters.items()):
        if not c["visual"]:
            continue
        parts[f"character {cid}"] = _digest({k: v for k, v in c.items() if k != "voice"})
        for name in c.get("seed_assets", []):
            parts[f"locked face {cid}"] = sha256(pkg.root / name)
    for lid, l in sorted(pkg.locations.items()):
        parts[f"location {lid}"] = _digest(l)
        for name in l.get("seed_assets", []):
            parts[f"locked view {lid}"] = sha256(pkg.root / name)
    parts["props"] = _digest(sorted(pkg.props))
    parts["style"] = _digest(pkg.style)
    parts["format"] = _digest({k: pkg.series["format"][k] for k in ("aspect_ratio", "width", "height")})
    return parts


def changed_parts(pkg, previous):
    """What moved since the pack on record was made, in words."""
    if not previous:
        return []
    current = fingerprint_parts(pkg)
    names = sorted(set(current) | set(previous))
    return [name for name in names if current.get(name) != previous.get(name)]


def keep_unchanged(pkg, references, previous, version):
    """Throw away only what the change actually touched.

    Rewriting one costume of one character emptied the entire pack, so a
    single word cost every image of every character over again — about forty
    pictures and an hour, to redraw thirty-nine that nobody had touched. The
    inputs are recorded piece by piece, so what moved is knowable.

    Style and format frame every picture, so a change to either does clear
    everything. Anything else reaches the owners it names. What survives is
    re-stamped with this version and keeps a note of the version it was
    actually drawn in, and none of it carries an approval forward: the pack
    as a whole is different and has to be looked at again.
    """
    blank = {"characters": {}, "locations": {}, "props": {}}
    moved = set(changed_parts(pkg, previous))
    if not previous or moved.intersection({"style", "format"}):
        references.clear()
        references.update(copy.deepcopy(blank))
        return 0
    for cid in list(references.get("characters") or {}):
        if (cid not in pkg.characters or f"character {cid}" in moved
                or f"locked face {cid}" in moved):
            references["characters"].pop(cid, None)
    for lid in list(references.get("locations") or {}):
        if lid not in pkg.locations or f"location {lid}" in moved:
            references["locations"].pop(lid, None)
    if "props" in moved:
        references["props"] = {}
    for key in blank:
        references.setdefault(key, {})
    kept = 0
    for kind, group in references.items():
        for pack in group.values():
            for rec in ([pack] if "path" in pack else pack.values()):
                if not isinstance(rec, dict):
                    continue
                rec["source_bible_version"] = rec.get("source_bible_version") or rec.get("bible_version")
                rec["bible_version"] = version
                rec["approval"] = "pending"
                kept += 1
    return kept


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _legacy_matches(pkg, state, version, package_version=True):
    """Evidence that a delivered episode was made from today's visuals.

    `version` is the package version only for deliveries that predate
    reference_version; newer provenance names the pack's own version, which
    this checksum cannot reproduce, so that comparison is skipped. What
    follows it — the bible's files and the format, compared file by file —
    is the substantive check either way.
    """
    checksums = state.get("package_checksums") or {}
    if not checksums or "series.json" not in checksums:
        return False
    if package_version:
        old_version = hashlib.sha256("".join(checksums[k] for k in sorted(checksums)).encode()).hexdigest()[:12]
        if old_version != version:
            return False
    # Old packages did not fingerprint seed bytes, so do not infer equivalence.
    if any(c.get("seed_assets") for c in list(pkg.characters.values()) + list(pkg.locations.values())):
        return False
    old_bible = {k: v for k, v in checksums.items() if k.startswith("bible/")}
    current_bible = {k: v for k, v in pkg.checksums.items() if k.startswith("bible/")}
    return old_bible == current_bible and all(
        (state.get("episode") or {}).get(k) == pkg.series["format"][k]
        for k in ("aspect_ratio", "width", "height"))


def expected(pkg):
    """Every picture a complete reference pack holds, by owner.

    The studio always knew this — it is how a recovered pack is verified —
    but only ever showed how many images exist, never how many there should
    be. A producer watching a count climb has no way to tell a pack that is
    finished from one that stopped a third of the way in.
    """
    wanted = {"characters": {}, "locations": {}, "props": {}}
    for cid, c in pkg.characters.items():
        if c["visual"]:
            pack = prompts.character_pack(c)
            wanted["characters"][cid] = [key for key, _ in pack if not key.startswith("fullbody")]
            wanted["characters"][cid] += [f"{key}__{variant}" for key, _ in pack
                                           if key.startswith("fullbody") for variant in c["wardrobe"]["variants"]]
    wanted["locations"] = {lid: [key for key, _ in prompts.LOCATION_PACK] for lid in pkg.locations}
    wanted["props"] = {pid: [pid] for pid in pkg.props}
    return wanted


def _verified_pack(pkg, refs, root, version, approved=False):
    """Require every expected image, its original version and its actual bytes."""
    wanted = expected(pkg)
    result = {"characters": {}, "locations": {}, "props": {}}
    try:
        for kind, owners in wanted.items():
            for owner, names in owners.items():
                for name in names:
                    rec = refs[kind][owner] if kind == "props" else refs[kind][owner][name]
                    path = Path(rec["path"])
                    if (not path.resolve().is_relative_to(root.resolve()) or not path.is_file()
                            or rec.get("bible_version") != version or sha256(path) != rec.get("checksum")
                            or (approved and rec.get("approval") != "approved")):
                        return None
                    if kind == "props":
                        result[kind][owner] = copy.deepcopy(rec)
                    else:
                        result[kind].setdefault(owner, {})[name] = copy.deepcopy(rec)
        return result
    except (KeyError, TypeError, ValueError, OSError):
        return None


def find_reusable(pkg, root, series_state):
    current = series_state.get("bible_version")
    inputs = fingerprint(pkg)
    states = [(path.parent, _read(path)) for path in sorted(root.glob("*/state.json"))]
    if current and series_state.get("reference_pack_complete") == current:
        same = series_state.get("reference_inputs_fingerprint") == inputs or (
            not series_state.get("reference_inputs_fingerprint") and
            any(_legacy_matches(pkg, state, current) for _, state in states))
        if same:
            refs = _verified_pack(pkg, series_state.get("references", {}), root, current)
            approval = series_state.get("approvals", {}).get("references")
            if refs is not None:
                if not approval or not approval.get("approved") or approval.get("bible_version") != current:
                    approval = None
                return refs, current, copy.deepcopy(approval)
    # A failed run may already have replaced series_state with a partial pack.
    # Delivered provenance is immutable evidence of the previous actors. Legacy
    # recovery restores images for review; it never manufactures an approval.
    for episode_dir, state in states:
        if state.get("stages", {}).get("deliver") != "done":
            continue
        master = Path(state.get("master_dir", ""))
        if not master.resolve().is_relative_to(episode_dir.resolve()):
            continue
        provenance = _read(master / "provenance.json")
        # Deliveries made since the pack got its own version name it directly.
        version = provenance.get("reference_version")
        package_version = not version
        version = version or provenance.get("bible_version")
        if not version or not _legacy_matches(pkg, state, version, package_version):
            continue
        refs = _verified_pack(pkg, provenance.get("references", {}), root, version, approved=True)
        if refs is not None:
            return refs, version, None
    return None
