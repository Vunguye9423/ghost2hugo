"""Build a synthetic Ghost JSON export covering every card type + multiple
date eras + both Lexical and Mobiledoc formats. Run once to (re)generate
`ghost-export.json` + sample asset files in this directory."""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
EXPORT_PATH = HERE / "ghost-export.json"


# ----------------------------------------------------------------------------
# Tiny PNG generator — produces a valid 4×4 single-colour PNG without Pillow.
# ----------------------------------------------------------------------------


def _make_png(r: int, g: int, b: int, size: int = 4) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(size):
        raw += b"\x00" + bytes([r, g, b]) * size
    idat = zlib.compress(raw, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _write_assets():
    files = {
        "content/images/2023/06/hello-2023.png": _make_png(140, 192, 124),
        "content/images/2024/03/screenshot.png": _make_png(60, 80, 200),
        "content/images/2024/03/gallery-1.png": _make_png(220, 30, 30),
        "content/images/2024/03/gallery-2.png": _make_png(30, 220, 30),
        "content/images/2024/03/gallery-3.png": _make_png(30, 30, 220),
        "content/images/2024/03/cover.png": _make_png(255, 215, 0),
        "content/images/2024/03/bookmark-thumb.png": _make_png(100, 100, 100),
        "content/images/2024/03/bookmark-icon.png": _make_png(50, 50, 50),
        "content/images/2025/11/long-post-hero.png": _make_png(40, 180, 200),
        "content/images/2026/03/everything.png": _make_png(200, 60, 200),
        # A tiny PDF (just header + EOF — enough for content-type sniffing)
        "content/files/2024/03/whitepaper.pdf": (
            b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
        ),
        # A fake mp3 (ID3 header)
        "content/media/2024/03/intro.mp3": b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 64,
        # A fake mp4 (ftyp box)
        "content/media/2024/03/demo.mp4": (
            b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
            + b"\x00" * 32
        ),
    }
    for rel, body in files.items():
        f = ASSETS / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body)


# ----------------------------------------------------------------------------
# Lexical content builders
# ----------------------------------------------------------------------------


def _text(t: str, fmt: int = 0) -> dict:
    return {"type": "text", "text": t, "format": fmt, "version": 1,
            "detail": 0, "mode": "normal", "style": ""}


def _p(*children) -> dict:
    return {"type": "paragraph", "children": list(children),
            "version": 1, "direction": "ltr", "format": "", "indent": 0}


def _h(level: int, text: str) -> dict:
    return {"type": "extended-heading", "tag": f"h{level}",
            "children": [_text(text)],
            "version": 1, "direction": "ltr", "format": "", "indent": 0}


def _code(code: str, lang: str = "") -> dict:
    return {"type": "codeblock", "code": code, "language": lang, "version": 1}


def _img(src: str, alt: str = "", caption: str = "") -> dict:
    return {"type": "image", "src": src, "altText": alt, "caption": caption,
            "version": 1}


def _link(href: str, text: str) -> dict:
    return {"type": "link", "url": href, "version": 1,
            "children": [_text(text)],
            "direction": "ltr", "format": "", "indent": 0}


def _list(ordered: bool, items: list[list[dict]]) -> dict:
    children = []
    for it in items:
        children.append({
            "type": "listitem", "value": 1, "version": 1,
            "children": it,
            "direction": "ltr", "format": "", "indent": 0,
        })
    return {"type": "list",
            "listType": "number" if ordered else "bullet",
            "tag": "ol" if ordered else "ul",
            "start": 1, "version": 1,
            "children": children,
            "direction": "ltr", "format": "", "indent": 0}


# Ghost-specific cards in Lexical


def _callout(emoji: str, body: str, color: str = "blue") -> dict:
    return {"type": "callout", "version": 1,
            "calloutEmoji": emoji,
            "calloutBackgroundColor": color,
            "children": [_p(_text(body))]}


def _bookmark(url: str, title: str, desc: str) -> dict:
    return {"type": "bookmark", "version": 1, "url": url,
            "metadata": {
                "title": title,
                "description": desc,
                "author": "Some Author",
                "publisher": "example.com",
                "thumbnail": "https://blog.example.com/content/images/2024/03/bookmark-thumb.png",
                "icon": "https://blog.example.com/content/images/2024/03/bookmark-icon.png",
            }}


def _gallery(srcs: list[str]) -> dict:
    return {"type": "gallery", "version": 1,
            "images": [{"src": s, "alt": f"image {i+1}", "width": 4, "height": 4}
                       for i, s in enumerate(srcs)]}


def _embed_youtube() -> dict:
    return {"type": "embed", "version": 1,
            "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "embedType": "video",
            "html": "<iframe src='...'></iframe>"}


def _file_card(src: str, name: str) -> dict:
    return {"type": "file", "version": 1, "src": src,
            "fileName": name, "fileTitle": name,
            "fileSize": 12345, "mimeType": "application/pdf"}


def _audio_card(src: str) -> dict:
    return {"type": "audio", "version": 1, "src": src,
            "title": "Intro track", "mimeType": "audio/mpeg"}


def _video_card(src: str) -> dict:
    return {"type": "video", "version": 1, "src": src,
            "title": "Demo video", "mimeType": "video/mp4"}


def _toggle(summary: str, body: str) -> dict:
    return {"type": "toggle", "version": 1, "heading": summary,
            "children": [_p(_text(body))]}


def _hr() -> dict:
    return {"type": "horizontalrule", "version": 1}


def _quote(text: str) -> dict:
    return {"type": "extended-quote", "version": 1,
            "children": [_p(_text(text))],
            "direction": "ltr", "format": "", "indent": 0}


def _lexical_root(children: list[dict]) -> str:
    return json.dumps({
        "root": {
            "type": "root", "version": 1,
            "direction": "ltr", "format": "", "indent": 0,
            "children": children,
        },
    })


# ----------------------------------------------------------------------------
# Mobiledoc content builder (one post)
# ----------------------------------------------------------------------------


def _mobiledoc() -> str:
    """A 2023 post in Mobiledoc — exercises the legacy parser path."""
    return json.dumps({
        "version": "0.3.1",
        "atoms": [],
        "cards": [
            ["image", {
                "src": "https://blog.example.com/content/images/2023/06/hello-2023.png",
                "alt": "first hello",
                "caption": "where it all started"
            }],
            ["code", {
                "code": "echo 'hello from 2023'",
                "language": "bash",
            }],
            ["hr", {}],
        ],
        "markups": [
            ["strong"],
            ["em"],
            ["a", ["href", "https://example.com"]],
        ],
        "sections": [
            [1, "h2", [[0, [], 0, "Welcome — first ever post"]]],
            [1, "p",  [[0, [], 0, "This is the very first post on the blog. "
                              "Written in "],
                       [0, [0], 1, "Mobiledoc"],
                       [0, [], 0, " back in "],
                       [0, [1], 1, "June 2023"],
                       [0, [], 0, "."]]],
            [10, 0],
            [1, "p",  [[0, [], 0, "Code that started it:"]]],
            [10, 1],
            [1, "p",  [[0, [], 0, "Check out "],
                       [0, [2], 1, "the example"],
                       [0, [], 0, "."]]],
            [10, 2],
            [3, "ul", [
                [[0, [], 0, "first item"]],
                [[0, [], 0, "second item"]],
                [[0, [], 0, "third item with "],
                 [0, [0], 1, "bold"],
                 [0, [], 0, " inside"]],
            ]],
        ],
    })


# ----------------------------------------------------------------------------
# Posts
# ----------------------------------------------------------------------------


def _everything_post_lexical() -> str:
    """A 2026 post that exercises every Ghost card type."""
    return _lexical_root([
        _p(_text("This post exercises every Ghost card type the migration "
                 "supports — to verify the full pipeline end-to-end.")),
        _h(2, "Inline formatting"),
        _p(_text("Plain, "),
           _text("bold", 1), _text(", "),
           _text("italic", 2), _text(", "),
           _text("strike", 4), _text(", "),
           _text("code", 16), _text(", "),
           _link("https://example.com", "a link"),
           _text(".")),
        _h(2, "A code block"),
        _code("def hello():\n    print('hi from 2026')\n",
              "python"),
        _h(2, "Lists"),
        _list(False, [
            [_text("Bullet item one")],
            [_text("Bullet item two with "), _text("bold", 1)],
            [_text("Bullet item three")],
        ]),
        _list(True, [
            [_text("ordered first")],
            [_text("ordered second")],
        ]),
        _h(2, "An image with caption"),
        _img("https://blog.example.com/content/images/2026/03/everything.png",
             "everything post hero", "Hero image with caption"),
        _h(2, "A callout"),
        _callout("💡", "This is an info callout. It maps to a Hugo shortcode.",
                 "blue"),
        _callout("⚠", "This is a warn callout.", "yellow"),
        _callout("✓", "This is a success callout.", "green"),
        _callout("🔥", "This is a danger callout.", "red"),
        _h(2, "A bookmark"),
        _bookmark("https://example.com/article",
                  "An example article",
                  "An example article description with quotes \"like this\" and 'these'."),
        _h(2, "A gallery"),
        _gallery([
            "https://blog.example.com/content/images/2024/03/gallery-1.png",
            "https://blog.example.com/content/images/2024/03/gallery-2.png",
            "https://blog.example.com/content/images/2024/03/gallery-3.png",
        ]),
        _h(2, "An embed"),
        _embed_youtube(),
        _h(2, "Attachments + audio + video"),
        _file_card("https://blog.example.com/content/files/2024/03/whitepaper.pdf",
                   "whitepaper.pdf"),
        _audio_card("https://blog.example.com/content/media/2024/03/intro.mp3"),
        _video_card("https://blog.example.com/content/media/2024/03/demo.mp4"),
        _h(2, "A toggle"),
        _toggle("Click to expand",
                "Hidden content inside a toggle/details element."),
        _quote("A wise quote about migration: take it post by post."),
        _hr(),
        _p(_text("End of everything post.")),
    ])


def _long_post_lexical() -> str:
    """A 2025 post — long paragraphs + headings + nested lists."""
    return _lexical_root([
        _img("https://blog.example.com/content/images/2025/11/long-post-hero.png",
             "long-post hero"),
        _h(2, "Introduction"),
        _p(_text("This is the introduction paragraph. It's intentionally long "
                 "to exercise the typography pass and verify that spacing "
                 "between paragraphs, headings, and other blocks is correct. "
                 "Smart quotes — like 'these' and \"these\" — should be "
                 "preserved verbatim. Em-dashes — also.")),
        _h(3, "A sub-section"),
        _p(_text("Another paragraph follows the H3. The renderer should put a "
                 "blank line between every block so CommonMark renders it "
                 "correctly.")),
        _list(False, [
            [_text("Level 1 item A")],
            [_text("Level 1 item B")],
        ]),
        _h(2, "Code that survived"),
        _code("// JS — make sure ``` inside the code is preserved\n"
              "const fence = '```';\nconsole.log(fence);", "javascript"),
        _p(_text("Closing paragraph after code.")),
    ])


def _basic_2024_post_lexical() -> str:
    return _lexical_root([
        _p(_text("A simple 2024 post with a screenshot.")),
        _img("https://blog.example.com/content/images/2024/03/screenshot.png",
             "an obs screenshot"),
        _p(_text("Plain body after the image.")),
    ])


def _draft_post_lexical() -> str:
    return _lexical_root([
        _p(_text("This post is a draft — should be kept as draft in Hugo.")),
    ])


# ----------------------------------------------------------------------------
# Top-level export builder
# ----------------------------------------------------------------------------


def build_export() -> dict:
    tags = [
        {"id": "tag-1", "name": "intro", "slug": "intro"},
        {"id": "tag-2", "name": "everything", "slug": "everything"},
        {"id": "tag-3", "name": "longread", "slug": "longread"},
        {"id": "tag-4", "name": "2024", "slug": "2024"},
    ]
    users = [
        {"id": "user-1", "name": "Anurag Vishwakarma", "slug": "anurag"},
    ]
    posts = [
        {
            "id": "post-2023-hello",
            "slug": "hello-from-2023",
            "title": "Hello from 2023",
            "html": "",
            "lexical": None,
            "mobiledoc": _mobiledoc(),
            "feature_image": "https://blog.example.com/content/images/2023/06/hello-2023.png",
            "feature_image_alt": "hello 2023",
            "published_at": "2023-06-15T09:00:00.000Z",
            "updated_at": "2023-06-15T09:00:00.000Z",
            "created_at": "2023-06-15T09:00:00.000Z",
            "status": "published",
            "visibility": "public",
            "type": "post",
            "custom_excerpt": "The first ever post — written in Mobiledoc.",
        },
        {
            "id": "post-2024-basic",
            "slug": "basic-2024-post",
            "title": "A simple post from 2024",
            "html": "",
            "lexical": _basic_2024_post_lexical(),
            "mobiledoc": None,
            "feature_image": "https://blog.example.com/content/images/2024/03/cover.png",
            "feature_image_alt": "cover",
            "published_at": "2024-03-10T12:00:00.000Z",
            "updated_at": "2024-03-12T15:30:00.000Z",
            "created_at": "2024-03-10T12:00:00.000Z",
            "status": "published",
            "visibility": "public",
            "type": "post",
            "custom_excerpt": "A simple post with a screenshot.",
            "og_image": "https://blog.example.com/content/images/2024/03/cover.png",
        },
        {
            "id": "post-2025-longread",
            "slug": "the-long-2025-read",
            "title": "The long 2025 read",
            "html": "",
            "lexical": _long_post_lexical(),
            "mobiledoc": None,
            "feature_image": "https://blog.example.com/content/images/2025/11/long-post-hero.png",
            "published_at": "2025-11-21T08:45:00.000Z",
            "updated_at": "2025-11-21T08:45:00.000Z",
            "created_at": "2025-11-21T08:45:00.000Z",
            "status": "published",
            "visibility": "public",
            "type": "post",
            "custom_excerpt": "A longer read about migration.",
        },
        {
            "id": "post-2026-everything",
            "slug": "everything-card-types",
            "title": "Every Ghost card type — verification",
            "html": "",
            "lexical": _everything_post_lexical(),
            "mobiledoc": None,
            "feature_image": "https://blog.example.com/content/images/2026/03/everything.png",
            "feature_image_alt": "every type",
            "feature_image_caption": "verifying every card type",
            "published_at": "2026-03-01T10:00:00.000Z",
            "updated_at": "2026-03-01T10:00:00.000Z",
            "created_at": "2026-03-01T10:00:00.000Z",
            "status": "published",
            "visibility": "public",
            "type": "post",
            "custom_excerpt": "A test post covering every Ghost card type.",
            "canonical_url": "https://blog.example.com/everything-card-types/",
        },
        {
            "id": "post-draft",
            "slug": "a-draft-post",
            "title": "An unpublished draft",
            "html": "",
            "lexical": _draft_post_lexical(),
            "mobiledoc": None,
            "feature_image": "",
            "published_at": "2026-04-02T10:00:00.000Z",
            "updated_at": "2026-04-02T10:00:00.000Z",
            "created_at": "2026-04-02T10:00:00.000Z",
            "status": "draft",
            "visibility": "public",
            "type": "post",
            "custom_excerpt": "Draft.",
        },
    ]
    posts_tags = [
        {"post_id": "post-2023-hello", "tag_id": "tag-1", "sort_order": 0},
        {"post_id": "post-2024-basic", "tag_id": "tag-4", "sort_order": 0},
        {"post_id": "post-2025-longread", "tag_id": "tag-3", "sort_order": 0},
        {"post_id": "post-2026-everything", "tag_id": "tag-2", "sort_order": 0},
        {"post_id": "post-2026-everything", "tag_id": "tag-1", "sort_order": 1},
    ]
    posts_authors = [
        {"post_id": p["id"], "author_id": "user-1", "sort_order": 0}
        for p in posts
    ]
    return {
        "db": [{
            "meta": {"exported_on": 1717000000000, "version": "5.0.0"},
            "data": {
                "posts": posts,
                "tags": tags,
                "users": users,
                "posts_tags": posts_tags,
                "posts_authors": posts_authors,
            }
        }]
    }


def main():
    _write_assets()
    export = build_export()
    EXPORT_PATH.write_text(json.dumps(export, indent=2), encoding="utf-8")
    print(f"wrote {EXPORT_PATH}")
    print(f"wrote {sum(1 for _ in ASSETS.rglob('*')) - sum(1 for _ in ASSETS.rglob('') if _.is_dir())} asset files under {ASSETS}")


if __name__ == "__main__":
    main()
