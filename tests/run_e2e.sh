#!/usr/bin/env bash
# End-to-end test for ghost-to-hugo.
#   1. Regenerate synthetic fixture (deterministic).
#   2. Run migration into tests/out/ (Hugo project skeleton copied first).
#   3. Hugo-build tests/out and verify ordering + URLs + content.
#
# Exits non-zero on any verification failure.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$ROOT/.venv/bin/python"
HUGO="${HUGO_BIN:-$HOME/.local/bin/hugo}"
OUT="$ROOT/tests/out"

echo "==> regenerating fixture"
"$PY" "$SCRIPT_DIR/fixtures/build_fixture.py" >/dev/null

echo "==> resetting tests/out from skeleton"
rm -rf "$OUT"
cp -r "$SCRIPT_DIR/hugo-skeleton" "$OUT"

echo "==> running migration (dry-run, local assets)"
cd "$ROOT"
"$PY" -m ghost_to_hugo \
  -c tests/fixtures/config.yaml \
  --dry-run \
  --local-assets tests/fixtures/assets \
  --overwrite

echo
echo "==> hugo build"
cd "$OUT"
"$HUGO" --minify --logLevel=warn >/tmp/hugo-build.log 2>&1
if grep -q "ERROR" /tmp/hugo-build.log; then
  echo "FAIL: hugo build had errors"
  cat /tmp/hugo-build.log
  exit 1
fi

echo
echo "==> verifying"
fail=0
check() {
  local desc="$1"; shift
  if eval "$@"; then
    echo "  ok  $desc"
  else
    echo "  FAIL $desc"
    fail=1
  fi
}

# (a) 4 published posts written, draft excluded by Hugo
check "4 published posts in public/" \
  "[ \$(ls -d $OUT/public/{hello-from-2023,basic-2024-post,the-long-2025-read,everything-card-types} 2>/dev/null | wc -l) -eq 4 ]"
check "draft NOT in public/" \
  "[ ! -d $OUT/public/a-draft-post ]"

# (b) chronological ordering in /posts/ list page
post_list="$OUT/public/posts/index.html"
check "chronological order (newest first)" \
  "grep -q -B0 -A0 'everything-card-types' '$post_list' && \
   python3 -c \"
import re,sys
html=open('$post_list').read()
order=[m for m in re.findall(r'/(hello-from-2023|basic-2024-post|the-long-2025-read|everything-card-types)/', html)]
sys.exit(0 if order==['everything-card-types','the-long-2025-read','basic-2024-post','hello-from-2023'] else 1)
\""

# (c) slug-only URLs (no /posts/ prefix)
check "everything post at /everything-card-types/" \
  "[ -f $OUT/public/everything-card-types/index.html ]"

# (d) no Ghost-origin URLs anywhere in output. We migrated to Ghost's
# natural /content/{images,files,media}/ paths under the CDN host, so
# /content/images/ in a URL is now LEGITIMATE — only flag the OLD Ghost
# origin (blog.example.com in the fixture).
check "no Ghost-origin URLs in posts" \
  "! grep -r 'blog\.example\.com\|\\.ghost\\.io' $OUT/public/*-*/index.html"

# (e) all CDN-hosted asset URLs follow the natural Ghost-path layout
check "asset URLs use cdn.example.com with natural paths" \
  "[ \$(grep -rho 'https://cdn.example.com/content/[a-z]*/' $OUT/public/ | wc -l) -ge 6 ]"

# (f) callout shortcodes rendered
check "all 4 callout variants rendered" \
  "[ \$(grep -oE 'callout-(info|warn|success|danger)' $OUT/public/everything-card-types/index.html | sort -u | wc -l) -eq 4 ]"

# (g) chronological dates correct
check "2023 post is dated 2023" \
  "grep -q 'datetime=2023-06-15' $OUT/public/hello-from-2023/index.html"
check "2026 post is dated 2026" \
  "grep -q 'datetime=2026-03-01' $OUT/public/everything-card-types/index.html"

# (h) inline spacing — 'Plain, **bold**, *italic*'
check "inline formatting preserves spaces" \
  "grep -q 'Plain, <strong>bold</strong>, <em>italic</em>' $OUT/public/everything-card-types/index.html"

# (i) Migration report exists OUTSIDE content tree
check "migration-report.md at project root" \
  "[ -f $OUT/migration-report.md ]"
check "migration-report.md NOT in content/" \
  "[ ! -f $OUT/content/migration-report.md ]"

# (j) state file
check "state file at project root" \
  "[ -f $OUT/.migration-state.json ]"

# (k) all 5 posts present in state.completed (including draft)
check "state lists all 5 posts" \
  "python3 -c \"
import json,sys
s=json.load(open('$OUT/.migration-state.json'))
sys.exit(0 if len(s['completed'])==5 else 1)
\""

echo
if [ $fail -eq 0 ]; then
  echo "ALL CHECKS PASSED"
else
  echo "VERIFICATION FAILED"
  exit 1
fi
