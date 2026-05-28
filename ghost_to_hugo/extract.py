"""L1+L2+L3 — Source parse, metadata extraction, block segmentation.

Reads a Ghost JSON export (top-level shape: {"db": [{"data": {...}}]}) and
produces a list of `Post` objects with their blocks fully resolved.

Tag/author joining is done here (Ghost stores them in separate tables with
join tables `posts_tags` / `posts_authors`).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from .ast_types import Post
from .parsers import html_fallback, lexical, mobiledoc

log = logging.getLogger(__name__)


def _clean_tag_name(name: str) -> str:
    """Normalize a Ghost tag name so Hugo's `urlize` produces the same slug
    Ghost has on file.

    Ghost's slug generator differs from Hugo's:
      - "AI  & ML"  → Ghost slug "ai-ml"   ; Hugo urlize "ai---ml"
      - "📪 Newsletter" → Ghost "newsletter"; Hugo "-newsletter"

    We clean the name so Hugo's urlize converges to Ghost's slug. Display
    name remains human-readable in the post listing.
    """
    if not name:
        return name
    # 1) Strip combining chars + non-letters that aren't ASCII letters/digits/
    #    space/hyphen/apostrophe — covers leading emojis, NBSP, etc.
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9 \-']", " ", s)
    # 2) Collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_export(path: Path | str, *, ghost_base_url: str = "") -> dict[str, Any]:
    """Load and lightly validate the Ghost JSON export shape.

    If `ghost_base_url` is given, all `__GHOST_URL__` placeholders (Ghost's
    portable-export marker) in post content fields are replaced with that URL.
    Ghost serves content by substituting `__GHOST_URL__` at render time, so
    the export contains them verbatim — we must do the same substitution.
    """
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw = f.read()

    if ghost_base_url:
        # The placeholder appears in lexical / mobiledoc / html string blobs.
        # Doing the substitution on the raw JSON text is safe because the
        # placeholder is a stable literal — no escaping concerns.
        base = ghost_base_url.rstrip("/")
        raw = raw.replace("__GHOST_URL__", base)
        # Some posts have legacy URLs hardcoded with a different TLD (`.in`
        # instead of `.io`, etc.). The current Ghost host is canonical.
        # We unify everything onto the canonical base so the asset pipeline,
        # internal-link rewriter, and validators all see one consistent URL.
        from urllib.parse import urlparse
        canonical_host = (urlparse(base).hostname or "").lower()
        if canonical_host:
            # Generate the family of "same brand, different TLD" hosts the
            # blog might have ever used.
            stem = canonical_host.rsplit(".", 1)[0]  # e.g. "myblog"
            for tld in ("io", "in", "com", "net", "co"):
                alt = f"{stem}.{tld}"
                if alt != canonical_host:
                    # URL prefixes
                    raw = raw.replace(f"https://{alt}/", f"{base}/")
                    raw = raw.replace(f"http://{alt}/", f"{base}/")
                    # Bare domain mentions (e.g. inside Ghost-stored bookmark
                    # metadata: "publisher: myblog.in"). Safe because
                    # any occurrence of the brand's old-TLD host is a known
                    # legacy artifact to normalise.
                    raw = raw.replace(alt, canonical_host)

    data = json.loads(raw)
    # Ghost wraps the data in db[0].data
    if "db" in data:
        db = data["db"]
        if not db or not isinstance(db, list):
            raise ValueError(f"{p}: 'db' is empty or not a list")
        return db[0].get("data") or {}
    if "data" in data:
        return data["data"]
    # Some exports are already flat
    if "posts" in data:
        return data
    raise ValueError(f"{p}: unrecognized Ghost export shape "
                     f"(top-level keys: {list(data.keys())})")


# Ghost statuses that mean "live on the public site". `sent` is for posts
# that were both published AND emailed to newsletter subscribers — those are
# still public posts on the blog.
PUBLISHED_STATUSES = {"published", "sent"}


def extract_posts(export_data: dict[str, Any], *,
                  include_pages: bool = False,
                  include_drafts: bool = True) -> list[Post]:
    """Turn the parsed export into a list of Post objects with blocks."""
    posts_raw = export_data.get("posts") or []
    tags_by_id = {t["id"]: t for t in (export_data.get("tags") or [])}
    users_by_id = {u["id"]: u for u in (export_data.get("users") or [])}
    posts_tags = export_data.get("posts_tags") or []
    posts_authors = export_data.get("posts_authors") or []

    # Ghost 6.x splits per-post SEO/OG/Twitter/feature-image-meta into the
    # `posts_meta` table joined by post_id. Merge those fields back onto the
    # post dict so the rest of the extractor sees a flat object.
    posts_meta = export_data.get("posts_meta") or []
    meta_by_post = {m["post_id"]: m for m in posts_meta if m.get("post_id")}
    META_FIELDS = (
        "feature_image_alt", "feature_image_caption",
        "meta_title", "meta_description",
        "og_image", "og_title", "og_description",
        "twitter_image", "twitter_title", "twitter_description",
        "email_subject", "frontmatter",
    )
    for p in posts_raw:
        meta = meta_by_post.get(p.get("id"))
        if not meta:
            continue
        for f in META_FIELDS:
            v = meta.get(f)
            if v and not p.get(f):
                p[f] = v

    # Build post_id → [(tag_obj, sort_order), ...] lookups
    tag_links: dict[str, list[tuple[dict, int]]] = {}
    for link in posts_tags:
        pid = link.get("post_id")
        tid = link.get("tag_id")
        order = int(link.get("sort_order") or 0)
        if pid and tid and tid in tags_by_id:
            tag_links.setdefault(pid, []).append((tags_by_id[tid], order))

    author_links: dict[str, list[tuple[dict, int]]] = {}
    for link in posts_authors:
        pid = link.get("post_id")
        uid = link.get("author_id") or link.get("user_id")
        order = int(link.get("sort_order") or 0)
        if pid and uid and uid in users_by_id:
            author_links.setdefault(pid, []).append((users_by_id[uid], order))

    out: list[Post] = []
    for raw in posts_raw:
        if not include_pages and (raw.get("type") == "page"):
            continue
        status = raw.get("status") or "published"
        if not include_drafts and status not in PUBLISHED_STATUSES:
            continue
        post = _build_post(raw, tag_links, author_links)
        if post is None:
            continue
        out.append(post)
    # Dedupe by normalized title — Ghost lets you create copies (suffixes
    # `-copy`, `-2`, etc.). We keep the most recently published version as
    # the canonical post; the others become `aliases` on it so their old
    # URLs 301 to the canonical one. Result: zero duplicate content, all
    # legacy URLs continue to resolve.
    return _dedupe_by_title(out)


def _dedupe_by_title(posts: list[Post]) -> list[Post]:
    """Group posts by normalized title; keep newest, attach older slugs as aliases."""
    from collections import defaultdict
    groups: dict[str, list[Post]] = defaultdict(list)
    for p in posts:
        groups[_normalize_title(p.title)].append(p)
    kept: list[Post] = []
    for _, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        # Pick canonical = SHORTEST slug (cleanest URL for SEO).
        # Within same slug length, prefer the most recent publication
        # (likely the most edited/up-to-date content).
        # Python sort is stable, so apply tiebreaker first, then primary key.
        group.sort(key=lambda p: (p.published_at or p.created_at or ""),
                   reverse=True)
        group.sort(key=lambda p: len(p.slug))
        canonical = group[0]
        # Older slugs become aliases of the canonical post — Hugo renders these
        # as HTTP redirects so the old URLs keep working for SEO.
        existing = list(canonical.raw.get("__aliases__") or [])
        for older in group[1:]:
            alias = f"/{older.slug}/"
            if alias != f"/{canonical.slug}/" and alias not in existing:
                existing.append(alias)
            log.info("dedupe: %r → keeping %r, redirecting %r",
                     canonical.title, canonical.slug, older.slug)
        canonical.raw["__aliases__"] = existing
        kept.append(canonical)
    return kept


def _normalize_title(title: str) -> str:
    if not title:
        return ""
    s = unicodedata.normalize("NFKD", title)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^A-Za-z0-9]+", " ", s).strip().lower()
    return s


def _build_post(raw: dict, tag_links, author_links) -> Post | None:
    pid = raw.get("id") or ""
    slug = raw.get("slug") or ""
    title = raw.get("title") or "Untitled"
    if not slug:
        log.warning("post %r has no slug — skipping", title)
        return None

    # Pick parser
    if lexical.can_parse(raw):
        blocks = lexical.parse(raw)
        fmt = "lexical"
    elif mobiledoc.can_parse(raw):
        blocks = mobiledoc.parse(raw)
        fmt = "mobiledoc"
    elif html_fallback.can_parse(raw):
        blocks = html_fallback.parse(raw)
        fmt = "html"
    else:
        blocks = []
        fmt = "empty"

    # Tags (preserve sort_order); normalize names so Hugo's urlize → Ghost slug.
    tag_pairs = tag_links.get(pid, [])
    tag_pairs.sort(key=lambda x: x[1])
    tag_names = [_clean_tag_name(t["name"]) for t, _ in tag_pairs
                 if t.get("name") and _clean_tag_name(t["name"])]
    primary_tag = tag_names[0] if tag_names else ""

    # Authors
    author_pairs = author_links.get(pid, [])
    author_pairs.sort(key=lambda x: x[1])
    author_names = [u.get("name") or u.get("slug") or ""
                    for u, _ in author_pairs]
    author_names = [a for a in author_names if a]
    primary_author = author_names[0] if author_names else ""

    return Post(
        id=pid,
        slug=slug,
        title=title,
        published_at=raw.get("published_at") or "",
        updated_at=raw.get("updated_at") or "",
        created_at=raw.get("created_at") or "",
        status=raw.get("status") or "published",
        visibility=raw.get("visibility") or "public",
        feature_image=raw.get("feature_image") or "",
        feature_image_alt=raw.get("feature_image_alt") or "",
        feature_image_caption=raw.get("feature_image_caption") or "",
        custom_excerpt=raw.get("custom_excerpt") or "",
        meta_title=raw.get("meta_title") or "",
        meta_description=raw.get("meta_description") or "",
        og_image=raw.get("og_image") or "",
        og_title=raw.get("og_title") or "",
        og_description=raw.get("og_description") or "",
        twitter_image=raw.get("twitter_image") or "",
        twitter_title=raw.get("twitter_title") or "",
        twitter_description=raw.get("twitter_description") or "",
        canonical_url=raw.get("canonical_url") or "",
        tags=tag_names,
        authors=author_names,
        primary_author=primary_author,
        primary_tag=primary_tag,
        blocks=blocks,
        source_format=fmt,
        raw=raw,
    )
