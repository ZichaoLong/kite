#!/usr/bin/env bash
# check-docs.sh — KITE docs consistency checks (adapted from FOCUS).
#
# Checks:
#   1. bilingual pairs: every docs/<dir>/<name>.md has <name>.zh-CN.md and vice versa
#   2. doc-index covers every doc (canonical names in doc-index.md, zh names in doc-index.zh-CN.md)
#   3. no trailing whitespace in docs
#   4. docs paths referenced by README.md exist
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0
err() { echo "check-docs: ERROR: $*" >&2; fail=1; }

DOC_DIRS="contracts architecture decisions verification research"

# 1. Bilingual pairs.
for dir in $DOC_DIRS; do
  [ -d "docs/$dir" ] || continue
  while IFS= read -r f; do
    [ -f "${f%.zh-CN.md}.md" ] || err "missing English canonical for $f"
  done < <(find "docs/$dir" -maxdepth 1 -name '*.zh-CN.md')
  while IFS= read -r f; do
    [ -f "${f%.md}.zh-CN.md" ] || err "missing zh-CN translation for $f"
  done < <(find "docs/$dir" -maxdepth 1 -name '*.md' ! -name '*.zh-CN.md')
done

# 2. doc-index coverage.
while IFS= read -r f; do
  grep -Fq "$(basename "$f")" docs/doc-index.md || err "doc-index.md does not mention $f"
done < <(find docs -mindepth 2 -name '*.md' ! -name '*.zh-CN.md' ! -path 'docs/_work/*')
while IFS= read -r f; do
  grep -Fq "$(basename "$f")" docs/doc-index.zh-CN.md || err "doc-index.zh-CN.md does not mention $f"
done < <(find docs -mindepth 2 -name '*.zh-CN.md' ! -path 'docs/_work/*')

# 3. No trailing whitespace.
while IFS= read -r line; do
  echo "check-docs: ERROR: trailing whitespace: $line" >&2; fail=1
done < <(grep -rInE ' +$' docs --include='*.md' | grep -v '^docs/_work/' || true)

# 4. README-referenced docs paths exist.
while IFS= read -r p; do
  [ -f "$p" ] || err "README.md references missing path: $p"
done < <(grep -oE 'docs/[A-Za-z0-9_./-]+\.md' README.md | sort -u)

if [ "$fail" -eq 0 ]; then
  echo "check-docs: OK"
fi
exit "$fail"
