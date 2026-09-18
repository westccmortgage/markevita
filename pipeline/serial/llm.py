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


# The model resizes anything larger than this before it looks at it, so
# sending more than this buys no accuracy — only tokens, and a request that
# can be refused outright. A reference sheet from the picture model arrives
# far above it: one 2K PNG passes the provider's five-megabyte limit for a
# single image on its own, and a QC call carries the whole reference pack
# plus the candidate. That is a 400 in the middle of the references stage,
# after the images have been generated and paid for.
QC_IMAGE_EDGE = 1568
QC_IMAGE_QUALITY = 85


def _img_block(path: Path) -> dict:
    from io import BytesIO
    from PIL import Image
    with Image.open(path) as im:
        im.load()
        if im.mode not in ("RGB", "L"):
            # JPEG has no alpha; a transparent background becomes white rather
            # than the black that dropping the channel would leave behind.
            flat = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            flat.paste(rgba, mask=rgba.split()[3])
            im = flat
        if max(im.size) > QC_IMAGE_EDGE:
            scale = QC_IMAGE_EDGE / max(im.size)
            im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                           Image.LANCZOS)
        buf = BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=QC_IMAGE_QUALITY)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(buf.getvalue()).decode()}}


class ModelRejected(RuntimeError):
    """The model service refused the request and said why.

    The refusal reads "BadRequestError. Production stopped." on the job page
    unless the sentence the service gave is carried out with it, which leaves
    the one fact that identifies the fault — an image over the size limit, a
    context that does not fit — in a log nobody sees.
    """


TRANSIENT_ATTEMPTS = 4


def _stated_reason(exc) -> str:
    """The service's own sentence, and nothing else from the exchange."""
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    # A gateway failure answers with something that is not the service's own
    # JSON, and then the only sentence there is hangs off the exception.
    message = message or getattr(exc, "message", "")
    return str(message)[:400] if message else ""


class LLM:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.client = None
        self.calls = 0
        if not cfg.dry_run:
            import anthropic
            # A five-hundred from the service, a moment of overload or a
            # dropped connection is not a decision about this request, and it
            # is not worth a twenty-minute run that has already paid for nine
            # reference images. The client retries only what is safe to retry
            # — 408, 409, 429 and 5xx — and backs off between attempts;
            # anything the service actually decided still comes straight back.
            self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key,
                                              max_retries=TRANSIENT_ATTEMPTS, timeout=180)

    def _message(self, params):
        """One completed reply, streamed.

        The direction stage asks for tens of thousands of tokens and the models
        this engine runs on think before they answer, so a plain create() sat
        past the HTTP timeout and the whole run died on APITimeoutError with
        nothing to show for the tokens already spent. Streaming is what the SDK
        needs at this size; the request itself is unchanged.
        """
        with self.client.messages.stream(**params) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "max_tokens":
            raise RuntimeError(
                f"The model ran out of room: {params['max_tokens']} tokens was not enough "
                "to finish. Thinking counts against the same limit.")
        if message.stop_reason == "refusal":
            raise RuntimeError("The model declined this request.")
        return message

    def _create(self, system, content, max_tokens):
        try:
            return self._attempt(system, content, max_tokens)
        except Exception as exc:
            reason = _stated_reason(exc)
            if reason:
                raise ModelRejected(f"{type(exc).__name__}: {reason}") from exc
            raise

    def _attempt(self, system, content, max_tokens):
        params = dict(model=self.cfg.anthropic_model, max_tokens=max_tokens, system=system,
                      messages=[{"role": "user", "content": content}])
        guard = getattr(self.cfg, "paid_calls", None)
        if guard:
            # Token counting is free and gives a pre-call upper cost bound.
            count = self.client.messages.count_tokens(**{k: v for k, v in params.items() if k != "max_tokens"})
            from .costs import anthropic_rates
            input_rate, output_rate = anthropic_rates(self.cfg.anthropic_model)
            reserve = (count.input_tokens * input_rate + max_tokens * output_rate) / 1_000_000
            def actual(result):
                u = result["usage"]
                return (u["input_tokens"] * input_rate + u["output_tokens"] * output_rate) / 1_000_000
            result = guard.once("anthropic", params, reserve,
                                lambda: self._message(params).model_dump(mode="json"), actual)
            return "".join(b["text"] for b in result["content"] if b.get("type") == "text")
        resp = self._message(params)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def _json(self, system: str, content, max_tokens: int = 8000) -> dict:
        self.calls += 1
        text = self._create(system, content, max_tokens)
        try:
            return json.loads(_strip_fences(text))
        except json.JSONDecodeError:
            self.calls += 1
            fix = self._create("Return ONLY valid JSON. Fix the following so it parses. No prose.", text, max_tokens)
            return json.loads(_strip_fences(fix))

    # ---------- bible ----------

    def import_bible_markdown(self, bible_md: str) -> dict:
        """Только для tools/import_bible_md.py: markdown -> JSON-пакет v2. В пайплайне не используется."""
        if self.cfg.dry_run:
            raise RuntimeError("import needs --live (Anthropic call, not a generation cost)")
        return self._json(prompts.BIBLE_PARSE, bible_md, max_tokens=32000)

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
        return self._json(prompts.DIRECTION, json.dumps(payload, ensure_ascii=False), max_tokens=48000)

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
        return self._json(prompts.QC_IMAGE, content, max_tokens=4000)

    def qc_video(self, refs: list[tuple[str, Path]], frames: list[Path], expected: str) -> dict:
        if self.cfg.dry_run:
            return {"pass": True, "score": 9, "issues": [], "fix_hint": ""}
        content = self._refs_content(refs)
        content.append({"type": "text", "text": f"CLIP FRAMES start/middle/end. EXPECTED: {expected}"})
        content += [_img_block(f) for f in frames]
        return self._json(prompts.QC_VIDEO, content, max_tokens=4000)
