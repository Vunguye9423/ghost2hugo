"""L11 — Final assembly.

Takes a Post (with rewritten asset URLs + normalized text) and writes
`<content_dir>/<slug>/index.md` with proper Hugo frontmatter + body.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .ast_types import Post
from .render import blocks_to_markdown

log = logging.getLogger(__name__)

# Regex matching the absolute prefix of any URL pointing back at the old Ghost
# domain. It is built from `ghost.base_url` in config via configure(); until
# then it matches nothing. The matched prefix is rewritten differently by path:
#   - /content/... → swap to the CDN host (asset URL, must be on R2 by L8)
#   - everything else → strip to a relative URL (cross-post + tag links)
GHOST_HOST_PREFIX_RE = re.compile(r"(?!)")  # matches nothing until configure()


def _build_host_re(base_url: str) -> "re.Pattern[str]":
    """Build the old-Ghost-host prefix regex from the configured base URL.

    Matches the configured host plus its registrable apex and the www variant,
    e.g. base_url=https://blog.example.com →
    https?://(blog.example.com|www.example.com|example.com)
    """
    from urllib.parse import urlparse
    host = (urlparse(base_url).hostname or "").lower().strip()
    if not host:
        return re.compile(r"(?!)")
    labels = host.split(".")
    apex = ".".join(labels[-2:]) if len(labels) >= 2 else host
    variants = {host, apex, "www." + apex}
    alt = "|".join(re.escape(h) for h in sorted(variants, key=len, reverse=True))
    return re.compile(r"https?://(?:" + alt + r")", re.IGNORECASE)


def configure(base_url: str) -> None:
    """Point the internal-link rewriter at this migration's Ghost host."""
    global GHOST_HOST_PREFIX_RE
    GHOST_HOST_PREFIX_RE = _build_host_re(base_url)


def to_frontmatter_dict(post: Post) -> dict[str, Any]:
    """Build the Hugo frontmatter dict for a Post."""
    fm: dict[str, Any] = {
        "title": post.title,
        "slug": post.slug,
        "date": post.published_at or post.created_at,
    }
    if post.updated_at and post.updated_at != post.published_at:
        fm["lastmod"] = post.updated_at
    # `sent` = Ghost shorthand for "published and emailed to newsletter
    # subscribers". Treated identically to `published` for the static site.
    if post.status not in ("published", "sent"):
        fm["draft"] = True
    description = post.meta_description or post.custom_excerpt
    if description:
        fm["description"] = description
    if post.tags:
        fm["tags"] = post.tags
    if post.primary_tag:
        fm["primary_tag"] = post.primary_tag
    if post.primary_author:
        # `author` (singular) — terminal theme + most themes render this as
        # the visible byline. Keep `authors` (plural list) for completeness.
        fm["author"] = post.primary_author
    if post.authors:
        fm["authors"] = post.authors
    if post.feature_image:
        # Emit `cover` as a flat string — most Hugo themes (terminal, PaperMod,
        # Ananke) read it that way. Themes that want structured cover can read
        # `cover_alt` / `cover_caption` from siblings.
        fm["cover"] = post.feature_image
        if post.feature_image_alt:
            fm["cover_alt"] = post.feature_image_alt
        if post.feature_image_caption:
            fm["cover_caption"] = post.feature_image_caption
    if post.meta_title and post.meta_title != post.title:
        fm["meta_title"] = post.meta_title
    if post.og_image:
        fm["og_image"] = post.og_image
    if post.og_title:
        fm["og_title"] = post.og_title
    if post.og_description:
        fm["og_description"] = post.og_description
    if post.twitter_image:
        fm["twitter_image"] = post.twitter_image
    if post.twitter_title:
        fm["twitter_title"] = post.twitter_title
    if post.twitter_description:
        fm["twitter_description"] = post.twitter_description
    # Keep `canonical` only if it points OFF our own old Ghost domain.
    # When Ghost set canonical to the same site, the migrated post should
    # let Hugo auto-canonicalise via .Permalink (cleaner SEO).
    if post.canonical_url and not GHOST_HOST_PREFIX_RE.match(post.canonical_url):
        fm["canonical"] = post.canonical_url
    # Aliases — older slugs whose content was deduped into this canonical post.
    # Hugo renders each alias as a 0-byte HTML redirect to the canonical URL.
    aliases = (post.raw or {}).get("__aliases__")
    if aliases:
        fm["aliases"] = aliases
    return fm


def rewrite_internal_links(body: str, *, cdn_base: str = "") -> str:
    """Rewrite every remaining absolute old-Ghost-host URL in the body.

    - `/content/*` paths → swap host to the CDN (asset URLs that the structural
      walker missed, e.g. URLs inside raw HTML or shortcode args)
    - everything else → strip to a relative URL (cross-post + tag links)

    If `cdn_base` is empty we still strip the prefix (resulting in a relative
    `/content/...` URL, which 404s but at least doesn't leak the old domain).
    """
    cdn_prefix = cdn_base.rstrip("/") if cdn_base else ""

    def repl(m: re.Match) -> str:
        # What's the path? Look at the next char(s) after the match end.
        end = m.end()
        rest = body[end:end + 12]  # peek
        if rest.startswith("/content/"):
            # Asset URL — point at the CDN
            return cdn_prefix or ""
        # Cross-post / tag / other — make it relative
        return ""
    return GHOST_HOST_PREFIX_RE.sub(repl, body)


def render_file(post: Post, *, cdn_base: str = "") -> str:
    """Return the full file contents (frontmatter + body) as a string."""
    fm = to_frontmatter_dict(post)
    fm_yaml = yaml.safe_dump(
        fm,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=10_000,  # avoid line wrapping inside the frontmatter
    ).rstrip()
    body = blocks_to_markdown(post.blocks).rstrip("\n")
    body = rewrite_internal_links(body, cdn_base=cdn_base)
    return f"---\n{fm_yaml}\n---\n\n{body}\n"


def write_post(post: Post, content_dir: Path, *,
               overwrite: bool = False,
               cdn_base: str = "") -> Path:
    """Write `<content_dir>/<slug>/index.md`. Returns the path written.

    Atomic: writes to a temp file in the same folder, then renames.
    Raises FileExistsError if the file exists and overwrite=False.
    """
    folder = Path(content_dir) / post.slug
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "index.md"
    if target.exists() and not overwrite:
        raise FileExistsError(target)
    contents = render_file(post, cdn_base=cdn_base)
    # Atomic write — temp file in same dir → rename
    fd, tmp_path = tempfile.mkstemp(
        prefix=".index.md.", suffix=".tmp", dir=str(folder),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(contents)
        os.replace(tmp_path, target)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return target
