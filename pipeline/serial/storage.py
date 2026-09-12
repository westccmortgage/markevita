"""Cloudflare R2 (spec §4). Ключи:
series/{series_id}/bible/characters/{character_id}/{version}/...
series/{series_id}/episodes/{episode_id}/scenes/{scene_id}/takes/{take_id}/...
series/{series_id}/episodes/{episode_id}/masters/{version}/...
series/{series_id}/episodes/{episode_id}/public/...   <- только это отдаёт публичный домен (R2_PUBLIC_BASE_URL)"""

import mimetypes
from pathlib import Path


class R2:
    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        self.enabled = cfg.r2_configured and not cfg.dry_run
        self.client = None
        self._put_cache: set[str] = set()
        if self.enabled:
            import boto3
            from botocore.config import Config as BotoConfig
            self.client = boto3.client("s3", endpoint_url=cfg.r2_s3_endpoint, aws_access_key_id=cfg.r2_access_key_id,
                                       aws_secret_access_key=cfg.r2_secret_access_key, region_name="auto",
                                       config=BotoConfig(signature_version="s3v4"))

    def put(self, path: Path, key: str) -> str:
        if key in self._put_cache:
            return key
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if self.enabled:
            self.client.upload_file(str(path), self.cfg.r2_bucket, key, ExtraArgs={"ContentType": ctype})
        else:
            self.log(f"  [dry] R2 put {key}")
        self._put_cache.add(key)
        return key

    def presign(self, key: str, expires: int = 3600) -> str:
        """Bearer-URL: не логировать целиком."""
        if not self.enabled:
            return f"dry://presigned/{key}"
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.cfg.r2_bucket, "Key": key}, ExpiresIn=expires)

    def copy(self, src_key: str, dst_key: str) -> str:
        if self.enabled:
            self.client.copy_object(Bucket=self.cfg.r2_bucket, CopySource={"Bucket": self.cfg.r2_bucket, "Key": src_key}, Key=dst_key)
        else:
            self.log(f"  [dry] R2 copy {src_key} -> {dst_key}")
        return dst_key

    def public_url(self, key: str) -> str:
        base = self.cfg.r2_public_base_url or "r2://public-domain-not-configured"
        return f"{base}/{key}"


class Keys:
    def __init__(self, series_id: str, episode_id: str):
        self.s, self.e = series_id, episode_id
        self.ep = f"series/{series_id}/episodes/{episode_id}"

    def bible_char(self, cid: str, version: str, name: str) -> str:
        return f"series/{self.s}/bible/characters/{cid}/{version}/{name}"

    def bible_loc(self, lid: str, version: str, name: str) -> str:
        return f"series/{self.s}/bible/locations/{lid}/{version}/{name}"

    def bible_prop(self, pid: str, version: str, name: str) -> str:
        return f"series/{self.s}/bible/props/{pid}/{version}/{name}"

    def brief(self) -> str:
        return f"{self.ep}/brief.json"

    def scene_ref(self, sid: str, name: str) -> str:
        return f"{self.ep}/scenes/{sid}/references/{name}"

    def take(self, sid: str, take_id: str, name: str) -> str:
        return f"{self.ep}/scenes/{sid}/takes/{take_id}/{name}"

    def audio(self, cid: str, line_id: str, name: str) -> str:
        return f"{self.ep}/audio/{cid}/{line_id}/{name}"

    def master(self, version: str, name: str) -> str:
        return f"{self.ep}/masters/{version}/{name}"

    def qa(self, version: str, name: str) -> str:
        return f"{self.ep}/qa/{version}/{name}"

    def public(self, name: str) -> str:
        return f"{self.ep}/public/{name}"
