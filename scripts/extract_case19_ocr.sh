#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
INPUT_DIR="${1:-$PROJECT_DIR/data/case-19/all}"
OUTPUT_DIR="${2:-$PROJECT_DIR/artifacts/case19-registry/ocr}"
VISION_SOURCE="$PROJECT_DIR/poc/kfz-row-extraction/vision_ocr.swift"
PDFTOPPM_BIN="/Users/data/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/pdftoppm"
MODULE_CACHE="$OUTPUT_DIR/module-cache"
BUILD_DIR="$OUTPUT_DIR/build"

mkdir -p "$OUTPUT_DIR" "$MODULE_CACHE" "$BUILD_DIR"

CLANG_MODULE_CACHE_PATH="$MODULE_CACHE" \
SWIFT_MODULECACHE_PATH="$MODULE_CACHE" \
swiftc "$VISION_SOURCE" -o "$BUILD_DIR/vision_ocr"

for input_file in "$INPUT_DIR"/*.pdf; do
  file_name="$(basename "$input_file")"
  if [[ "$file_name" == 01_* ]]; then
    continue
  fi
  stem="${file_name%.pdf}"
  "$PDFTOPPM_BIN" -f 1 -singlefile -r 300 -png \
    "$input_file" "$OUTPUT_DIR/$stem"
  "$BUILD_DIR/vision_ocr" "$OUTPUT_DIR/$stem.png" "$OUTPUT_DIR/$stem.json"
done

for input_file in "$INPUT_DIR"/*.png; do
  file_name="$(basename "$input_file")"
  stem="${file_name%.png}"
  "$BUILD_DIR/vision_ocr" "$input_file" "$OUTPUT_DIR/$stem.json"
done
