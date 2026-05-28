"""L9b — Card normalisation layer.

Each Ghost-specific card type that CAN be expressed as plain markdown is
rewritten in place to a sequence of vanilla blocks (heading, image, paragraph,
quote, html). The remaining shortcode-needing cards (gallery, embed, audio,
video, toggle, attachment, callout) stay as-is.

Why: shortcodes are opaque to non-Hugo consumers (RSS readers, search
crawlers that don't run Hugo, future static-site engines). Plain markdown
travels everywhere.

Mappings:
  bookmark  → image (thumb)? + heading-link(title) + p(description) + p(meta)
  product   → image + heading(title) + html(description) + p(button-link)
  callout   → KEPT as shortcode (visual block, needs theme styling)
  gallery   → KEPT (needs CSS grid)
  toggle    → KEPT (needs <details> wrapper)
  audio     → KEPT (needs <audio> element)
  video     → KEPT (needs <video> element)
  embed     → KEPT (needs iframe / raw HTML)
  attachment → KEPT (needs styled download card)
"""

from __future__ import annotations

import logging
from typing import Callable

from .ast_types import Block, Inline, Post

log = logging.getLogger(__name__)


def normalize_cards(post: Post) -> dict[str, int]:
    """Walk post.blocks and rewrite normalisable cards in place.

    Returns counts of normalisations applied (for the report).
    """
    counts: dict[str, int] = {}
    new_blocks: list[Block] = []
    for blk in post.blocks:
        replacement = _normalise_one(blk, counts)
        if replacement is None:
            new_blocks.append(blk)
        else:
            new_blocks.extend(replacement)
    post.blocks = new_blocks
    return counts


def _normalise_one(blk: Block, counts: dict[str, int]) -> list[Block] | None:
    """Return a list of replacement blocks, or None to keep the block as-is."""
    if blk.kind == "bookmark":
        counts["bookmark"] = counts.get("bookmark", 0) + 1
        return _bookmark_to_markdown(blk)
    if blk.kind == "product":
        counts["product"] = counts.get("product", 0) + 1
        return _product_to_markdown(blk)
    return None


# ----------------------------------------------------------------------------
# Bookmark — emit native markdown that reads like a "link preview" card.
# Hugo + theme CSS will style `.bookmark-card` if present, but the underlying
# markup is portable.
# ----------------------------------------------------------------------------


def _bookmark_to_markdown(b: Block) -> list[Block]:
    url = (b.url or "").strip()
    title = (b.title or url or "Link").strip()
    desc = (b.description or "").strip()
    publisher = (b.publisher or "").strip()
    author = (b.author or "").strip()

    out: list[Block] = []

    # Re-use the theme's existing `.bookmark` styles (defined in style.css)
    # — keeps the visual consistent without inventing new classes.
    head_lines = [f'<a class="bookmark" href="{url}">']

    # Thumb (if any) — natural-size image, contained within the card.
    if b.thumbnail:
        head_lines.append(
            f'  <img class="bookmark-thumb" src="{b.thumbnail}" alt="" loading="lazy">'
        )

    head_lines.append('  <div class="bookmark-meta">')
    head_lines.append(f'    <strong class="bookmark-title">{_html_escape(title)}</strong>')
    if desc:
        head_lines.append(f'    <p class="bookmark-desc">{_html_escape(desc)}</p>')
    meta_bits = [x for x in (publisher, author) if x]
    if meta_bits:
        host_part = _html_escape(" · ".join(meta_bits))
        head_lines.append(f'    <span class="bookmark-host">{host_part}</span>')
    head_lines.append('  </div>')
    head_lines.append('</a>')

    out.append(Block(kind="html", raw="\n".join(head_lines)))
    return out


# ----------------------------------------------------------------------------
# Product — emit image + heading + description (HTML) + buy link.
# This way the image isn't constrained to a card aspect ratio: it shows at
# its natural dimensions, with the rest of the content flowing as normal
# markdown beneath it.
# ----------------------------------------------------------------------------


def _product_to_markdown(b: Block) -> list[Block]:
    out: list[Block] = []
    title = (b.title or "").strip()
    url = (b.url or "").strip()
    desc_html = (b.description or "").strip()
    meta = b.meta or {}

    # Image (if any) — natural-size markdown image link to the product page
    if b.src:
        img_alt = title or "product image"
        if url:
            # Wrap image as a link to the product URL
            out.append(Block(
                kind="paragraph",
                inlines=[Inline(
                    kind="link", href=url,
                    children=[Inline(kind="text", text=f"![{img_alt}]({b.src})")],
                )],
            ))
        else:
            out.append(Block(kind="image", src=b.src, alt=img_alt))

    # Title as a heading (H3 — products usually nest under a section H2)
    if title:
        out.append(Block(
            kind="heading", level=3,
            inlines=[Inline(kind="text", text=title)],
        ))

    # Rating (if enabled) — render as ★★★★☆ on its own paragraph
    if meta.get("ratingEnabled") and meta.get("starRating"):
        try:
            n = int(meta["starRating"])
        except (TypeError, ValueError):
            n = 0
        stars = "★" * max(0, min(5, n)) + "☆" * max(0, min(5, 5 - n))
        if stars:
            out.append(Block(
                kind="paragraph",
                inlines=[Inline(kind="text", text=stars)],
            ))

    # Description — raw HTML (Ghost stores rich HTML)
    if desc_html:
        out.append(Block(kind="html", raw=desc_html))

    # Buy link
    if url:
        button_text = meta.get("buttonText") or "Buy now →"
        out.append(Block(
            kind="paragraph",
            inlines=[Inline(
                kind="link", href=url,
                children=[Inline(kind="bold", children=[
                    Inline(kind="text", text=button_text),
                ])],
            )],
        ))

    return out


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))
