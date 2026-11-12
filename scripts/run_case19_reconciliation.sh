#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="/Users/data/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
REGISTRY="$PROJECT_DIR/artifacts/case19-registry/results/document-registry.json"
REPORT_OCR="$PROJECT_DIR/artifacts/kfz-row-extraction/300dpi/region-run-1.json"

if [[ ! -f "$REGISTRY" ]]; then
  echo "Missing registry: run scripts/run_case19_registry.sh first" >&2
  exit 2
fi

if [[ ! -f "$REPORT_OCR" ]]; then
  echo "Missing report OCR: run poc/kfz-row-extraction/run_local_baseline.sh first" >&2
  exit 2
fi

PYTHONPATH="$PROJECT_DIR/src" "$PYTHON_BIN" -m financial_verification.reconciliation \
  --registry "$REGISTRY" \
  --input-dir "$PROJECT_DIR/data/case-19/all" \
  --ocr-dir "$PROJECT_DIR/artifacts/case19-registry/ocr" \
  --context "$PROJECT_DIR/config/case19-verification-context.json" \
  --rules "$PROJECT_DIR/config/cost-article-rules.json" \
  --report-ocr "$REPORT_OCR" \
  --output-dir "$PROJECT_DIR/artifacts/case19-reconciliation"
