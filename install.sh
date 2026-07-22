#!/usr/bin/env bash
set -euo pipefail

if command -v python3.12 >/dev/null 2>&1; then
  PYTHON=python3.12
elif command -v python3.11 >/dev/null 2>&1; then
  PYTHON=python3.11
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  echo "需要 Python 3.11 或更高版本。" >&2
  exit 1
fi

exec "$PYTHON" "$(cd "$(dirname "$0")" && pwd)/install.py" "$@"
