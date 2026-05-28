"""L4 — Typography normalization.

Walks every Inline node and every text-bearing field, applies:
  - UTF-8 mojibake detection (no fixup, just logging — we trust Ghost JSON)
  - Strip zero-width chars
  - Normalize NBSP (U+00A0 → space) ONLY in plain text — preserved inside code
  - Strip trailing whitespace per inline run
  - Preserve smart quotes / em-dashes / em-spaces — they ARE the content

Does NOT touch code-block contents (byte-perfect preservation is required).
"""

from __future__ import annotations

import logging
import re

from .ast_types import Block, Inline, Post

log = logging.getLogger(__name__)

ZERO_WIDTH = re.compile(r"[​-‍﻿]")
NBSP = " "
MOJIBAKE_HINT = re.compile(r"â€™|â€œ|â€\x9d|Ã©|Â |�")

from .html_clean import strip_ghost_noise_html  # re-export


def normalize(post: Post) -> dict[str, int]:
    """Mutate post in place. Returns counts of normalizations done."""
    counts = {"nbsp_collapsed": 0, "zero_width_stripped": 0,
              "mojibake_flagged": 0}
    for block in post.blocks:
        _walk_block(block, counts)
    # Also normalize title and excerpt (but leave SEO descriptions as-is —
    # they often have intentional weird chars)
    post.title = _normalize_text(post.title, counts)
    post.custom_excerpt = _normalize_text(post.custom_excerpt, counts)
    return counts


def _walk_block(block: Block, counts: dict[str, int]) -> None:
    # Skip code blocks — preserve byte-for-byte
    if block.kind == "code":
        return
    if block.kind == "html":
        return  # raw HTML — don't touch
    # Inlines
    for inl in block.inlines:
        _walk_inline(inl, counts)
    for item in block.items:  # list items
        for inl in item:
            _walk_inline(inl, counts)
    for child in block.children:
        _walk_block(child, counts)
    for nested in block.nested:
        _walk_block(nested, counts)
    # Text-bearing block fields
    block.caption = _normalize_text(block.caption, counts)
    block.title = _normalize_text(block.title, counts)
    block.description = _normalize_text(block.description, counts)
    block.summary = _normalize_text(block.summary, counts)
    block.alt = _normalize_text(block.alt, counts)
    for img in block.images:
        if "alt" in img:
            img["alt"] = _normalize_text(img["alt"], counts)
        if "caption" in img:
            img["caption"] = _normalize_text(img["caption"], counts)


def _walk_inline(inl: Inline, counts: dict[str, int]) -> None:
    if inl.kind == "code":
        # Inline code — don't normalize, byte-perfect
        return
    if inl.text:
        inl.text = _normalize_text(inl.text, counts)
    for child in inl.children:
        _walk_inline(child, counts)


def _normalize_text(text: str, counts: dict[str, int]) -> str:
    if not text:
        return text
    if MOJIBAKE_HINT.search(text):
        counts["mojibake_flagged"] += 1
    # Strip Ghost's `<span style="white-space: pre-wrap;">` noise wrappers
    # before any other processing — they have to go before we count NBSPs etc.
    text = strip_ghost_noise_html(text)
    # Strip zero-width
    new = ZERO_WIDTH.sub("", text)
    if new != text:
        counts["zero_width_stripped"] += 1
        text = new
    # Collapse NBSP — Ghost editors often insert these accidentally between words.
    # Real intentional NBSPs (line breaks, hard spaces) are extremely rare in
    # blog content; if you need them, this is the place to flag and skip.
    if NBSP in text:
        text = text.replace(NBSP, " ")
        counts["nbsp_collapsed"] += 1
    # NOTE: do NOT rstrip trailing spaces — for inline runs the trailing space
    # IS the separator between adjacent inlines (e.g. "in " + bold("Mobiledoc")).
    return text
