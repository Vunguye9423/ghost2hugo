"""Migration report generation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PostReport:
    slug: str
    title: str
    source_format: str  # lexical | mobiledoc | html
    success: bool
    quarantine_reason: str = ""
    failed_layer: str = ""
    blocks_count: int = 0
    word_count_source: int = 0
    word_count_output: int = 0
    assets_referenced: int = 0
    assets_deduped: int = 0
    assets_uploaded: int = 0
    layer_pass: dict[str, bool] = field(default_factory=dict)


@dataclass
class RunReport:
    posts_in_export: int = 0
    posts: list[PostReport] = field(default_factory=list)
    asset_uploads_total: int = 0
    asset_unique_objects: int = 0
    asset_bytes_uploaded: int = 0
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        success = [p for p in self.posts if p.success]
        failed = [p for p in self.posts if not p.success]
        formats = Counter(p.source_format for p in self.posts)
        layer_fails = Counter(p.failed_layer for p in failed if p.failed_layer)

        lines = []
        lines.append("# Ghost → Hugo migration report")
        lines.append("")
        lines.append(f"- Posts in export:    {self.posts_in_export}")
        lines.append(f"- Posts written:      {len(success)}")
        lines.append(f"- Quarantined:        {len(failed)}")
        lines.append(f"- Duration:           {self.duration_seconds:.1f}s")
        lines.append("")
        lines.append("## Source format breakdown")
        for fmt, n in formats.most_common():
            lines.append(f"- {fmt:9s}  {n}")
        lines.append("")
        lines.append("## Assets")
        lines.append(f"- Total uploads requested:   {self.asset_uploads_total}")
        lines.append(f"- Unique objects in R2:      {self.asset_unique_objects}")
        lines.append(f"- Bytes uploaded:            {self.asset_bytes_uploaded:,}")
        lines.append("")
        # Per-layer pass rate
        lines.append("## Per-layer pass rate")
        layers = ["L2-metadata", "L3-blocks", "L4-typography",
                  "L8-assets", "L10-spacing", "L11-hugo"]
        for layer in layers:
            ok = sum(1 for p in self.posts if p.layer_pass.get(layer, False))
            total = len(self.posts)
            mark = "✓" if ok == total else "⚠"
            lines.append(f"- {layer:14s} {ok}/{total}  {mark}")
        lines.append("")
        if failed:
            lines.append("## Quarantined posts")
            for p in failed:
                lines.append(f"- `{p.slug}` — {p.failed_layer or '?'}: {p.quarantine_reason}")
            lines.append("")
            if layer_fails:
                lines.append("### Fails by layer")
                for layer, n in layer_fails.most_common():
                    lines.append(f"- {layer:14s} {n}")
                lines.append("")
        if self.warnings:
            lines.append("## Warnings (non-blocking)")
            for w in self.warnings:
                lines.append(f"- {w}")
            lines.append("")
        return "\n".join(lines) + "\n"

    def write(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_markdown(), encoding="utf-8")
        return p
