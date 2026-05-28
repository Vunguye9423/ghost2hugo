"""L5+L6+L7+L10 — Block AST → Markdown rendering.

Produces CommonMark-correct markdown with explicit blank-line separators
between blocks. Layers handled here:
  - L5 inline formatting (bold/italic/strike/code/link/br)
  - L6 code blocks (byte-perfect)
  - L7 lists with nested indentation
  - L10 spacing (blank lines around every block)

Card-type blocks (callout, gallery, bookmark, etc.) are delegated to cards.py.
"""

from __future__ import annotations

from . import cards
from .ast_types import Block, Inline

# Two newlines between every block is CommonMark-safe for paragraphs, headings,
# code fences, lists, blockquotes, and HTML.
SEP = "\n\n"


def blocks_to_markdown(blocks: list[Block]) -> str:
    """Render a list of blocks to a markdown body."""
    parts: list[str] = []
    for blk in blocks:
        rendered = _render(blk)
        if rendered:
            parts.append(rendered)
    return SEP.join(parts) + "\n"


# ----------------------------------------------------------------------------
# Block dispatch
# ----------------------------------------------------------------------------


def _render(block: Block) -> str:
    kind = block.kind
    if kind == "paragraph":
        return _inlines(block.inlines)
    if kind == "heading":
        level = max(1, min(6, block.level or 2))
        return f'{"#" * level} {_inlines(block.inlines)}'
    if kind == "quote":
        body = _inlines(block.inlines)
        return "\n".join(f"> {line}" for line in body.splitlines() or [""])
    if kind == "code":
        return _code_block(block)
    if kind == "list":
        return _list(block, depth=0)
    if kind == "image":
        return _image(block)
    if kind == "hr":
        return "---"
    if kind == "html":
        # Pass HTML through verbatim — Hugo handles raw HTML in markdown
        return (block.raw or "").rstrip()
    if kind == "callout":
        return cards.callout(block)
    if kind == "bookmark":
        return cards.bookmark(block)
    if kind == "gallery":
        return cards.gallery(block)
    if kind == "attachment":
        return cards.attachment(block)
    if kind == "audio":
        return cards.audio(block)
    if kind == "video":
        return cards.video(block)
    if kind == "embed":
        return cards.embed(block)
    if kind == "toggle":
        return cards.toggle(block)
    if kind == "product":
        return cards.product(block)
    # Unknown — drop with a marker
    return f"<!-- ghost-to-hugo: unknown block kind={kind!r} -->"


# ----------------------------------------------------------------------------
# Inline rendering
# ----------------------------------------------------------------------------


def _inlines(inlines: list[Inline]) -> str:
    return "".join(_inline(i) for i in inlines)


def _inline(inl: Inline) -> str:
    k = inl.kind
    if k == "text":
        return _escape_text(inl.text)
    if k == "br":
        return "  \n"  # CommonMark hard break (two trailing spaces + newline)
    if k == "bold":
        inner = _inlines(inl.children)
        return f"**{inner}**" if inner else ""
    if k == "italic":
        inner = _inlines(inl.children)
        return f"*{inner}*" if inner else ""
    if k == "strike":
        inner = _inlines(inl.children)
        return f"~~{inner}~~" if inner else ""
    if k == "code":
        # Use plain text (no escaping) — inline code preserves content
        raw = "".join(_collect_text(c) for c in inl.children) if inl.children else inl.text
        # If the code contains backticks, fence with more backticks
        fence = "`"
        while fence in raw:
            fence += "`"
        # Add padding spaces if raw starts/ends with backtick
        pad = " " if raw.startswith("`") or raw.endswith("`") else ""
        return f"{fence}{pad}{raw}{pad}{fence}"
    if k == "link":
        inner = _inlines(inl.children) or inl.text or inl.href
        href = inl.href or ""
        # Escape parens in href
        href = href.replace("(", "%28").replace(")", "%29")
        return f"[{inner}]({href})"
    return _escape_text(inl.text or "")


def _collect_text(inl: Inline) -> str:
    if inl.text:
        return inl.text
    return "".join(_collect_text(c) for c in inl.children)


# Characters that have special meaning in markdown body
_ESCAPE_CHARS = "\\`*_{}[]<>"


def _escape_text(text: str) -> str:
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in _ESCAPE_CHARS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# ----------------------------------------------------------------------------
# Code blocks
# ----------------------------------------------------------------------------


def _code_block(block: Block) -> str:
    # Strip a single trailing newline (Ghost often stores it) so the fence
    # closes flush against the last line of code instead of having a blank
    # line before it. Internal blank lines inside the code are preserved.
    code = (block.code or "").rstrip("\n")
    lang = (block.language or "").strip()
    # Pick a fence of backticks longer than any run inside the code
    fence_len = 3
    if "```" in code:
        # Find longest backtick run, fence = max + 1
        max_run = 0
        run = 0
        for ch in code:
            if ch == "`":
                run += 1
                max_run = max(max_run, run)
            else:
                run = 0
        fence_len = max(fence_len, max_run + 1)
    fence = "`" * fence_len
    return f"{fence}{lang}\n{code}\n{fence}"


# ----------------------------------------------------------------------------
# Lists (with nesting)
# ----------------------------------------------------------------------------


def _list(block: Block, depth: int) -> str:
    marker_prefix = "  " * depth
    out_lines: list[str] = []
    for idx, item_inlines in enumerate(block.items):
        marker = f"{idx + 1}." if block.ordered else "-"
        text = _inlines(item_inlines).strip()
        first_line = f"{marker_prefix}{marker} {text}" if text else f"{marker_prefix}{marker}"
        out_lines.append(first_line)
        # Nested list (if any)
        nested = block.nested[idx] if idx < len(block.nested) else None
        if nested and nested.kind == "list" and nested.items:
            out_lines.append(_list(nested, depth + 1))
    return "\n".join(out_lines)


# ----------------------------------------------------------------------------
# Images (used for both inline-body images and image blocks)
# ----------------------------------------------------------------------------


def _image(block: Block) -> str:
    # Empty src means the asset pipeline marked this image as dead-external —
    # drop the entire image block rather than render a broken reference.
    if not (block.src or "").strip():
        return ""
    alt = (block.alt or "").replace("[", "(").replace("]", ")")
    src = block.src or ""
    src_escaped = src.replace("(", "%28").replace(")", "%29")
    img = f"![{alt}]({src_escaped})"
    if block.caption:
        # Hugo built-in `figure` shortcode for image+caption. Use the same
        # quote/backslash escaping as our card shortcodes so captions with
        # quotes don't break the shortcode.
        args = [f"src={cards._shortcode_arg(src)}"]
        if alt:
            args.append(f"alt={cards._shortcode_arg(alt)}")
        args.append(f"caption={cards._shortcode_arg(block.caption)}")
        return "{{< figure " + " ".join(args) + " >}}"
    href = (block.meta or {}).get("href")
    if href:
        return f"[{img}]({href})"
    return img
