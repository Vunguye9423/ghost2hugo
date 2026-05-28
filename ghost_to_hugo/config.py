"""Config loader (YAML) with simple env-var override."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .r2 import R2Config


@dataclass
class GhostConfig:
    export_file: Path
    base_url: str


@dataclass
class HugoConfig:
    content_dir: Path
    permalink_pattern: str


@dataclass
class AssetsConfig:
    rehost_external: bool
    max_retries: int
    timeout: int
    cache_control: str


@dataclass
class PipelineConfig:
    workers: int
    strict: bool
    overwrite: bool


@dataclass
class Config:
    ghost: GhostConfig
    hugo: HugoConfig
    r2: R2Config
    assets: AssetsConfig
    pipeline: PipelineConfig
    log_level: str = "INFO"


def load_config(path: Path | str) -> Config:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}
    g = raw.get("ghost") or {}
    h = raw.get("hugo") or {}
    r = raw.get("r2") or {}
    a = raw.get("assets") or {}
    pl = raw.get("pipeline") or {}
    lg = raw.get("logging") or {}

    # Env overrides for sensitive R2 fields
    r2_cfg = R2Config(
        endpoint_url=os.environ.get("R2_ENDPOINT_URL", r.get("endpoint_url", "")),
        access_key_id=os.environ.get("R2_ACCESS_KEY_ID", r.get("access_key_id", "")),
        secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", r.get("secret_access_key", "")),
        bucket=os.environ.get("R2_BUCKET", r.get("bucket", "")),
        public_base_url=r.get("public_base_url", ""),
        prefix=r.get("prefix", "blog/"),
        cache_control=a.get("cache_control",
                            "public, max-age=31536000, immutable"),
    )

    return Config(
        ghost=GhostConfig(
            export_file=Path(g.get("export_file", "./ghost-export.json")),
            base_url=g.get("base_url", "").rstrip("/"),
        ),
        hugo=HugoConfig(
            content_dir=Path(h.get("content_dir", "./content/posts")),
            permalink_pattern=h.get("permalink_pattern", "/:slug/"),
        ),
        r2=r2_cfg,
        assets=AssetsConfig(
            rehost_external=bool(a.get("rehost_external", True)),
            max_retries=int(a.get("max_retries", 3)),
            timeout=int(a.get("timeout", 30)),
            cache_control=a.get("cache_control",
                                "public, max-age=31536000, immutable"),
        ),
        pipeline=PipelineConfig(
            workers=int(pl.get("workers", 8)),
            strict=bool(pl.get("strict", True)),
            overwrite=bool(pl.get("overwrite", False)),
        ),
        log_level=lg.get("level", "INFO"),
    )
