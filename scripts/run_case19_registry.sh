#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="/Users/data/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"

bash "$SCRIPT_DIR/extract_case19_ocr.sh" \
  "$PROJECT_DIR/data/case-19/all" \
  "$PROJECT_DIR/artifacts/case19-registry/ocr"

PYTHONPATH="$PROJECT_DIR/src" "$PYTHON_BIN" -m financial_verification.document_registry \
  --input-dir "$PROJECT_DIR/data/case-19/all" \
  --context "$PROJECT_DIR/config/case19-verification-context.json" \
  --ocr-dir "$PROJECT_DIR/artifacts/case19-registry/ocr" \
  --output-dir "$PROJECT_DIR/artifacts/case19-registry/results"
