"""From a wish in plain words to a script the engine accepts.

The producer should not learn a markup language, invent identifiers or copy
technical fields between episodes. This module does that work: it opens the
next episode inside the right series, gathers what the series already
established, asks the model for a continuation, and refuses anything that
would not survive validation.

Nothing here writes a scene until the whole episode has passed the engine's
own validator. A partially understood draft never replaces work that exists.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .config import PIPELINE_DIR, settings
from .ingest import history
from .packaging import materialize, package_dir
from .store import store

sys.path.insert(0, str(PIPELINE_DIR))

from serial.config import Config                                  # noqa: E402
from serial.package import (PackageError, SeriesPackage,          # noqa: E402
                            validate_episode, word_budget)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


class AuthoringError(ValueError):
    """Something the producer can read and act on."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── opening the next episode ───────────────────────────────────────────────

def _season_of(series_id: str):
    seasons = store.list("seasons", {"series_id": series_id}, order="number")
    if not seasons:
        raise AuthoringError("This series has no season yet. Add one on the series page.")
    return seasons[-1]


def episode_is_untouched(series_id: str, episode_id: str) -> bool:
    """No scenes written and no production started."""
    if store.list("scenes", {"series_id": series_id, "episode_id": episode_id}):
        return False
    return not store.list("production_jobs", {"series_id": series_id, "episode_id": episode_id})


def next_episode(series_id: str, actor: str = "") -> dict:
    """Open the next episode of THIS series, or reuse an empty one.

    Pressing the button twice must not leave two blank episodes behind, and
    the episode must land in the series the producer is looking at — an
    earlier flow let a continuation of one series be loaded into another.
    """
    series = store.get("series", {"id": series_id})
    if not series:
        raise AuthoringError(f"Series {series_id!r} not found.")
    season = _season_of(series_id)
    existing = store.list("episodes", {"series_id": series_id}, order="number")

    for episode in reversed(existing):
        if episode_is_untouched(series_id, episode["episode_id"]):
            return {**episode, "reused": True}

    number = max([int(e.get("number") or 0) for e in existing], default=0) + 1
    episode_id = f"{season['season_id']}e{number:02d}"
    if store.get("episodes", {"series_id": series_id, "episode_id": episode_id}):
        episode_id = f"{episode_id}_{len(existing) + 1}"
    if not ID_RE.match(episode_id):
        raise AuthoringError(f"Could not build a valid episode id from season {season['season_id']!r}.")

    limits = series.get("production_limits") or {}
    record = store.upsert("episodes", {
        "series_id": series_id, "episode_id": episode_id, "season_id": season["season_id"],
        "number": number, "title": "", "logline": "", "status": "draft",
        "budget_usd": float(limits.get("maximum_episode_budget_usd") or 50),
        "spent_usd": 0, "created_at": _now(), "updated_at": _now(),
    })
    order = list(season.get("episode_order") or [])
    if episode_id not in order:
        order.append(episode_id)
        store.update("seasons", {"series_id": series_id, "season_id": season["season_id"]},
                     {"episode_order": order})
    history(series_id, episode_id, "episode.opened", entity_type="episode",
            entity_id=episode_id, actor=actor, detail={"number": number})
    return {**record, "reused": False}


# ── what the series has already established ────────────────────────────────

def previous_episode(series_id: str, episode_id: str) -> dict | None:
    episodes = store.list("episodes", {"series_id": series_id}, order="number")
    before = [e for e in episodes if e["episode_id"] != episode_id
              and store.list("scenes", {"series_id": series_id, "episode_id": e["episode_id"]})]
    return before[-1] if before else None


def series_memory(series_id: str, episode_id: str) -> dict:
    """Everything a continuation must stay consistent with.

    Drawn from what the series actually recorded, never from a model's earlier
    suggestion: a draft is not a fact until it has been produced and kept.
    """
    series = store.get("series", {"id": series_id}) or {}
    characters = []
    for c in store.list("characters", {"series_id": series_id}, order="character_id"):
        wardrobe = store.list("clothing", {"series_id": series_id,
                                           "character_id": c["character_id"]}, order="variant_id")
        voice = store.get("voices", {"series_id": series_id, "character_id": c["character_id"]})
        characters.append({
            "id": c["character_id"], "name": c.get("name"), "on_camera": bool(c.get("visual", True)),
            "role": c.get("role", ""), "appearance": (c.get("appearance") or "")[:400],
            "wardrobe_variants": [w["variant_id"] for w in wardrobe],
            "default_wardrobe": next((w["variant_id"] for w in wardrobe if w.get("is_default")),
                                     wardrobe[0]["variant_id"] if wardrobe else None),
            "has_voice": bool(voice and voice.get("voice_env")),
        })
    locations = [{
        "id": l["location_id"], "name": l.get("name"),
        "description": (l.get("description") or "")[:400],
        "lighting_states": list((l.get("lighting_states") or {}).keys()),
    } for l in store.list("locations", {"series_id": series_id}, order="location_id")]

    previous = previous_episode(series_id, episode_id)
    tail, carried = [], {"knowledge": {}, "relationships": {}}
    if previous:
        scenes = store.list("scenes", {"series_id": series_id,
                                       "episode_id": previous["episode_id"]}, order="sequence")
        tail = [{"scene_id": s["scene_id"], "location": s.get("location"),
                 "action": s.get("action"), "continuity_out": s.get("continuity_out"),
                 "dialogue": [{"speaker": d.get("speaker"), "text": d.get("text")}
                              for d in (s.get("dialogue") or [])]}
                for s in scenes[-3:]]
        for row in store.list("knowledge_state", {"series_id": series_id,
                                                  "episode_id": previous["episode_id"]}):
            bucket = "knowledge" if row.get("kind") == "knowledge" else "relationships"
            carried[bucket][row["subject_id"]] = row.get("value")

    return {
        "series_id": series_id,
        "title": series.get("title"),
        "dialogue_language": series.get("language") or "en-US",
        "genre": series.get("genre", ""),
        "style_sentence": (series.get("style") or {}).get("style_sentence", ""),
        "characters": characters,
        "locations": locations,
        "props": [{"id": p["prop_id"], "description": p.get("description", "")}
                  for p in store.list("props", {"series_id": series_id})],
        "relationships": [{"id": r["rel_id"], "between": [r["a"], r["b"]], "type": r.get("type"),
                           "state": r.get("state"), "allowed_states": r.get("allowed_states") or []}
                          for r in store.list("relationships", {"series_id": series_id})],
        "secrets": [{"id": s["secret_id"], "description": s.get("description", ""),
                     "known_by_at_series_start": s.get("holders_initial") or []}
                    for s in store.list("secrets_bible", {"series_id": series_id})],
        "previous_episode": previous["episode_id"] if previous else None,
        "previous_final_scenes": tail,
        "carried_state": carried,
        "limits": {**(series.get("production_limits") or {}),
                   "allowed_clip_seconds": (series.get("production_limits") or {}).get(
                       "allowed_clip_seconds", [4, 6, 8])},
        "format": series.get("format") or {},
    }


# ── validating a candidate without touching what exists ────────────────────

def validate_candidate(series_id: str, episode_id: str, brief: dict) -> dict:
    """Run the engine's own validator over a draft, in a throwaway copy.

    The live package is never written to, so a draft that does not survive
    validation cannot replace scenes already produced.
    """
    source = materialize(series_id)
    scratch = Path(tempfile.mkdtemp(prefix="markevita-draft-"))
    try:
        root = scratch / series_id
        shutil.copytree(source, root)
        target = root / "episodes" / episode_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "brief.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        season = (store.get("episodes", {"series_id": series_id, "episode_id": episode_id})
                  or {}).get("season_id") or "s01"
        series_json = json.loads((root / "series.json").read_text(encoding="utf-8"))
        for entry in series_json.get("seasons", []):
            if entry["season_id"] == season and episode_id not in entry["episodes"]:
                entry["episodes"].append(episode_id)
        (root / "series.json").write_text(json.dumps(series_json, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        cfg = Config.load(PIPELINE_DIR, live=False)
        pkg = SeriesPackage(root)
        prev = pkg.previous_episode(episode_id)
        prev_end = None
        if prev and (pkg.episode_dir(prev) / "brief.json").exists():
            prev_end = validate_episode(pkg, pkg.load_episode(prev), None, cfg)["end_state"]
        return validate_episode(pkg, pkg.load_episode(episode_id), prev_end, cfg)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ── a script a person can read ─────────────────────────────────────────────

def readable(scenes: list[dict], memory: dict | None = None) -> list[dict]:
    """The script as prose. Identifiers and markup stay inside the system."""
    names = {c["id"]: c.get("name") or c["id"] for c in (memory or {}).get("characters", [])}
    places = {l["id"]: l.get("name") or l["id"] for l in (memory or {}).get("locations", [])}
    out = []
    for scene in sorted(scenes, key=lambda s: int(s.get("sequence") or 0)):
        out.append({
            "scene_id": scene["scene_id"],
            "number": int(scene.get("sequence") or 0),
            "seconds": int(scene.get("duration_seconds") or 0),
            "place": places.get(scene.get("location"), scene.get("location") or ""),
            "light": scene.get("lighting_state") or "",
            "action": scene.get("action") or "",
            "shot": " · ".join(x for x in (scene.get("shot_type"), scene.get("lens"),
                                           scene.get("camera_motion")) if x),
            "lines": [{"who": names.get(d.get("speaker"), d.get("speaker") or ""),
                       "how": d.get("delivery") or "", "text": d.get("text") or "",
                       "voice_over": bool(d.get("voice_over"))}
                      for d in (scene.get("dialogue") or [])],
            "words": sum(len((d.get("text") or "").split()) for d in (scene.get("dialogue") or [])),
            "word_budget": word_budget(int(scene.get("duration_seconds") or 4),
                                       len(scene.get("dialogue") or [])),
            "is_cliffhanger": bool(scene.get("is_cliffhanger")),
        })
    return out


# ── asking the model, and refusing what it gets wrong ──────────────────────

SYSTEM = """You write one episode of an ongoing vertical drama series, as structured JSON.

Return ONLY a JSON object, no prose around it:
{"title": str, "logline": str, "scenes": [scene], "cliffhanger": {"scene_id": str, "hook": str, "resolves_in": "tbd"}}

scene = {"scene_id": "scNN", "sequence": int, "duration_seconds": int,
         "location": id, "lighting_state": key, "characters_in_frame": [id],
         "wardrobe": {character_id: variant_id}, "action": str,
         "dialogue": [{"speaker": id, "delivery": str, "text": str}],
         "shot_type": str, "lens": "24mm"|"35mm"|"50mm"|"85mm", "camera_motion": str,
         "continuity_in": str, "continuity_out": str, "is_cliffhanger": bool}

Hard rules. A script breaking any of them is rejected and wastes the producer's time.

IDENTIFIERS
- Use ONLY character, location, wardrobe-variant and prop ids listed in the series memory.
  Never invent one, never rename one, never introduce a new character or place.

LANGUAGE
- Every `text` is in the series dialogue language given in the memory, whatever
  language the producer's request is written in. Translate the intent; do not
  echo the request's language.
- `action`, `continuity_in`, `continuity_out`, `shot_type`, `camera_motion` are
  always English: they are read by image and video models.

CLIPS
- Each scene is one continuous shot. `duration_seconds` must be one of the
  allowed values in the memory.
- Exactly ONE character who is on camera may speak in a scene. A second speaker
  must be a different scene. This is a limitation of the lip-sync adapter.
- Respect the scene count and total-length limits in the memory.

SPOKEN LENGTH
- Every scene carries a word budget in the memory's limits. Dialogue that
  exceeds it cannot be voiced inside the clip. Short lines are better than
  trimmed ones.

DELIVERY
- `delivery` is a short performance cue of at most FOUR words with no sentence
  punctuation: "quiet, guilty", "low, controlled", "whispers". Longer notes are
  read aloud by the voice model and ruin the take. Put staging in `action`.

ACTION AND CAMERA
- `action` describes only what is VISIBLE: bodies, faces, gaze, props, light.
  Never put spoken words, character names in caps, headings or editing notes in it.
- The camera is locked per scene. No internal cuts, zooms or invented moves.
- Give every scene explicit `continuity_in` and `continuity_out` so the next
  scene can begin where this one ended.

CONTINUITY
- Continue from `previous_final_scenes` and `carried_state`. Keep the geography,
  wardrobe and established positions. Do not restate what the audience just saw.
- The final scene sets `is_cliffhanger: true` and leaves a question open."""


def _client():
    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise AuthoringError("Anthropic is not configured. Add ANTHROPIC_API_KEY in the "
                             "service environment; the Integrations page shows the state.")
    import anthropic
    return anthropic.Anthropic(api_key=key, max_retries=1, timeout=180)


def _model() -> str:
    return (os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5").strip()


def _extract_json(text: str) -> dict:
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise AuthoringError("The studio could not read the model's answer. Try again.")
    try:
        return json.loads(body[start:end + 1])
    except json.JSONDecodeError as e:
        raise AuthoringError(f"The studio could not read the model's answer ({e}). Try again.") from e


def _budgets(memory: dict) -> dict:
    allowed = memory["limits"].get("allowed_clip_seconds") or [4, 6, 8]
    return {f"{seconds}s": {f"{lines} line(s)": word_budget(seconds, lines) for lines in (1, 2)}
            for seconds in allowed}


def _brief_for(series_id: str, episode_id: str, draft: dict) -> dict:
    episode = store.get("episodes", {"series_id": series_id, "episode_id": episode_id}) or {}
    scenes = []
    for i, scene in enumerate(draft.get("scenes") or [], start=1):
        scene = dict(scene)
        scene.setdefault("scene_id", f"sc{i:02d}")
        scene["sequence"] = i
        scenes.append(scene)
    return {
        "schema_version": "2.0", "series_id": series_id,
        "season_id": episode.get("season_id") or "s01", "episode_id": episode_id,
        "number": int(episode.get("number") or 1),
        "title": draft.get("title") or episode.get("title") or episode_id,
        "logline": draft.get("logline", ""),
        "scenes": scenes,
        "cliffhanger": draft.get("cliffhanger") or {},
    }


def _record_cost(series_id: str, episode_id: str, response) -> float:
    """Authoring is a paid call; it belongs in the episode's cost record."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0.0
    amount = round(getattr(usage, "input_tokens", 0) / 1e6 * 2.0
                   + getattr(usage, "output_tokens", 0) / 1e6 * 10.0, 4)
    store.insert("costs", {"series_id": series_id, "episode_id": episode_id, "stage": "authoring",
                           "provider": "anthropic", "endpoint": _model(), "take_id": "",
                           "estimated_usd": amount, "actual_usd": amount, "created_at": _now()})
    return amount


def _ask(series_id: str, episode_id: str, memory: dict, request: str, current: list[dict] | None):
    """One drafting exchange, with a single correction round.

    The engine's validator is the judge; its complaints go back to the model
    verbatim so the second attempt fixes the real problem rather than guessing.
    """
    client, spend = _client(), 0.0
    payload = {"series_memory": memory, "word_budgets": _budgets(memory), "request": request}
    if current is not None:
        payload["current_script"] = current
    messages = [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    last_error = ""
    for attempt in range(2):
        response = client.messages.create(model=_model(), max_tokens=8000, system=SYSTEM,
                                          messages=messages)
        spend += _record_cost(series_id, episode_id, response)
        answer = "".join(block.text for block in response.content if block.type == "text")
        try:
            brief = _brief_for(series_id, episode_id, _extract_json(answer))
            normalized = validate_candidate(series_id, episode_id, brief)
            return brief, normalized, spend
        except PackageError as e:
            last_error = str(e)
            if attempt == 0:
                messages += [{"role": "assistant", "content": answer},
                             {"role": "user", "content":
                              "The studio rejected that script:\n" + last_error +
                              "\nReturn the corrected complete JSON object, nothing else."}]
        except AuthoringError as e:
            last_error = str(e)
            if attempt == 0:
                messages += [{"role": "assistant", "content": answer},
                             {"role": "user", "content": "Return only a single JSON object."}]
    raise AuthoringError("The studio could not produce a script that passes its own checks.\n\n"
                         + last_error)


def _commit(series_id: str, episode_id: str, brief: dict, actor: str, source: str) -> dict:
    """Write the episode only once it has passed validation."""
    from . import scripts as scriptmod
    store.delete("scenes", {"series_id": series_id, "episode_id": episode_id})
    for scene in brief["scenes"]:
        store.upsert("scenes", {
            "series_id": series_id, "episode_id": episode_id,
            "scene_id": scene["scene_id"], "sequence": int(scene["sequence"]),
            "duration_seconds": int(scene["duration_seconds"]),
            "location": scene.get("location", ""),
            "lighting_state": scene.get("lighting_state") or "default",
            "characters_in_frame": scene.get("characters_in_frame") or [],
            "wardrobe": scene.get("wardrobe") or {}, "action": scene.get("action", ""),
            "dialogue": scene.get("dialogue") or [], "shot_type": scene.get("shot_type", ""),
            "lens": scene.get("lens", ""), "camera_motion": scene.get("camera_motion", ""),
            "continuity_in": scene.get("continuity_in", ""),
            "continuity_out": scene.get("continuity_out", ""),
            "props": scene.get("props") or [],
            "knowledge_required": scene.get("knowledge_required") or [],
            "knowledge_gained": scene.get("knowledge_gained") or [],
            "relationship_changes": scene.get("relationship_changes") or [],
            "is_cliffhanger": bool(scene.get("is_cliffhanger")), "status": "draft",
        })
    store.update("episodes", {"series_id": series_id, "episode_id": episode_id},
                 {"title": brief.get("title") or "", "logline": brief.get("logline", ""),
                  "cliffhanger": brief.get("cliffhanger") or {}, "updated_at": _now()})
    scenes = store.list("scenes", {"series_id": series_id, "episode_id": episode_id},
                        order="sequence")
    prior = store.list("scripts", {"series_id": series_id, "episode_id": episode_id})
    version = max([int(s.get("version") or 0) for s in prior], default=0) + 1
    store.insert("scripts", {
        "series_id": series_id, "episode_id": episode_id, "version": version, "source": source,
        "filename": "", "content": scriptmod.to_script_text(scenes),
        "parsed": {"scenes": scenes, "format": source}, "created_by": actor, "created_at": _now()})
    history(series_id, episode_id, f"script.{source}", entity_type="script",
            entity_id=str(version), actor=actor, detail={"scenes": len(scenes)})
    return {"version": version, "scenes": len(scenes)}


def draft(series_id: str, episode_id: str, wish: str = "", actor: str = "") -> dict:
    """Prepare this episode from a wish, a plain-prose script, or from nothing.

    With no wish, the continuation follows the recorded ending of the previous
    episode. A prose script with its own headings is accepted here; it does not
    have to be written in the engine's markup.
    """
    if not store.get("episodes", {"series_id": series_id, "episode_id": episode_id}):
        raise AuthoringError(
            f"Episode {episode_id!r} has not been opened yet. Use \u201cNext episode\u201d "
            "on the series page.")
    memory = series_memory(series_id, episode_id)
    if not memory["characters"]:
        raise AuthoringError("This series has no characters yet. Add them before writing an episode.")
    if not memory["locations"]:
        raise AuthoringError("This series has no locations yet. Add one before writing an episode.")
    request = (wish or "").strip() or (
        "Continue the series from the recorded ending of the previous episode. "
        "Resolve nothing that was left deliberately open until the final scene."
        if memory["previous_episode"] else
        "Write the opening episode of this series.")
    brief, normalized, spend = _ask(series_id, episode_id, memory, request, None)
    result = _commit(series_id, episode_id, brief, actor, "studio")
    return {**result, "clips": len(normalized["scenes"]),
            "seconds": normalized["total_seconds"], "spend_usd": spend,
            "warnings": normalized.get("warnings") or []}


def revise(series_id: str, episode_id: str, instruction: str, actor: str = "") -> dict:
    """Change the script in plain words, keeping what was not asked about."""
    instruction = (instruction or "").strip()
    if not instruction:
        raise AuthoringError("Describe what to change.")
    scenes = store.list("scenes", {"series_id": series_id, "episode_id": episode_id},
                        order="sequence")
    if not scenes:
        raise AuthoringError("There is no script to change yet.")
    memory = series_memory(series_id, episode_id)
    before = {s["scene_id"]: s for s in scenes}
    brief, normalized, spend = _ask(
        series_id, episode_id, memory,
        "Apply this change and return the complete script. Leave every scene the change does "
        "not concern exactly as it is, including its wording.\n\n" + instruction,
        readable(scenes, memory))
    result = _commit(series_id, episode_id, brief, actor, "revision")
    return {**result, "clips": len(normalized["scenes"]),
            "seconds": normalized["total_seconds"], "spend_usd": spend,
            "changed": changed_scenes(before, brief["scenes"]),
            "warnings": normalized.get("warnings") or []}


VISUAL_FIELDS = ("duration_seconds", "location", "lighting_state", "characters_in_frame",
                 "wardrobe", "action", "shot_type", "lens", "camera_motion",
                 "continuity_in", "continuity_out")


def changed_scenes(before: dict, after: list[dict]) -> list[dict]:
    """What each edited scene will cost to redo.

    Words alone mean new speech. A changed image or movement means the clip
    itself is generated again: saying otherwise would promise footage that
    cannot survive the edit.
    """
    out = []
    for scene in after:
        old = before.get(scene["scene_id"])
        if not old:
            out.append({"scene_id": scene["scene_id"], "redo": "new scene"})
            continue
        visual = any((old.get(f) or "") != (scene.get(f) or "") for f in VISUAL_FIELDS)
        spoken = [(d.get("text") or "") for d in (old.get("dialogue") or [])] != \
                 [(d.get("text") or "") for d in (scene.get("dialogue") or [])]
        if visual:
            out.append({"scene_id": scene["scene_id"], "redo": "image and video"})
        elif spoken:
            out.append({"scene_id": scene["scene_id"], "redo": "speech"})
    return out
