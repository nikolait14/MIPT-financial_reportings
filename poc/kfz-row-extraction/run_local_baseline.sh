#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 INPUT_PDF OUTPUT_DIR" >&2
  exit 2
fi

INPUT_PDF="$1"
OUTPUT_DIR="$2"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"
PDFTOPPM_BIN="${PDFTOPPM_BIN:-$(command -v pdftoppm)}"
BUILD_DIR="$OUTPUT_DIR/build"
MODULE_CACHE="$OUTPUT_DIR/module-cache"

mkdir -p "$BUILD_DIR" "$MODULE_CACHE"

CLANG_MODULE_CACHE_PATH="$MODULE_CACHE" \
SWIFT_MODULECACHE_PATH="$MODULE_CACHE" \
swiftc "$SCRIPT_DIR/vision_ocr.swift" -o "$BUILD_DIR/vision_ocr"

for dpi in 300 400 600; do
  DPI_DIR="$OUTPUT_DIR/${dpi}dpi"
  mkdir -p "$DPI_DIR"

  "$PDFTOPPM_BIN" -f 1 -singlefile -r "$dpi" -png \
    "$INPUT_PDF" "$DPI_DIR/page"

  for run in 1 2 3; do
    "$BUILD_DIR/vision_ocr" "$DPI_DIR/page.png" "$DPI_DIR/full-page-run-${run}.json"
  done

  "$PYTHON_BIN" "$SCRIPT_DIR/prepare_regions.py" \
    --image "$DPI_DIR/page.png" \
    --ocr-json "$DPI_DIR/full-page-run-1.json" \
    --output-dir "$DPI_DIR"

  for run in 1 2 3; do
    "$BUILD_DIR/vision_ocr" "$DPI_DIR/cells-contact-sheet.png" \
      "$DPI_DIR/region-run-${run}.json"

    "$PYTHON_BIN" "$SCRIPT_DIR/evaluate_runs.py" \
      --regions "$DPI_DIR/regions.json" \
      --full-page-json "$DPI_DIR/full-page-run-${run}.json" \
      --contact-sheet-json "$DPI_DIR/region-run-${run}.json" \
      --output "$DPI_DIR/evaluation-run-${run}.json"
  done
done
