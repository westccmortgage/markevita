import base64
import json
import re
from pathlib import Path

from . import prompts


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _img_block(path: Path) -> dict:
    ext = path.suffix.lower().lstrip(".")
    media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext]
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": base64.standard_b64encode(path.read_bytes()).decode()}}


class LLM:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.client = None
        self.calls = 0
        if not cfg.dry_run:
            import anthropic
            self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    def _json(self, system: str, content, max_tokens: int = 8000) -> dict:
        self.calls += 1
        resp = self.client.messages.create(model=self.cfg.anthropic_model, max_tokens=max_tokens, system=system,
                                           messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        try:
            return json.loads(_strip_fences(text))
        except json.JSONDecodeError:
            self.calls += 1
            fix = self.client.messages.create(model=self.cfg.anthropic_model, max_tokens=max_tokens,
                                              system="Return ONLY valid JSON. Fix the following so it parses. No prose.",
                                              messages=[{"role": "user", "content": text}])
            return json.loads(_strip_fences("".join(b.text for b in fix.content if getattr(b, "type", "") == "text")))

    # ---------- bible ----------

    def import_bible_markdown(self, bible_md: str) -> dict:
        """Только для tools/import_bible_md.py: markdown -> JSON-пакет v2. В пайплайне не используется."""
        if self.cfg.dry_run:
            raise RuntimeError("import needs --live (Anthropic call, not a generation cost)")
        return self._json(prompts.BIBLE_PARSE, bible_md, max_tokens=16000)

    # ---------- direction ----------

    def direct(self, bible: dict, scenes: list[dict]) -> dict:
        if self.cfg.dry_run:
            return {"scenes": [{
                "scene_id": s["scene_id"], "lens": "50mm", "time_of_day": "after hours",
                "continuity": {"characters": {c: {"wardrobe": "canonical", "expression": "neutral", "props": "none", "position": "default mark"} for c in s["characters_in_frame"]},
                               "location_state": "abstract light", "entrance_state": s.get("continuity_in") or "", "exit_state": s.get("continuity_out") or ""},
                "keyframe_prompt": f"{' and '.join(c.upper() + ' (see reference)' for c in s['characters_in_frame']) or 'Empty frame'} in the {s['location']}. {s['action']}",
                "video_prompt": f"{s['action']} Camera: {s.get('camera_motion')}.",
                "negative": "", "keyframe_expected": s["action"], "video_expected": s["action"],
            } for s in scenes]}
        payload = {"BIBLE": {k: bible[k] for k in ("characters", "locations", "props", "relationships", "secrets") if k in bible},
                   "STYLE": bible.get("style_sentence", ""), "CAMERA_RULES": bible.get("camera_rules", ""), "SCENES": scenes}
        return self._json(prompts.DIRECTION, json.dumps(payload, ensure_ascii=False), max_tokens=24000)

    # ---------- QC ----------

    def _refs_content(self, refs: list[tuple[str, Path]]) -> list:
        content = []
        for label, p in refs:
            content.append({"type": "text", "text": f"REFERENCE: {label}"})
            content.append(_img_block(p))
        return content

    def qc_image(self, refs: list[tuple[str, Path]], candidate: Path, expected: str) -> dict:
        if self.cfg.dry_run:
            return {"pass": True, "score": 9, "issues": [], "fix_hint": ""}
        content = self._refs_content(refs)
        content.append({"type": "text", "text": f"CANDIDATE frame. EXPECTED: {expected}"})
        content.append(_img_block(candidate))
        return self._json(prompts.QC_IMAGE, content, max_tokens=800)

    def qc_video(self, refs: list[tuple[str, Path]], frames: list[Path], expected: str) -> dict:
        if self.cfg.dry_run:
            return {"pass": True, "score": 9, "issues": [], "fix_hint": ""}
        content = self._refs_content(refs)
        content.append({"type": "text", "text": f"CLIP FRAMES start/middle/end. EXPECTED: {expected}"})
        content += [_img_block(f) for f in frames]
        return self._json(prompts.QC_VIDEO, content, max_tokens=800)
