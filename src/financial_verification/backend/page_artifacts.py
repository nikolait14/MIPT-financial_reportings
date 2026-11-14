"""Create session-scoped, page-level previews without public file URLs."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


PREVIEW_DPI = 144


def _page_count(pdf: Path) -> int:
    completed = subprocess.run(
        ["pdfinfo", str(pdf)], check=True, capture_output=True, text=True
    )
    match = re.search(r"^Pages:\s*(\d+)\s*$", completed.stdout, re.MULTILINE)
    if not match:
        raise ValueError("pdfinfo did not return page count")
    return int(match.group(1))


def _native_text_present(pdf: Path, page: int) -> bool:
    completed = subprocess.run(
        ["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(pdf), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(re.search(r"[\wА-Яа-яЁё]", completed.stdout))


def build_page_artifacts(workspace: Path, documents: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Render previews inside workspace and return only safe relative locations.

    Errors are data: a failed preview never prevents the financial result from
    being returned, and the API will not pretend an unavailable page exists.
    """
    input_dir = workspace / "input"
    preview_root = workspace / "artifacts" / "page-previews"
    result: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        document_id = document["document_id"]
        source = input_dir / document["file_name"]
        extension = source.suffix.lower()
        if extension == ".png":
            result[document_id] = [
                {
                    "page": 1,
                    "media_type": "image/png",
                    "width": None,
                    "height": None,
                    "extraction_method": "image_source",
                    "status": "AVAILABLE",
                    "source_file": source.name,
                }
            ]
            continue
        if extension != ".pdf":
            result[document_id] = []
            continue
        try:
            count = _page_count(source)
            target_dir = preview_root / document_id
            target_dir.mkdir(parents=True, exist_ok=True)
            prefix = target_dir / "page"
            subprocess.run(
                [
                    "pdftoppm", "-r", str(PREVIEW_DPI), "-png", "-f", "1", "-l", str(count),
                    str(source), str(prefix),
                ],
                check=True,
                capture_output=True,
            )
            pages = []
            for page in range(1, count + 1):
                image = prefix.with_name(f"page-{page}.png")
                if not image.exists():
                    raise FileNotFoundError(f"missing preview {image.name}")
                pages.append(
                    {
                        "page": page,
                        "media_type": "image/png",
                        "width": None,
                        "height": None,
                        "extraction_method": (
                            "native_pdf_text" if _native_text_present(source, page) else "ocr"
                        ),
                        "status": "AVAILABLE",
                        "artifact_path": str(image.relative_to(workspace)),
                    }
                )
            result[document_id] = pages
        except (OSError, ValueError, subprocess.CalledProcessError):
            result[document_id] = []
    return result


def resolve_page_path(workspace: Path, page: dict[str, Any]) -> Path | None:
    """Resolve one generated preview/source image without path traversal."""
    if page.get("artifact_path"):
        candidate = (workspace / page["artifact_path"]).resolve()
        artifacts = (workspace / "artifacts").resolve()
        return candidate if candidate.is_file() and artifacts in candidate.parents else None
    if page.get("source_file"):
        candidate = (workspace / "input" / page["source_file"]).resolve()
        inputs = (workspace / "input").resolve()
        return candidate if candidate.is_file() and candidate.parent == inputs else None
    return None
