#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ $# -gt 0 ]]; then
  echo "Custom paths are not supported; edit compose.yaml volumes instead" >&2
  exit 2
fi

cd "$PROJECT_DIR"
docker compose run --rm --build source-ocr
