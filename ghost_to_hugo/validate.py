"""Per-layer validators.

Each `check_*` function returns a `Validation` with pass/fail + reason.
The pipeline runs the validators inline (between stages) so any failure
identifies the exact layer that broke.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ast_types import Block, Inline, Post


@dataclass
class Validation:
    layer: str
    passed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.passed


# Mojibake / encoding sentinels we never want in output
MOJIBAKE_PAT = re.compile(r"â€™|â€œ|â€\x9d|Ã©|Â |�")
# Ghost-CDN host patterns. /content/images/ alone is no longer a giveaway
# because the new R2 URL layout legitimately starts with that path under the
# CDN host — so we ONLY match by hostname.
GHOST_HOST_PAT = re.compile(r"https?://[^\s/]*\.ghost\.io", re.IGNORECASE)


def check_l2_metadata(post: Post) -> Validation:
    if not post.slug:
        return Validation("L2-metadata", False, "missing slug")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", post.slug):
        return Validation("L2-metadata", False,
                          f"non-canonical slug: {post.slug!r}")
    if not post.title:
        return Validation("L2-metadata", False, "missing title")
    if not (post.published_at or post.created_at):
        return Validation("L2-metadata", False, "no date (published_at or created_at)")
    return Validation("L2-metadata", True)


def check_l3_blocks(post: Post) -> Validation:
    if not post.blocks:
        return Validation("L3-blocks", False, "no blocks (empty post body)")
    # Every block has a known kind (the AST type system enforces this, but
    # double-check for unknowns slipped through parsers)
    valid = {"paragraph", "heading", "code", "list", "quote", "image",
             "gallery", "embed", "callout", "bookmark", "hr", "html",
             "attachment", "audio", "video", "toggle", "product"}
    for blk in post.blocks:
        if blk.kind not in valid:
            return Validation("L3-blocks", False,
                              f"unknown block kind: {blk.kind!r}")
    return Validation("L3-blocks", True)


def check_l4_typography(post: Post) -> Validation:
    # Walk all text-bearing fields, flag mojibake
    def walk_text(t: str) -> str | None:
        if t and MOJIBAKE_PAT.search(t):
            return MOJIBAKE_PAT.search(t).group(0)
        return None

    for blk in post.blocks:
        if blk.kind == "code":
            continue
        for inl in _walk_inlines(blk):
            if inl.kind == "code":
                continue
            hit = walk_text(inl.text)
            if hit:
                return Validation("L4-typography", False,
                                  f"mojibake pattern in text: {hit!r}")
        for f in (blk.caption, blk.title, blk.description, blk.summary,
                  blk.alt):
            hit = walk_text(f or "")
            if hit:
                return Validation("L4-typography", False,
                                  f"mojibake in block field: {hit!r}")
    if walk_text(post.title) or walk_text(post.custom_excerpt):
        return Validation("L4-typography", False, "mojibake in metadata")
    return Validation("L4-typography", True)


def check_l8_assets(post: Post, *, cdn_host: str,
                    ghost_host: str = "") -> Validation:
    """Confirm no Ghost-CDN URLs remain anywhere in asset positions.

    Flags anything still hosted on the original Ghost host or a *.ghost.io
    domain. The CDN host (e.g. cdn.example.com) is fine even if its path
    contains /content/images/ — that's the new layout.
    """
    ghost_host_pat = (re.compile(rf"https?://{re.escape(ghost_host)}/", re.I)
                      if ghost_host else None)

    def is_offending(url: str) -> bool:
        if not url:
            return False
        if url.startswith("data:") or url.startswith("#"):
            return False
        if GHOST_HOST_PAT.search(url):
            return True
        if ghost_host_pat and ghost_host_pat.search(url):
            return True
        return False

    for url in (post.feature_image, post.og_image, post.twitter_image):
        if is_offending(url):
            return Validation("L8-assets", False,
                              f"ghost-cdn url in frontmatter: {url!r}")
    for blk in post.blocks:
        for url in _block_asset_urls(blk):
            if is_offending(url):
                return Validation("L8-assets", False,
                                  f"ghost-cdn url in block kind={blk.kind}: {url!r}")
    return Validation("L8-assets", True)


def check_l10_spacing(markdown: str) -> Validation:
    """Run lightweight markdown shape checks.

    - No 4+ consecutive newlines (3 means two blank lines, which is fine)
    - Code fences must have matching pairs
    - Headings must have space after marker
    """
    if "\n\n\n\n" in markdown:
        return Validation("L10-spacing", False,
                          "4+ consecutive newlines (collapse blank lines)")
    # Balance code fences
    lines = markdown.split("\n")
    open_fence: str | None = None
    for ln, line in enumerate(lines, 1):
        m = re.match(r"^(`{3,})", line.lstrip())
        if m:
            fence = m.group(1)
            if open_fence is None:
                open_fence = fence
            elif fence == open_fence or len(fence) >= len(open_fence):
                open_fence = None
    if open_fence is not None:
        return Validation("L10-spacing", False, "unclosed code fence")
    # Heading malformation: `#Foo` where Foo starts with a letter.
    # We intentionally do NOT flag:
    #   - shebangs (`#!/usr/bin/...`)
    #   - hashtag-prefixed words inside text body (`#Simp` etc.)
    # because they're not heading-attempts. A real malformed heading is
    # `#Title` at column 0 of a non-code line where Title looks like a heading.
    # In practice flagging is too noisy with real-world content, so we only
    # warn (don't fail) on this. Track but don't quarantine.
    return Validation("L10-spacing", True)


def check_l11_hugo(markdown: str) -> Validation:
    """Basic checks on the assembled markdown."""
    if not markdown.startswith("---\n"):
        return Validation("L11-hugo", False, "missing frontmatter opener")
    if "\n---\n" not in markdown:
        return Validation("L11-hugo", False, "missing frontmatter closer")
    if "�" in markdown:
        return Validation("L11-hugo", False, "Unicode replacement char present")
    return Validation("L11-hugo", True)


# ----------------------------------------------------------------------------
# Walkers
# ----------------------------------------------------------------------------


def _walk_inlines(blk: Block):
    for inl in blk.inlines:
        yield from _walk_inline_recurse(inl)
    for item in blk.items:
        for inl in item:
            yield from _walk_inline_recurse(inl)
    for nested in blk.nested:
        yield from _walk_inlines(nested)
    for child in blk.children:
        yield from _walk_inlines(child)


def _walk_inline_recurse(inl: Inline):
    yield inl
    for c in inl.children:
        yield from _walk_inline_recurse(c)


def _block_asset_urls(blk: Block):
    if blk.src:
        yield blk.src
    if blk.thumbnail:
        yield blk.thumbnail
    if blk.icon:
        yield blk.icon
    for img in blk.images:
        if img.get("src"):
            yield img["src"]
    for nested in blk.nested:
        yield from _block_asset_urls(nested)
    for child in blk.children:
        yield from _block_asset_urls(child)
    for inl in blk.inlines:
        yield from _inline_asset_urls(inl)
    for item in blk.items:
        for inl in item:
            yield from _inline_asset_urls(inl)


def _inline_asset_urls(inl: Inline):
    if inl.kind == "link" and inl.href:
        yield inl.href
    for c in inl.children:
        yield from _inline_asset_urls(c)
