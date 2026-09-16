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
import threading
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
# "S02E02: The Fourth Woman" appeared as the name of episode three of season
# one. The model numbers episodes itself when left to it; the studio already
# knows the number, and two disagreeing ones on one screen help nobody.
EPISODE_LABEL_RE = re.compile(r"^\s*(episode\s*\d+|s\d{1,2}\s*[e·-]\s*\d{1,3})\s*[:.\u2014-]*\s*",
                              re.IGNORECASE)


def episode_title(text: str) -> str:
    """The episode's name without a numbering the studio did not ask for."""
    return EPISODE_LABEL_RE.sub("", text or "").strip()


class AuthoringError(ValueError):
    """Something the producer can read and act on."""


class SeriesProblem(AuthoringError):
    """The series itself is in the way; no draft can fix it.

    Raising this stops the drafting loop instead of sending the complaint back
    to the model, which cannot see — let alone repair — an episode other than
    the one it was asked to write.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── opening the next episode ───────────────────────────────────────────────

PREVIEW_SEASON = "previews"


def _season_of(series_id: str, actor: str = ""):
    """The season a real episode belongs in, creating the first one if needed.

    The first-clip preview leaves a series holding one season called
    "previews" at number 0. Continuing from it put real episodes inside that
    holder and built ids like "previewse02". A preview is a sample, not a
    season of the show.
    """
    seasons = store.list("seasons", {"series_id": series_id}, order="number")
    real = [s for s in seasons
            if s["season_id"] != PREVIEW_SEASON and int(s.get("number") or 0) > 0]
    if real:
        return real[-1]
    number = max([int(s.get("number") or 0) for s in seasons], default=0) + 1
    season_id = f"s{number:02d}"
    if store.get("seasons", {"series_id": series_id, "season_id": season_id}):
        raise AuthoringError(f"Season {season_id!r} already exists but is not usable. "
                             "Check the seasons on the series page.")
    record = store.upsert("seasons", {
        "series_id": series_id, "season_id": season_id, "number": number,
        "title": "", "arc": "", "episode_order": []})
    history(series_id, "", "season.opened", entity_type="season", entity_id=season_id,
            actor=actor, detail={"number": number})
    return record


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
    existing = store.list("episodes", {"series_id": series_id}, order="number")

    for episode in reversed(existing):
        if episode_is_untouched(series_id, episode["episode_id"]):
            return {**episode, "reused": True}

    # Only now, when an episode is actually going to be placed in it. Opening a
    # season first left an empty one behind whenever the button reused an
    # episode, and an empty season invalidates the package.
    season = _season_of(series_id, actor)
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
            # The previous episode is read for one thing only: where it leaves
            # the characters. Its length is not judged here — raising the
            # series' limits must not retroactively condemn an episode that is
            # already written, or worse, block every episode after it.
            try:
                prev_end = validate_episode(pkg, pkg.load_episode(prev), None, cfg,
                                            size_limits=False)["end_state"]
            except PackageError as exc:
                raise SeriesProblem(
                    f"Episode {prev} can no longer be read, so the studio cannot tell where "
                    f"{episode_id} starts:\n\n{exc}\n\nNothing was written. Fix {prev} first — "
                    "usually a bible entry it refers to was renamed or removed.") from exc
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
    return (os.getenv("ANTHROPIC_MODEL") or "claude-opus-5").strip()


# An episode is 12-18 scenes, each with dialogue, camera and continuity, and a
# whole cast is a paragraph of appearance apiece. At 8000 tokens the answer was
# cut off mid-JSON and surfaced as "could not read the model's answer" — a
# parser complaint for what was really a ceiling. The model is billed for what
# it writes, not for the room it is given, so the ceiling is generous and the
# call streams, which is what the SDK needs at this size to avoid a timeout.
SCRIPT_TOKENS = 64000
BIBLE_TOKENS = 32000


def _answer(client, system: str, messages: list[dict], max_tokens: int):
    """One completed reply, or a plain account of why there isn't one."""
    with client.messages.stream(model=_model(), max_tokens=max_tokens,
                                system=system, messages=messages) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "max_tokens":
        raise AuthoringError(
            "The model ran out of room before it finished. Ask for fewer scenes, "
            "or a shorter episode in the series settings.")
    if response.stop_reason == "refusal":
        raise AuthoringError("The model declined to write this. Rephrase the request.")
    return response


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
        "title": episode_title(draft.get("title")) or episode.get("title") or episode_id,
        "logline": draft.get("logline", ""),
        "scenes": scenes,
        "cliffhanger": draft.get("cliffhanger") or {},
    }


def _record_cost(series_id: str, episode_id: str, response) -> float:
    """Authoring is a paid call; it belongs in the episode's cost record."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0.0
    from serial.costs import anthropic_rates
    # Priced from the model that actually answered, never a fixed tariff: the
    # figures in Costs are what the producer reads before approving a run.
    input_rate, output_rate = anthropic_rates(_model())
    amount = round(getattr(usage, "input_tokens", 0) / 1e6 * input_rate
                   + getattr(usage, "output_tokens", 0) / 1e6 * output_rate, 4)
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
        response = _answer(client, SYSTEM, messages, SCRIPT_TOKENS)
        spend += _record_cost(series_id, episode_id, response)
        answer = "".join(block.text for block in response.content if block.type == "text")
        try:
            brief = _brief_for(series_id, episode_id, _extract_json(answer))
            normalized = validate_candidate(series_id, episode_id, brief)
            return brief, normalized, spend
        except SeriesProblem:
            raise
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


# ── what the series still needs before it can be written or filmed ─────────

def setup_problems(series_id: str) -> list[dict]:
    """Missing bible entries, named and linked.

    The engine reports these as schema paths — "bible/characters.json/0:
    'appearance' is a required property" — which names an array index rather
    than a person and offers nowhere to go. An on-camera character really does
    need an appearance and a wardrobe: the image models read that text
    literally, and without it faces drift between clips.

    Each problem carries its wording and its values separately so the name of
    a character or place is never fed through the translation table.
    """
    out = []

    def add(message, label, page, **values):
        out.append({"message": message, "names": values, "label": label,
                    "href": f"/series/{series_id}/{page}".rstrip("/")})

    characters = store.list("characters", {"series_id": series_id}, order="character_id")
    if not characters:
        add("This series has no characters yet. Add them before writing an episode.",
            "Add characters", "characters")
    for c in characters:
        if not c.get("visual", True):
            continue
        who = c.get("name") or c["character_id"]
        if not (c.get("appearance") or "").strip():
            add("{who}: no appearance description. The image models read it literally, so without "
                "it the face changes between clips.", "Describe the character", "characters", who=who)
        if not store.list("clothing", {"series_id": series_id, "character_id": c["character_id"]}):
            add("{who}: no clothing variant. Every on-camera character needs at least one.",
                "Add clothing", "characters", who=who)

    # The engine clamps a series' budget to the server's own ceiling, silently.
    # Raising the series setting alone then does nothing, and the run stops at a
    # number nobody chose.
    limits = (store.get("series", {"id": series_id}) or {}).get("production_limits") or {}
    wanted = float(limits.get("maximum_episode_budget_usd") or 0)
    ceiling = float(os.getenv("MAX_EPISODE_BUDGET_USD") or 50)
    if wanted > ceiling:
        add("This series is set to spend up to ${wanted:.0f} per episode, but the server "
            "caps every episode at ${ceiling:.0f}. Raise MAX_EPISODE_BUDGET_USD to at least "
            "${wanted:.0f} in the service environment, or lower the series budget.",
            "Series settings", "", wanted=wanted, ceiling=ceiling)

    locations = store.list("locations", {"series_id": series_id}, order="location_id")
    if not locations:
        add("This series has no locations yet. Add one before writing an episode.",
            "Add a location", "locations")
    for l in locations:
        where = l.get("name") or l["location_id"]
        if not (l.get("description") or "").strip():
            add("{where}: no description. It is what keeps the space the same between clips.",
                "Describe the location", "locations", where=where)
    return out


# ── filling the bible from what the series already knows ───────────────────

CAST_SYSTEM = """You complete the production bible of an existing vertical drama series.

Return ONLY a JSON object:
{"style": {"style_sentence": str, "camera_rules": str, "color_rules": str,
           "negative_image": str, "negative_video": str},
 "characters": [{"id": "snake_case", "name": str, "role": str, "age": str,
                 "visual": bool, "appearance": str, "behavior": str,
                 "wardrobe": [{"id": "snake_case", "description": str, "is_default": bool}]}],
 "locations": [{"id": "snake_case", "name": str, "description": str,
                "lighting_states": {"default": str}}]}

These entries are read by image and video models, so:

- `appearance` is 80-150 words of ENGLISH camera-ready description: height, build,
  skin, face shape, eyes, brows, nose, lips, hair length, part and wave pattern,
  hands, distinguishing marks. It is what keeps one face across every clip, so
  vagueness here becomes drift. Never mention clothing in it.
- `description` for a location is 80-150 ENGLISH words: geometry, materials,
  fixtures, the view, and above all what must never move between shots.
- Each wardrobe entry is one outfit in 15-40 ENGLISH words. Exactly one is default.
- `lighting_states` always has "default"; add "night" or others only if the story
  needs them. Each value describes that light in English.
- ids are lowercase snake_case, derived from the name, never renamed later.
- The style block is appended to EVERY image and video prompt, so it is the
  single strongest control over how the series looks. `style_sentence` is one
  dense English sentence naming the photographic look: film stock or sensor
  character, lens behaviour, depth of field, light quality and direction,
  contrast and grain, skin rendering. `camera_rules` states how the camera
  behaves across the series; `color_rules` the palette and grade.
  `negative_image` and `negative_video` list what must never appear.
  Write them for the genre and format given, and for a face that must stay the
  same across hundreds of clips.

Only characters and places the given material actually implies. Do not invent a
cast the story does not have. Keep every id, name and relationship already listed
as existing exactly as it is: those are established and are being continued."""


def _material(series_id: str) -> dict:
    """Everything the series has already said about itself."""
    series = store.get("series", {"id": series_id}) or {}
    episodes = store.list("episodes", {"series_id": series_id}, order="number")
    told = []
    for e in episodes:
        entry = {"episode_id": e["episode_id"], "title": e.get("title") or "",
                 "logline": e.get("logline") or ""}
        lines = [d.get("text", "") for s in store.list(
                     "scenes", {"series_id": series_id, "episode_id": e["episode_id"]},
                     order="sequence")
                 for d in (s.get("dialogue") or [])]
        if lines:
            entry["dialogue"] = lines[:40]
        told.append(entry)
    return {
        "series_title": series.get("title"), "genre": series.get("genre", ""),
        "logline": series.get("logline", ""),
        "dialogue_language": series.get("language") or "en-US",
        "style_sentence": (series.get("style") or {}).get("style_sentence", ""),
        "episodes_so_far": told,
        "existing_characters": [{"id": c["character_id"], "name": c.get("name"),
                                 "has_appearance": bool((c.get("appearance") or "").strip())}
                                for c in store.list("characters", {"series_id": series_id},
                                                    order="character_id")],
        "existing_locations": [{"id": l["location_id"], "name": l.get("name"),
                                "has_description": bool((l.get("description") or "").strip())}
                               for l in store.list("locations", {"series_id": series_id},
                                                   order="location_id")],
    }


def drafted_ids(series_id: str) -> set[str]:
    """Who the studio described, read back from the history it already writes.

    Kept out of the bible tables on purpose: a marker is not production data,
    and adding a column to a live database to hold one is not worth it.
    """
    out: set[str] = set()
    for row in store.list("generation_history", {"series_id": series_id,
                                                 "event": "bible.drafted"}):
        detail = row.get("detail") or {}
        for key in ("added", "completed", "locations"):
            out.update(detail.get(key) or [])
    return out


STYLE_FIELDS = ("style_sentence", "camera_rules", "color_rules",
                "negative_image", "negative_video")


def _is_written(value) -> bool:
    """Text a person actually wrote, as opposed to nothing or a seed marker."""
    text = (value or "").strip()
    return bool(text) and "PLACEHOLDER" not in text.upper()


def _slug_id(value: str) -> str:
    out = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return out[:64]


def fill_bible(series_id: str, actor: str = "") -> dict:
    """Complete the cast and places from what the series already established.

    A series started from the first-clip preview has episodes but an empty
    bible: its people lived only inside that clip's prompt. Re-typing them for
    the next episode is the studio's job, not the producer's.

    Nothing already written is overwritten. Entries the studio drafts are
    marked as such, so it is always visible which descriptions a person chose
    and which the studio proposed.
    """
    material = _material(series_id)
    if not any([material["logline"], material["episodes_so_far"], material["series_title"]]):
        raise AuthoringError("This series has nothing written yet. Add a logline or an episode first.")

    client = _client()
    response = _answer(client, CAST_SYSTEM,
                       [{"role": "user", "content": json.dumps(material, ensure_ascii=False)}],
                       BIBLE_TOKENS)
    spend = _record_cost(series_id, "", response)
    answer = "".join(b.text for b in response.content if b.type == "text")
    proposal = _extract_json(answer)

    added, completed = [], []
    for c in proposal.get("characters") or []:
        cid = _slug_id(c.get("id") or c.get("name") or "")
        if not ID_RE.match(cid or ""):
            continue
        existing = store.get("characters", {"series_id": series_id, "character_id": cid})
        record = {"series_id": series_id, "character_id": cid,
                  "name": c.get("name") or cid, "visual": bool(c.get("visual", True)),
                  "role": c.get("role", ""), "age": str(c.get("age") or ""),
                  "appearance": (c.get("appearance") or "").strip(),
                  "behavior": c.get("behavior", ""), "immutable": [], "props": [],
                  "seed_assets": [], "updated_at": _now()}
        if existing:
            # Keep every field a person filled in; only fill what is empty.
            kept = {k: v for k, v in existing.items()
                    if k in record and str(v or "").strip()}
            filled = not all(str(existing.get(k) or "").strip()
                             for k in ("appearance", "name"))
            record = {**record, **kept}
            if filled:
                completed.append(cid)
        else:
            added.append(cid)
        store.upsert("characters", record)
        if not store.get("voices", {"series_id": series_id, "character_id": cid}):
            slot = next_free_slot(series_id)
            if slot:
                store.upsert("voices", {
                    "series_id": series_id, "character_id": cid, "provider": "elevenlabs",
                    "voice_env": slot, "model_id": "eleven_v3",
                    "language": material["dialogue_language"], "style_notes": "",
                    "phone_fx": False, "locked": False})
        if record["visual"] and not store.list("clothing", {"series_id": series_id,
                                                            "character_id": cid}):
            variants = [v for v in (c.get("wardrobe") or []) if _slug_id(v.get("id") or "")]
            if not variants:
                variants = [{"id": "w_default", "description": "", "is_default": True}]
            if not any(v.get("is_default") for v in variants):
                variants[0]["is_default"] = True
            for v in variants:
                store.upsert("clothing", {
                    "series_id": series_id, "character_id": cid,
                    "variant_id": _slug_id(v["id"]), "is_default": bool(v.get("is_default")),
                    "description": v.get("description", ""), "immutable": []})

    # The style block is what makes a series look like one thing. It is written
    # only when nobody has written it: a placeholder left by the seed counts as
    # nobody, an actual sentence does not.
    style = dict(store.get("series", {"id": series_id}) or {}).get("style") or {}
    proposed_style = proposal.get("style") or {}
    wrote_style = False
    if proposed_style and not _is_written(style.get("style_sentence")):
        store.update("series", {"id": series_id},
                     {"style": {**style,
                                **{k: str(v) for k, v in proposed_style.items()
                                   if k in STYLE_FIELDS and not _is_written(style.get(k))}},
                      "updated_at": _now()})
        wrote_style = True

    places = []
    for l in proposal.get("locations") or []:
        lid = _slug_id(l.get("id") or l.get("name") or "")
        if not ID_RE.match(lid or ""):
            continue
        existing = store.get("locations", {"series_id": series_id, "location_id": lid})
        if existing and (existing.get("description") or "").strip():
            continue
        states = {k: v for k, v in (l.get("lighting_states") or {}).items() if isinstance(v, str)}
        states.setdefault("default", "")
        store.upsert("locations", {
            "series_id": series_id, "location_id": lid,
            "name": l.get("name") or (existing or {}).get("name") or lid,
            "description": (l.get("description") or "").strip(),
            "lighting_states": states, "marks": "", "immutable": [], "seed_assets": []})
        places.append(lid)

    history(series_id, "", "bible.drafted", entity_type="series", entity_id=series_id,
            actor=actor, detail={"added": added, "completed": completed, "locations": places})
    return {"added": added, "completed": completed, "locations": places,
            "style": wrote_style, "spend_usd": spend,
            "remaining": setup_problems(series_id)}


# ── voice slots ────────────────────────────────────────────────────────────

SLOT_PREFIX = "ELEVENLABS_VOICE_"
LEGACY_PREFIX = "ELEVENLABS_VOICE_ID_"
SLOT_RE = re.compile(r"^ELEVENLABS_VOICE_(?:SLOT_)?([0-9]{1,2})$")


def voice_slots() -> list[dict]:
    """Voice variables this server actually holds, as choices.

    Binding a character to ELEVENLABS_VOICE_ID_<NAME> meant a new server
    variable and a redeploy for every character — impossible from the browser,
    and "fill this in automatically" can add five at once. A fixed pool is set
    once; assigning one is then a choice in the studio.

    The id itself is never read here and never reaches the database: only the
    variable's name, and whether it is filled in.
    """
    slots = []
    for name, value in os.environ.items():
        match = SLOT_RE.match(name)
        if match:
            slots.append({"env": name, "number": int(match.group(1)),
                          "configured": bool((value or "").strip())})
    slots.sort(key=lambda s: s["number"])
    return slots


def voice_choices(series_id: str) -> list[dict]:
    """The pool, with who currently holds each slot."""
    taken = {}
    for v in store.list("voices", {"series_id": series_id}):
        taken.setdefault(v.get("voice_env"), []).append(v["character_id"])
    return [{**slot, "used_by": sorted(taken.get(slot["env"], []))} for slot in voice_slots()]


def next_free_slot(series_id: str) -> str:
    """A configured slot nobody in this series holds, else any configured one.

    Returns "" when the server has no voice slots at all; the series then runs
    on the video model's own speech until slots are set.
    """
    choices = voice_choices(series_id)
    ready = [c for c in choices if c["configured"]]
    free = [c for c in ready if not c["used_by"]]
    pick = (free or ready or choices)
    return pick[0]["env"] if pick else ""


def voice_env_is_known(name: str) -> bool:
    """A pool slot, or the per-character name the studio used before."""
    return bool(SLOT_RE.match(name) or name.startswith(LEGACY_PREFIX))


# ── work that outlives the request that started it ─────────────────────────

# Describing a cast, or writing an episode, takes the model a minute or two.
# That is longer than the proxy in front of the service will hold a browser
# request open, so it answered with a gateway timeout while the work carried
# on invisibly. Long work runs behind the request and reports through the
# history table, which every worker of the service can read.

BIBLE = ("bible.drafting", "bible.drafted", "bible.draft_failed")
SCRIPT = ("script.drafting", "script.drafted", "script.draft_failed")
EVENTS = {"bible": BIBLE, "script": SCRIPT}


def work_state(series_id: str, kind: str = "bible", episode_id: str = "") -> dict:
    """Where the last attempt of this kind got to."""
    started, done, failed = EVENTS[kind]
    rows = [r for r in store.list("generation_history", {"series_id": series_id},
                                  order="created_at", desc=True)
            if r.get("event") in (started, done, failed)
            and (r.get("episode_id") or "") == episode_id]
    if not rows:
        return {"state": "idle"}
    last = rows[0]
    return {"state": {started: "running", done: "done", failed: "failed"}[last["event"]],
            "at": last.get("created_at"), **(last.get("detail") or {})}


def fill_state(series_id: str) -> dict:
    return work_state(series_id, "bible")


def _start(series_id: str, episode_id: str, kind: str, work, actor: str) -> dict:
    """Begin the work and return at once."""
    started, done, failed = EVENTS[kind]
    if work_state(series_id, kind, episode_id)["state"] == "running":
        return {"already": True}
    history(series_id, episode_id, started, entity_type="series", entity_id=series_id,
            actor=actor)

    def run():
        try:
            detail = work() or {}
        except Exception as exc:                                   # noqa: BLE001
            history(series_id, episode_id, failed, entity_type="series",
                    entity_id=series_id, actor=actor, detail={"error": str(exc)[:400]})
        else:
            history(series_id, episode_id, done, entity_type="series",
                    entity_id=series_id, actor=actor, detail=detail)

    threading.Thread(target=run, name=f"{kind}:{series_id}:{episode_id}", daemon=True).start()
    return {"already": False}


def start_fill(series_id: str, actor: str = "") -> dict:
    if not store.get("series", {"id": series_id}):
        raise AuthoringError(f"Series {series_id!r} not found.")
    return _start(series_id, "", "bible",
                  lambda: {k: v for k, v in fill_bible(series_id, actor).items()
                           if k in ("added", "completed", "locations")}, actor)


def start_draft(series_id: str, episode_id: str, wish: str, actor: str = "") -> dict:
    """Write the episode behind the request."""
    if not store.get("episodes", {"series_id": series_id, "episode_id": episode_id}):
        raise AuthoringError(
            f"Episode {episode_id!r} has not been opened yet. Use \u201cNext episode\u201d "
            "on the series page.")
    return _start(series_id, episode_id, "script",
                  lambda: _summary(draft(series_id, episode_id, wish, actor)), actor)


def start_revise(series_id: str, episode_id: str, instruction: str, actor: str = "") -> dict:
    instruction = (instruction or "").strip()
    if not instruction:
        raise AuthoringError("Describe what to change.")
    return _start(series_id, episode_id, "script",
                  lambda: _summary(revise(series_id, episode_id, instruction, actor)), actor)


def _summary(result: dict) -> dict:
    return {"clips": result.get("clips"), "seconds": result.get("seconds"),
            "version": result.get("version"),
            "changed": [f"{c['scene_id']} ({c['redo']})" for c in result.get("changed") or []],
            "warnings": result.get("warnings") or []}
