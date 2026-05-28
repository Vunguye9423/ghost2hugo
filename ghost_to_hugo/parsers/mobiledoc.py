"""Mobiledoc parser — Ghost 3.x / 4.x format.

Mobiledoc spec: github.com/bustle/mobiledoc-kit/blob/master/MOBILEDOC.md

Structure:
{
  "version": "0.3.1",
  "atoms":   [<atom>, ...],                  # inline atoms (rarely used)
  "cards":   [["card-name", {payload}], ...],
  "markups": [["tagName", ["attr", "val"]], ...],
  "sections":[
    [1, "p"  | "h1".."h6" | "blockquote", [<marker>, ...]],   # markup section
    [2, "src", w, h, "alt"],                                  # image section (rare)
    [3, "ul" | "ol", [[<marker>, ...], ...]],                 # list section
    [10, <card-index>]                                        # card section
  ]
}

A marker is: [type, openMarkupIndexes, numCloseMarkups, payload]
  - type 0 = text, payload = string
  - type 1 = atom, payload = atom index
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..ast_types import Block, Inline

log = logging.getLogger(__name__)

SECTION_MARKUP = 1
SECTION_IMAGE = 2
SECTION_LIST = 3
SECTION_CARD = 10

MARKER_TEXT = 0
MARKER_ATOM = 1


def can_parse(post: dict[str, Any]) -> bool:
    md = post.get("mobiledoc")
    if not md:
        return False
    if isinstance(md, str):
        s = md.strip()
        return s.startswith("{") and ("sections" in s or "atoms" in s)
    if isinstance(md, dict):
        return "sections" in md
    return False


def parse(post: dict[str, Any]) -> list[Block]:
    raw = post.get("mobiledoc")
    data = _coerce(raw)
    if not data:
        return []
    cards = data.get("cards") or []
    markups = data.get("markups") or []
    sections = data.get("sections") or []
    out: list[Block] = []
    for sec in sections:
        try:
            blk = _parse_section(sec, cards, markups)
        except Exception as exc:
            log.warning("mobiledoc: dropped section %s err=%s", sec[:2], exc)
            continue
        if blk is None:
            continue
        if isinstance(blk, list):
            out.extend(blk)
        else:
            out.append(blk)
    return out


# ----------------------------------------------------------------------------
# Section dispatch
# ----------------------------------------------------------------------------


def _parse_section(sec, cards, markups) -> Block | list[Block] | None:
    if not sec:
        return None
    kind = sec[0]
    if kind == SECTION_MARKUP:
        return _markup_section(sec, markups)
    if kind == SECTION_IMAGE:
        return _image_section(sec)
    if kind == SECTION_LIST:
        return _list_section(sec, markups)
    if kind == SECTION_CARD:
        return _card_section(sec, cards)
    return None


def _markup_section(sec, markups) -> Block | None:
    _, tag, raw_markers = sec
    tag = (tag or "p").lower()
    inlines = _render_markers(raw_markers, markups)
    if tag == "p":
        return Block(kind="paragraph", inlines=inlines)
    if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return Block(kind="heading", level=int(tag[1]), inlines=inlines)
    if tag == "blockquote":
        return Block(kind="quote", inlines=inlines)
    if tag == "pull-quote":
        return Block(kind="quote", inlines=inlines, meta={"variant": "pull"})
    if tag == "aside":
        return Block(kind="callout", accent="info",
                     children=[Block(kind="paragraph", inlines=inlines)])
    return Block(kind="paragraph", inlines=inlines, meta={"tag": tag})


def _image_section(sec) -> Block:
    # [2, src, w, h, alt, title]
    src = sec[1] if len(sec) > 1 else ""
    alt = sec[4] if len(sec) > 4 else ""
    return Block(kind="image", src=src or "", alt=alt or "")


def _list_section(sec, markups) -> Block:
    # [3, "ul"|"ol", [[<marker>, ...], ...]]
    tag = (sec[1] if len(sec) > 1 else "ul").lower()
    items_raw = sec[2] if len(sec) > 2 else []
    items_inlines = [_render_markers(item, markups) for item in items_raw]
    nested = [Block(kind="paragraph") for _ in items_inlines]
    return Block(kind="list", ordered=(tag == "ol"),
                 items=items_inlines, nested=nested)


def _card_section(sec, cards) -> Block | list[Block] | None:
    # [10, card_index]
    idx = sec[1]
    if idx is None or idx >= len(cards):
        return None
    entry = cards[idx]
    if not isinstance(entry, list) or len(entry) < 2:
        return None
    name, payload = entry[0], entry[1] or {}
    name = (name or "").lower()
    if name == "image":
        return Block(
            kind="image",
            src=payload.get("src", "") or "",
            alt=payload.get("alt", "") or "",
            caption=payload.get("caption", "") or "",
        )
    if name == "markdown":
        # Markdown cards have raw markdown — pass through unchanged
        return Block(kind="html", raw=payload.get("markdown", "") or "",
                     meta={"source": "markdown-card"})
    if name == "html":
        return Block(kind="html", raw=payload.get("html", "") or "")
    if name == "code":
        return Block(
            kind="code",
            code=payload.get("code", "") or "",
            language=(payload.get("language") or "").lower(),
            caption=payload.get("caption") or "",
        )
    if name == "embed":
        return Block(
            kind="embed",
            url=payload.get("url", "") or "",
            html=payload.get("html", "") or "",
            embed_type=(payload.get("type") or "").lower(),
            caption=payload.get("caption", "") or "",
        )
    if name == "hr":
        return Block(kind="hr")
    if name == "bookmark":
        md = payload.get("metadata") or {}
        return Block(
            kind="bookmark",
            url=payload.get("url", "") or "",
            title=md.get("title", "") or "",
            description=md.get("description", "") or "",
            author=md.get("author", "") or "",
            publisher=md.get("publisher", "") or "",
            thumbnail=md.get("thumbnail", "") or "",
            icon=md.get("icon", "") or "",
            caption=payload.get("caption", "") or "",
        )
    if name == "gallery":
        imgs = []
        for img in payload.get("images") or []:
            imgs.append({
                "src": img.get("src", "") or "",
                "alt": img.get("alt", "") or "",
                "caption": img.get("caption", "") or "",
                "width": str(img.get("width", "") or ""),
                "height": str(img.get("height", "") or ""),
            })
        return Block(kind="gallery", images=imgs,
                     caption=payload.get("caption", "") or "")
    if name == "file":
        return Block(
            kind="attachment",
            src=payload.get("src", "") or "",
            filename=payload.get("fileName", "") or "",
            title=payload.get("fileTitle", "") or "",
            caption=payload.get("fileCaption", "") or "",
            size_bytes=int(payload.get("fileSize") or 0),
            mime_type=payload.get("mimeType", "") or "",
        )
    if name == "audio":
        return Block(
            kind="audio",
            src=payload.get("src", "") or "",
            title=payload.get("title", "") or "",
            mime_type=payload.get("mimeType", "") or "audio/mpeg",
        )
    if name == "video":
        return Block(
            kind="video",
            src=payload.get("src", "") or "",
            title=payload.get("title", "") or "",
            mime_type=payload.get("mimeType", "") or "video/mp4",
        )
    if name == "callout":
        # Older Ghost: callouts had raw HTML
        return Block(
            kind="callout",
            emoji=payload.get("calloutEmoji", "") or "",
            accent="info",
            children=[Block(kind="html",
                            raw=payload.get("calloutText", "") or "")],
        )
    if name == "toggle":
        from ..html_clean import strip_ghost_noise_html
        return Block(
            kind="toggle",
            summary=payload.get("heading", "") or "",
            children=[Block(kind="html",
                            raw=strip_ghost_noise_html(payload.get("content", "") or ""))],
        )
    if name == "button":
        text = payload.get("buttonText", "") or "Click"
        url = payload.get("buttonUrl", "") or "#"
        return Block(kind="paragraph",
                     inlines=[Inline(kind="link", href=url,
                                     children=[Inline(kind="text", text=text)])])
    if name in ("email", "email-cta", "signup", "product"):
        return None  # Members-only — dropped on static migration
    log.debug("mobiledoc: unknown card name=%r", name)
    if "html" in payload:
        return Block(kind="html", raw=payload["html"] or "",
                     meta={"unknown_card": name})
    return None


# ----------------------------------------------------------------------------
# Markers → Inlines (with markup stack)
# ----------------------------------------------------------------------------


def _render_markers(raw_markers, markups) -> list[Inline]:
    if not raw_markers:
        return []
    # The markup stack: each item is a markup tuple (tag, attrs)
    stack: list[tuple[str, dict[str, str]]] = []
    out: list[Inline] = []
    for marker in raw_markers:
        if not marker:
            continue
        m_type = marker[0]
        open_idxs = marker[1] if len(marker) > 1 else []
        close_count = marker[2] if len(marker) > 2 else 0
        payload = marker[3] if len(marker) > 3 else ""
        # Push opens
        for oi in open_idxs:
            if oi < len(markups):
                m = markups[oi]
                tag = (m[0] if len(m) > 0 else "").lower()
                attrs = _attrs(m[1] if len(m) > 1 else [])
                stack.append((tag, attrs))
        # Emit
        if m_type == MARKER_TEXT and payload:
            out.append(_wrap_inlines(str(payload), stack))
        elif m_type == MARKER_ATOM:
            # Atoms in Ghost: typically `soft-return` for <br>
            out.append(Inline(kind="br"))
        # Pop closes
        for _ in range(int(close_count)):
            if stack:
                stack.pop()
    return out


def _wrap_inlines(text: str, stack) -> Inline:
    node = Inline(kind="text", text=text)
    # Apply stack in reverse so outermost markup is at the top of resulting tree
    for tag, attrs in reversed(stack):
        if tag == "strong" or tag == "b":
            node = Inline(kind="bold", children=[node])
        elif tag == "em" or tag == "i":
            node = Inline(kind="italic", children=[node])
        elif tag == "s" or tag == "strike" or tag == "del":
            node = Inline(kind="strike", children=[node])
        elif tag == "code":
            node = Inline(kind="code", children=[node])
        elif tag == "a":
            href = attrs.get("href", "") or ""
            node = Inline(kind="link", href=href, children=[node])
        elif tag == "u":
            pass  # underline → drop
        elif tag == "sub" or tag == "sup":
            pass  # rare, drop
    return node


def _attrs(attr_list) -> dict[str, str]:
    out: dict[str, str] = {}
    if not attr_list:
        return out
    it = iter(attr_list)
    for k in it:
        try:
            v = next(it)
        except StopIteration:
            break
        out[str(k)] = str(v)
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
            log.warning("mobiledoc: invalid JSON: %s", exc)
            return None
    return None
