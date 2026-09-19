import os
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import load_dotenv
from .fal_auth import normalize_key


def _bool(v, default: bool) -> bool:
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None or v == "" else v


# Обе поддерживаемые модели видео принимают одну и ту же схему запроса и
# отличаются только ценой и качеством. Список — источник истины и для
# валидации настройки, и для выпадающего списка в студии.
VIDEO_MODELS = {
    "fal-ai/veo3.1/fast/image-to-video": "Veo 3.1 Fast",
    "fal-ai/veo3.1/image-to-video": "Veo 3.1",
}
DEFAULT_VIDEO_MODEL = "fal-ai/veo3.1/fast/image-to-video"


@dataclass
class Config:
    # LLM
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    # fal
    fal_key: str = ""
    fal_video_model: str = DEFAULT_VIDEO_MODEL
    fal_image_model: str = "fal-ai/nano-banana-2/edit"
    fal_image_t2i_model: str = "fal-ai/nano-banana-2"
    fal_image_pro_model: str = "fal-ai/nano-banana-pro"
    fal_lipsync_model: str = "fal-ai/sync-lipsync/v2"
    fal_music_model: str = "cassetteai/music-generator"
    lipsync_variant: str = "lipsync-2"
    # elevenlabs
    elevenlabs_api_key: str = ""
    elevenlabs_model_id: str = "eleven_v3"
    voice_ids: dict = field(default_factory=dict)
    phone_fx_speakers: list = field(default_factory=lambda: ["client_voice"])
    # R2
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_s3_endpoint: str = ""
    r2_public_base_url: str = ""
    provider_input_mode: str = "r2_presigned"   # r2_presigned | fal_storage
    # limits
    max_episode_budget_usd: float = 50.0
    max_scene_regenerations: int = 2
    # What the quality check must score for a shot to be kept, and how far
    # below that is close enough to keep anyway. Every miss is a fully paid
    # regeneration, so the difference between 7 and 6 here is the difference
    # between one clip and two for the same scene. Set the tolerance to 0 to
    # pay for the retry every time.
    qc_pass_score: float = 7.0
    qc_close_enough: float = 1.0
    max_clip_seconds: int = 10
    # How long one queued provider request may stay unfinished before the run
    # gives up waiting. Without it a job sits in "running" forever: the worker
    # keeps renewing its lease, the stage never advances, and nobody is told.
    fal_request_timeout_seconds: int = 1800
    allowed_clip_seconds: tuple = (4, 6, 8)
    min_scenes: int = 12
    max_scenes: int = 18
    min_episode_seconds: int = 90
    max_episode_seconds: int = 120
    # generation settings (spec 3.1 / 3.2)
    video_resolution: str = "1080p"
    video_generate_audio: bool = False
    video_auto_fix: bool = False
    image_resolution: str = "1K"
    # publishing
    instagram_publish_enabled: bool = False
    # runtime
    mode: str = "mock"          # mock | live. live только с --live, PIPELINE_ALLOW_PAID=true и series.approval.status=approved
    allow_paid_env: bool = False
    root: Path = Path(".")

    @property
    def dry_run(self) -> bool:
        return self.mode != "live"

    @classmethod
    def load(cls, root: Path, live: bool = False) -> "Config":
        load_dotenv(root / ".env")
        load_dotenv(root.parent / ".env")  # .env в корне репо тоже подходит
        voice_ids = {}
        for k, v in os.environ.items():
            if k.startswith("ELEVENLABS_VOICE_ID_") and v:
                voice_ids[k[len("ELEVENLABS_VOICE_ID_"):].lower()] = v
        # спец. соответствие из .env.example: ELEVENLABS_VOICE_ID_CLIENT -> client_voice
        if "client" in voice_ids and "client_voice" not in voice_ids:
            voice_ids["client_voice"] = voice_ids["client"]
        acc = _env("R2_ACCOUNT_ID")
        c = cls(
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            anthropic_model=_env("ANTHROPIC_MODEL", "claude-opus-5"),
            fal_key=normalize_key(_env("FAL_KEY")),
            fal_video_model=_env("FAL_VIDEO_MODEL", DEFAULT_VIDEO_MODEL),
            fal_image_model=_env("FAL_IMAGE_MODEL", "fal-ai/nano-banana-2/edit"),
            fal_lipsync_model=_env("FAL_LIPSYNC_MODEL", "fal-ai/sync-lipsync/v2"),
            fal_music_model=_env("FAL_MUSIC_MODEL", "cassetteai/music-generator"),
            lipsync_variant=_env("LIPSYNC_VARIANT", "lipsync-2"),
            elevenlabs_api_key=_env("ELEVENLABS_API_KEY"),
            elevenlabs_model_id=_env("ELEVENLABS_MODEL_ID", "eleven_v3"),
            voice_ids=voice_ids,
            phone_fx_speakers=[s for s in _env("PHONE_FX_SPEAKERS", "client_voice").split(",") if s],
            r2_account_id=acc,
            r2_access_key_id=_env("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_env("R2_SECRET_ACCESS_KEY"),
            r2_bucket=_env("R2_BUCKET"),
            r2_s3_endpoint=_env("R2_S3_ENDPOINT", f"https://{acc}.r2.cloudflarestorage.com" if acc else ""),
            r2_public_base_url=_env("R2_PUBLIC_BASE_URL").rstrip("/"),
            provider_input_mode=_env("PROVIDER_INPUT_MODE", "r2_presigned"),
            max_episode_budget_usd=float(_env("MAX_EPISODE_BUDGET_USD", "50")),
            max_scene_regenerations=int(_env("MAX_SCENE_REGENERATIONS", "2")),
            qc_pass_score=float(_env("QC_PASS_SCORE", "7")),
            qc_close_enough=float(_env("QC_CLOSE_ENOUGH", "1")),
            max_clip_seconds=int(_env("MAX_CLIP_SECONDS", "10")),
            fal_request_timeout_seconds=int(_env("FAL_REQUEST_TIMEOUT_SECONDS", "1800")),
            video_resolution=_env("VIDEO_RESOLUTION", "1080p"),
            video_generate_audio=_bool(os.getenv("VIDEO_GENERATE_AUDIO"), False),
            video_auto_fix=_bool(os.getenv("VIDEO_AUTO_FIX"), False),
            image_resolution=_env("IMAGE_RESOLUTION", "1K"),
            instagram_publish_enabled=_bool(os.getenv("INSTAGRAM_PUBLISH_ENABLED"), False),
            mode="live" if live else "mock",
            allow_paid_env=_bool(os.getenv("PIPELINE_ALLOW_PAID"), False),
            root=root,
        )
        if c.fal_video_model not in VIDEO_MODELS:
            raise ValueError(
                f"FAL_VIDEO_MODEL={c.fal_video_model!r} is not supported. "
                f"Choose one of: {', '.join(sorted(VIDEO_MODELS))}.")
        if c.fal_key:
            os.environ["FAL_KEY"] = c.fal_key
        return c

    @property
    def r2_configured(self) -> bool:
        return all([self.r2_account_id, self.r2_access_key_id, self.r2_secret_access_key, self.r2_bucket])

    def missing_for(self, stages: list[str]) -> list[str]:
        if self.dry_run:
            return []
        need = []
        s = set(stages)
        if s & {"intake", "direction", "keyframes", "video"} and not self.anthropic_api_key:
            need.append("ANTHROPIC_API_KEY")
        if s & {"references", "keyframes", "video", "lipsync"} and not self.fal_key:
            need.append("FAL_KEY")
        if "voice" in s and not self.elevenlabs_api_key:
            need.append("ELEVENLABS_API_KEY")
        if s & {"deliver", "publish"} and not self.r2_configured:
            need.append("R2_ACCOUNT_ID/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET")
        if self.provider_input_mode == "r2_presigned" and s & {"references", "keyframes", "video", "lipsync"} and not self.r2_configured:
            need.append("R2_* (или PROVIDER_INPUT_MODE=fal_storage)")
        return need
