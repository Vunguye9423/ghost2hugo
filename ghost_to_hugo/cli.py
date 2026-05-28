"""CLI entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

from . import extract, verify as verify_mod
from .assets import AssetPipeline, LocalFilePipeline, SkipAssetsPipeline
from .config import load_config
from .pipeline import run as run_pipeline
from .r2 import R2, R2Config, R2Stub
from .report import RunReport
from .state import State

console = Console()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging(args.verbose)

    # Dispatch verify-only subcommand BEFORE loading the (possibly absent)
    # migration config — verify works against any built Hugo site.
    if args.subcommand == "verify":
        return _cmd_verify(args)

    cfg = load_config(args.config)

    # Override config from CLI
    if args.workers is not None:
        cfg.pipeline.workers = args.workers
    if args.overwrite:
        cfg.pipeline.overwrite = True

    console.rule("[bold]ghost-to-hugo[/]")
    console.print(f"  export:        [cyan]{cfg.ghost.export_file}[/]")
    console.print(f"  content_dir:   [cyan]{cfg.hugo.content_dir}[/]")
    console.print(f"  R2 bucket:     [cyan]{cfg.r2.bucket}[/]")
    console.print(f"  R2 CDN:        [cyan]{cfg.r2.public_base_url}[/]")
    console.print(f"  workers:       [cyan]{cfg.pipeline.workers}[/]")
    console.print(f"  dry-run:       [cyan]{args.dry_run}[/]")
    if args.local_assets:
        console.print(f"  local-assets:  [cyan]{args.local_assets}[/]")
    if args.posts:
        console.print(f"  limit slugs:   [cyan]{', '.join(args.posts)}[/]")
    console.print()

    # 1. Load export
    if not cfg.ghost.export_file.exists():
        console.print(f"[red]✗ export file not found: {cfg.ghost.export_file}[/]")
        return 2
    data = extract.load_export(cfg.ghost.export_file,
                                ghost_base_url=cfg.ghost.base_url)
    posts = extract.extract_posts(data,
                                  include_pages=args.include_pages,
                                  include_drafts=not args.skip_drafts)
    console.print(f"[green]✓[/] export parsed: {len(posts)} posts")

    # Apply exclusion filter — slugs in --exclude or --exclude-file are dropped
    # BEFORE any asset processing, so their images never hit R2.
    excluded: set[str] = set(args.exclude or [])
    if args.exclude_file:
        ep = Path(args.exclude_file)
        if not ep.exists():
            console.print(f"[red]✗ exclude file not found: {ep}[/]")
            return 2
        for line in ep.read_text(encoding="utf-8").splitlines():
            slug = line.strip()
            if slug and not slug.startswith("#"):
                excluded.add(slug)
    if excluded:
        before = len(posts)
        posts = [p for p in posts if p.slug not in excluded]
        dropped = before - len(posts)
        console.print(f"  excluded:      [yellow]{dropped}[/] slugs "
                      f"({len(excluded)} listed)")

    if args.limit:
        posts = posts[: args.limit]
        console.print(f"  limited to first {args.limit}")

    # 2. R2 client — only validated when we'll actually upload.
    if args.dry_run or args.skip_assets:
        r2_client = R2Stub(cfg.r2)
        if args.dry_run:
            console.print("[yellow]⚠[/] dry-run: using in-memory R2 stub (no network)")
        else:
            console.print("[yellow]⚠[/] --skip-assets: R2 smoke test bypassed")
    else:
        r2_client = R2(cfg.r2)
        try:
            r2_client.smoke_test()
            console.print(f"[green]✓[/] R2 reachable: {cfg.r2.bucket}")
        except Exception as exc:
            console.print(f"[red]✗ R2 smoke test failed: {exc}[/]")
            return 3

    asset_kwargs = {}
    asset_class = AssetPipeline
    if args.skip_assets:
        asset_class = SkipAssetsPipeline
        console.print("[yellow]⚠[/] --skip-assets: leaving every URL unchanged "
                      "(images will load from the original Ghost CDN)")
    elif args.local_assets:
        asset_class = LocalFilePipeline
        asset_kwargs = {"local_root": Path(args.local_assets)}

    # 3. State + run — placed at the HUGO PROJECT ROOT (content_dir.parent.parent),
    # not inside the content tree, so Hugo doesn't pick it up as a page.
    project_root = Path(cfg.hugo.content_dir).parent.parent
    state_path = Path(args.state) if args.state else \
        project_root / ".migration-state.json"
    state = State.load(state_path)

    limit_slugs = set(args.posts) if args.posts else None

    report = RunReport(posts_in_export=len(posts))
    report = run_pipeline(
        posts, cfg,
        r2_client=r2_client,
        asset_pipeline_class=asset_class,
        asset_pipeline_kwargs=asset_kwargs,
        state=state,
        report=report,
        limit_slugs=limit_slugs,
    )

    # 4. Asset stats from R2 client (if it's a Stub it has .uploads)
    if hasattr(r2_client, "uploads"):
        report.asset_uploads_total = len(r2_client.uploads)
        report.asset_unique_objects = len({u[0] for u in r2_client.uploads})
        report.asset_bytes_uploaded = sum(u[1] for u in r2_client.uploads)

    # 5. Write report at project root (not in the Hugo content tree)
    report_path = Path(args.report) if args.report else \
        project_root / "migration-report.md"
    report.write(report_path)

    # Console summary
    success = sum(1 for p in report.posts if p.success)
    failed = sum(1 for p in report.posts if not p.success)
    console.print()
    console.rule("[bold]migration results[/]")
    console.print(f"  written:       [green]{success}[/]")
    console.print(f"  quarantined:   [{'red' if failed else 'green'}]{failed}[/]")
    console.print(f"  report:        [cyan]{report_path}[/]")
    console.print(f"  state:         [cyan]{state.path}[/]")
    console.print(f"  duration:      {report.duration_seconds:.1f}s")

    # ---- Auto-verify unless --no-verify ----
    if args.no_verify or not args.site_url:
        if not args.no_verify and not args.site_url:
            console.print("\n[yellow]⚠[/] verify skipped: pass --site-url http://127.0.0.1:3000")
        return 0 if failed == 0 else 1

    return _run_verify(args, cfg, project_root)


def _run_verify(args, cfg, project_root) -> int:
    """Run verifier against the built Hugo site. Returns exit code."""
    console.print()
    console.rule("[bold]verifying[/]")
    # Point the leak detector at the old Ghost host (from config).
    verify_mod.configure(cfg.ghost.base_url)
    console.print(f"  site:          [cyan]{args.site_url}[/]")
    if args.browser:
        console.print(f"  mode:          [cyan]browser (Playwright)[/]")
    else:
        console.print(f"  mode:          [cyan]HTTP parallel[/]")

    cdn_host = (Path(cfg.r2.public_base_url).name
                if cfg.r2.public_base_url else "")
    from urllib.parse import urlparse
    cdn_host = (urlparse(cfg.r2.public_base_url).hostname or "")
    screenshot_dir = (project_root / ".verify-screenshots") if args.browser else None

    report = verify_mod.verify_run(
        content_dir=cfg.hugo.content_dir,
        site_url=args.site_url,
        cdn_hostname=cdn_host,
        workers=args.verify_workers,
        browser=args.browser,
        check_external_assets=args.check_external,
        screenshot_dir=screenshot_dir,
    )
    verify_report_path = project_root / "verification-report.md"
    verify_mod.write_report(report, verify_report_path)
    console.print()
    console.print(report.summary())
    console.print()
    console.print(f"  verify report: [cyan]{verify_report_path}[/]")
    if not report.passed():
        console.print("[red]✗ verification FAILED — see report[/]")
        return 2
    console.print("[green]✓ verification PASSED[/]")
    return 0


def _cmd_verify(args) -> int:
    """Standalone `verify` subcommand — no migration."""
    if not args.config:
        console.print("[red]verify needs --config to know content_dir + CDN host[/]")
        return 2
    cfg = load_config(args.config)
    project_root = Path(cfg.hugo.content_dir).parent.parent
    return _run_verify(args, cfg, project_root)


def _parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="ghost-to-hugo",
        description="Migrate a Ghost CMS export to Hugo content with R2 asset "
                    "rehosting + end-to-end verification.",
    )
    ap.add_argument("subcommand", nargs="?", default="migrate",
                    choices=["migrate", "verify"],
                    help="`migrate` (default) runs the full pipeline; "
                         "`verify` runs only the verifier against a built site")
    ap.add_argument("-c", "--config", default="config.yaml",
                    help="path to config.yaml (default: ./config.yaml)")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="don't upload to R2; use in-memory stub")
    ap.add_argument("--overwrite", action="store_true",
                    help="overwrite existing content/posts/<slug>/index.md")
    ap.add_argument("--workers", type=int, default=None,
                    help="override worker count")
    ap.add_argument("--limit", type=int, default=None,
                    help="process only the first N posts")
    ap.add_argument("--posts", nargs="*", metavar="SLUG",
                    help="only process these slugs (space-separated)")
    ap.add_argument("--exclude", nargs="*", metavar="SLUG", default=[],
                    help="skip these slugs (space-separated)")
    ap.add_argument("--exclude-file", default=None,
                    help="path to a file with one slug-to-skip per line "
                         "(# comments allowed)")
    ap.add_argument("--skip-drafts", action="store_true",
                    help="skip posts whose status != published")
    ap.add_argument("--include-pages", action="store_true",
                    help="include type=page entries (default: posts only)")
    ap.add_argument("--state", default=None,
                    help="path to state json (default: <content_dir>/../.migration-state.json)")
    ap.add_argument("--report", default=None,
                    help="path to migration-report.md")
    ap.add_argument("--local-assets", default=None,
                    help="(testing) serve assets from a local root instead of HTTP")
    ap.add_argument("--skip-assets", action="store_true",
                    help="preview mode: leave every asset URL untouched (no R2 upload, "
                         "no rewrite). Images load from the original Ghost CDN.")
    # ---- Verification flags ----
    ap.add_argument("--site-url", default=None,
                    help="URL of the running Hugo site (enables auto-verify after migration). "
                         "Example: http://127.0.0.1:3000")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip post-migration verification")
    ap.add_argument("--browser", action="store_true",
                    help="use Playwright/Chromium for verification (slower, deeper). "
                         "Default: HTTP parallel.")
    ap.add_argument("--verify-workers", type=int, default=16,
                    help="parallel workers for verification (default: 16)")
    ap.add_argument("--check-external", action="store_true",
                    help="also HEAD-check external (non-CDN) image URLs in HTTP mode")
    ap.add_argument("-v", "--verbose", action="count", default=0,
                    help="increase log verbosity (-v, -vv)")
    return ap.parse_args(argv)


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    if verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, show_path=False,
                              rich_tracebacks=True)],
    )


if __name__ == "__main__":
    sys.exit(main())
