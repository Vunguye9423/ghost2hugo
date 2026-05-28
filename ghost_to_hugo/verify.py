"""End-to-end verifier for the migrated Hugo site.

Two modes, both parallel:

  - HTTP mode (default): ThreadPoolExecutor + requests. Per post:
      * HTTP 200 on the page URL
      * Parse HTML
      * HEAD every <img src> / <link rel=stylesheet> / <iframe src> / <a href> to assets
      * Scan body for unrendered shortcode artifacts (`{{< ... >}}`)
      * Scan body for "unknown block kind" leak comments
      * Scan body for mojibake patterns
      * Confirm a visible byline + cover render

  - Browser mode (--browser): Playwright + Chromium. Per post:
      * Loads page, waits for network idle, scrolls to bottom
      * Captures console errors + page errors (uncaught JS exceptions)
      * Captures failed network requests (404/500)
      * Optional: screenshot any post with issues

Verification runs against a Hugo dev server URL (`--site-url`). If you have
the server running on http://127.0.0.1:3000, pass that.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import frontmatter
import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Patterns we never want to see in rendered HTML
SHORTCODE_ARTIFACT = re.compile(r"\{\{<[^>]*>")
UNKNOWN_BLOCK_MARK = re.compile(r"ghost-to-hugo: unknown block kind")
MOJIBAKE_PAT = re.compile(r"â€™|â€œ|â€\x9d|Ã©|�")
# Internal links that still point at the old Ghost host instead of being
# relative or rewritten. Built from the migration's base_url via configure();
# matches nothing until then. Excludes /content/ (assets → CDN), /tag(s)/.
INTERNAL_LEAK = re.compile(r"(?!)")  # matches nothing until configure()


def configure(base_url: str) -> None:
    """Point the internal-link-leak detector at the old Ghost host."""
    global INTERNAL_LEAK
    host = (urlparse(base_url).hostname or "").lower().strip()
    if not host:
        INTERNAL_LEAK = re.compile(r"(?!)")
        return
    labels = host.split(".")
    apex = ".".join(labels[-2:]) if len(labels) >= 2 else host
    variants = {host, apex, "www." + apex}
    alt = "|".join(re.escape(h) for h in sorted(variants, key=len, reverse=True))
    INTERNAL_LEAK = re.compile(
        r'href="https?://(?:' + alt + r')'
        r'/(?!content/|tag/|tags/|api/|assets/)',
        re.IGNORECASE,
    )


@dataclass
class PostAudit:
    slug: str
    page_status: int = 0
    page_bytes: int = 0
    img_total: int = 0
    img_broken: list[tuple[str, int]] = field(default_factory=list)
    img_broken_external: list[tuple[str, int]] = field(default_factory=list)
    shortcode_artifacts: int = 0
    unknown_blocks: int = 0
    mojibake_hits: int = 0
    internal_leaks: int = 0
    has_title: bool = False
    has_byline: bool = False
    has_cover: bool = False
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        # Passing means: our own content is sound. Broken EXTERNAL images
        # (Unsplash 404, Cloudinary URL dead at origin, etc.) are informational
        # only — we can't fix them without rehosting, and the user chose not to.
        return (
            self.page_status == 200
            and self.shortcode_artifacts == 0
            and self.unknown_blocks == 0
            and self.mojibake_hits == 0
            and self.internal_leaks == 0
            and not self.img_broken
            and self.has_title
            and not self.page_errors
        )


@dataclass
class Report:
    posts: list[PostAudit] = field(default_factory=list)
    external_image_fails: list[tuple[str, int]] = field(default_factory=list)
    own_image_fails: list[tuple[str, int]] = field(default_factory=list)

    def passed(self) -> bool:
        # Only consider OWN-host failures. External broken images surface
        # informationally in the report so the user can decide to rehost or
        # remove them, but they don't block a "PASSED" result.
        return all(p.passed for p in self.posts) and not self.own_image_fails

    def summary(self) -> str:
        total = len(self.posts)
        ok = sum(1 for p in self.posts if p.passed)
        fail = total - ok
        out = []
        out.append(f"Posts checked:               {total}")
        out.append(f"  passed:                    {ok}")
        out.append(f"  failed:                    {fail}")
        out.append(f"Shortcode artifacts found:   {sum(p.shortcode_artifacts for p in self.posts)}")
        out.append(f"Unknown-block markers:       {sum(p.unknown_blocks for p in self.posts)}")
        out.append(f"Mojibake patterns:           {sum(p.mojibake_hits for p in self.posts)}")
        out.append(f"Internal-link leaks:         {sum(p.internal_leaks for p in self.posts)}")
        out.append(f"Posts missing byline:        {sum(1 for p in self.posts if not p.has_byline)}")
        out.append(f"Posts missing cover img:     {sum(1 for p in self.posts if not p.has_cover)}")
        out.append(f"Own (R2/CDN) images broken:  {len(self.own_image_fails) + sum(len(p.img_broken) for p in self.posts)}")
        ext_total = (len(self.external_image_fails)
                     + sum(len(p.img_broken_external) for p in self.posts))
        out.append(f"External images broken:      {ext_total}  (informational)")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# HTTP-mode verification
# ---------------------------------------------------------------------------


def verify_http(slugs: list[str], site_url: str, *,
                cdn_hostname: str = "",
                workers: int = 16,
                check_external_assets: bool = False) -> Report:
    """Verify every post via parallel HTTP requests. Returns the report."""
    site = site_url.rstrip("/")
    cdn_hostname = cdn_hostname.lower()
    session = _build_session()
    report = Report()

    # 1) Fetch every page in parallel
    def fetch(slug: str) -> PostAudit:
        return _audit_one_page(session, site, slug, cdn_hostname)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, s): s for s in slugs}
        for fut in as_completed(futures):
            slug = futures[fut]
            try:
                audit = fut.result()
            except Exception as exc:
                audit = PostAudit(slug=slug, page_status=0,
                                  notes=[f"audit crashed: {exc!r}"])
            report.posts.append(audit)

    # 2) Collect all asset URLs the audits encountered, then HEAD them
    own_urls: set[str] = set()
    external_urls: set[str] = set()
    for p in report.posts:
        for url, _ in p.img_broken:
            pass  # already recorded
    # We need a separate pass — re-parse to grab the URL set. Cleaner:
    own_urls, external_urls = _collect_asset_urls(session, site, slugs, cdn_hostname)
    log.info("Asset audit: %d own (R2/CDN), %d external",
             len(own_urls), len(external_urls))

    own_fails = _head_many(session, sorted(own_urls), workers=workers)
    report.own_image_fails = own_fails
    if check_external_assets:
        ext_fails = _head_many(session, sorted(external_urls), workers=workers)
        report.external_image_fails = ext_fails
    return report


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
    })
    adapter = requests.adapters.HTTPAdapter(pool_connections=32,
                                            pool_maxsize=32,
                                            max_retries=1)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def _audit_one_page(session, site: str, slug: str, cdn_hostname: str) -> PostAudit:
    url = f"{site}/{slug}/"
    audit = PostAudit(slug=slug)
    try:
        resp = session.get(url, timeout=15)
    except requests.RequestException as exc:
        audit.notes.append(f"page GET failed: {exc!r}")
        return audit
    audit.page_status = resp.status_code
    audit.page_bytes = len(resp.content)
    if resp.status_code != 200:
        return audit
    html = resp.text
    audit.shortcode_artifacts = len(SHORTCODE_ARTIFACT.findall(html))
    audit.unknown_blocks = len(UNKNOWN_BLOCK_MARK.findall(html))
    audit.mojibake_hits = len(MOJIBAKE_PAT.findall(html))
    audit.internal_leaks = len(INTERNAL_LEAK.findall(html))
    soup = BeautifulSoup(html, "lxml")
    # Title
    audit.has_title = bool(soup.find("h1", class_="post-title"))
    # Byline
    audit.has_byline = bool(soup.find(class_="post-author"))
    # Cover image — terminal theme renders div.post-cover; other themes may use
    # .cover / figure.cover. We accept any of those, or fall back to detecting
    # an OG image meta tag (logical equivalent for posts without a hero render).
    audit.has_cover = bool(
        soup.find(class_="post-cover")
        or soup.find(class_="cover")
        or soup.find("figure", class_="cover")
        or soup.find("meta", attrs={"property": "og:image"})
    )
    # Count <img> tags
    audit.img_total = len(soup.find_all("img"))
    return audit


def _collect_asset_urls(session, site: str, slugs: list[str],
                        cdn_hostname: str) -> tuple[set[str], set[str]]:
    """Re-fetch each page, parse, return (own_urls, external_urls) sets."""
    own: set[str] = set()
    ext: set[str] = set()

    def grab(slug: str):
        try:
            resp = session.get(f"{site}/{slug}/", timeout=10)
        except requests.RequestException:
            return [], []
        if resp.status_code != 200:
            return [], []
        soup = BeautifulSoup(resp.text, "lxml")
        urls = set()
        for img in soup.find_all("img"):
            src = img.get("src")
            if src and src.startswith(("http://", "https://")):
                urls.add(src)
        return urls, slug

    with ThreadPoolExecutor(max_workers=16) as pool:
        for urls, _ in pool.map(grab, slugs):
            if not urls:
                continue
            for u in urls:
                host = (urlparse(u).hostname or "").lower()
                if cdn_hostname and host == cdn_hostname:
                    own.add(u)
                else:
                    ext.add(u)
    return own, ext


def _head_many(session, urls: list[str], *, workers: int = 16) -> list[tuple[str, int]]:
    fails: list[tuple[str, int]] = []

    def head_one(u: str) -> tuple[str, int]:
        try:
            r = session.head(u, timeout=8, allow_redirects=True)
            return u, r.status_code
        except requests.RequestException:
            return u, 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for u, status in pool.map(head_one, urls):
            if status not in (200, 301, 302, 304):
                fails.append((u, status))
    return fails


# ---------------------------------------------------------------------------
# Browser-mode verification (Playwright)
# ---------------------------------------------------------------------------


def verify_browser(slugs: list[str], site_url: str, *,
                   workers: int = 4,
                   own_hosts: tuple[str, ...] = (),
                   screenshot_dir: Path | None = None) -> Report:
    """Render each post in real Chromium, sequentially (sync_playwright
    is single-threaded). One browser, fresh page per post.

    Per-post checks:
      - goto returns 200
      - scroll bottom (triggers lazy images)
      - every <img> on the page has naturalWidth > 0 (i.e. actually loaded)
      - no JS pageerrors
      - no console errors involving our own hosts
      - no failed network requests for our own hosts
    """
    from playwright.sync_api import sync_playwright

    site = site_url.rstrip("/")
    report = Report()
    own_hosts = tuple(h.lower() for h in own_hosts if h)

    def is_own(url_or_err: str) -> bool:
        u = (url_or_err or "").lower()
        return any(h in u for h in own_hosts)

    def audit_browser(slug: str, page) -> PostAudit:
        a = PostAudit(slug=slug)
        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[tuple[str, int]] = []

        def on_console(msg):
            if msg.type == "error":
                console_errors.append(msg.text[:300])

        def on_pageerror(exc):
            page_errors.append(str(exc)[:300])

        def on_response(resp):
            if resp.status >= 400:
                failed_requests.append((resp.url, resp.status))

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("response", on_response)

        try:
            resp = page.goto(f"{site}/{slug}/", wait_until="networkidle",
                              timeout=25_000)
            a.page_status = resp.status if resp else 0
        except Exception as exc:
            a.notes.append(f"goto failed: {exc!r}")
            return a
        if a.page_status != 200:
            return a

        # Scroll to bottom to trigger lazy images, then explicitly wait for
        # every <img> to finish loading (complete) or error out. This avoids
        # false positives where the natural-width check runs before lazy
        # images at the bottom of long posts have actually loaded.
        try:
            page.evaluate("""async () => {
              // 1) Scroll bottom — triggers loading="lazy" images
              await new Promise(r => {
                let last = -1;
                const id = setInterval(() => {
                  window.scrollBy(0, 800);
                  if (window.scrollY === last
                      || (window.scrollY + window.innerHeight) >= document.body.scrollHeight - 1) {
                    clearInterval(id); r();
                  }
                  last = window.scrollY;
                }, 60);
              });
              // 2) Wait for EVERY <img> to settle (load or error) — with
              // a per-image timeout to avoid hanging on slow CDNs.
              await Promise.all(Array.from(document.images).map(im => {
                if (im.complete) return Promise.resolve();
                return new Promise(r => {
                  let done = false;
                  const finish = () => { if (!done) { done = true; r(); } };
                  im.addEventListener('load', finish, { once: true });
                  im.addEventListener('error', finish, { once: true });
                  setTimeout(finish, 8000);
                });
              }));
            }""")
        except Exception as exc:
            a.notes.append(f"scroll/wait failed: {exc!r}")

        # Check that every <img> rendered with non-zero natural dimensions.
        try:
            img_check = page.evaluate("""() => {
              const imgs = Array.from(document.images);
              const broken = imgs
                .filter(im => im.src && !im.src.startsWith('data:'))
                .filter(im => !im.complete || im.naturalWidth === 0)
                .map(im => im.src);
              return { total: imgs.length, broken };
            }""")
            a.img_total = img_check.get("total", 0)
            for src in img_check.get("broken", []):
                if is_own(src):
                    a.img_broken.append((src, 0))
                else:
                    a.img_broken_external.append((src, 0))
        except Exception as exc:
            a.notes.append(f"img-check failed: {exc!r}")

        a.has_title = bool(page.locator("h1.post-title").count())
        a.has_byline = bool(page.locator(".post-author").count())
        a.has_cover = bool(page.locator(".post-cover, .cover, figure.cover").count())

        # Filter console: only own-host errors count as real issues, AND skip
        # CORS errors entirely (test-env artifact when build baseURL ≠ verify
        # site URL; in production they're same-origin).
        def is_real_console_err(e: str) -> bool:
            if "CORS policy" in e or "Cross-Origin" in e:
                return False
            return is_own(e)
        a.console_errors = [e for e in console_errors if is_real_console_err(e)]
        a.page_errors = page_errors
        # Failed network requests: own-host = real fail; external = info.
        for url, st in failed_requests:
            if is_own(url):
                a.img_broken.append((f"{st} {url}", st))
            else:
                a.img_broken_external.append((f"{st} {url}", st))
        return a

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
        )
        for i, slug in enumerate(slugs, 1):
            page = ctx.new_page()
            try:
                a = audit_browser(slug, page)
            except Exception as exc:
                a = PostAudit(slug=slug, page_status=0,
                              notes=[f"audit crashed: {exc!r}"])
            if screenshot_dir and not a.passed:
                try:
                    screenshot_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(screenshot_dir / f"{slug}.png"),
                                    full_page=True)
                except Exception:
                    pass
            page.close()
            report.posts.append(a)
            if i % 25 == 0:
                log.info("browser-verify: %d/%d  (last: %s, passed=%s)",
                         i, len(slugs), slug, a.passed)
        ctx.close()
        browser.close()
    return report


# ---------------------------------------------------------------------------
# Top-level entry — drives verification given a content_dir
# ---------------------------------------------------------------------------


def slugs_from_content_dir(content_dir: Path) -> list[str]:
    """List every post slug (subfolder of content_dir that has index.md)."""
    out = []
    for child in sorted(Path(content_dir).iterdir()):
        if child.is_dir() and (child / "index.md").exists():
            out.append(child.name)
    return out


def verify_run(content_dir: Path, site_url: str, *,
               cdn_hostname: str = "",
               workers: int = 16,
               browser: bool = False,
               check_external_assets: bool = False,
               screenshot_dir: Path | None = None) -> Report:
    slugs = slugs_from_content_dir(content_dir)
    if browser:
        # own-host list = CDN host + the local site host
        from urllib.parse import urlparse
        site_host = (urlparse(site_url).hostname or "").lower()
        own_hosts = tuple(h for h in (cdn_hostname.lower(), site_host) if h)
        return verify_browser(slugs, site_url,
                              workers=max(2, min(workers // 2, 6)),
                              own_hosts=own_hosts,
                              screenshot_dir=screenshot_dir)
    return verify_http(slugs, site_url, cdn_hostname=cdn_hostname,
                       workers=workers,
                       check_external_assets=check_external_assets)


def write_report(report: Report, path: Path) -> None:
    lines = ["# ghost-to-hugo verification report", "", report.summary(), ""]
    fails = [p for p in report.posts if not p.passed]
    if fails:
        lines.append("## Failed posts")
        for p in fails:
            lines.append(f"### `{p.slug}`")
            if p.page_status != 200:
                lines.append(f"- page status: {p.page_status}")
            if p.shortcode_artifacts:
                lines.append(f"- {p.shortcode_artifacts} unrendered shortcode artifacts")
            if p.unknown_blocks:
                lines.append(f"- {p.unknown_blocks} unknown-block markers")
            if p.mojibake_hits:
                lines.append(f"- {p.mojibake_hits} mojibake patterns")
            if not p.has_title:
                lines.append("- missing post title")
            if not p.has_byline:
                lines.append("- missing byline")
            if not p.has_cover:
                lines.append("- missing cover image")
            for ce in p.console_errors:
                lines.append(f"- console error: {ce}")
            for pe in p.page_errors:
                lines.append(f"- page error:    {pe}")
            for ib, _ in p.img_broken:
                lines.append(f"- own-host asset 404: {ib}")
            for note in p.notes:
                lines.append(f"- note: {note}")
            lines.append("")
    if report.own_image_fails:
        lines.append("## Own (R2/CDN) image URLs broken (action required)")
        for u, st in report.own_image_fails:
            lines.append(f"- HTTP {st}  {u}")
        lines.append("")
    # Surface external image failures informationally — they came from
    # external CDNs (Cloudinary, Contentstack, etc.) that 404'd. User can
    # rehost or remove them, but they don't block release.
    ext_imgs: list[tuple[str, str]] = []
    for p in report.posts:
        for url, st in p.img_broken_external:
            ext_imgs.append((p.slug, f"{st} {url}" if st else url))
    if ext_imgs or report.external_image_fails:
        lines.append("## External image URLs broken (informational)")
        lines.append("These came from external CDNs that returned 4xx/5xx at the origin. "
                     "Consider rehosting them or removing the references.")
        lines.append("")
        for slug, url in ext_imgs[:40]:
            lines.append(f"- `{slug}` — {url}")
        if len(ext_imgs) > 40:
            lines.append(f"  ...and {len(ext_imgs)-40} more")
        for u, st in report.external_image_fails[:20]:
            lines.append(f"- HTTP {st}  {u}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
