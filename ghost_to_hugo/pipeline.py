"""Orchestrator — runs the per-post layered pipeline across N workers."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from . import assemble, normalize, typography, validate
from .assets import AssetPipeline, SkipAssetsPipeline
from .ast_types import Block, Inline, Post
from .config import Config
from .render import blocks_to_markdown
from .report import PostReport, RunReport
from .state import State

log = logging.getLogger(__name__)


def run(posts: list[Post], cfg: Config, *,
        r2_client,
        asset_pipeline_class=AssetPipeline,
        asset_pipeline_kwargs: dict | None = None,
        state: State | None = None,
        report: RunReport | None = None,
        limit_slugs: set[str] | None = None) -> RunReport:
    """Process posts through the full 11-layer pipeline using a worker pool."""
    state = state or State(path=Path(".migration-state.json"))
    report = report or RunReport(posts_in_export=len(posts))

    # Point the internal-link rewriter at this run's old Ghost host (from config)
    # before any worker starts. Read-only thereafter, so it's thread-safe.
    assemble.configure(cfg.ghost.base_url)

    # Filter posts to process
    todo: list[Post] = []
    for p in posts:
        if limit_slugs and p.slug not in limit_slugs:
            continue
        if not cfg.pipeline.overwrite and state.is_done(p.slug):
            log.info("skip already-done: %s", p.slug)
            continue
        todo.append(p)

    cdn_host = (urlparse(cfg.r2.public_base_url).hostname or "")

    started = time.time()
    asset_kwargs = asset_pipeline_kwargs or {}

    # ONE AssetPipeline shared by all workers — its URL cache is then
    # cross-post, so an image referenced by 5 posts is downloaded once.
    # `requests` + boto3 + dict get/set are GIL-safe; a concurrent re-download
    # of the same URL is harmless because R2 keys are content-hashed (the
    # dedupe HEAD on R2 makes the second PUT a no-op).
    shared_assets = asset_pipeline_class(
        r2_client,
        cfg.ghost.base_url,
        rehost_external=cfg.assets.rehost_external,
        max_retries=cfg.assets.max_retries,
        timeout=cfg.assets.timeout,
        **asset_kwargs,
    )

    def _worker(post: Post) -> PostReport:
        return _process_one(post, cfg, shared_assets, cdn_host)

    with ThreadPoolExecutor(max_workers=cfg.pipeline.workers) as pool:
        fut_to_post = {pool.submit(_worker, p): p for p in todo}
        for fut in as_completed(fut_to_post):
            post = fut_to_post[fut]
            try:
                pr = fut.result()
            except Exception as exc:
                log.exception("worker crashed on slug=%s", post.slug)
                pr = PostReport(
                    slug=post.slug, title=post.title,
                    source_format=post.source_format,
                    success=False,
                    quarantine_reason=f"worker crash: {exc!r}",
                    failed_layer="L0-worker",
                )
            report.posts.append(pr)
            if pr.success:
                state.completed.add(post.slug)
            else:
                state.quarantined[post.slug] = (
                    f"{pr.failed_layer}: {pr.quarantine_reason}"
                )
            # Periodic checkpoint
            state.save()

    report.duration_seconds = time.time() - started
    state.save()
    return report


# ----------------------------------------------------------------------------
# Per-post layered pipeline
# ----------------------------------------------------------------------------


def _process_one(post: Post, cfg: Config, assets: AssetPipeline,
                 cdn_host: str) -> PostReport:
    pr = PostReport(
        slug=post.slug,
        title=post.title,
        source_format=post.source_format,
        success=False,
        blocks_count=len(post.blocks),
        word_count_source=_word_count_blocks(post.blocks),
    )

    # L2 metadata
    v = validate.check_l2_metadata(post)
    pr.layer_pass[v.layer] = bool(v)
    if not v:
        pr.failed_layer, pr.quarantine_reason = v.layer, v.reason
        return pr

    # L3 blocks
    v = validate.check_l3_blocks(post)
    pr.layer_pass[v.layer] = bool(v)
    if not v:
        pr.failed_layer, pr.quarantine_reason = v.layer, v.reason
        return pr

    # L4 typography (mutates post in place)
    typography.normalize(post)
    v = validate.check_l4_typography(post)
    pr.layer_pass[v.layer] = bool(v)
    if not v:
        pr.failed_layer, pr.quarantine_reason = v.layer, v.reason
        return pr

    # L4b — Ghost card normalisation: rewrite bookmark + product cards as
    # plain markdown blocks (image + heading + description + link). Cards
    # that need interactive behavior (gallery, embed, toggle, audio, video)
    # stay as shortcodes. Done BEFORE L8 so URL rewrites still catch the
    # extracted image blocks.
    normalize.normalize_cards(post)

    # L8 asset pipeline (mutates URLs + downloads + uploads).
    # In SkipAssetsPipeline mode (preview), no URLs are rewritten — so the
    # ghost-CDN URL validator is intentionally skipped.
    touched = assets.process_post(post)
    pr.assets_referenced = len(touched)
    pr.assets_deduped = sum(1 for a in touched if getattr(a, "deduplicated", False))
    pr.assets_uploaded = sum(1 for a in touched if not getattr(a, "deduplicated", False))
    if isinstance(assets, SkipAssetsPipeline):
        pr.layer_pass["L8-assets"] = True  # bypassed
    else:
        v = validate.check_l8_assets(post, cdn_host=cdn_host)
        pr.layer_pass[v.layer] = bool(v)
        if not v:
            pr.failed_layer, pr.quarantine_reason = v.layer, v.reason
            return pr

    # L5/L6/L7/L9/L10 — render to markdown
    body = blocks_to_markdown(post.blocks)
    v = validate.check_l10_spacing(body)
    pr.layer_pass[v.layer] = bool(v)
    if not v:
        pr.failed_layer, pr.quarantine_reason = v.layer, v.reason
        return pr

    # L11 — frontmatter + body + write
    text = assemble.render_file(post)
    v = validate.check_l11_hugo(text)
    pr.layer_pass[v.layer] = bool(v)
    if not v:
        pr.failed_layer, pr.quarantine_reason = v.layer, v.reason
        return pr
    try:
        assemble.write_post(post, cfg.hugo.content_dir,
                            overwrite=cfg.pipeline.overwrite,
                            cdn_base=cfg.r2.public_base_url)
    except FileExistsError:
        pr.failed_layer = "L11-hugo"
        pr.quarantine_reason = "destination already exists (use overwrite: true)"
        return pr
    pr.word_count_output = _word_count_text(body)
    pr.success = True
    return pr


# ----------------------------------------------------------------------------
# Word count (for content-retention reporting)
# ----------------------------------------------------------------------------


def _word_count_blocks(blocks: Iterable[Block]) -> int:
    n = 0
    for b in blocks:
        n += _word_count_inlines(b.inlines)
        for item in b.items:
            n += _word_count_inlines(item)
        if b.kind == "code":
            n += len(b.code.split())
        if b.kind == "html":
            n += len(re.findall(r"\w+", b.raw))
        for nested in b.nested:
            n += _word_count_blocks([nested])
        for child in b.children:
            n += _word_count_blocks([child])
    return n


def _word_count_inlines(inlines: Iterable[Inline]) -> int:
    n = 0
    for inl in inlines:
        if inl.text:
            n += len(inl.text.split())
        n += _word_count_inlines(inl.children)
    return n


def _word_count_text(text: str) -> int:
    body = text.split("---\n", 2)[-1] if "---\n" in text else text
    return len(re.findall(r"\w+", body))
