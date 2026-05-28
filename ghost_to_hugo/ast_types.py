"""Unified AST shared by every parser. Each parser (Lexical / Mobiledoc / HTML)
produces a list[Block]; every downstream layer operates on the same shape."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ----------------------------------------------------------------------------
# Inline nodes — used inside paragraphs, headings, list items, quotes.
# ----------------------------------------------------------------------------

InlineKind = Literal["text", "bold", "italic", "code", "link", "strike", "br"]


@dataclass
class Inline:
    kind: InlineKind
    text: str = ""
    children: list[Inline] = field(default_factory=list)
    href: str = ""  # link target


# ----------------------------------------------------------------------------
# Block nodes — top-level document children.
# ----------------------------------------------------------------------------

BlockKind = Literal[
    "paragraph",
    "heading",
    "code",
    "list",
    "quote",
    "image",
    "gallery",
    "embed",
    "callout",
    "bookmark",
    "hr",
    "html",
    "attachment",
    "audio",
    "video",
    "toggle",
    "product",
]


@dataclass
class Block:
    kind: BlockKind
    # Universal fields (only populated where relevant)
    inlines: list[Inline] = field(default_factory=list)
    # Heading
    level: int = 0
    # Code
    code: str = ""
    language: str = ""
    # List
    ordered: bool = False
    items: list[list[Inline]] = field(default_factory=list)
    nested: list[Block] = field(default_factory=list)  # nested lists (legacy)
    # Image / gallery
    src: str = ""
    alt: str = ""
    caption: str = ""
    images: list[dict[str, str]] = field(default_factory=list)  # gallery items
    # Embed / bookmark
    url: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    publisher: str = ""
    thumbnail: str = ""
    icon: str = ""
    # Callout
    emoji: str = ""
    accent: str = ""  # info / warn / success / danger
    children: list[Block] = field(default_factory=list)  # for callout body
    # Embed
    html: str = ""
    embed_type: str = ""  # youtube, twitter, etc
    # Attachment / file / audio / video
    filename: str = ""
    size_bytes: int = 0
    mime_type: str = ""
    # Toggle / detail
    summary: str = ""
    # Raw passthrough
    raw: str = ""
    # Provenance (for debugging)
    meta: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Post — what a parser returns, what the pipeline consumes.
# ----------------------------------------------------------------------------


@dataclass
class Post:
    # Identity
    id: str
    slug: str
    title: str
    # Dates (ISO 8601 strings)
    published_at: str = ""
    updated_at: str = ""
    created_at: str = ""
    # Status
    status: str = "published"  # published | draft | scheduled
    visibility: str = "public"
    # SEO / cover
    feature_image: str = ""
    feature_image_alt: str = ""
    feature_image_caption: str = ""
    custom_excerpt: str = ""
    meta_title: str = ""
    meta_description: str = ""
    og_image: str = ""
    og_title: str = ""
    og_description: str = ""
    twitter_image: str = ""
    twitter_title: str = ""
    twitter_description: str = ""
    canonical_url: str = ""
    # Taxonomy
    tags: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    primary_author: str = ""
    primary_tag: str = ""
    # Content
    blocks: list[Block] = field(default_factory=list)
    # Source format used (for the report)
    source_format: str = "unknown"  # lexical | mobiledoc | html
    # Raw post dict from export, kept for fallback/debugging
    raw: dict[str, Any] = field(default_factory=dict)
