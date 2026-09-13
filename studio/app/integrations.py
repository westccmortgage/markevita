"""Provider integration status.

Reports whether each provider is configured, which model is selected, when it
was last tested and the last error. Secret values never leave this module —
only booleans, model names, timestamps and error strings are returned.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from .config import settings
from .store import store

# name -> (required env vars, model env var, model default, what it powers)
PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic",
        "required": ["ANTHROPIC_API_KEY"],
        "model_env": "ANTHROPIC_MODEL",
        "model_default": "claude-sonnet-5",
        "powers": "Scene direction and vision QC",
    },
    "openai": {
        "label": "OpenAI",
        "required": ["OPENAI_API_KEY"],
        "model_env": "OPENAI_MODEL",
        "model_default": "gpt-4.1",
        "powers": "Reserved alternate director / QC model",
    },
    "fal": {
        "label": "fal.ai",
        "required": ["FAL_KEY"],
        "model_env": "FAL_VIDEO_MODEL",
        "model_default": "fal-ai/veo3.1/fast/image-to-video",
        "powers": "Reference images, keyframes, video, lip-sync",
        "extra_models": ["FAL_IMAGE_MODEL", "FAL_LIPSYNC_MODEL"],
    },
    "elevenlabs": {
        "label": "ElevenLabs",
        "required": ["ELEVENLABS_API_KEY"],
        "model_env": "ELEVENLABS_MODEL_ID",
        "model_default": "eleven_v3",
        "powers": "Character voices",
    },
    "r2": {
        "label": "Cloudflare R2",
        "required": ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"],
        "model_env": "R2_BUCKET",
        "model_default": "",
        "powers": "All media: references, takes, voices, subtitles, masters, manifests",
    },
    "supabase": {
        "label": "Supabase",
        "required": ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"],
        "model_env": "SUPABASE_URL",
        "model_default": "",
        "powers": "Administrator auth and all studio records",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _present(var: str) -> bool:
    return bool((os.getenv(var) or "").strip())


def _selected_model(spec: dict) -> str:
    """Model/target identifier. For R2 and Supabase this is a bucket or URL —
    both are non-secret identifiers, not credentials."""
    env = spec.get("model_env") or ""
    if env in ("R2_BUCKET",):
        return os.getenv(env, "") or ""
    if env == "SUPABASE_URL":
        return os.getenv(env, "") or ""
    return os.getenv(env, "") or spec.get("model_default", "")


def status(provider: str) -> dict:
    """Status for one provider. Contains no secret values."""
    spec = PROVIDERS[provider]
    missing = [v for v in spec["required"] if not _present(v)]
    placeholders = [v for v in spec['required']
                    if (os.getenv(v) or '').strip().lower() in ('changeme', 'your_key_here', 'xxx')]
    record = store.get("integration_status", {"provider": provider}) or {}
    extra = {}
    for var in spec.get("extra_models", []):
        defaults = {'FAL_IMAGE_MODEL': 'fal-ai/nano-banana-2/edit', 'FAL_LIPSYNC_MODEL': 'fal-ai/sync-lipsync/v2'}
        val = os.getenv(var, "") or defaults.get(var, '')
        if val:
            extra[var] = val
    return {
        "provider": provider,
        "label": spec["label"],
        "powers": spec["powers"],
        # Presence is not proof of inference permission. A failed test must
        # remain visible instead of being overwritten by a green presence flag.
        "connected": not missing and not placeholders and not record.get('last_error'),
        "state": 'Missing' if missing else ('Check failed' if placeholders or record.get('last_error') else 'Configured — access not verified'),
        "missing": missing,
        "required": spec["required"],
        "model": _selected_model(spec),
        "extra_models": extra,
        "last_test_at": record.get("last_test_at"),
        "last_error": ('Placeholder value in: ' + ', '.join(placeholders)) if placeholders else record.get("last_error"),
    }


def status_all() -> list[dict]:
    return [status(p) for p in PROVIDERS]


def test_connection(provider: str) -> dict:
    """Record a connection test.

    Offline by default: it verifies that every required variable is present and
    non-placeholder, without sending a request to the provider. This keeps the
    guarantee of zero provider traffic. Setting
    STUDIO_ALLOW_CONNECTION_TESTS=true permits free metadata probes — still
    never a generation call.
    """
    if provider not in PROVIDERS:
        raise KeyError(provider)
    spec = PROVIDERS[provider]
    missing = [v for v in spec["required"] if not _present(v)]
    error: str | None = None
    if missing:
        error = "Missing: " + ", ".join(missing)
    else:
        placeholders = [v for v in spec["required"]
                        if (os.getenv(v) or "").strip().lower() in ("changeme", "your_key_here", "xxx")]
        if placeholders:
            error = "Placeholder value in: " + ", ".join(placeholders)
        elif settings.allow_connection_tests:
            error = _probe(provider)
        else:
            error = None
    store.upsert("integration_status", {
        "provider": provider,
        "connected": not missing and not error,
        "model": _selected_model(spec),
        "last_test_at": _now(),
        "last_error": error,
        "updated_at": _now(),
    })
    return status(provider)


def _probe(provider: str) -> str | None:
    """Free metadata probe. Only reachable with STUDIO_ALLOW_CONNECTION_TESTS=true.
    Never calls a generation endpoint, so it can never incur generation cost."""
    import httpx
    try:
        if provider == "elevenlabs":
            r = httpx.get("https://api.elevenlabs.io/v1/user",
                          headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]}, timeout=10)
            return None if r.status_code == 200 else f"HTTP {r.status_code}"
        if provider == "supabase":
            r = httpx.get(f"{os.environ['SUPABASE_URL'].rstrip('/')}/rest/v1/",
                          headers={"apikey": os.environ["SUPABASE_SERVICE_ROLE_KEY"]}, timeout=10)
            return None if 200 <= r.status_code < 300 else f"HTTP {r.status_code}"
        if provider == "r2":
            import boto3
            boto3.client(
                "s3",
                endpoint_url=os.getenv("R2_S3_ENDPOINT") or f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                region_name="auto",
            ).head_bucket(Bucket=os.environ["R2_BUCKET"])
            return None
        # anthropic / openai / fal: no free unauthenticated metadata endpoint we
        # want to depend on. Presence check only.
        return None
    except Exception as e:
        # SDK exceptions can contain keys, capability URLs and headers.
        return f"Connection check failed ({type(e).__name__})."
