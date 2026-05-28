"""HTML cleanup utilities shared by parsers + typography.

The single use case so far is stripping Ghost's `<span style="white-space:
pre-wrap;">…</span>` wrappers, which Ghost's WYSIWYG editor inserts around
nearly every run of text. They carry no semantic meaning — just a whitespace
preservation hint that's only relevant inside Ghost's own editor — and look
ugly when rendered as plain HTML in a Hugo post.
"""

from __future__ import annotations

import re

# A <span> whose ONLY attribute is style="white-space: pre-wrap;" — unwrap entirely.
_GHOST_NOISE_SPAN = re.compile(
    r'<span\s+style\s*=\s*"[^"]*white-space\s*:\s*pre-wrap[^"]*"\s*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
# The same style applied to ANY other tag (code, strong, b, em, …) — strip
# just the style attribute, keep the tag.
_NOISE_STYLE_ATTR = re.compile(
    r'\s+style\s*=\s*"[^"]*white-space\s*:\s*pre-wrap[^"]*"',
    re.IGNORECASE,
)
# `spellcheck="false"` is editor cruft Ghost emits — strip it.
_SPELLCHECK_ATTR = re.compile(
    r'\s+spellcheck\s*=\s*"[^"]*"',
    re.IGNORECASE,
)
_BARE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)


def strip_ghost_noise_html(text: str) -> str:
    """Remove Ghost's WYSIWYG noise from text without harming semantic markup.

    Specifically:
      - <span style="white-space: pre-wrap;">X</span>  →  X            (unwrap)
      - <code style="white-space: pre-wrap;">X</code>  →  <code>X</code> (strip style)
      - <strong style="…">X</strong>                   →  <strong>X</strong>
      - spellcheck="false" attribute                   →  removed
      - <br> / <br/>                                    →  single space
    """
    if not text:
        return text
    prev = None
    cur = text
    # Unwrap noise-only <span> wrappers — repeat until stable (Ghost nests them)
    while prev != cur:
        prev = cur
        cur = _GHOST_NOISE_SPAN.sub(r"\1", cur)
    # Strip the style attribute from ANY remaining tag
    cur = _NOISE_STYLE_ATTR.sub("", cur)
    # Strip spellcheck="false" cruft
    cur = _SPELLCHECK_ATTR.sub("", cur)
    # <br> → space
    cur = _BARE_BR.sub(" ", cur)
    return cur
