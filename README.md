# ghost-to-hugo

Migrate a [Ghost](https://ghost.org) blog to [Hugo](https://gohugo.io) without losing posts, URLs, or images.

It reads a Ghost JSON export and, for every post, writes a clean
`content/posts/<slug>/index.md`, re-hosts every image/file/video on
Cloudflare R2 (or any S3-compatible storage), and rewrites the links to
match. URLs are preserved exactly, so your old links keep working.

- **Layered pipeline** — each post flows through a fixed sequence of stages, every stage validated.
- **Parallel** — posts are processed concurrently across a pool of workers.
- **Asset re-hosting** — images are downloaded, content-hashed, uploaded once (deduped), and re-linked.
- **Verifiable** — failures are quarantined, not silently written, and every run produces a report.
- **Resumable** — re-running skips posts that already succeeded.

## How it works

One worker takes one post and carries it through these stages in order
(many posts run at once). If any stage fails its validator, the post is
quarantined and the reason recorded.

1. **Parse** — Ghost's Lexical / Mobiledoc / HTML formats into one common block structure.
2. **Metadata** — title, slug, dates, tags, authors, cover. The slug is kept exactly for SEO.
3. **Blocks** — split the body into typed blocks (headings, lists, code, images, cards…).
4. **Typography** — strip zero-width characters, fix non-breaking spaces, catch mojibake.
5. **Formatting** — bold/italic/links, nested lists, and code blocks preserved byte-for-byte.
6. **Assets** — download every image/file/video, hash it, upload to R2, rewrite the URL.
7. **Cards** — map Ghost cards (callout, bookmark, gallery, embed, toggle, audio, video) to Hugo shortcodes.
8. **Assemble** — write `content/posts/<slug>/index.md` and confirm Hugo can build it.

At the end you get a `migration-report.md`: posts in the export vs.
written vs. quarantined, plus a pass rate for every stage.

## Requirements

- Python 3.11+
- A Cloudflare R2 bucket (or any S3-compatible storage) for assets
- [Hugo](https://gohugo.io) — to verify the build (optional but recommended)

## Setup

```bash
git clone https://github.com/Harsh-2002/ghost-to-hugo.git
cd ghost-to-hugo
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
```

## Configure

```bash
cp config.example.yaml config.yaml
```

Then edit `config.yaml`:

- `ghost.export_file` — path to your Ghost export (Ghost Admin → Settings → Export).
- `ghost.base_url` — your old Ghost site URL (used to resolve relative image paths).
- `hugo.content_dir` — your Hugo site's `content/posts` directory.
- `r2.*` — your Cloudflare R2 endpoint, keys, bucket, and public CDN URL.

`config.yaml` is gitignored, so your credentials never get committed.

> **Shortcodes:** Ghost cards become Hugo shortcodes, so your Hugo site
> needs matching shortcode templates. A working set is in
> [`tests/hugo-skeleton/layouts/shortcodes/`](tests/hugo-skeleton/layouts/shortcodes) — copy what you need.

## Run

Always start with a dry run. It validates everything and writes the
report without uploading anything.

```bash
ghost-to-hugo --dry-run            # validate the whole export, no uploads
ghost-to-hugo --dry-run --limit 3  # quick sanity check on the first 3 posts

ghost-to-hugo                      # the real run: upload assets + write posts
ghost-to-hugo                      # re-run anytime — finished posts are skipped
```

Useful flags: `--overwrite` (re-emit existing posts), `--skip-drafts`,
`--posts SLUG …` (only these), `--workers N`. Run `ghost-to-hugo --help`
for the full list.

## Excluding posts

To leave some posts behind (old listicles, test posts, etc.):

```bash
cp exclude.example.txt exclude.txt   # then add one slug per line
ghost-to-hugo --exclude-file exclude.txt
```

`exclude.txt` is gitignored. You can also pass slugs inline with
`--exclude slug-one slug-two`.

## Verify

A run is "done" when:

1. `migration-report.md` shows zero quarantined posts and every stage at 100%.
2. `hugo --gc --minify` builds with no errors.
3. A search for old-domain URLs in `content/` comes back empty.

## Tests

A self-contained end-to-end test builds a synthetic Ghost export, runs
the migration, and Hugo-builds the result to verify URLs, ordering,
cards, and asset rewriting — all offline.

```bash
bash tests/run_e2e.sh
```

## License

[MIT](LICENSE) © Anurag Vishwakarma
