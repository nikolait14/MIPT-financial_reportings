#!/usr/bin/env python3
"""Generate local Linux OCR artifacts for source PDF and PNG documents."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from financial_verification.ocr import provider_by_name


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_first_page(pdf: Path, output_path: Path, dpi: int) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "pdftoppm",
            "-f",
            "1",
            "-singlefile",
            "-r",
            str(dpi),
            "-png",
            str(pdf),
            str(output_path.with_suffix("")),
        ],
        check=True,
    )
    return output_path


def pdf_page_count(pdf: Path) -> int:
    completed = subprocess.run(
        ["pdfinfo", str(pdf)], check=True, capture_output=True, text=True
    )
    for line in completed.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise ValueError("pdfinfo did not provide page count")


def render_pages(pdf: Path, output_dir: Path, dpi: int) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    count = pdf_page_count(pdf)
    prefix = output_dir / "page"
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", "-f", "1", "-l", str(count), str(pdf), str(prefix)],
        check=True,
    )
    pages = [prefix.with_name(f"page-{page}.png") for page in range(1, count + 1)]
    if not all(page.exists() for page in pages):
        raise FileNotFoundError("Poppler did not render all PDF pages")
    return pages


def has_usable_native_text(pdf: Path) -> bool:
    """Skip OCR when Poppler finds a meaningful searchable text layer."""
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return sum(char.isalnum() for char in completed.stdout) >= 20


def cached_result_is_current(
    output_path: Path, source_sha256: str, provider_name: str, dpi: int
) -> bool:
    if not output_path.exists():
        return False
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        payload.get("sourceSha256") == source_sha256
        and payload.get("provider") == provider_name
        and payload.get("renderDpi") == dpi
        and bool(payload.get("observations") or payload.get("pages"))
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--provider", default="paddle", choices=("paddle", "tesseract"))
    parser.add_argument("--dpi", default=300, type=int)
    return parser.parse_args()


def process_source_file(
    source: Path,
    output_dir: Path,
    *,
    provider_name: str,
    dpi: int,
) -> tuple[Path, dict]:
    """OCR one PDF or PNG and return the cache path and payload."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pages_dir = output_dir / "pages"
    provider = provider_by_name(provider_name)
    source_hash = sha256_file(source)
    output_path = output_dir / f"{source.stem}.json"
    if cached_result_is_current(output_path, source_hash, provider.name, dpi):
        return output_path, json.loads(output_path.read_text(encoding="utf-8"))
    image_paths = [source]
    if source.suffix.lower() == ".pdf":
        image_paths = render_pages(source, pages_dir / source.stem, dpi)
    page_results = [asdict(provider.recognize(image_path)) for image_path in image_paths]
    payload = page_results[0] if len(page_results) == 1 else {
        "width": page_results[0]["width"],
        "height": page_results[0]["height"],
        "recognitionLanguages": page_results[0]["recognitionLanguages"],
        "provider": page_results[0]["provider"],
        "observations": page_results[0]["observations"],
        "pages": [
            {"page": index, **result}
            for index, result in enumerate(page_results, start=1)
        ],
    }
    payload.update(
        {
            "sourceFile": source.name,
            "sourceSha256": source_hash,
            "renderDpi": dpi,
            "page": 1,
        }
    )
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output_path, payload


def main() -> int:
    args = parse_args()
    context = json.loads(args.context.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    candidates = [
        path
        for path in sorted(args.input_dir.iterdir())
        if path.is_file()
        and path.name != context["report_file"]
        and path.suffix.lower() in {".pdf", ".png"}
    ]
    provider = provider_by_name(args.provider)
    processed = []
    cached = []
    native_text = []
    failures = []

    for source in candidates:
        if source.suffix.lower() == ".pdf" and has_usable_native_text(source):
            native_text.append(source.name)
            continue
        output_path = args.output_dir / f"{source.stem}.json"
        source_hash = sha256_file(source)
        if cached_result_is_current(output_path, source_hash, provider.name, args.dpi):
            cached.append(source.name)
            continue
        try:
            process_source_file(
                source,
                args.output_dir,
                provider_name=args.provider,
                dpi=args.dpi,
            )
            processed.append(source.name)
        except Exception as error:  # noqa: BLE001 - a single file must remain isolated
            failures.append(
                {"file_name": source.name, "error": f"{type(error).__name__}: {error}"}
            )

    manifest = {
        "status": "PASS" if not failures else "PARTIAL",
        "provider": provider.name,
        "dpi": args.dpi,
        "candidate_count": len(candidates),
        "processed": processed,
        "cached": cached,
        "native_text": native_text,
        "failures": failures,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False))
    # Per-file failures are data in the registry, not a process-level failure.
    return 0


if __name__ == "__main__":
    sys.exit(main())
