"""Script intake: pasted or uploaded text → structured scenes.

Two input formats are accepted, both deterministic — no model call, so intake
costs nothing and behaves identically every time:

1. **JSON** — a complete episode brief in series-package schema 2.0. Imported
   as-is after validation by the engine.

2. **Structured script text** — the format below. Everything the engine needs
   per clip is explicit, because the engine must never invent continuity:

       SCENE sc01 | 6s | villa_terrace | night
       CHARACTERS: char_a, char_b
       WARDROBE: char_a=w_evening
       SHOT: medium close-up | 50mm | slow push-in
       ACTION: What visibly happens in one continuous shot.
       IN: state at the first frame
       OUT: state at the last frame
       PROPS: passenger_list=folded in jacket pocket
       KNOWS: char_a needs secret_x
       LEARNS: char_a gains secret_x via overhears
       REL: rel_a_b -> confessed
       CLIFFHANGER
       char_a (quiet, certain): A spoken line.
       char_b: Another line.
       vo narrator: A voice-over line.

   `SCENE` and `ACTION` are required. Lighting defaults to `default`, duration
   to 6 seconds. A line prefixed `vo ` is a voice-over.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .store import store

SCENE_RE = re.compile(r"^SCENE\s+(?P<id>[a-z0-9_]+)\s*(?:\|(?P<rest>.*))?$", re.I)
DIALOGUE_RE = re.compile(r"^(?P<vo>vo\s+)?(?P<speaker>[a-z0-9_]+)\s*(?:\((?P<delivery>[^)]*)\))?\s*:\s*(?P<text>.+)$", re.I)
FIELD_RE = re.compile(r"^(?P<key>CHARACTERS|WARDROBE|SHOT|ACTION|IN|OUT|PROPS|KNOWS|LEARNS|REL)\s*:\s*(?P<value>.*)$", re.I)


class ScriptError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse(text: str) -> dict:
    """Parse a script into {'scenes': [...], 'cliffhanger': {...}}."""
    text = (text or "").strip()
    if not text:
        raise ScriptError("The script is empty.")
    if text.lstrip().startswith("{"):
        return _parse_json(text)
    return _parse_text(text)


def _parse_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ScriptError(f"Not valid JSON: {e}") from e
    if not isinstance(data, dict) or "scenes" not in data:
        raise ScriptError("JSON must be an episode brief object containing a 'scenes' array.")
    return {
        "scenes": data["scenes"],
        "cliffhanger": data.get("cliffhanger") or {},
        "opening_state": data.get("opening_state") or {},
        "title": data.get("title", ""),
        "logline": data.get("logline", ""),
        "format": "json",
    }


def _parse_text(text: str) -> dict:
    scenes: list[dict] = []
    current: dict | None = None
    cliffhanger_scene = ""

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        m = SCENE_RE.match(line)
        if m:
            current = _new_scene(m, len(scenes) + 1)
            scenes.append(current)
            continue

        if current is None:
            raise ScriptError(f"Content before the first SCENE header: {line!r}")

        if line.upper() == "CLIFFHANGER":
            current["is_cliffhanger"] = True
            cliffhanger_scene = current["scene_id"]
            continue

        f = FIELD_RE.match(line)
        if f:
            _apply_field(current, f.group("key").upper(), f.group("value").strip())
            continue

        d = DIALOGUE_RE.match(line)
        if d:
            entry = {"speaker": d.group("speaker").lower(), "text": d.group("text").strip()}
            if d.group("delivery"):
                entry["delivery"] = d.group("delivery").strip()
            if d.group("vo"):
                entry["voice_over"] = True
            current["dialogue"].append(entry)
            continue

        # A bare line continues the action block.
        current["action"] = (current["action"] + " " + line).strip()

    if not scenes:
        raise ScriptError("No SCENE headers found. See the format help above the editor.")
    missing = [s["scene_id"] for s in scenes if not s["action"]]
    if missing:
        raise ScriptError("These scenes have no ACTION: " + ", ".join(missing))

    cliffhanger = {}
    if cliffhanger_scene:
        cliffhanger = {"scene_id": cliffhanger_scene, "hook": "", "resolves_in": "tbd"}
    return {"scenes": scenes, "cliffhanger": cliffhanger, "opening_state": {}, "format": "text"}


def _new_scene(m: re.Match, sequence: int) -> dict:
    rest = [p.strip() for p in (m.group("rest") or "").split("|") if p.strip()]
    duration, location, lighting = 6, "", "default"
    for part in rest:
        if re.fullmatch(r"\d+\s*s?", part, re.I):
            duration = int(re.sub(r"[^\d]", "", part))
        elif not location:
            location = part
        else:
            lighting = part
    return {
        "scene_id": m.group("id").lower(), "sequence": sequence, "duration_seconds": duration,
        "location": location, "lighting_state": lighting or "default",
        "characters_in_frame": [], "wardrobe": {}, "action": "", "dialogue": [],
        "shot_type": "", "lens": "", "camera_motion": "",
        "continuity_in": "", "continuity_out": "", "props": [],
        "knowledge_required": [], "knowledge_gained": [], "relationship_changes": [],
        "is_cliffhanger": False,
    }


def _apply_field(scene: dict, key: str, value: str) -> None:
    if key == "CHARACTERS":
        scene["characters_in_frame"] = [c.strip().lower() for c in value.split(",") if c.strip()]
    elif key == "WARDROBE":
        for pair in value.split(","):
            if "=" in pair:
                c, v = pair.split("=", 1)
                scene["wardrobe"][c.strip().lower()] = v.strip().lower()
    elif key == "SHOT":
        parts = [p.strip() for p in value.split("|")]
        scene["shot_type"] = parts[0] if parts else ""
        if len(parts) > 1:
            scene["lens"] = parts[1]
        if len(parts) > 2:
            scene["camera_motion"] = parts[2]
    elif key == "ACTION":
        scene["action"] = (scene["action"] + " " + value).strip()
    elif key == "IN":
        scene["continuity_in"] = value
    elif key == "OUT":
        scene["continuity_out"] = value
    elif key == "PROPS":
        for pair in value.split(","):
            if "=" in pair:
                p, st = pair.split("=", 1)
                scene["props"].append({"prop_id": p.strip().lower(), "state": st.strip()})
    elif key == "KNOWS":
        for clause in value.split(","):
            m = re.match(r"\s*([a-z0-9_]+)\s+needs\s+([a-z0-9_]+)\s*$", clause, re.I)
            if m:
                scene["knowledge_required"].append({"character": m.group(1).lower(), "secret": m.group(2).lower()})
    elif key == "LEARNS":
        for clause in value.split(","):
            m = re.match(r"\s*([a-z0-9_]+)\s+gains\s+([a-z0-9_]+)(?:\s+via\s+(.+))?$", clause.strip(), re.I)
            if m:
                entry = {"character": m.group(1).lower(), "secret": m.group(2).lower()}
                if m.group(3):
                    entry["how"] = m.group(3).strip()
                scene["knowledge_gained"].append(entry)
    elif key == "REL":
        for clause in value.split(","):
            m = re.match(r"\s*([a-z0-9_]+)\s*->\s*(.+)$", clause.strip(), re.I)
            if m:
                scene["relationship_changes"].append({"id": m.group(1).lower(), "state": m.group(2).strip()})


def to_script_text(scenes: list[dict]) -> str:
    """Render stored scenes back into the structured script format.

    Editing one line must not leave the saved script text describing a
    different episode, so the source is rewritten from the scenes it produced.
    Doubles as a way to read the current script back out.
    """
    out: list[str] = []
    for scene in sorted(scenes, key=lambda s: int(s.get("sequence") or 0)):
        header = [f"SCENE {scene['scene_id']}", f"{int(scene.get('duration_seconds') or 6)}s"]
        if scene.get("location"):
            header.append(scene["location"])
        if (scene.get("lighting_state") or "default") != "default":
            header.append(scene["lighting_state"])
        out.append(" | ".join(header))
        if scene.get("characters_in_frame"):
            out.append("CHARACTERS: " + ", ".join(scene["characters_in_frame"]))
        if scene.get("wardrobe"):
            out.append("WARDROBE: " + ", ".join(f"{c}={v}" for c, v in scene["wardrobe"].items()))
        shot = [scene.get("shot_type") or "", scene.get("lens") or "", scene.get("camera_motion") or ""]
        while shot and not shot[-1]:
            shot.pop()
        if any(shot):
            out.append("SHOT: " + " | ".join(shot))
        out.append("ACTION: " + (scene.get("action") or ""))
        if scene.get("continuity_in"):
            out.append("IN: " + scene["continuity_in"])
        if scene.get("continuity_out"):
            out.append("OUT: " + scene["continuity_out"])
        if scene.get("props"):
            out.append("PROPS: " + ", ".join(f"{p['prop_id']}={p.get('state','')}" for p in scene["props"]))
        if scene.get("knowledge_required"):
            out.append("KNOWS: " + ", ".join(f"{k['character']} needs {k['secret']}" for k in scene["knowledge_required"]))
        if scene.get("knowledge_gained"):
            out.append("LEARNS: " + ", ".join(
                f"{k['character']} gains {k['secret']}" + (f" via {k['how']}" if k.get("how") else "")
                for k in scene["knowledge_gained"]))
        if scene.get("relationship_changes"):
            out.append("REL: " + ", ".join(f"{r['id']} -> {r['state']}" for r in scene["relationship_changes"]))
        if scene.get("is_cliffhanger"):
            out.append("CLIFFHANGER")
        for line in scene.get("dialogue") or []:
            prefix = "vo " if line.get("voice_over") else ""
            delivery = f" ({line['delivery']})" if line.get("delivery") else ""
            out.append(f"{prefix}{line['speaker']}{delivery}: {line.get('text','')}")
        out.append("")
    return "\n".join(out).strip() + "\n"


def reword_scene(series_id: str, episode_id: str, scene_id: str, texts: list[str],
                 actor: str = "") -> dict:
    """Replace the WORDS of a scene's lines, changing nothing else.

    Speaker, delivery, voice-over flag and line count are untouched, which is
    the one edit a produced episode can absorb without discarding its video.
    """
    scene = store.get("scenes", {"series_id": series_id, "episode_id": episode_id,
                                 "scene_id": scene_id})
    if not scene:
        raise ScriptError(f"Scene {scene_id} not found.")
    lines = list(scene.get("dialogue") or [])
    if len(texts) != len(lines):
        raise ScriptError("The number of lines must stay the same.")
    cleaned = [" ".join(t.split()) for t in texts]
    if not all(cleaned):
        raise ScriptError("A spoken line cannot be empty.")
    updated = [{**line, "text": text} for line, text in zip(lines, cleaned)]
    store.update("scenes", {"series_id": series_id, "episode_id": episode_id,
                            "scene_id": scene_id}, {"dialogue": updated})

    scenes = store.list("scenes", {"series_id": series_id, "episode_id": episode_id},
                        order="sequence")
    prior = store.list("scripts", {"series_id": series_id, "episode_id": episode_id})
    version = max([int(s.get("version") or 0) for s in prior], default=0) + 1
    store.insert("scripts", {
        "series_id": series_id, "episode_id": episode_id, "version": version,
        "source": "reword", "filename": "", "content": to_script_text(scenes),
        "parsed": {"scenes": scenes, "format": "reword"}, "created_by": actor,
        "created_at": _now(),
    })
    from .ingest import history
    history(series_id, episode_id, "scene.reworded", entity_type="scene", entity_id=scene_id,
            detail={"version": version, "lines": len(updated)}, actor=actor)
    return {"scene_id": scene_id, "version": version, "lines": len(updated)}


def save_script(series_id: str, episode_id: str, content: str, *, source: str = "paste",
                filename: str = "", actor: str = "") -> dict:
    """Store the script, parse it, and replace the episode's scenes."""
    parsed = parse(content)
    prior = store.list("scripts", {"series_id": series_id, "episode_id": episode_id})
    version = max([int(s.get("version") or 0) for s in prior], default=0) + 1

    record = store.insert("scripts", {
        "series_id": series_id, "episode_id": episode_id, "version": version,
        "source": source, "filename": filename, "content": content,
        "parsed": parsed, "created_by": actor, "created_at": _now(),
    })

    store.delete("scenes", {"series_id": series_id, "episode_id": episode_id})
    for i, scene in enumerate(parsed["scenes"], start=1):
        row = {
            "series_id": series_id, "episode_id": episode_id,
            "scene_id": scene.get("scene_id") or f"sc{i:02d}",
            "sequence": int(scene.get("sequence") or i),
            "duration_seconds": int(scene.get("duration_seconds") or 6),
            "location": scene.get("location", ""),
            "lighting_state": scene.get("lighting_state") or "default",
            "characters_in_frame": scene.get("characters_in_frame") or [],
            "wardrobe": scene.get("wardrobe") or {},
            "action": scene.get("action", ""),
            "dialogue": scene.get("dialogue") or [],
            "shot_type": scene.get("shot_type", ""),
            "lens": scene.get("lens", ""),
            "camera_motion": scene.get("camera_motion", ""),
            "continuity_in": scene.get("continuity_in", ""),
            "continuity_out": scene.get("continuity_out", ""),
            "props": scene.get("props") or [],
            "knowledge_required": scene.get("knowledge_required") or [],
            "knowledge_gained": scene.get("knowledge_gained") or [],
            "relationship_changes": scene.get("relationship_changes") or [],
            "is_cliffhanger": bool(scene.get("is_cliffhanger")),
            "status": "draft",
        }
        store.upsert("scenes", row)

    patch: dict = {"updated_at": _now()}
    if parsed.get("cliffhanger"):
        existing = store.get("episodes", {"series_id": series_id, "episode_id": episode_id}) or {}
        merged = {**(existing.get("cliffhanger") or {}), **parsed["cliffhanger"]}
        # Keep a hook the producer already wrote rather than blanking it.
        if not merged.get("hook"):
            merged["hook"] = (existing.get("cliffhanger") or {}).get("hook", "")
        patch["cliffhanger"] = merged
    if parsed.get("opening_state"):
        patch["opening_state"] = parsed["opening_state"]
    if parsed.get("title"):
        patch["title"] = parsed["title"]
    if parsed.get("logline"):
        patch["logline"] = parsed["logline"]
    store.update("episodes", {"series_id": series_id, "episode_id": episode_id}, patch)

    from .ingest import history
    history(series_id, episode_id, "script.saved", entity_type="script", entity_id=str(record["id"]),
            detail={"version": version, "scenes": len(parsed["scenes"]), "source": source}, actor=actor)
    return {"version": version, "scenes": len(parsed["scenes"]), "format": parsed.get("format")}
