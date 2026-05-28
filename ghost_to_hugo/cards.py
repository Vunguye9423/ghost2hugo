"""L9 — Card → Hugo shortcode mapper.

Maps every Ghost card-type Block to a Hugo shortcode call that matches the
shortcodes in your Hugo site (a working set is in tests/hugo-skeleton).

Hugo shortcode templates expected to exist in `layouts/shortcodes/`:
  - callout.html       (info | warn | success | danger)
  - bookmark.html
  - gallery.html
  - attachment.html
  - audio.html
  - video.html
  - embed.html
  - toggle.html

If any of these don't exist yet, the assemble step will emit a stub that
documents the expected params. Migrating posts is not blocked on shortcode
templates — Hugo renders missing shortcodes as raw text but build still
succeeds. The migration report flags posts using shortcodes the blog
doesn't yet have a template for.
"""

from __future__ import annotations

from .ast_types import Block


def callout(block: Block) -> str:
    accent = block.accent or "info"
    emoji = _shortcode_arg(block.emoji)
    body = _render_block_children_as_markdown(block.children)
    return (
        f'{{{{< callout type="{accent}" emoji={emoji} >}}}}\n'
        f'{body}\n'
        f'{{{{< /callout >}}}}'
    )


def bookmark(block: Block) -> str:
    args = [
        f'url={_shortcode_arg(block.url)}',
        f'title={_shortcode_arg(block.title)}',
    ]
    if block.description:
        args.append(f'desc={_shortcode_arg(block.description)}')
    if block.author:
        args.append(f'author={_shortcode_arg(block.author)}')
    if block.publisher:
        args.append(f'publisher={_shortcode_arg(block.publisher)}')
    if block.thumbnail:
        args.append(f'thumbnail={_shortcode_arg(block.thumbnail)}')
    if block.icon:
        args.append(f'icon={_shortcode_arg(block.icon)}')
    return f'{{{{< bookmark {" ".join(args)} >}}}}'


def gallery(block: Block) -> str:
    cols = min(max(len(block.images), 2), 5)
    inner_lines = []
    for img in block.images:
        src = img.get("src", "")
        alt = img.get("alt", "")
        caption = img.get("caption", "")
        args = [f'src={_shortcode_arg(src)}']
        if alt:
            args.append(f'alt={_shortcode_arg(alt)}')
        if caption:
            args.append(f'caption={_shortcode_arg(caption)}')
        inner_lines.append(f'  {{{{< gallery-item {" ".join(args)} >}}}}')
    inner = "\n".join(inner_lines)
    return (
        f'{{{{< gallery cols="{cols}" >}}}}\n'
        f'{inner}\n'
        f'{{{{< /gallery >}}}}'
    )


def attachment(block: Block) -> str:
    args = [f'src={_shortcode_arg(block.src)}']
    if block.filename:
        args.append(f'name={_shortcode_arg(block.filename)}')
    if block.size_bytes:
        args.append(f'size="{_format_size(block.size_bytes)}"')
    if block.mime_type:
        args.append(f'mime={_shortcode_arg(block.mime_type)}')
    return f'{{{{< attachment {" ".join(args)} >}}}}'


def audio(block: Block) -> str:
    args = [
        f'src={_shortcode_arg(block.src)}',
        f'mime={_shortcode_arg(block.mime_type)}',
    ]
    if block.title:
        args.append(f'title={_shortcode_arg(block.title)}')
    if block.caption:
        args.append(f'caption={_shortcode_arg(block.caption)}')
    return f'{{{{< audio {" ".join(args)} >}}}}'


def video(block: Block) -> str:
    args = [
        f'src={_shortcode_arg(block.src)}',
        f'mime={_shortcode_arg(block.mime_type)}',
    ]
    if block.title:
        args.append(f'title={_shortcode_arg(block.title)}')
    if block.caption:
        args.append(f'caption={_shortcode_arg(block.caption)}')
    poster = (block.meta or {}).get("thumbnailSrc") or ""
    if poster:
        args.append(f'poster={_shortcode_arg(poster)}')
    return f'{{{{< video {" ".join(args)} >}}}}'


def embed(block: Block) -> str:
    # If embed is YouTube and we can extract a video id, prefer Hugo's built-in
    # `youtube` shortcode. Otherwise emit our generic `embed`.
    yt_id = _youtube_id(block.url)
    if yt_id:
        return f'{{{{< youtube "{yt_id}" >}}}}'
    args = [f'url={_shortcode_arg(block.url)}']
    if block.embed_type:
        args.append(f'type={_shortcode_arg(block.embed_type)}')
    if block.caption:
        args.append(f'caption={_shortcode_arg(block.caption)}')
    # The generic embed shortcode passes the raw HTML through if provided.
    if block.html:
        # Inner-content shortcode: html is the body
        return (f'{{{{< embed {" ".join(args)} >}}}}\n'
                f'{block.html}\n'
                f'{{{{< /embed >}}}}')
    return f'{{{{< embed {" ".join(args)} >}}}}'


def product(block: Block) -> str:
    """Ghost product card → Hugo `product` shortcode.

    Description is passed as inner-content so it can contain raw HTML
    (Ghost stores rich HTML descriptions).
    """
    args = [f'title={_shortcode_arg(block.title)}']
    if block.src:
        args.append(f'image={_shortcode_arg(block.src)}')
    meta = block.meta or {}
    if block.url:
        args.append(f'url={_shortcode_arg(block.url)}')
    if meta.get("buttonText"):
        args.append(f'button={_shortcode_arg(meta["buttonText"])}')
    if meta.get("ratingEnabled") and meta.get("starRating"):
        args.append(f'rating="{meta["starRating"]}"')
    # Always emit a closing tag — the Hugo template uses .Inner so the
    # shortcode is paired, even if the description is empty.
    inner = (block.description or "").strip()
    return (f'{{{{< product {" ".join(args)} >}}}}\n'
            f'{inner}\n'
            f'{{{{< /product >}}}}')


def toggle(block: Block) -> str:
    summary = _shortcode_arg(block.summary or "Details")
    body = _render_block_children_as_markdown(block.children)
    return (
        f'{{{{< toggle summary={summary} >}}}}\n'
        f'{body}\n'
        f'{{{{< /toggle >}}}}'
    )


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _shortcode_arg(value: str | None) -> str:
    """Quote a value so it can appear as a Hugo shortcode positional/named arg.

    - Strips inline HTML (Ghost embeds noise like `<span style="white-space:
      pre-wrap;">...</span>` in titles + captions).
    - Collapses whitespace runs (Hugo can't handle newlines inside quoted args).
    - Escapes backslashes + double quotes.
    """
    if value is None:
        return '""'
    s = str(value)
    # Strip any HTML tags from titles/captions — shortcode args are plain text
    import re
    s = re.sub(r"<[^>]+>", "", s)
    # HTML entity decode for the common ones (Ghost emits &amp; etc.)
    s = (s.replace("&amp;", "&").replace("&lt;", "<")
            .replace("&gt;", ">").replace("&quot;", '"')
            .replace("&#39;", "'").replace("&nbsp;", " "))
    # Collapse whitespace runs (incl. \n, \r, \t)
    s = re.sub(r"\s+", " ", s).strip()
    # Escape backslashes and double quotes
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _render_block_children_as_markdown(children: list[Block]) -> str:
    """Render the children of a callout/toggle as markdown body.

    Avoids circular import by deferring `render` import.
    """
    if not children:
        return ""
    from . import render
    return render.blocks_to_markdown(children).rstrip()


def _format_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}".replace(".0 ", " ")
        f /= 1024
    return f"{n} B"


def _youtube_id(url: str) -> str:
    """Extract the YouTube video id from common URL shapes, else ""."""
    import re
    m = re.search(r"(?:youtube\.com/(?:watch\?v=|embed/)|youtu\.be/)"
                  r"([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""
