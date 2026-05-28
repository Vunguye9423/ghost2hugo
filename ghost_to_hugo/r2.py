"""Cloudflare R2 client wrapper.

R2 is S3-compatible — we use boto3 with a custom endpoint. Two operations
the asset pipeline needs:

  - head_object(key)  → check if an object already exists (dedupe gate)
  - put_object(key, data, content_type) → upload with immutable cache headers

Both raise on transport errors; "object not found" returns False/None.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class R2Config:
    endpoint_url: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    public_base_url: str
    prefix: str = "blog/"
    cache_control: str = "public, max-age=31536000, immutable"


class R2:
    def __init__(self, cfg: R2Config):
        self.cfg = cfg
        self._client = boto3.client(
            "s3",
            endpoint_url=cfg.endpoint_url,
            aws_access_key_id=cfg.access_key_id,
            aws_secret_access_key=cfg.secret_access_key,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
                region_name="auto",
            ),
        )

    def head(self, key: str) -> dict | None:
        try:
            return self._client.head_object(Bucket=self.cfg.bucket, Key=key)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self.cfg.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            CacheControl=self.cfg.cache_control,
        )

    def public_url(self, key: str) -> str:
        base = self.cfg.public_base_url.rstrip("/")
        return f"{base}/{key}"

    def smoke_test(self) -> None:
        """Validate credentials + bucket access by listing one object.

        Raises if creds are wrong or the bucket is unreachable.
        """
        try:
            self._client.list_objects_v2(Bucket=self.cfg.bucket, MaxKeys=1)
        except ClientError as e:
            raise RuntimeError(
                f"R2 smoke test failed for bucket {self.cfg.bucket!r} "
                f"at {self.cfg.endpoint_url}: {e}"
            ) from e


# ----------------------------------------------------------------------------
# In-memory R2 stub (for dry-run + tests)
# ----------------------------------------------------------------------------


class R2Stub:
    """Drop-in replacement that pretends every PUT succeeds, no network.

    Used in dry-run mode and during the synthetic-fixture e2e test.
    The migration still produces correct content/posts/<slug>/index.md
    files with valid R2 URLs — they just don't resolve until you re-run
    with a real R2 client.
    """

    def __init__(self, cfg: R2Config, *, prepopulate: set[str] | None = None):
        self.cfg = cfg
        self._objects: set[str] = set(prepopulate or set())
        self.uploads: list[tuple[str, int, str]] = []  # (key, size, content_type)

    def head(self, key: str) -> dict | None:
        return {"ContentLength": 1} if key in self._objects else None

    def exists(self, key: str) -> bool:
        return key in self._objects

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self._objects.add(key)
        self.uploads.append((key, len(body), content_type))

    def public_url(self, key: str) -> str:
        base = self.cfg.public_base_url.rstrip("/")
        return f"{base}/{key}"

    def smoke_test(self) -> None:
        return  # always passes
