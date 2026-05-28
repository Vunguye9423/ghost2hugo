"""HTML fallback parser.

Last-resort when a Ghost post has neither Lexical nor Mobiledoc — e.g. a very
old post or an imported one. Uses BeautifulSoup to walk the rendered HTML and
produce our Block AST.
"""

from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from ..ast_types import Block, Inline

log = logging.getLogger(__name__)

INLINE_TAGS = {"a", "strong", "b", "em", "i", "code", "s", "del",
               "strike", "u", "br", "span"}


def can_parse(post: dict[str, Any]) -> bool:
    return bool(post.get("html"))


def parse(post: dict[str, Any]) -> list[Block]:
    html = post.get("html") or ""
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    body = soup.body or soup
    out: list[Block] = []
    for child in body.children:
        if isinstance(child, NavigableString):
            text = str(child).strip()
            if text:
                out.append(Block(kind="paragraph",
                                 inlines=[Inline(kind="text", text=text)]))
            continue
        if isinstance(child, Tag):
            blk = _parse_tag(child)
            if blk is None:
                continue
            if isinstance(blk, list):
                out.extend(blk)
            else:
                out.append(blk)
    return out


def _parse_tag(tag: Tag) -> Block | list[Block] | None:
    name = (tag.name or "").lower()
    if name == "p":
        return Block(kind="paragraph", inlines=_inlines(tag))
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return Block(kind="heading", level=int(name[1]),
                     inlines=_inlines(tag))
    if name == "blockquote":
        return Block(kind="quote", inlines=_inlines(tag))
    if name == "pre":
        code_tag = tag.find("code")
        code = (code_tag.get_text() if code_tag else tag.get_text()) or ""
        lang = ""
        if code_tag:
            cls = code_tag.get("class") or []
            for c in cls:
                if c.startswith("language-"):
                    lang = c.removeprefix("language-")
                    break
        return Block(kind="code", code=code.rstrip("\n"), language=lang)
    if name == "hr":
        return Block(kind="hr")
    if name in ("ul", "ol"):
        return _list(tag)
    if name == "figure":
        return _figure(tag)
    if name == "img":
        return Block(kind="image", src=tag.get("src", "") or "",
                     alt=tag.get("alt", "") or "")
    if name == "iframe":
        return Block(kind="embed", url=tag.get("src", "") or "",
                     html=str(tag), embed_type="iframe")
    if name == "div":
        # Ghost wraps cards in <div class="kg-card kg-XXX-card">
        cls = " ".join(tag.get("class") or [])
        if "kg-bookmark-card" in cls:
            return _bookmark_div(tag)
        if "kg-callout-card" in cls:
            return _callout_div(tag)
        if "kg-gallery-card" in cls:
            return _gallery_div(tag)
        if "kg-embed-card" in cls:
            iframe = tag.find("iframe")
            if iframe:
                return Block(kind="embed", url=iframe.get("src", "") or "",
                             html=str(iframe), embed_type="iframe")
        # Generic div → flatten children
        children: list[Block] = []
        for c in tag.children:
            if isinstance(c, Tag):
                sub = _parse_tag(c)
                if sub is None:
                    continue
                if isinstance(sub, list):
                    children.extend(sub)
                else:
                    children.append(sub)
        return children or None
    # Inline-level — wrap in paragraph
    if name in INLINE_TAGS:
        return Block(kind="paragraph", inlines=_inlines(tag))
    log.debug("html_fallback: unknown tag=%r — kept as html", name)
    return Block(kind="html", raw=str(tag))


def _list(tag: Tag) -> Block:
    ordered = tag.name == "ol"
    items: list[list[Inline]] = []
    nested: list[Block] = []
    for li in tag.find_all("li", recursive=False):
        items.append(_inlines(li, ignore=("ul", "ol")))
        nested_list = li.find(["ul", "ol"], recursive=False)
        nested.append(_list(nested_list) if nested_list
                      else Block(kind="paragraph"))
    return Block(kind="list", ordered=ordered, items=items, nested=nested)


def _figure(tag: Tag) -> Block | list[Block] | None:
    img = tag.find("img")
    caption_tag = tag.find("figcaption")
    caption = (caption_tag.get_text(" ", strip=True) if caption_tag else "")
    if img:
        return Block(
            kind="image",
            src=img.get("src", "") or "",
            alt=img.get("alt", "") or "",
            caption=caption,
        )
    iframe = tag.find("iframe")
    if iframe:
        return Block(
            kind="embed",
            url=iframe.get("src", "") or "",
            html=str(iframe),
            embed_type="iframe",
            caption=caption,
        )
    # Fallback: figure with arbitrary content → paragraph
    return Block(kind="paragraph", inlines=_inlines(tag))


def _bookmark_div(tag: Tag) -> Block:
    a = tag.find("a", class_="kg-bookmark-container") or tag.find("a")
    url = a.get("href", "") if a else ""
    title_el = tag.find(class_="kg-bookmark-title")
    desc_el = tag.find(class_="kg-bookmark-description")
    author_el = tag.find(class_="kg-bookmark-author")
    publisher_el = tag.find(class_="kg-bookmark-publisher")
    thumb_el = tag.find(class_="kg-bookmark-thumbnail")
    icon_el = tag.find(class_="kg-bookmark-icon")
    return Block(
        kind="bookmark",
        url=url or "",
        title=(title_el.get_text(strip=True) if title_el else ""),
        description=(desc_el.get_text(strip=True) if desc_el else ""),
        author=(author_el.get_text(strip=True) if author_el else ""),
        publisher=(publisher_el.get_text(strip=True) if publisher_el else ""),
        thumbnail=(thumb_el.find("img").get("src", "") if thumb_el and thumb_el.find("img") else ""),
        icon=(icon_el.get("src", "") if icon_el else ""),
    )


def _callout_div(tag: Tag) -> Block:
    emoji_el = tag.find(class_="kg-callout-emoji")
    text_el = tag.find(class_="kg-callout-text")
    return Block(
        kind="callout",
        emoji=(emoji_el.get_text(strip=True) if emoji_el else ""),
        accent="info",
        children=[Block(kind="paragraph",
                        inlines=_inlines(text_el) if text_el else [])],
    )


def _gallery_div(tag: Tag) -> Block:
    imgs = []
    for img in tag.find_all("img"):
        imgs.append({
            "src": img.get("src", "") or "",
            "alt": img.get("alt", "") or "",
            "caption": "",
            "width": str(img.get("width", "") or ""),
            "height": str(img.get("height", "") or ""),
        })
    return Block(kind="gallery", images=imgs)


# ----------------------------------------------------------------------------
# Inline rendering
# ----------------------------------------------------------------------------


def _inlines(tag: Tag, ignore=()) -> list[Inline]:
    out: list[Inline] = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if text:
                out.append(Inline(kind="text", text=text))
            continue
        if isinstance(child, Tag):
            n = (child.name or "").lower()
            if n in ignore:
                continue
            if n == "br":
                out.append(Inline(kind="br"))
            elif n in ("strong", "b"):
                out.append(Inline(kind="bold", children=_inlines(child)))
            elif n in ("em", "i"):
                out.append(Inline(kind="italic", children=_inlines(child)))
            elif n in ("s", "strike", "del"):
                out.append(Inline(kind="strike", children=_inlines(child)))
            elif n == "code":
                # Inline code only — flatten to plain text
                out.append(Inline(kind="code",
                                  children=[Inline(kind="text",
                                                   text=child.get_text())]))
            elif n == "a":
                out.append(Inline(kind="link", href=child.get("href", "") or "",
                                  children=_inlines(child)))
            elif n == "span":
                out.extend(_inlines(child))
            elif n == "img":
                # Inline image — emit as text placeholder (rare in body)
                out.append(Inline(kind="text",
                                  text=f"![{child.get('alt') or ''}]({child.get('src') or ''})"))
            else:
                out.extend(_inlines(child))
    return out
