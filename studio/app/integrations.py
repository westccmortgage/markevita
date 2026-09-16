"""Provider integration status.

Reports configuration, selected models and check results. A fal key's public
UUID prefix can be compared with its dashboard entry; secrets never leave here.
"""
from __future__ import annotations

import os
import hashlib
from datetime import datetime, timezone
from serial.fal_auth import normalize_key, key_problem, key_id_prefix

from .config import settings
from .store import store

# Deliberately process-local: a restart or changed credential requires a new
# check. A prior key's successful/failed check must not describe its replacement.
_fal_check = None


def _fal_identity():
    return hashlib.sha256(normalize_key(os.getenv('FAL_KEY')).encode()).hexdigest()

# name -> (required env vars, model env var, model default, what it powers)
PROVIDERS: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic",
        "required": ["ANTHROPIC_API_KEY"],
        "model_env": "ANTHROPIC_MODEL",
        "model_default": "claude-opus-5",
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
    verified = False
    if provider == 'fal':
        record = _fal_check[1] if _fal_check and _fal_check[0] == _fal_identity() else {}
        record = dict(record)
        if error := key_problem(os.getenv('FAL_KEY')):
            record['last_error'] = error
        verified = bool(record.get('verified')) and not record.get('last_error')
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
        "state": 'Missing' if missing else ('Check failed' if placeholders or record.get('last_error') else ('Authentication checked — generation not tested' if verified else 'Configured — access not verified')),
        "auth_verified": verified,
        "key_id_prefix": key_id_prefix(os.getenv('FAL_KEY')) if provider == 'fal' else None,
        "connection_tests_enabled": settings.allow_connection_tests,
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
    global _fal_check
    if provider not in PROVIDERS:
        raise KeyError(provider)
    spec = PROVIDERS[provider]
    identity = _fal_identity() if provider == 'fal' else None
    missing = [v for v in spec["required"] if not _present(v)]
    error: str | None = None
    performed = False
    if missing:
        error = "Missing: " + ", ".join(missing)
    else:
        placeholders = [v for v in spec["required"]
                        if (os.getenv(v) or "").strip().lower() in ("changeme", "your_key_here", "xxx")]
        if placeholders:
            error = "Placeholder value in: " + ", ".join(placeholders)
        elif provider == 'fal' and key_problem(os.getenv('FAL_KEY')):
            error = key_problem(os.getenv('FAL_KEY'))
        elif settings.allow_connection_tests:
            error = _probe(provider)
            performed = provider == 'fal'
        else:
            error = None
    tested_at = _now()
    if provider == 'fal':
        _fal_check = (identity, {'last_test_at': tested_at, 'last_error': error,
                                      'verified': performed and not error})
    store.upsert("integration_status", {
        "provider": provider,
        "connected": not missing and not error,
        "model": _selected_model(spec),
        "last_test_at": tested_at,
        "last_error": error,
        "updated_at": _now(),
    })
    return status(provider)


def _probe(provider: str) -> str | None:
    """Free metadata probe. Only reachable with STUDIO_ALLOW_CONNECTION_TESTS=true.
    Never calls a generation endpoint, so it can never incur generation cost."""
    import httpx
    try:
        if provider == 'fal':
            key = normalize_key(os.getenv('FAL_KEY'))
            if error := key_problem(key):
                return error
            # Model search is public and CANNOT prove authentication. Pricing
            # requires an API-scope key and only reads metadata, never inference.
            r = httpx.get('https://api.fal.ai/v1/models/pricing',
                          params={'endpoint_id': 'fal-ai/nano-banana-2/edit'},
                          headers={'Authorization': 'Key ' + key},
                          timeout=10, follow_redirects=False)
            if r.status_code == 401:
                return 'fal.ai rejected the key loaded by this server (HTTP 401). Check the full FAL_KEY value in Render.'
            if r.status_code != 200:
                return f'fal.ai metadata check returned HTTP {r.status_code}. Authentication was not confirmed.'
            data = r.json()
            if not isinstance(data, dict) or not isinstance(data.get('prices'), list) or not data['prices']:
                return 'fal.ai returned an unexpected metadata response. Authentication was not confirmed.'
            return None
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
        # anthropic / openai: no free unauthenticated metadata endpoint we
        # want to depend on. Presence check only.
        return None
    except Exception as e:
        # SDK exceptions can contain keys, capability URLs and headers.
        return f"Connection check failed ({type(e).__name__})."
