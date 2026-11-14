#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="${1:-$PROJECT_DIR/config/case19-drive-files.tsv}"
OUTPUT_DIR="${2:-$PROJECT_DIR/data/case-19/all}"

mkdir -p "$OUTPUT_DIR"

file_size() {
  if stat -f '%z' "$1" >/dev/null 2>&1; then
    stat -f '%z' "$1"
  else
    stat -c '%s' "$1"
  fi
}

while IFS=$'\t' read -r file_id expected_size file_name; do
  [[ -n "$file_id" ]] || continue
  destination="$OUTPUT_DIR/$file_name"
  if [[ -f "$destination" ]]; then
    existing_size="$(file_size "$destination")"
    if [[ "$existing_size" == "$expected_size" ]]; then
      echo "existing\t$file_name\t$existing_size"
      continue
    fi
  fi
  curl -L --fail --silent --show-error \
    "https://drive.google.com/uc?export=download&id=$file_id" \
    -o "$destination"

  actual_size="$(file_size "$destination")"
  if [[ "$actual_size" != "$expected_size" ]]; then
    echo "Size mismatch for $file_name: expected $expected_size, got $actual_size" >&2
    exit 1
  fi
  echo "downloaded\t$file_name\t$actual_size"
done < "$MANIFEST"
