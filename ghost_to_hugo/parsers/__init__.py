"""Source parsers — L1 of the pipeline.

Every parser exposes one function:

    parse(post_dict: dict) -> list[Block]

The orchestrator picks the right parser based on which fields the post has:
    - `lexical` (string, JSON-encoded) → Lexical (Ghost 5.x)
    - `mobiledoc` (string, JSON-encoded) → Mobiledoc (Ghost 3-4.x)
    - else → HTML fallback (`html` field)
"""

from . import html_fallback, lexical, mobiledoc

__all__ = ["lexical", "mobiledoc", "html_fallback"]
