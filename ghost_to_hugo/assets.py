"""L8 — Asset pipeline.

For each post:
  1. Walk the AST + frontmatter-image fields to collect every asset URL.
  2. For each URL: download → sha256 → upload to R2 (idempotent) → record map.
  3. Rewrite all references in the post to the new R2 CDN URL.

Operates per-post (one worker handles one post end-to-end). R2 upload is
naturally idempotent: keys are hash-derived, so two workers uploading the
same image race safely.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

# Ghost's image resizer inserts `/size/<spec>/` into image URLs. The spec
# can be a single dim (`w256`), packed combinations (`w256h256`), or a chain
# (`w256/h256`). We strip the entire `/size/.../` segment regardless.
_GHOST_SIZE_RE = re.compile(
    r"/size/[whx0-9]+(?:/[whx0-9]+)*(?=/)",
    re.IGNORECASE,
)

from .ast_types import Block, Inline, Post

log = logging.getLogger(__name__)


# Common image / file extensions Ghost emits
_KNOWN_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".svg",
    ".mp3", ".m4a", ".wav", ".ogg",
    ".mp4", ".webm", ".mov",
    ".pdf", ".zip", ".csv", ".txt", ".json",
}


@dataclass
class AssetResult:
    original_url: str
    r2_key: str
    r2_url: str
    size_bytes: int
    content_type: str
    deduplicated: bool  # True = HEAD said already-exists, skipped upload


class AssetPipeline:
    def __init__(self, r2, ghost_base_url: str, *,
                 rehost_external: bool = True,
                 max_retries: int = 3,
                 timeout: int = 30):
        self.r2 = r2
        self.ghost_base = ghost_base_url.rstrip("/")
        self.rehost_external = rehost_external
        self.max_retries = max_retries
        self.timeout = timeout
        # Cross-post URL cache: avoids re-downloading the same image when
        # multiple posts reference it (also avoids redundant HEAD calls).
        self._cache: dict[str, AssetResult] = {}
        # External URL liveness cache (host alive at source, not 404)
        # url → True (alive) | False (dead) | None (never checked)
        self._external_alive: dict[str, bool] = {}
        # Persisting stats for the report
        self.stats = {
            "downloaded": 0,
            "uploaded": 0,
            "deduped": 0,
            "external_kept": 0,
            "external_dead": 0,
            "errors": 0,
        }

    def process_post(self, post: Post) -> list[AssetResult]:
        """Mutate post in place. Returns list of assets touched."""
        touched: list[AssetResult] = []

        # 1. Cover / feature image + OG / Twitter images
        post.feature_image = self._maybe(post.feature_image, touched)
        post.og_image = self._maybe(post.og_image, touched)
        post.twitter_image = self._maybe(post.twitter_image, touched)

        # 2. Walk blocks
        for block in post.blocks:
            self._walk_block(block, touched)

        # 3. Sweep — find Ghost-CDN URLs in raw HTML blocks (Ghost html cards)
        # that the structural walker missed. This catches stuff like inline
        # <img src="..."> tags inside an html block.
        for block in post.blocks:
            self._sweep_block_strings(block, touched)
        return touched

    def _sweep_block_strings(self, block: Block, touched: list[AssetResult]) -> None:
        if block.kind == "html" and block.raw:
            block.raw = self._sweep_string(block.raw, touched)
        for inl in block.inlines:
            self._sweep_inline_strings(inl, touched)
        for item in block.items:
            for inl in item:
                self._sweep_inline_strings(inl, touched)
        for child in block.children:
            self._sweep_block_strings(child, touched)
        for nested in block.nested:
            self._sweep_block_strings(nested, touched)

    def _sweep_inline_strings(self, inl: Inline, touched: list[AssetResult]) -> None:
        if inl.text:
            inl.text = self._sweep_string(inl.text, touched)
        for c in inl.children:
            self._sweep_inline_strings(c, touched)

    _URL_RE = __import__("re").compile(
        r"https?://[A-Za-z0-9.\-_]+/[A-Za-z0-9._/%~\-+:?#=&]*"
    )

    # Match an HTML <img …> tag (greedy on the closing >, which is fine because
    # > can't appear in attribute values without entity-encoding).
    _IMG_TAG_RE = __import__("re").compile(
        r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\'][^>]*?>',
        __import__("re").IGNORECASE,
    )

    def _sweep_string(self, text: str, touched: list[AssetResult]) -> str:
        """Find every URL in `text`, route through _maybe; return rewritten text.

        Special handling: when a URL is dead-external (returns "" from _maybe)
        AND it's wrapped in an `<img src="..." …>` tag, the entire tag is
        removed (not just the src) — otherwise we'd leave `<img src="">` which
        renders as a tiny broken image icon in most browsers.
        """
        # First pass: drop entire <img> tags whose src is a dead external URL.
        def repl_img(m):
            src = m.group(1)
            new = self._maybe(src, touched)
            if new == "":
                return ""  # whole tag drops
            if new != src:
                # URL rewritten — substitute in the tag, leave rest intact
                return m.group(0).replace(src, new, 1)
            return m.group(0)
        text = self._IMG_TAG_RE.sub(repl_img, text)

        # Second pass: bare URL rewrites (links, src in code blocks, etc.)
        def repl(m):
            url = m.group(0).rstrip(".,;)]\"'")
            if not self._looks_like_asset(url) and not self._is_ghost_url(url):
                return m.group(0)
            new = self._maybe(url, touched)
            if new == url:
                return m.group(0)
            tail = m.group(0)[len(url):]
            return new + tail
        return self._URL_RE.sub(repl, text)

    def _is_ghost_url(self, url: str) -> bool:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        ghost = (urlparse(self.ghost_base).hostname or "").lower()
        return bool(host) and (host == ghost or host.endswith(".ghost.io"))

    # ------------------------------------------------------------------------

    def _walk_block(self, block: Block, touched: list[AssetResult]) -> None:
        if block.kind == "image":
            new = self._maybe(block.src, touched)
            block.src = new
            # If the external URL is dead, render will drop the block (empty src)
        elif block.kind == "gallery":
            kept = []
            for img in block.images:
                new = self._maybe(img.get("src", ""), touched)
                if new != "":  # drop dead external gallery items
                    img["src"] = new
                    kept.append(img)
            block.images = kept
        elif block.kind == "bookmark":
            block.thumbnail = self._maybe(block.thumbnail, touched)
            block.icon = self._maybe(block.icon, touched)
        elif block.kind in ("attachment", "audio", "video"):
            block.src = self._maybe(block.src, touched)
            thumb = (block.meta or {}).get("thumbnailSrc")
            if thumb:
                block.meta["thumbnailSrc"] = self._maybe(thumb, touched)
        elif block.kind == "product":
            # Ghost product cards have a product image (block.src) + a rich
            # HTML description (block.description) that may itself contain
            # <img> tags pointing at the Ghost CDN.
            block.src = self._maybe(block.src, touched)
            if block.description:
                block.description = self._sweep_string(block.description, touched)
        elif block.kind == "embed":
            # Don't rehost iframe-targeted URLs; they're embed sources, not assets.
            pass
        # Inline links/images
        for inl in block.inlines:
            self._walk_inline(inl, touched)
        for item in block.items:
            for inl in item:
                self._walk_inline(inl, touched)
        for nested in block.nested:
            self._walk_block(nested, touched)
        for child in block.children:
            self._walk_block(child, touched)

    def _walk_inline(self, inl: Inline, touched: list[AssetResult]) -> None:
        if inl.kind == "link" and self._looks_like_asset(inl.href):
            inl.href = self._maybe(inl.href, touched)
        for child in inl.children:
            self._walk_inline(child, touched)

    # ------------------------------------------------------------------------

    def _maybe(self, url: str, touched: list[AssetResult]) -> str:
        """If url is something we should re-host, do so and return new URL.
        Otherwise return url unchanged.

        Special return value `""` means "drop this asset entirely": the URL
        is external (we don't rehost) AND it's 404/dead at the source. The
        caller should remove the reference from output.
        """
        if not url:
            return url
        if url.startswith("data:"):
            return url
        if url.startswith("#") or url.startswith("mailto:"):
            return url

        absolute = self._absolutize(url)
        if not self._should_rehost(absolute):
            # Not a rehostable Ghost asset. Could be:
            #   (a) a post / tag / page URL on the Ghost domain
            #   (b) an external page link (cross-references in body text)
            #   (c) a real external media asset (image / pdf / video)
            # Only (c) should be dead-checked — (a) and (b) return text/html
            # which our strict alive-check classifies as dead, and stripping
            # those breaks bookmark cards + internal cross-post links.
            if not self._looks_like_asset(absolute):
                # Just pass through; rewrite_internal_links handles host
                # rewriting later for Ghost-domain URLs.
                return url
            if self._check_external_alive(absolute):
                self.stats["external_kept"] += 1
                return url
            self.stats["external_dead"] += 1
            log.info("asset: dropping dead external url=%s", absolute)
            return ""  # signal to caller: drop this asset

        # Ghost-hosted — rehost to R2
        cached = self._cache.get(absolute)
        if cached:
            touched.append(cached)
            return cached.r2_url

        try:
            result = self._fetch_and_upload(absolute)
        except requests.HTTPError as exc:
            # 4xx at source = asset is permanently gone from Ghost. Drop the
            # reference so the post doesn't render a broken image.
            status = getattr(exc.response, "status_code", 0) if exc.response is not None else 0
            if 400 <= status < 500:
                log.info("asset: dropping permanently-dead internal url=%s status=%d",
                         absolute, status)
                self.stats["external_dead"] += 1
                return ""
            log.error("asset: transient HTTP error url=%s err=%s", absolute, exc)
            self.stats["errors"] += 1
            return url
        except Exception as exc:
            log.error("asset: failed url=%s err=%s", absolute, exc)
            self.stats["errors"] += 1
            return url
        self._cache[absolute] = result
        touched.append(result)
        return result.r2_url

    # All media content-types we treat as "real asset" responses.
    _MEDIA_CT_PREFIXES = (
        "image/", "video/", "audio/", "font/",
        "application/pdf", "application/zip",
        "application/octet-stream",
        "application/x-",        # tarballs, etc.
        "application/json",      # some APIs serve binary as json — rare
    )

    def _check_external_alive(self, url: str) -> bool:
        """Check an external media URL is reachable AND really a media asset.
        Cached. Covers images, video, audio, PDFs, fonts, archives, etc.

        Strict definition of "alive":
          - Final status (after redirects) is 2xx
          - Content-Type is a media type, NOT text/html (which indicates an
            error page / login wall returned with a 200)
          - Or — when the URL path ends in a known media extension — accept
            anything that isn't obviously HTML/JSON error.
        """
        if url in self._external_alive:
            return self._external_alive[url]
        alive = False
        try:
            resp = requests.head(url, timeout=8, headers=self._HEADERS,
                                 allow_redirects=True)
            status = resp.status_code
            ct = (resp.headers.get("Content-Type") or "").lower().split(";")[0].strip()
            if status == 405 or (200 <= status < 300 and not ct):
                resp2 = requests.get(url, timeout=8, headers=self._HEADERS,
                                     stream=True, allow_redirects=True)
                status = resp2.status_code
                ct = (resp2.headers.get("Content-Type") or "").lower().split(";")[0].strip()
                resp2.close()
            if 200 <= status < 300:
                if any(ct.startswith(p) for p in self._MEDIA_CT_PREFIXES):
                    alive = True
                elif self._looks_like_asset(url) and ct not in ("text/html", "text/plain"):
                    # Known media URL + non-error content-type → accept.
                    alive = True
        except requests.RequestException:
            alive = False
        self._external_alive[url] = alive
        return alive

    def _absolutize(self, url: str) -> str:
        if url.startswith(("http://", "https://", "//")):
            if url.startswith("//"):
                return "https:" + url
            return url
        # Relative — join with Ghost base
        return urljoin(self.ghost_base + "/", url.lstrip("/"))

    def _should_rehost(self, url: str) -> bool:
        """Return True only when this URL is a Ghost-served ASSET (under
        /content/), not a Ghost cross-post URL or a tag page URL.

        Without this filter we'd download HTML post pages and store them
        in R2 as `content/external/<hash>.bin` — pure garbage.
        """
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        ghost_host = (urlparse(self.ghost_base).hostname or "").lower()
        is_ghost_host = (host == ghost_host
                         or host.endswith(".ghost.io")
                         or "ghost-cdn" in host)
        if is_ghost_host:
            # Only rehost things under /content/ (Ghost's asset dir).
            # Cross-post links / tag pages / etc. stay external.
            return path.startswith("/content/")
        # Foreign hosts: only rehost if explicitly enabled AND it looks like
        # an actual asset (image / pdf / etc.)
        return bool(self.rehost_external) and self._looks_like_asset(url)

    def _looks_like_asset(self, url: str) -> bool:
        """Heuristic: link href points to an asset file we should re-host."""
        if not url:
            return False
        path = urlparse(url).path
        ext = os.path.splitext(path)[1].lower()
        return ext in _KNOWN_EXTS

    # ------------------------------------------------------------------------

    def _fetch_and_upload(self, url: str) -> AssetResult:
        # Decide R2 key BEFORE downloading. If it's a Ghost-CDN URL, we can
        # use the URL's path (sans the /size/wNNNN/ resizer) as the key, which
        # means a HEAD check can skip the download entirely on re-runs.
        provisional_key = self._key_for(url, body=None, content_type=None)
        if provisional_key and self.r2.exists(provisional_key):
            self.stats["deduped"] += 1
            return AssetResult(
                original_url=url,
                r2_key=provisional_key,
                r2_url=self.r2.public_url(provisional_key),
                size_bytes=0,
                content_type="",
                deduplicated=True,
            )
        # Download
        body, content_type = self._download(url)
        self.stats["downloaded"] += 1
        # Final key (external URLs need the hash, which requires bytes)
        key = self._key_for(url, body=body, content_type=content_type)
        # Second HEAD in case two workers raced on an external asset
        if self.r2.exists(key):
            self.stats["deduped"] += 1
            return AssetResult(
                original_url=url, r2_key=key,
                r2_url=self.r2.public_url(key),
                size_bytes=len(body), content_type=content_type,
                deduplicated=True,
            )
        self.r2.put(key, body, content_type)
        self.stats["uploaded"] += 1
        return AssetResult(
            original_url=url, r2_key=key,
            r2_url=self.r2.public_url(key),
            size_bytes=len(body), content_type=content_type,
            deduplicated=False,
        )

    def _key_for(self, url: str, *,
                 body: bytes | None,
                 content_type: str | None) -> str:
        """Compute the R2 key for an asset URL.

        Ghost-hosted URLs (anything under /content/{images,files,media}/) map
        directly to the same path in R2 — preserves Ghost's natural structure
        for SEO + browsability + consistency with the existing R2 layout.
        The /size/wNNNN/ image-resizer segment is stripped.

        External URLs (Unsplash, github, etc.) have no Ghost path, so they
        go under content/external/{sha256-16}.{ext}. The hash requires the
        bytes; this returns "" if called before the download for an external.
        """
        parsed = urlparse(url)
        path = parsed.path or ""
        # Strip the resizer segment
        path = _GHOST_SIZE_RE.sub("", path)
        # Collapse double slashes that might result
        while "//" in path:
            path = path.replace("//", "/")
        path = path.lstrip("/")

        ghost_host = (urlparse(self.ghost_base).hostname or "").lower()
        host = (parsed.hostname or "").lower()
        is_ghost = (host == ghost_host
                    or host.endswith(".ghost.io")
                    or "ghost-cdn" in host)

        if is_ghost and path.startswith("content/"):
            return path  # use Ghost's path verbatim

        # External — need body bytes to hash. If not yet available, signal
        # caller by returning "" so it knows to download first.
        if body is None:
            return ""
        digest = hashlib.sha256(body).hexdigest()[:16]
        ext = _ext_for(url, content_type or "")
        return f"content/external/{digest}{ext}"

    # Browser-like UA — Ghost's Cloudflare-fronted CDN drops connections
    # for non-browser User-Agents (caused our first migration attempt to fail).
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def _download(self, url: str) -> tuple[bytes, str]:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, timeout=self.timeout,
                                    headers=self._HEADERS,
                                    allow_redirects=True)
                resp.raise_for_status()
                ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                if not ct:
                    ct = mimetypes.guess_type(url)[0] or "application/octet-stream"
                return resp.content, ct
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
        assert last_exc is not None
        raise last_exc


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


_EXT_FROM_CT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
    "image/svg+xml": ".svg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "application/pdf": ".pdf",
}


def _ext_for(url: str, content_type: str) -> str:
    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext and ext in _KNOWN_EXTS:
        # Normalize .jpeg → .jpg for consistency
        return ".jpg" if ext == ".jpeg" else ext
    ext = _EXT_FROM_CT.get((content_type or "").lower())
    return ext or ".bin"


# ----------------------------------------------------------------------------
# Local-file asset pipeline (for testing without network)
# ----------------------------------------------------------------------------


class LocalFilePipeline(AssetPipeline):
    """Test variant: serves assets from a local directory instead of HTTPing.

    Maps URLs of the form `https://blog.example.com/content/images/foo.png`
    to a local path like `<root>/content/images/foo.png`. Used by the e2e test
    to exercise the full pipeline without network.
    """

    def __init__(self, r2, ghost_base_url: str, local_root: Path, **kw):
        super().__init__(r2, ghost_base_url, **kw)
        self.local_root = Path(local_root)

    def _download(self, url: str) -> tuple[bytes, str]:
        path = urlparse(url).path.lstrip("/")
        f = self.local_root / path
        if not f.exists():
            raise FileNotFoundError(f"local fixture missing: {f}")
        ct = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        return f.read_bytes(), ct


class SkipAssetsPipeline:
    """Preview-mode pipeline: leaves every URL untouched.

    Use during local Hugo preview when you don't yet want to upload to R2.
    Images then load from the original Ghost CDN (which is still live).
    """

    def __init__(self, *args, **kwargs):
        self.stats = {"downloaded": 0, "uploaded": 0, "deduped": 0,
                      "external_kept": 0, "errors": 0}

    def process_post(self, post) -> list:
        return []
