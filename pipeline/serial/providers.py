"""Провайдеры. Каждый платный вызов = take с provenance (spec §7, §8):
reserve -> submit (request_id сохраняется до ожидания) -> result -> settle. При падении после submit
повторный запуск забирает результат по request_id, а не создаёт второй платный запрос."""

import subprocess
import time
from pathlib import Path

import requests

from . import prompts
from .state import now, sha256


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


class InputPublisher:
    """Локальный файл -> URL, который примет провайдер. r2_presigned (spec §4) или fal_storage."""

    def __init__(self, cfg, log, r2, key_prefix: str):
        self.cfg, self.log, self.r2, self.prefix = cfg, log, r2, key_prefix
        self._cache: dict[str, str] = {}

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
            import fal_client
            u = fal_client.upload_file(str(path))
        self._cache[h] = u
        return u


class Fal:
    def __init__(self, cfg, log, state, budget, inputs: InputPublisher):
        self.cfg, self.log, self.state, self.budget, self.inputs = cfg, log, state, budget, inputs

    def _wait(self, endpoint: str, request_id: str) -> dict:
        import fal_client
        delay = 3
        while True:
            st = fal_client.status(endpoint, request_id, with_logs=False)
            if isinstance(st, fal_client.Completed):
                return fal_client.result(endpoint, request_id)
            time.sleep(delay)
            delay = min(delay + 2, 15)

    def run(self, endpoint: str, args: dict, take_id: str, est_cost: float, what: str, stub) -> tuple[dict, dict]:
        """Возвращает (result, take). stub() делает результат в dry-run."""
        take = self.state.take(take_id)
        if take.get("status") == "succeeded" and take.get("result"):
            self.log(f"  {take_id}: уже есть результат, пропускаю")
            return take["result"], take
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
                import fal_client
                try:
                    handle = fal_client.submit(endpoint, arguments=args)
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
    est = costs.video_cost(seconds, cfg.video_generate_audio, cfg.video_resolution)
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

def tts(cfg, log, text: str, delivery: str, voice_id: str, dest: Path, model_id: str | None = None, settings: dict | None = None) -> dict:
    """Одна реплика -> mp3. Возвращает provenance dict. eleven_v3: delivery как audio tag в квадратных скобках."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    model_id = model_id or cfg.elevenlabs_model_id
    is_v3 = model_id.startswith("eleven_v3")
    spoken = f"[{delivery}] {text}" if (is_v3 and delivery) else text
    settings = dict(settings or {"stability": 0.5, "similarity_boost": 0.8, "style": 0.2})
    if not is_v3:
        settings.setdefault("speed", 1.0)
    prov = {"provider": "elevenlabs", "model_id": model_id, "voice_id_ref": f"env:{voice_id[:4]}…" if voice_id else "",
            "text": text, "spoken_text": spoken, "voice_settings": settings, "created_at": now()}
    if cfg.dry_run:
        secs = max(1.0, min(7.5, len(text.split()) / 2.4))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=220:duration={secs:.2f}",
                        "-c:a", "libmp3lame", str(dest)], check=True)
    else:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"
        body = {"text": spoken, "model_id": model_id, "voice_settings": settings}
        r = requests.post(url, json=body, headers={"xi-api-key": cfg.elevenlabs_api_key, "Accept": "audio/mpeg"}, timeout=180)
        if r.status_code >= 400:
            raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:300]}")
        dest.write_bytes(r.content)
        prov["request_id"] = r.headers.get("request-id", "")
    prov["local_path"] = str(dest); prov["checksum"] = sha256(dest)
    return prov
