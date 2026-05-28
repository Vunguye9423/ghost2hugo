"""Lexical parser — Ghost 5.x native format.

Lexical is Facebook's editor state JSON. Ghost stores the editor tree as a
JSON-encoded STRING in the `lexical` column. We parse it into our unified
Block AST.

Reference: facebook.github.io/lexical/docs/concepts/editor-state
Ghost-specific cards: github.com/TryGhost/Koenig (their Lexical nodes)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..ast_types import Block, Inline

log = logging.getLogger(__name__)

# Lexical text-format bitmask
FMT_BOLD = 1
FMT_ITALIC = 2
FMT_STRIKE = 4
FMT_UNDERLINE = 8
FMT_CODE = 16
FMT_SUBSCRIPT = 32
FMT_SUPERSCRIPT = 64
FMT_HIGHLIGHT = 128


# ----------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------


def can_parse(post: dict[str, Any]) -> bool:
    """Return True if this post has Lexical content."""
    lex = post.get("lexical")
    if not lex:
        return False
    if isinstance(lex, str):
        s = lex.strip()
        return s.startswith("{") and "root" in s
    if isinstance(lex, dict):
        return "root" in lex
    return False


def parse(post: dict[str, Any]) -> list[Block]:
    """Parse a Ghost post's `lexical` field into a list of Blocks."""
    raw = post.get("lexical")
    data = _coerce(raw)
    if not data:
        return []
    root = data.get("root")
    if not isinstance(root, dict):
        return []
    out: list[Block] = []
    for child in root.get("children") or []:
        try:
            blk = _parse_block(child)
        except Exception as exc:  # parser must not crash the pipeline
            log.warning("lexical: dropped node type=%s err=%s",
                        child.get("type"), exc)
            continue
        if blk is None:
            continue
        if isinstance(blk, list):
            out.extend(blk)
        else:
            out.append(blk)
    return out


# ----------------------------------------------------------------------------
# Block dispatch
# ----------------------------------------------------------------------------


def _parse_block(node: dict[str, Any]) -> Block | list[Block] | None:
    t = node.get("type", "")
    # Standard Lexical
    if t == "paragraph":
        return Block(kind="paragraph", inlines=_inlines(node.get("children") or []))
    if t == "aside":
        # Ghost's aside renders as a soft-bordered callout. Empty asides are
        # often inserted by the editor as spacers — drop them.
        children = node.get("children") or []
        if not children:
            return None
        inner_inlines = _inlines(children)
        return Block(kind="callout", accent="info",
                     children=[Block(kind="paragraph", inlines=inner_inlines)])
    if t == "rich":
        # Generic raw-HTML card (Tally embeds, custom widgets, etc.)
        return Block(kind="html", raw=node.get("html", "") or "",
                     meta={"source": "rich-card"})
    if t == "paywall":
        return None  # Members-only paywall — dropped on static migration
    if t == "twitter":
        # Ghost's twitter card has url + html + author metadata
        return Block(
            kind="embed",
            url=node.get("url", "") or "",
            html=node.get("html", "") or "",
            embed_type="twitter",
            author=node.get("author_name", "") or "",
        )
    if t in ("heading", "extended-heading"):
        return _heading(node)
    if t in ("quote", "extended-quote"):
        return Block(kind="quote", inlines=_inlines(_flatten_children(node)))
    if t == "list":
        return _list(node)
    if t == "horizontalrule":
        return Block(kind="hr")
    if t == "linebreak":
        # Bare linebreak at top level - safe to drop
        return None
    # Ghost cards
    if t == "image":
        return _image(node)
    if t == "codeblock":
        return Block(
            kind="code",
            code=node.get("code", "") or "",
            language=(node.get("language") or "").lower(),
            caption=node.get("caption") or "",
        )
    if t == "callout":
        return _callout(node)
    if t == "bookmark":
        return _bookmark(node)
    if t == "gallery":
        return _gallery(node)
    if t == "embed":
        return _embed(node)
    if t in ("file", "attachment"):
        return _file(node)
    if t == "audio":
        return _audio(node)
    if t == "video":
        return _video(node)
    if t in ("toggle", "collapsible"):
        return _toggle(node)
    if t == "html":
        from ..html_clean import strip_ghost_noise_html
        return Block(kind="html",
                     raw=strip_ghost_noise_html(node.get("html", "") or ""))
    if t == "markdown":
        # We render this raw — markdown is already markdown
        return Block(kind="html", raw=node.get("markdown", "") or "",
                     meta={"source": "markdown-card"})
    if t == "button":
        return _button(node)
    if t == "header":
        return _header_card(node)
    if t == "signup":
        return None  # Ghost-only members CTA; dropped on static migration
    if t == "email-cta":
        return None
    if t == "email":
        return None
    if t == "product":
        return _product(node)
    if t == "tweet":
        # Older Ghost twitter card
        return Block(kind="embed", url=node.get("url", "") or "",
                     embed_type="twitter")
    log.debug("lexical: unknown block type=%r — keeping as html passthrough", t)
    # Last-resort: keep as HTML if present
    if "html" in node:
        return Block(kind="html", raw=node.get("html") or "",
                     meta={"unknown_type": t})
    return None


# ----------------------------------------------------------------------------
# Block builders
# ----------------------------------------------------------------------------


def _heading(node: dict[str, Any]) -> Block:
    tag = (node.get("tag") or "h2").lower()
    level = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}.get(tag, 2)
    return Block(kind="heading", level=level,
                 inlines=_inlines(node.get("children") or []))


def _image(node: dict[str, Any]) -> Block:
    return Block(
        kind="image",
        src=node.get("src", "") or "",
        alt=node.get("altText", "") or node.get("alt", "") or "",
        caption=node.get("caption", "") or "",
        meta={
            "width": node.get("width"),
            "height": node.get("height"),
            "cardWidth": node.get("cardWidth"),
            "href": node.get("href"),  # if image links somewhere
        },
    )


def _callout(node: dict[str, Any]) -> Block:
    # Ghost's callout has children (paragraphs) inside it.
    # We flatten them: emoji + first paragraph's text → callout block.
    children_blocks: list[Block] = []
    for c in node.get("children") or []:
        b = _parse_block(c)
        if b is None:
            continue
        if isinstance(b, list):
            children_blocks.extend(b)
        else:
            children_blocks.append(b)
    # Map Ghost callout background colour → our accent variants.
    color = (node.get("backgroundColor") or node.get("calloutBackgroundColor")
             or "blue").lower()
    accent_map = {
        "blue": "info", "grey": "info", "gray": "info",
        "yellow": "warn", "orange": "warn",
        "green": "success", "teal": "success",
        "red": "danger", "pink": "danger",
    }
    return Block(
        kind="callout",
        emoji=node.get("emoji") or node.get("calloutEmoji") or "",
        accent=accent_map.get(color, "info"),
        children=children_blocks,
    )


def _bookmark(node: dict[str, Any]) -> Block:
    md = node.get("metadata") or {}
    return Block(
        kind="bookmark",
        url=node.get("url", "") or "",
        title=md.get("title", "") or "",
        description=md.get("description", "") or "",
        author=md.get("author", "") or "",
        publisher=md.get("publisher", "") or "",
        thumbnail=md.get("thumbnail", "") or "",
        icon=md.get("icon", "") or "",
        caption=node.get("caption", "") or "",
    )


def _gallery(node: dict[str, Any]) -> Block:
    imgs = []
    for img in node.get("images") or []:
        imgs.append({
            "src": img.get("src", "") or "",
            "alt": img.get("alt", "") or img.get("altText", "") or "",
            "caption": img.get("caption", "") or "",
            "width": str(img.get("width", "") or ""),
            "height": str(img.get("height", "") or ""),
        })
    return Block(kind="gallery", images=imgs,
                 caption=node.get("caption") or "")


def _embed(node: dict[str, Any]) -> Block:
    return Block(
        kind="embed",
        url=node.get("url", "") or "",
        html=node.get("html", "") or "",
        embed_type=(node.get("embedType") or "").lower(),
        caption=node.get("caption", "") or "",
    )


def _file(node: dict[str, Any]) -> Block:
    return Block(
        kind="attachment",
        src=node.get("src", "") or node.get("fileSrc", "") or "",
        filename=node.get("fileName", "") or node.get("title", "") or "",
        title=node.get("fileTitle", "") or node.get("title", "") or "",
        caption=node.get("fileCaption", "") or node.get("caption", "") or "",
        size_bytes=int(node.get("fileSize") or 0),
        mime_type=node.get("mimeType", "") or "",
    )


def _audio(node: dict[str, Any]) -> Block:
    return Block(
        kind="audio",
        src=node.get("src", "") or "",
        title=node.get("title", "") or "",
        caption=node.get("caption", "") or "",
        mime_type=node.get("mimeType", "") or "audio/mpeg",
        meta={"thumbnailSrc": node.get("thumbnailSrc") or ""},
    )


def _video(node: dict[str, Any]) -> Block:
    return Block(
        kind="video",
        src=node.get("src", "") or "",
        title=node.get("title", "") or "",
        caption=node.get("caption", "") or "",
        mime_type=node.get("mimeType", "") or "video/mp4",
        meta={
            "thumbnailSrc": node.get("thumbnailSrc") or node.get("customThumbnailSrc") or "",
            "width": node.get("width"),
            "height": node.get("height"),
        },
    )


def _toggle(node: dict[str, Any]) -> Block:
    """Ghost's toggle (accordion) card. Body lives in `content` (HTML string),
    NOT in `children` — which is what tripped earlier parsing.
    """
    children_blocks: list[Block] = []
    # Lexical-form children (rare)
    for c in node.get("children") or []:
        b = _parse_block(c)
        if b is None:
            continue
        if isinstance(b, list):
            children_blocks.extend(b)
        else:
            children_blocks.append(b)
    # Ghost-form content (the common case) — raw HTML string. Strip the
    # Ghost-editor noise spans up front so we don't carry them into output.
    # Pass through as an `html` block; Hugo (goldmark with unsafe=true)
    # renders HTML inside markdown.
    from ..html_clean import strip_ghost_noise_html
    content = strip_ghost_noise_html(node.get("content") or "")
    if content:
        children_blocks.append(Block(kind="html", raw=content))
    return Block(
        kind="toggle",
        summary=node.get("heading", "") or node.get("summary", "") or "",
        children=children_blocks,
    )


def _button(node: dict[str, Any]) -> Block:
    # Rendered as a labelled link inside a paragraph
    text = node.get("buttonText", "") or "Click"
    url = node.get("buttonUrl", "") or "#"
    return Block(
        kind="paragraph",
        inlines=[Inline(kind="link", href=url,
                        children=[Inline(kind="text", text=text)])],
    )


def _header_card(node: dict[str, Any]) -> list[Block]:
    out: list[Block] = []
    h = node.get("header") or node.get("heading")
    if h:
        out.append(Block(kind="heading", level=2,
                         inlines=[Inline(kind="text", text=str(h))]))
    sub = node.get("subheader") or node.get("subheading")
    if sub:
        out.append(Block(kind="paragraph",
                         inlines=[Inline(kind="text", text=str(sub))]))
    return out


def _product(node: dict[str, Any]) -> Block:
    """Ghost product card → Block(kind="product"). Rendered via Hugo shortcode."""
    return Block(
        kind="product",
        src=node.get("productImageSrc", "") or "",
        title=node.get("productTitle", "") or "",
        description=node.get("productDescription", "") or "",
        # productButtonEnabled + productButtonText/Url
        url=(node.get("productButtonUrl", "") or "") if node.get("productButtonEnabled") else "",
        meta={
            "buttonText": node.get("productButtonText", "") or "",
            "buttonEnabled": bool(node.get("productButtonEnabled")),
            "ratingEnabled": bool(node.get("productRatingEnabled")),
            "starRating": node.get("productStarRating") or "",
            "imageWidth": node.get("productImageWidth"),
            "imageHeight": node.get("productImageHeight"),
        },
    )


# ----------------------------------------------------------------------------
# Lists — recursive, preserves nesting
# ----------------------------------------------------------------------------


def _list(node: dict[str, Any]) -> Block:
    ordered = (node.get("listType") or "bullet") == "number"
    items_inlines: list[list[Inline]] = []
    items_nested: list[Block] = []
    for li in node.get("children") or []:
        if li.get("type") != "listitem":
            continue
        inlines: list[Inline] = []
        nested: Block | None = None
        for child in li.get("children") or []:
            if child.get("type") == "list":
                # Nested list - parsed as its own Block then attached
                nested = _list(child)
            else:
                inlines.extend(_inlines([child]))
        items_inlines.append(inlines)
        items_nested.append(nested or Block(kind="paragraph"))
    return Block(kind="list", ordered=ordered,
                 items=items_inlines, nested=items_nested)


# ----------------------------------------------------------------------------
# Inline rendering — Lexical text nodes + links
# ----------------------------------------------------------------------------


def _inlines(children: list[dict[str, Any]]) -> list[Inline]:
    out: list[Inline] = []
    for c in children or []:
        t = c.get("type", "")
        # `text` (classic Lexical) and `extended-text` (Ghost 6.x) — identical
        # shape: { text, format (bitmask) }. Both carry inline formatting.
        if t in ("text", "extended-text"):
            fmt = int(c.get("format") or 0)
            txt = c.get("text", "") or ""
            out.append(_format_text(txt, fmt))
        elif t == "linebreak":
            out.append(Inline(kind="br"))
        elif t == "tab":
            out.append(Inline(kind="text", text="\t"))
        elif t in ("link", "autolink"):
            href = c.get("url", "") or ""
            out.append(Inline(kind="link", href=href,
                              children=_inlines(c.get("children") or [])))
        elif "children" in c:
            # Unknown inline node with children — flatten
            out.extend(_inlines(c["children"]))
        elif "text" in c:
            out.append(Inline(kind="text", text=c.get("text", "")))
    return out


def _format_text(text: str, fmt: int) -> Inline:
    """Wrap text in the right Inline nodes based on the Lexical format bitmask."""
    node = Inline(kind="text", text=text)
    if fmt & FMT_CODE:
        node = Inline(kind="code", children=[node])
    if fmt & FMT_STRIKE:
        node = Inline(kind="strike", children=[node])
    if fmt & FMT_ITALIC:
        node = Inline(kind="italic", children=[node])
    if fmt & FMT_BOLD:
        node = Inline(kind="bold", children=[node])
    return node


def _flatten_children(node: dict[str, Any]) -> list[dict[str, Any]]:
    """For block-level wrappers (e.g. quote → paragraph → text), flatten.

    Inserts a single linebreak BETWEEN paragraphs but not after the last one,
    so the rendered quote does not end with a stray hard break.
    """
    paragraphs = []
    other: list[dict[str, Any]] = []
    for c in node.get("children") or []:
        if c.get("type") == "paragraph":
            paragraphs.append(c.get("children") or [])
        else:
            other.append(c)
    out: list[dict[str, Any]] = []
    for i, p in enumerate(paragraphs):
        out.extend(p)
        if i < len(paragraphs) - 1:
            out.append({"type": "linebreak"})
    out.extend(other)
    return out


def _coerce(value: Any) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, str)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("lexical: invalid JSON: %s", exc)
            return None
    return None
