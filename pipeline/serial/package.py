"""Series package: загрузка, валидация схем, кросс-проверки, ledger знаний/отношений, cliffhanger, нормализация сцен.
Ничего платного. Ничего сюжетного: пайплайн не знает, о чём сериал."""

import hashlib
import json
import re
from itertools import product
from pathlib import Path

import jsonschema

from . import schema

WORDS_PER_SEC_MAX = 2.6


class PackageError(ValueError):
    pass


def _words(text: str) -> int:
    return len(re.findall(r"[\w'’-]+", text))


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise PackageError(f"{path}: invalid JSON: {e}")


def _validate(obj, sch, label: str):
    v = jsonschema.Draft202012Validator(sch)
    errs = sorted(v.iter_errors(obj), key=lambda e: list(e.path))
    if errs:
        msg = "\n  - ".join(f"{label}{'/' + '/'.join(str(p) for p in e.path) if e.path else ''}: {e.message}" for e in errs[:20])
        raise PackageError(f"schema errors:\n  - {msg}")


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SeriesPackage:
    def __init__(self, root: Path):
        self.root = Path(root)
        if not (self.root / "series.json").exists():
            raise PackageError(f"{self.root}/series.json not found")
        self.series = _load(self.root / "series.json")
        _validate(self.series, schema.SERIES, "series.json")
        self.files: dict[str, dict | list] = {}
        self.checksums: dict[str, str] = {"series.json": file_sha(self.root / "series.json")}
        for rel, sch in schema.FILES.items():
            if rel == "series.json":
                continue
            p = self.root / rel
            if not p.exists():
                if rel in schema.OPTIONAL_FILES:
                    self.files[rel] = []
                    continue
                raise PackageError(f"missing {rel}")
            obj = _load(p)
            _validate(obj, sch, rel)
            self.files[rel] = obj
            self.checksums[rel] = file_sha(p)
        self.characters = {c["id"]: c for c in self.files["bible/characters.json"]}
        self.locations = {l["id"]: l for l in self.files["bible/locations.json"]}
        self.props = {p["id"]: p for p in self.files["bible/props.json"]}
        self.relationships = {r["id"]: r for r in self.files["bible/relationships.json"]}
        self.secrets = {s["id"]: s for s in self.files["bible/secrets.json"]}
        self.style = self.files["bible/style.json"]
        self._cross_check_bible()

    # ---------- bible ----------

    @property
    def bible_version(self) -> str:
        h = hashlib.sha256("".join(self.checksums[k] for k in sorted(self.checksums)).encode()).hexdigest()
        return h[:12]

    def _cross_check_bible(self):
        errs = []
        ids = set(self.characters)
        if len(ids) != len(self.files["bible/characters.json"]):
            errs.append("duplicate character ids")
        for c in self.characters.values():
            if c["visual"]:
                w = c["wardrobe"]
                if w["default"] not in w["variants"]:
                    errs.append(f"character {c['id']}: wardrobe.default {w['default']!r} not in variants")
            for pr in c.get("props", []):
                if pr["prop_id"] not in self.props:
                    errs.append(f"character {c['id']}: unknown prop {pr['prop_id']!r}")
            for a in c.get("seed_assets", []):
                if not (self.root / a).exists():
                    errs.append(f"character {c['id']}: seed asset {a} not found")
        for l in self.locations.values():
            for a in l.get("seed_assets", []):
                if not (self.root / a).exists():
                    errs.append(f"location {l['id']}: seed asset {a} not found")
        for r in self.relationships.values():
            for side in ("a", "b"):
                if r[side] not in ids:
                    errs.append(f"relationship {r['id']}: unknown character {r[side]!r}")
        for s in self.secrets.values():
            for h in s["holders_initial"]:
                if h not in ids:
                    errs.append(f"secret {s['id']}: unknown holder {h!r}")
        seen = set()
        for season in self.series["seasons"]:
            for ep in season["episodes"]:
                if ep in seen:
                    errs.append(f"episode id {ep} listed twice")
                seen.add(ep)
        if errs:
            raise PackageError("bible cross-check:\n  - " + "\n  - ".join(errs))

    def episode_order(self) -> list[tuple[str, str]]:
        out = []
        for season in sorted(self.series["seasons"], key=lambda s: s["number"]):
            for ep in season["episodes"]:
                out.append((season["season_id"], ep))
        return out

    def previous_episode(self, episode_id: str) -> str | None:
        order = [e for _, e in self.episode_order()]
        i = order.index(episode_id) if episode_id in order else -1
        return order[i - 1] if i > 0 else None

    def limits(self, cfg) -> dict:
        pl = self.series.get("production_limits", {})
        return {
            "budget": min(float(pl.get("maximum_episode_budget_usd", cfg.max_episode_budget_usd)), cfg.max_episode_budget_usd),
            "regen": min(int(pl.get("maximum_regenerations_per_scene", cfg.max_scene_regenerations)), cfg.max_scene_regenerations),
            "allowed": tuple(pl.get("allowed_clip_seconds", cfg.allowed_clip_seconds)),
            "min_scenes": int(pl.get("min_scenes", cfg.min_scenes)), "max_scenes": int(pl.get("max_scenes", cfg.max_scenes)),
            "min_sec": int(pl.get("min_episode_seconds", cfg.min_episode_seconds)), "max_sec": int(pl.get("max_episode_seconds", cfg.max_episode_seconds)),
        }

    # ---------- episode ----------

    def episode_dir(self, episode_id: str) -> Path:
        return self.root / "episodes" / episode_id

    def load_episode(self, episode_id: str) -> dict:
        p = self.episode_dir(episode_id) / "brief.json"
        if not p.exists():
            raise PackageError(f"missing {p}")
        ep = _load(p)
        _validate(ep, schema.EPISODE, f"episodes/{episode_id}/brief.json")
        if ep["series_id"] != self.series["series_id"]:
            raise PackageError(f"{episode_id}: series_id mismatch")
        if ep["episode_id"] != episode_id:
            raise PackageError(f"{episode_id}: episode_id field is {ep['episode_id']!r}")
        if (ep["season_id"], episode_id) not in self.episode_order():
            raise PackageError(f"{episode_id}: not listed under season {ep['season_id']} in series.json")
        ep["_sha256"] = file_sha(p)
        return ep

    def load_production_prompts(self, episode_id: str) -> dict | None:
        p = self.episode_dir(episode_id) / "production_prompts.json"
        if not p.exists():
            return None
        pp = _load(p)
        _validate(pp, schema.PRODUCTION_PROMPTS, f"episodes/{episode_id}/production_prompts.json")
        return pp


# ---------- validation + ledgers ----------

def _partition(total: int, n: int, allowed: tuple) -> list[int] | None:
    best = None
    for combo in product(allowed, repeat=n):
        if sum(combo) == total:
            spread = max(combo) - min(combo)
            if best is None or spread < best[0]:
                best = (spread, list(combo))
    return best[1] if best else None


def _visible_speakers(scene: dict) -> list[str]:
    seen = []
    for d in scene.get("dialogue", []):
        if d.get("voice_over"):
            continue
        if d["speaker"] in scene["characters_in_frame"] and d["speaker"] not in seen:
            seen.append(d["speaker"])
    return seen


def validate_episode(pkg: SeriesPackage, ep: dict, prev_end_state: dict | None, cfg) -> dict:
    """Возвращает normalized episode: сцены во внутреннем формате + ledgers + end_state. Бросает PackageError со списком ошибок."""
    L = pkg.limits(cfg)
    errs, warns = [], []
    scenes = sorted(ep["scenes"], key=lambda s: s["sequence"])
    if not (L["min_scenes"] <= len(scenes) <= L["max_scenes"]):
        errs.append(f"scenes: {len(scenes)}, need {L['min_scenes']}-{L['max_scenes']}")

    # --- opening state vs previous episode ---
    knowledge: dict[str, set[str]] = {sid: set(s["holders_initial"]) for sid, s in pkg.secrets.items()}
    relstate: dict[str, str] = {rid: r["state"] for rid, r in pkg.relationships.items()}
    prev_id = pkg.previous_episode(ep["episode_id"])
    if prev_id:
        if prev_end_state is None:
            warns.append(f"previous episode {prev_id} has no recorded end state; starting from bible initial state")
        else:
            knowledge = {k: set(v) for k, v in prev_end_state.get("knowledge", {}).items()}
            for sid in pkg.secrets:
                knowledge.setdefault(sid, set(pkg.secrets[sid]["holders_initial"]))
            relstate.update(prev_end_state.get("relationships", {}))
    os_ = ep.get("opening_state", {})
    for sid, chars in os_.get("knowledge", {}).items():
        if sid not in pkg.secrets:
            errs.append(f"opening_state.knowledge: unknown secret {sid!r}")
        elif set(chars) != knowledge.get(sid, set()):
            errs.append(f"opening_state.knowledge[{sid}] = {sorted(chars)} but carried state is {sorted(knowledge.get(sid, set()))}")
    for rid, st in os_.get("relationships", {}).items():
        if rid not in pkg.relationships:
            errs.append(f"opening_state.relationships: unknown relationship {rid!r}")
        elif relstate.get(rid) != st:
            errs.append(f"opening_state.relationships[{rid}] = {st!r} but carried state is {relstate.get(rid)!r}")

    # --- scenes ---
    total, seq, norm = 0, 0, []
    seen_ids, ledger = set(), []
    prop_state: dict[str, str] = {}
    for sc in scenes:
        sid = sc["scene_id"]
        if sid in seen_ids:
            errs.append(f"duplicate scene_id {sid}")
        seen_ids.add(sid)
        d = sc["duration_seconds"]
        if d not in L["allowed"]:
            errs.append(f"{sid}: duration {d}s not in {L['allowed']}")
        total += d
        if sc["location"] not in pkg.locations:
            errs.append(f"{sid}: unknown location {sc['location']!r}")
        elif sc.get("lighting_state") and sc["lighting_state"] not in pkg.locations[sc["location"]].get("lighting_states", {}):
            errs.append(f"{sid}: lighting_state {sc['lighting_state']!r} not defined for {sc['location']}")
        for c in sc["characters_in_frame"]:
            if c not in pkg.characters:
                errs.append(f"{sid}: unknown character {c!r}")
            elif not pkg.characters[c]["visual"]:
                errs.append(f"{sid}: {c} is voice-only and cannot be in frame")
        for c, variant in sc.get("wardrobe", {}).items():
            if c not in pkg.characters or not pkg.characters[c]["visual"] or variant not in pkg.characters[c]["wardrobe"]["variants"]:
                errs.append(f"{sid}: wardrobe {c}:{variant} unknown")
        for line in sc.get("dialogue", []):
            if line["speaker"] not in pkg.characters:
                errs.append(f"{sid}: unknown speaker {line['speaker']!r}")
            elif not line.get("voice_over") and line["speaker"] not in sc["characters_in_frame"]:
                warns.append(f"{sid}: {line['speaker']} speaks but is not in frame; treated as voice-over")
            if not _words(line["text"]):
                errs.append(f"{sid}: empty dialogue line")
        words = sum(_words(l["text"]) for l in sc.get("dialogue", []))
        if words > d * WORDS_PER_SEC_MAX:
            errs.append(f"{sid}: {words} words in {d}s exceeds {WORDS_PER_SEC_MAX} w/s")
        for pr in sc.get("props", []):
            if pr["prop_id"] not in pkg.props:
                errs.append(f"{sid}: unknown prop {pr['prop_id']!r}")
            prop_state[pr["prop_id"]] = pr["state"]
        # knowledge
        for k in sc.get("knowledge_required", []):
            if k["secret"] not in pkg.secrets:
                errs.append(f"{sid}: knowledge_required unknown secret {k['secret']!r}")
            elif k["character"] not in knowledge.get(k["secret"], set()):
                errs.append(f"{sid}: {k['character']} must know {k['secret']} here, but nobody told them yet "
                            f"(known by: {sorted(knowledge.get(k['secret'], set()))})")
        for k in sc.get("knowledge_gained", []):
            if k["secret"] not in pkg.secrets:
                errs.append(f"{sid}: knowledge_gained unknown secret {k['secret']!r}")
            elif k["character"] not in pkg.characters:
                errs.append(f"{sid}: knowledge_gained unknown character {k['character']!r}")
            else:
                if k["character"] in knowledge[k["secret"]]:
                    warns.append(f"{sid}: {k['character']} already knew {k['secret']}")
                knowledge[k["secret"]].add(k["character"])
                ledger.append({"scene_id": sid, "type": "knowledge", "character": k["character"], "secret": k["secret"], "how": k.get("how", "")})
        for rc in sc.get("relationship_changes", []):
            if rc["id"] not in pkg.relationships:
                errs.append(f"{sid}: unknown relationship {rc['id']!r}")
            else:
                allowed = pkg.relationships[rc["id"]].get("allowed_states")
                if allowed and rc["state"] not in allowed:
                    errs.append(f"{sid}: relationship {rc['id']} state {rc['state']!r} not in allowed_states")
                ledger.append({"scene_id": sid, "type": "relationship", "id": rc["id"], "from": relstate.get(rc["id"]), "to": rc["state"], "note": rc.get("note", "")})
                relstate[rc["id"]] = rc["state"]

        # normalize (+ split two visible speakers: spec §3.4)
        base = {k: sc.get(k) for k in ("location", "lighting_state", "characters_in_frame", "action", "shot_type", "lens", "camera_motion",
                                       "continuity_in", "continuity_out", "props", "wardrobe", "is_cliffhanger")}
        base["wardrobe"] = {c: (sc.get("wardrobe", {}).get(c) or pkg.characters[c]["wardrobe"]["default"])
                            for c in sc["characters_in_frame"] if c in pkg.characters and pkg.characters[c]["visual"]}
        speakers = _visible_speakers(sc)
        if len(speakers) <= 1:
            seq += 1
            norm.append({**base, "scene_id": sid, "source_scene_id": sid, "order": seq, "duration": d,
                         "dialogue": list(sc.get("dialogue", [])), "lipsync_speaker": speakers[0] if speakers else None, "split": False})
            continue
        lines = sc.get("dialogue", [])
        parts = _partition(d, len(lines), L["allowed"])
        if not parts:
            errs.append(f"{sid}: {len(lines)} lines by {len(speakers)} visible speakers cannot be split into clips of {L['allowed']} summing to {d}s; "
                        f"change the duration or split the scene in the brief")
            continue
        for i, (line, part) in enumerate(zip(lines, parts)):
            seq += 1
            other = [c for c in sc["characters_in_frame"] if c != line["speaker"]]
            norm.append({**base, "scene_id": f"{sid}{chr(ord('a') + i)}", "source_scene_id": sid, "order": seq, "duration": part,
                         "dialogue": [line], "lipsync_speaker": None if line.get("voice_over") else line["speaker"], "split": True,
                         "shot_type": f"medium close-up on {line['speaker']}, {', '.join(other)} soft in background; same geography as: {sc['shot_type']}",
                         "camera_motion": "locked tripod" if i else sc["camera_motion"],
                         "continuity_in": sc.get("continuity_in") if i == 0 else f"continues directly from {sid}{chr(ord('a') + i - 1)}; positions unchanged",
                         "continuity_out": sc.get("continuity_out") if i == len(lines) - 1 else "hold positions; cut on the line",
                         "is_cliffhanger": bool(sc.get("is_cliffhanger")) and i == len(lines) - 1})

    if not (L["min_sec"] <= total <= L["max_sec"]):
        errs.append(f"total {total}s not within {L['min_sec']}-{L['max_sec']}")

    # --- cliffhanger ---
    ch = ep["cliffhanger"]
    last = scenes[-1]["scene_id"] if scenes else None
    if ch["scene_id"] != last:
        errs.append(f"cliffhanger.scene_id {ch['scene_id']!r} must be the last scene ({last})")
    if not ch.get("hook", "").strip():
        errs.append("cliffhanger.hook is empty")
    if scenes and not (scenes[-1].get("dialogue") or scenes[-1].get("action", "").strip()):
        errs.append("cliffhanger scene has neither dialogue nor action")
    res = ch.get("resolves_in")
    all_eps = [e for _, e in pkg.episode_order()]
    if res and res != "tbd" and res not in all_eps:
        errs.append(f"cliffhanger.resolves_in {res!r} is not an episode in series.json")
    if res == ep["episode_id"]:
        errs.append("cliffhanger cannot resolve in the same episode")
    if scenes and not scenes[-1].get("is_cliffhanger", True):
        errs.append("last scene is marked is_cliffhanger=false")
    if scenes and "cut" not in (scenes[-1].get("continuity_out") or "").lower():
        warns.append("cliffhanger scene continuity_out does not mention a cut/hard out; assembly ends on the last frame")

    if errs:
        raise PackageError(f"episode {ep['episode_id']} rejected:\n  - " + "\n  - ".join(errs))

    fmt = pkg.series["format"]
    return {
        "series_id": pkg.series["series_id"], "series_title": pkg.series["title"], "season_id": ep["season_id"],
        "episode_id": ep["episode_id"], "number": ep["number"], "title": ep["title"], "logline": ep.get("logline", ""),
        "language": pkg.series["language"], "aspect_ratio": fmt["aspect_ratio"], "width": fmt["width"], "height": fmt["height"],
        "captions": fmt.get("captions", "srt"), "total_seconds": total, "brief_sha256": ep["_sha256"], "bible_version": pkg.bible_version,
        "limits": L, "scenes": norm, "ledger": ledger, "warnings": warns, "cliffhanger": ch,
        "end_state": {"knowledge": {k: sorted(v) for k, v in knowledge.items()}, "relationships": relstate, "props": prop_state},
    }


def attach_prompts(norm: dict, pp: dict | None) -> list[str]:
    """Промпты из production_prompts.json (если авторы дали) кладём в сцены; возвращаем id сцен без промптов."""
    missing = []
    for s in norm["scenes"]:
        src = (pp or {}).get("scenes", {}).get(s["scene_id"]) or (pp or {}).get("scenes", {}).get(s["source_scene_id"])
        if src:
            for k in ("keyframe_prompt", "video_prompt", "negative", "keyframe_expected", "video_expected", "lens"):
                if src.get(k):
                    s[k] = src[k]
            s["prompts_source"] = "package"
        else:
            missing.append(s["scene_id"])
    return missing


def estimate_first_pass(norm: dict, pkg: SeriesPackage, cfg, refs_needed: bool) -> dict:
    from . import costs
    L = norm["limits"]
    vid = sum(costs.video_cost(s["duration"], cfg.video_generate_audio, cfg.video_resolution) for s in norm["scenes"])
    kf = len(norm["scenes"]) * costs.image_cost(False, cfg.image_resolution)
    refs = 0.0
    if refs_needed:
        n_vis = sum(1 for c in pkg.characters.values() if c["visual"])
        n_var = sum(len(c["wardrobe"]["variants"]) for c in pkg.characters.values() if c["visual"])
        refs = (n_vis * (costs.image_cost(True, cfg.image_resolution) + 8 * costs.image_cost(False, cfg.image_resolution))
                + n_var * 2 * costs.image_cost(False, cfg.image_resolution)
                + len(pkg.locations) * (costs.image_cost(True, cfg.image_resolution) + 4 * costs.image_cost(False, cfg.image_resolution))
                + len(pkg.props) * costs.image_cost(False, cfg.image_resolution))
    ls = sum(costs.lipsync_cost(s["duration"], cfg.lipsync_variant) for s in norm["scenes"] if s.get("lipsync_speaker"))
    llm = (len(norm["scenes"]) * 3 + 2) * costs.PRICE["anthropic_per_call_estimate"]
    return {"video": round(vid, 2), "keyframes": round(kf, 2), "references": round(refs, 2), "lipsync": round(ls, 2), "llm": round(llm, 2),
            "total_first_pass": round(vid + kf + refs + ls + llm, 2), "budget_cap": L["budget"]}
