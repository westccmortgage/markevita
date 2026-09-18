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


def _verified_pack(pkg, refs, root, version, approved=False):
    """Require every expected image, its original version and its actual bytes."""
    wanted = {"characters": {}, "locations": {}, "props": {}}
    for cid, c in pkg.characters.items():
        if c["visual"]:
            wanted["characters"][cid] = [key for key, _ in prompts.CHARACTER_PACK if not key.startswith("fullbody")]
            wanted["characters"][cid] += [f"{key}__{variant}" for key, _ in prompts.CHARACTER_PACK
                                           if key.startswith("fullbody") for variant in c["wardrobe"]["variants"]]
    wanted["locations"] = {lid: [key for key, _ in prompts.LOCATION_PACK] for lid in pkg.locations}
    wanted["props"] = {pid: [pid] for pid in pkg.props}
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
