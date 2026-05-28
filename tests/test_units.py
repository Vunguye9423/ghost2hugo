"""Unit + edge-case tests for ghost-to-hugo.

Run with:  .venv/bin/python -m unittest discover tests -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from ghost_to_hugo.ast_types import Block, Inline, Post
from ghost_to_hugo import assemble, render, typography, validate
from ghost_to_hugo.parsers import html_fallback, lexical, mobiledoc


# ----------------------------------------------------------------------------
# render.py — inline + block rendering edge cases
# ----------------------------------------------------------------------------


class TestRender(unittest.TestCase):

    def test_inline_spaces_preserved(self):
        blocks = [Block(kind="paragraph", inlines=[
            Inline(kind="text", text="Plain, "),
            Inline(kind="bold", children=[Inline(kind="text", text="bold")]),
            Inline(kind="text", text=", "),
            Inline(kind="italic", children=[Inline(kind="text", text="italic")]),
            Inline(kind="text", text="."),
        ])]
        md = render.blocks_to_markdown(blocks)
        self.assertIn("Plain, **bold**, *italic*.", md)

    def test_code_block_no_trailing_blank_line(self):
        b = Block(kind="code", code="x = 1\n", language="python")
        out = render.blocks_to_markdown([b])
        self.assertIn("```python\nx = 1\n```", out)
        self.assertNotIn("\n\n```\n", out)

    def test_code_block_with_inner_backticks_expands_fence(self):
        b = Block(kind="code", code="```nested```", language="markdown")
        out = render.blocks_to_markdown([b])
        # Must use ````` (4+ backticks) so fence isn't terminated mid-content
        self.assertTrue(out.startswith("````markdown") or "`````" in out)

    def test_heading_levels_clamped(self):
        # level=0 means "not set" → defaults to h2 (most common in body content)
        for level, expected in [(0, "## "), (1, "# "), (4, "#### "),
                                (6, "###### "), (9, "###### ")]:
            md = render.blocks_to_markdown([Block(
                kind="heading", level=level,
                inlines=[Inline(kind="text", text="x")],
            )])
            self.assertTrue(md.startswith(expected),
                            f"level={level} got {md!r}")

    def test_empty_paragraph_dropped(self):
        out = render.blocks_to_markdown([
            Block(kind="paragraph"),  # no inlines
            Block(kind="paragraph", inlines=[Inline(kind="text", text="ok")]),
        ])
        self.assertEqual(out.strip(), "ok")

    def test_nested_list(self):
        block = Block(kind="list", ordered=False, items=[
            [Inline(kind="text", text="parent")],
        ], nested=[
            Block(kind="list", ordered=False, items=[
                [Inline(kind="text", text="child")],
            ], nested=[Block(kind="paragraph")])
        ])
        out = render.blocks_to_markdown([block])
        self.assertIn("- parent", out)
        self.assertIn("  - child", out)

    def test_link_with_parens_in_href(self):
        i = Inline(kind="link", href="https://example.com/a(b)c",
                   children=[Inline(kind="text", text="x")])
        out = render.blocks_to_markdown([Block(kind="paragraph", inlines=[i])])
        self.assertIn("](https://example.com/a%28b%29c)", out)

    def test_inline_code_with_backticks_padded(self):
        i = Inline(kind="code", children=[
            Inline(kind="text", text="`hi`")
        ])
        out = render.blocks_to_markdown([Block(kind="paragraph", inlines=[i])])
        # When code starts/ends with backtick, padding spaces required
        self.assertIn("`` `hi` ``", out)

    def test_figure_shortcode_escapes_caption_quotes(self):
        b = Block(kind="image", src="https://x/y.png", alt='a "title"',
                  caption='caption with "quote" inside')
        out = render.blocks_to_markdown([b])
        self.assertIn(r'caption="caption with \"quote\" inside"', out)


# ----------------------------------------------------------------------------
# typography.py — text normalization
# ----------------------------------------------------------------------------


class TestTypography(unittest.TestCase):

    def test_preserves_inline_trailing_space(self):
        # The critical fix — inline runs need trailing space as separator
        p = Post(id="x", slug="x", title="x", blocks=[
            Block(kind="paragraph", inlines=[
                Inline(kind="text", text="hello "),
                Inline(kind="bold", children=[Inline(kind="text", text="world")]),
            ])
        ])
        typography.normalize(p)
        self.assertEqual(p.blocks[0].inlines[0].text, "hello ")

    def test_strips_zero_width_chars(self):
        p = Post(id="x", slug="x", title="x", blocks=[
            Block(kind="paragraph", inlines=[
                Inline(kind="text", text="visible​‌hidden")
            ])
        ])
        counts = typography.normalize(p)
        self.assertEqual(p.blocks[0].inlines[0].text, "visiblehidden")
        self.assertEqual(counts["zero_width_stripped"], 1)

    def test_nbsp_collapsed(self):
        p = Post(id="x", slug="x", title="x", blocks=[
            Block(kind="paragraph", inlines=[
                Inline(kind="text", text="a b c")
            ])
        ])
        counts = typography.normalize(p)
        self.assertEqual(p.blocks[0].inlines[0].text, "a b c")
        self.assertEqual(counts["nbsp_collapsed"], 1)

    def test_code_inline_not_normalized(self):
        # Inline code must preserve byte-for-byte (no NBSP → space, etc.)
        p = Post(id="x", slug="x", title="x", blocks=[
            Block(kind="paragraph", inlines=[
                Inline(kind="code", children=[
                    Inline(kind="text", text="x y​z")
                ])
            ])
        ])
        typography.normalize(p)
        # The inner text is preserved because L4 skips kind=='code'
        self.assertEqual(p.blocks[0].inlines[0].children[0].text, "x y​z")

    def test_code_block_untouched(self):
        p = Post(id="x", slug="x", title="x", blocks=[
            Block(kind="code", code="    line1\n    line2 \n", language="py")
        ])
        typography.normalize(p)
        self.assertEqual(p.blocks[0].code, "    line1\n    line2 \n")


# ----------------------------------------------------------------------------
# validate.py — per-layer validators
# ----------------------------------------------------------------------------


class TestValidators(unittest.TestCase):

    def test_slug_must_be_canonical(self):
        for bad in ["", "Has Spaces", "UPPER", "weird_underscore",
                    "no!special", "-leading-dash"]:
            p = Post(id="1", slug=bad, title="t", published_at="2024-01-01")
            v = validate.check_l2_metadata(p)
            self.assertFalse(v, f"should reject slug={bad!r}")
        for good in ["hello", "hello-world", "x123", "a-b-c-d"]:
            p = Post(id="1", slug=good, title="t", published_at="2024-01-01")
            v = validate.check_l2_metadata(p)
            self.assertTrue(v, f"should accept slug={good!r}")

    def test_l3_requires_blocks(self):
        p = Post(id="1", slug="x", title="t", published_at="2024-01-01",
                 blocks=[])
        self.assertFalse(validate.check_l3_blocks(p))
        p.blocks = [Block(kind="paragraph",
                          inlines=[Inline(kind="text", text="hi")])]
        self.assertTrue(validate.check_l3_blocks(p))

    def test_l8_flags_ghost_urls(self):
        p = Post(id="1", slug="x", title="t", published_at="2024-01-01",
                 feature_image="https://blog.example.ghost.io/img.png")
        v = validate.check_l8_assets(p, cdn_host="cdn.example.com")
        self.assertFalse(v)

    def test_l10_spacing_rejects_unclosed_fence(self):
        md = "---\nfoo: 1\n---\n\n```python\nx = 1\n"
        v = validate.check_l10_spacing(md)
        self.assertFalse(v)

    def test_l10_spacing_rejects_4_newlines(self):
        md = "para1\n\n\n\npara2"
        v = validate.check_l10_spacing(md)
        self.assertFalse(v)

    def test_l11_requires_frontmatter(self):
        self.assertFalse(validate.check_l11_hugo("no frontmatter here"))
        self.assertTrue(validate.check_l11_hugo("---\nx: 1\n---\nbody"))


# ----------------------------------------------------------------------------
# assemble.py — frontmatter shape
# ----------------------------------------------------------------------------


class TestAssemble(unittest.TestCase):

    def test_cover_is_flat_string(self):
        p = Post(id="1", slug="x", title="T", published_at="2024-01-01",
                 feature_image="https://cdn.example.com/img.png",
                 feature_image_alt="alt",
                 feature_image_caption="cap")
        fm = assemble.to_frontmatter_dict(p)
        self.assertEqual(fm["cover"], "https://cdn.example.com/img.png")
        self.assertEqual(fm["cover_alt"], "alt")
        self.assertEqual(fm["cover_caption"], "cap")
        self.assertNotIsInstance(fm["cover"], dict)

    def test_draft_status_to_flag(self):
        p = Post(id="1", slug="x", title="T", published_at="2024-01-01",
                 status="draft")
        fm = assemble.to_frontmatter_dict(p)
        self.assertTrue(fm["draft"])

    def test_published_not_marked_draft(self):
        p = Post(id="1", slug="x", title="T", published_at="2024-01-01")
        fm = assemble.to_frontmatter_dict(p)
        self.assertNotIn("draft", fm)

    def test_lastmod_omitted_if_same_as_published(self):
        p = Post(id="1", slug="x", title="T",
                 published_at="2024-01-01T00:00:00Z",
                 updated_at="2024-01-01T00:00:00Z")
        fm = assemble.to_frontmatter_dict(p)
        self.assertNotIn("lastmod", fm)

    def test_atomic_write_no_leftover_tmp(self, ):
        import tempfile, shutil
        d = Path(tempfile.mkdtemp())
        try:
            p = Post(id="1", slug="atomic-test", title="T",
                     published_at="2024-01-01",
                     blocks=[Block(kind="paragraph",
                                   inlines=[Inline(kind="text", text="body")])])
            assemble.write_post(p, d)
            tmps = list((d / "atomic-test").glob(".index.md.*"))
            self.assertEqual(tmps, [], "no leftover tmp file")
            self.assertTrue((d / "atomic-test" / "index.md").exists())
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------------
# parsers — coverage edge cases
# ----------------------------------------------------------------------------


class TestLexicalParser(unittest.TestCase):

    def test_quote_no_trailing_hard_break(self):
        node = {
            "type": "extended-quote",
            "children": [
                {"type": "paragraph", "children": [
                    {"type": "text", "text": "first sentence."}
                ]}
            ]
        }
        post = {"lexical": json.dumps({"root": {"children": [node]}})}
        blocks = lexical.parse(post)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "quote")
        # Should NOT have a trailing br
        self.assertFalse(any(i.kind == "br" for i in blocks[0].inlines))

    def test_unknown_card_kept_as_html_when_html_present(self):
        node = {"type": "exotic-card", "html": "<div>raw</div>"}
        post = {"lexical": json.dumps({"root": {"children": [node]}})}
        blocks = lexical.parse(post)
        self.assertEqual(blocks[0].kind, "html")
        self.assertEqual(blocks[0].raw, "<div>raw</div>")

    def test_invalid_json_returns_empty(self):
        blocks = lexical.parse({"lexical": "{not-json"})
        self.assertEqual(blocks, [])


class TestMobiledocParser(unittest.TestCase):

    def test_minimal_post_parses(self):
        md = {
            "version": "0.3.1",
            "atoms": [],
            "cards": [],
            "markups": [["strong"]],
            "sections": [
                [1, "h1", [[0, [], 0, "Title"]]],
                [1, "p", [[0, [], 0, "Hello "],
                          [0, [0], 1, "world"]]]
            ]
        }
        post = {"mobiledoc": json.dumps(md)}
        blocks = mobiledoc.parse(post)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].kind, "heading")
        self.assertEqual(blocks[1].kind, "paragraph")


class TestHTMLFallback(unittest.TestCase):

    def test_basic_html(self):
        post = {"html": "<p>hi <strong>bold</strong></p><h2>head</h2>"}
        blocks = html_fallback.parse(post)
        kinds = [b.kind for b in blocks]
        self.assertEqual(kinds, ["paragraph", "heading"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
