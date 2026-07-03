#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv/bin"

cd "$ROOT"

echo "=== lint ==="
"$VENV/ruff" check .

echo ""
echo "=== tests ==="
"$VENV/python" -m pytest tests/ -v "$@"
