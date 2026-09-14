"""Провайдеры. Каждый платный вызов = take с provenance (spec §7, §8):
reserve -> submit (request_id сохраняется до ожидания) -> result -> settle. При падении после submit
повторный запуск забирает результат по request_id, а не создаёт второй платный запрос."""

import subprocess
import tempfile
import time
from pathlib import Path

import requests

from . import prompts
from .state import now, sha256


def download(url: str, dest: Path) -> Path:
    """Collect an existing provider asset; never submit another generation.

    A truncated response must not replace a complete file. Retry only this
    idempotent GET and atomically publish the completed download.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        partial = None
        try:
            with requests.get(url, stream=True, timeout=(30, 120)) as r:
                r.raise_for_status()
                with tempfile.NamedTemporaryFile(dir=dest.parent, prefix=".download-", suffix=".part", delete=False) as f:
                    partial = Path(f.name)
                    size = 0
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        size += len(chunk)
                expected = r.headers.get("Content-Length")
                if not size or (expected and expected.isdigit() and not r.headers.get("Content-Encoding") and size != int(expected)):
                    raise requests.exceptions.ChunkedEncodingError("Incomplete provider asset download")
            partial.replace(dest)
            return dest
        except (requests.exceptions.ChunkedEncodingError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout, requests.exceptions.HTTPError) as exc:
            retryable = not isinstance(exc, requests.exceptions.HTTPError) or (
                exc.response is not None and exc.response.status_code in (408, 429, 500, 502, 503, 504))
            if not retryable or attempt == 2:
                raise
            time.sleep(attempt + 1)
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)


def fal_client_for(key: str):
    """An SDK client bound to THIS key.

    The module-level fal_client functions share one client that resolves its
    credential from the environment once and caches it. A process that set
    FAL_KEY after that first resolution keeps submitting with one key and
    reading results with another, which surfaces as an HTTP 403 on a request
    the queue had already accepted. Binding the key explicitly removes the
    second credential entirely.
    """
    from fal_client.client import SyncClient
    return SyncClient(key=key)


class InputPublisher:
    """Локальный файл -> URL, который примет провайдер. r2_presigned (spec §4) или fal_storage."""

    def __init__(self, cfg, log, r2, key_prefix: str):
        self.cfg, self.log, self.r2, self.prefix = cfg, log, r2, key_prefix
        self._cache: dict[str, str] = {}
        self._client = None

    def url(self, path: Path, key_hint: str = "inputs") -> str:
        h = sha256(path)
        if h in self._cache:
            return self._cache[h]
        if self.cfg.dry_run:
            u = f"dry://{path.name}"
        elif self.cfg.provider_input_mode == "r2_presigned" and self.r2 and self.r2.enabled:
            key = f"{self.prefix}/{key_hint}/{h[:16]}{path.suffix.lower()}"
            self.r2.put(path, key)
            u = self.r2.presign(key, 3600)
        else:
            if self._client is None:
                self._client = fal_client_for(self.cfg.fal_key)
            u = self._client.upload_file(str(path))
        self._cache[h] = u
        return u


class Fal:
    def __init__(self, cfg, log, state, budget, inputs: InputPublisher):
        self.cfg, self.log, self.state, self.budget, self.inputs = cfg, log, state, budget, inputs

    @property
    def client(self):
        client = getattr(self, "_client", None)
        if client is None:
            client = self._client = fal_client_for(self.cfg.fal_key)
        return client

    def _wait(self, endpoint: str, request_id: str) -> dict:
        import fal_client
        delay = 3
        while True:
            st = self.client.status(endpoint, request_id, with_logs=False)
            if isinstance(st, fal_client.Completed):
                return self.client.result(endpoint, request_id)
            time.sleep(delay)
            delay = min(delay + 2, 15)

    def run(self, endpoint: str, args: dict, take_id: str, est_cost: float, what: str, stub) -> tuple[dict, dict]:
        """Возвращает (result, take). stub() делает результат в dry-run."""
        take = self.state.take(take_id)
        if take.get("status") == "succeeded" and take.get("result"):
            self.log(f"  {take_id}: уже есть результат, пропускаю")
            return take["result"], take
        submitted_to = take.get("endpoint")
        if (take.get("request_id") and submitted_to and submitted_to != endpoint
                and take.get("status") not in ("succeeded",)):
            # The series may have been switched to the other Veo model since.
            # A saved request belongs to the model that accepted it: polling
            # the new one would report nothing and invite a second paid call.
            self.log(f"  {take_id}: сохранённый запрос принадлежит {submitted_to}, забираю результат оттуда")
            endpoint = submitted_to
        take.update({"provider": "fal.ai", "endpoint": endpoint, "what": what,
                     "params": {k: v for k, v in args.items() if k not in ("image_url", "image_urls", "video_url", "audio_url")},
                     "input_refs": {k: ("<presigned/transport url omitted>" if isinstance(v, str) else f"{len(v)} urls")
                                    for k, v in args.items() if k in ("image_url", "image_urls", "video_url", "audio_url")},
                     "estimated_cost": round(est_cost, 4)})
        if take.get("status") == "submitted" and take.get("request_id") and not self.cfg.dry_run:
            self.log(f"  {take_id}: найден незавершённый request {take['request_id']}, забираю результат")
            result = self._wait(endpoint, take["request_id"])
        else:
            self.budget.reserve(est_cost, what)
            take["status"] = "reserved"; take["submitted_at"] = now()
            self.state.save()
            if self.cfg.dry_run:
                take["request_id"] = f"dry-{take_id}"
                result = stub()
            else:
                try:
                    handle = self.client.submit(endpoint, arguments=args)
                except Exception as e:
                    take["status"] = "failed"; take["error"] = str(e)[:500]
                    self.budget.settle(est_cost, 0.0, f"{what} (submit failed)", take_id)
                    self.state.set_status("failed_provider")
                    raise
                take["request_id"] = handle.request_id; take["status"] = "submitted"
                self.state.save()
                try:
                    result = self._wait(endpoint, handle.request_id)
                except Exception as e:
                    # запрос мог быть оплачен: списываем оценку (spec §6: billed failures count)
                    take["status"] = "failed"; take["error"] = str(e)[:500]
                    self.budget.settle(est_cost, est_cost, f"{what} (failed after submit)", take_id)
                    self.state.set_status("failed_provider")
                    raise
            self.budget.settle(est_cost, est_cost, what, take_id)
        take["status"] = "succeeded"; take["completed_at"] = now()
        take["actual_cost"] = round(est_cost, 4); take["actual_cost_source"] = "estimate (fal не возвращает цену в ответе)"
        take["result"] = {k: v for k, v in result.items() if k in ("images", "video", "seed", "description")}
        self.state.save()
        return result, take


# ---------------- dry-run заглушки ----------------

def _placeholder_png(dest: Path, text: str, aspect: str) -> Path:
    from PIL import Image, ImageDraw
    size = {"9:16": (1080, 1920), "16:9": (1920, 1080), "3:4": (1080, 1440), "1:1": (1080, 1080)}.get(aspect, (1080, 1920))
    img = Image.new("RGB", size, (236, 228, 214))
    ImageDraw.Draw(img).text((40, 40), text[:300], fill=(60, 50, 40))
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    return dest


def _placeholder_mp4(dest: Path, seconds: int, text: str, aspect: str) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    size = "1080x1920" if aspect == "9:16" else "1920x1080"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=0xE8DFD0:s={size}:r=24:d={seconds}",
                    "-vf", f"drawtext=text='{text[:40]}':fontcolor=0x333333:fontsize=48:x=40:y=120",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(dest)], check=True)
    return dest


# ---------------- images ----------------

def gen_image(fal: Fal, take_id: str, prompt: str, dest: Path, refs: list[Path], aspect: str, pro: bool, what: str) -> tuple[Path, dict]:
    from . import costs
    cfg = fal.cfg
    args = {"prompt": prompt, "num_images": 1, "aspect_ratio": aspect, "output_format": "png",
            "resolution": cfg.image_resolution, "enable_web_search": False, "safety_tolerance": "4"}
    if refs:
        endpoint = cfg.fal_image_model
        args["image_urls"] = [fal.inputs.url(p, "references") for p in refs[:14]]
    else:
        endpoint = cfg.fal_image_pro_model if pro else cfg.fal_image_t2i_model
    est = costs.image_cost(pro and not refs, cfg.image_resolution)
    result, take = fal.run(endpoint, args, take_id, est, what,
                           stub=lambda: {"images": [{"url": "dry://img"}], "description": ""})
    take["prompt"] = prompt; take["negative"] = prompts.NEGATIVE_IMAGE
    take["input_checksums"] = [sha256(p) for p in refs]
    if cfg.dry_run:
        _placeholder_png(dest, prompt, aspect)
    else:
        download(result["images"][0]["url"], dest)
    take["local_path"] = str(dest); take["checksum"] = sha256(dest)
    fal.state.save()
    return dest, take


# ---------------- video ----------------

def gen_video(fal: Fal, take_id: str, keyframe: Path, prompt: str, negative: str, seconds: int, dest: Path, aspect: str, what: str) -> tuple[Path, dict]:
    from . import costs
    cfg = fal.cfg
    args = {"prompt": prompt, "image_url": fal.inputs.url(keyframe, "keyframes"), "aspect_ratio": aspect,
            "duration": f"{seconds}s", "resolution": cfg.video_resolution, "generate_audio": cfg.video_generate_audio,
            "negative_prompt": (negative + ", " if negative else "") + prompts.NEGATIVE_VIDEO,
            "auto_fix": cfg.video_auto_fix, "safety_tolerance": "4"}
    est = costs.video_cost(seconds, cfg.video_generate_audio, cfg.video_resolution, cfg.fal_video_model)
    result, take = fal.run(cfg.fal_video_model, args, take_id, est, what, stub=lambda: {"video": {"url": "dry://video"}})
    take["prompt"] = prompt; take["negative"] = args["negative_prompt"]
    take["input_checksums"] = [sha256(keyframe)]
    if cfg.dry_run:
        _placeholder_mp4(dest, seconds, prompt, aspect)
    else:
        download(result["video"]["url"], dest)
    take["local_path"] = str(dest); take["checksum"] = sha256(dest)
    fal.state.save()
    return dest, take


# ---------------- lipsync ----------------

def lipsync(fal: Fal, take_id: str, video: Path, audio: Path, seconds: float, dest: Path, what: str) -> tuple[Path, dict]:
    from . import costs
    cfg = fal.cfg
    args = {"model": cfg.lipsync_variant, "video_url": fal.inputs.url(video, "lipsync"), "audio_url": fal.inputs.url(audio, "lipsync"),
            "sync_mode": "cut_off"}
    est = costs.lipsync_cost(seconds, cfg.lipsync_variant)
    result, take = fal.run(cfg.fal_lipsync_model, args, take_id, est, what, stub=lambda: {"video": {"url": "dry://lipsync"}})
    take["input_checksums"] = [sha256(video), sha256(audio)]
    if cfg.dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "copy", "-c:a", "aac", "-shortest", str(dest)], check=True)
    else:
        download(result["video"]["url"], dest)
    take["local_path"] = str(dest); take["checksum"] = sha256(dest)
    fal.state.save()
    return dest, take


# ---------------- ElevenLabs ----------------

# eleven_v3 audio tags are short auditory cues — [whispers], [sighs],
# [sarcastic]. A sentence of stage direction in brackets is not a tag: the
# model reads it out. "You knew?" — two words — was voiced as 5.86 seconds
# because sixteen words of direction went in with it.
MAX_TAG_WORDS = 4


def audio_tag(delivery: str) -> str:
    """The bracketed cue for a delivery note, or '' when it is prose.

    Direction written for a human ("clearly articulated American English; a
    question addressed to Adrian, hurt turning into suspicion") is kept in the
    package and shown to the director, but never spoken.
    """
    cue = " ".join((delivery or "").split())
    if not cue or len(cue.split()) > MAX_TAG_WORDS or any(c in cue for c in ".;:"):
        return ""
    return cue


def tts(cfg, log, text: str, delivery: str, voice_id: str, dest: Path, model_id: str | None = None, settings: dict | None = None, language_code: str | None = None) -> dict:
    """Одна реплика -> mp3. Возвращает provenance dict. eleven_v3: delivery как audio tag в квадратных скобках."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    model_id = model_id or cfg.elevenlabs_model_id
    is_v3 = model_id.startswith("eleven_v3")
    tag = audio_tag(delivery) if is_v3 else ""
    spoken = f"[{tag}] {text}" if tag else text
    if is_v3 and delivery and not tag:
        log(f"voice: delivery is direction, not an audio tag; not spoken: {delivery[:60]}…")
    settings = dict(settings or {"stability": 0.5, "similarity_boost": 0.8, "style": 0.2})
    if not is_v3:
        settings.setdefault("speed", 1.0)
    prov = {"provider": "elevenlabs", "model_id": model_id, "voice_id_ref": f"env:{voice_id[:4]}…" if voice_id else "",
            "text": text, "spoken_text": spoken, "voice_settings": settings, "created_at": now()}
    if language_code:
        language_code = language_code.replace("_", "-").split("-", 1)[0].lower()
        prov["language_code"] = language_code
    if cfg.dry_run:
        secs = max(1.0, min(7.5, len(text.split()) / 2.4))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=220:duration={secs:.2f}",
                        "-c:a", "libmp3lame", str(dest)], check=True)
    else:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
        body = {"text": spoken, "model_id": model_id, "voice_settings": settings}
        if language_code and model_id != "eleven_multilingual_v2":
            body["language_code"] = language_code
        r = requests.post(url, json=body, headers={"xi-api-key": cfg.elevenlabs_api_key, "Accept": "audio/mpeg"}, timeout=180)
        if r.status_code >= 400:
            raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:300]}")
        dest.write_bytes(r.content)
        prov["request_id"] = r.headers.get("request-id", "")
    prov["local_path"] = str(dest); prov["checksum"] = sha256(dest)
    return prov
