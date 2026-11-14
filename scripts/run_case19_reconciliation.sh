#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REGISTRY="$PROJECT_DIR/artifacts/case19-registry/results/document-registry.json"
LINUX_REPORT_OCR="$PROJECT_DIR/artifacts/linux-report-ocr/report-ocr.json"
REPORT_OCR="${REPORT_OCR:-$LINUX_REPORT_OCR}"

if [[ ! -f "$REGISTRY" ]]; then
  echo "Missing registry: run scripts/run_case19_registry.sh first" >&2
  exit 2
fi

if [[ "$REPORT_OCR" != "$LINUX_REPORT_OCR" ]]; then
  echo "Custom REPORT_OCR is not supported by the Docker wrapper" >&2
  exit 2
fi

if [[ ! -f "$LINUX_REPORT_OCR" ]]; then
  echo "Missing report OCR: run scripts/run_case19_linux_ocr.sh first" >&2
  exit 2
fi

cd "$PROJECT_DIR"
docker compose run --rm reconciliation
