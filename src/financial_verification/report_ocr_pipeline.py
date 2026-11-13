#!/usr/bin/env python3
"""Extract a configured report row with Linux OCR without using evaluation gold."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from financial_verification.kfz_regions import prepare_regions
from financial_verification.ocr import OCRObservation, OCRResult, provider_by_name, write_result


def normalize_number(text: str) -> str | None:
    value = re.sub(r"[^0-9,.-]", "", text.replace("\u00a0", "").replace(" ", ""))
    if not value:
        return None
    if "," in value and "." in value:
        value = (
            value.replace(".", "").replace(",", ".")
            if value.rfind(",") > value.rfind(".")
            else value.replace(",", "")
        )
    elif "," in value:
        value = value.replace(",", ".")
    elif value.count(".") > 1:
        parts = value.split(".")
        value = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return f"{Decimal(value):.2f}"
    except InvalidOperation:
        return None


def observation_center(observation: dict[str, Any]) -> tuple[float, float]:
    left, top, width, height = observation["bboxPixels"]
    return left + width / 2, top + height / 2


def observations_in_bbox(observations: list[dict], bbox: list[int]) -> list[dict]:
    left, top, right, bottom = bbox
    matches = []
    for observation in observations:
        center_x, center_y = observation_center(observation)
        if left < center_x < right and top - 4 <= center_y <= bottom + 4:
            matches.append(observation)
    return sorted(matches, key=lambda item: item["bboxPixels"][0])


def extract_fields(regions: dict, ocr_payload: dict, bbox_key: str) -> list[dict]:
    fields = []
    for cell in regions["cells"]:
        observations = observations_in_bbox(ocr_payload["observations"], cell[bbox_key])
        raw = " ".join(item["text"] for item in observations)
        fields.append(
            {
                "field_id": cell["field_id"],
                "raw": raw,
                "normalized": normalize_number(raw),
                "bbox": cell["bbox"],
                "observations": observations,
            }
        )
    return fields


def require_complete(fields: list[dict]) -> None:
    incomplete = [field["field_id"] for field in fields if field["normalized"] is None]
    if incomplete:
        raise RuntimeError(f"Non-numeric or missing report fields: {', '.join(incomplete)}")


def render_page(pdf: Path, dpi: int, output_path: Path) -> Path:
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


def contact_sheet_payload_from_full_page(
    full_payload: dict, regions: dict, fields: list[dict], provider_name: str
) -> OCRResult:
    """Project full-page observations into contact-sheet slots for downstream compatibility."""
    observations = []
    for field, cell in zip(fields, regions["cells"], strict=True):
        slot_left, slot_top, slot_right, slot_bottom = cell["slot_bbox"]
        if not field["raw"]:
            continue
        observations.append(
            OCRObservation(
                text=field["raw"],
                confidence=min(
                    (float(item.get("confidence", 0.0)) for item in field["observations"]),
                    default=0.0,
                ),
                bboxNormalized=[],
                bboxPixels=[
                    slot_left + 8,
                    slot_top + 8,
                    max(1, slot_right - slot_left - 16),
                    max(1, slot_bottom - slot_top - 16),
                ],
                alternatives=[],
            )
        )
    return OCRResult(
        image=regions["contact_sheet"],
        width=900,
        height=120 * len(regions["cells"]),
        recognitionLanguages=full_payload.get("recognitionLanguages", ["ru"]),
        provider=provider_name,
        observations=observations,
    )


def run_attempt(
    pdf: Path,
    context: dict,
    output_dir: Path,
    provider_key: str,
    dpi: int,
    strategy: str,
) -> dict:
    attempt_dir = output_dir / f"{provider_key}-{dpi}dpi"
    page = render_page(pdf, dpi, attempt_dir / "page.png")
    provider = provider_by_name(provider_key)
    full_result = provider.recognize(page)
    full_payload = asdict(full_result)
    write_result(full_result, attempt_dir / "full-page.json")
    regions = prepare_regions(
        page,
        full_payload,
        context["target_row_label"],
        context["report_row_fields"],
        attempt_dir,
    )
    if strategy == "region_first":
        region_result = provider.recognize(attempt_dir / "cells-contact-sheet.png", numeric=True)
        region_payload = asdict(region_result)
        fields = extract_fields(regions, region_payload, "slot_bbox")
        require_complete(fields)
        report_result = region_result
    elif strategy == "full_page":
        fields = extract_fields(regions, full_payload, "bbox")
        require_complete(fields)
        report_result = contact_sheet_payload_from_full_page(
            full_payload, regions, fields, provider.name
        )
    else:
        raise ValueError(f"Unsupported OCR strategy: {strategy}")
    write_result(report_result, attempt_dir / "report-ocr.json")
    return {
        "status": "PASS",
        "provider_key": provider_key,
        "provider": provider.name,
        "dpi": dpi,
        "strategy": strategy,
        "report_ocr": str(attempt_dir / "report-ocr.json"),
        "regions": str(attempt_dir / "regions.json"),
        "fields": fields,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-pdf", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--primary-provider", default="paddle")
    parser.add_argument("--primary-dpi", default=300, type=int)
    parser.add_argument("--primary-strategy", default="region_first")
    parser.add_argument("--fallback-provider", default="tesseract")
    parser.add_argument("--fallback-dpi", default=400, type=int)
    parser.add_argument("--fallback-strategy", default="full_page")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    context = json.loads(args.context.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    attempts = [
        (args.primary_provider, args.primary_dpi, args.primary_strategy),
        (args.fallback_provider, args.fallback_dpi, args.fallback_strategy),
    ]
    errors = []
    for provider_key, dpi, strategy in attempts:
        try:
            result = run_attempt(
                args.input_pdf, context, args.output_dir, provider_key, dpi, strategy
            )
            result["fallback_used"] = bool(errors)
            result["failed_attempts"] = errors
            canonical_ocr = args.output_dir / "report-ocr.json"
            canonical_ocr.write_text(
                Path(result["report_ocr"]).read_text(encoding="utf-8"), encoding="utf-8"
            )
            result["report_ocr"] = str(canonical_ocr)
            (args.output_dir / "extraction.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {
                        "status": result["status"],
                        "provider": result["provider"],
                        "dpi": dpi,
                        "strategy": strategy,
                        "fields": len(result["fields"]),
                        "fallback_used": result["fallback_used"],
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        except Exception as error:  # noqa: BLE001 - fallback is an explicit product behavior
            errors.append(
                {
                    "provider_key": provider_key,
                    "dpi": dpi,
                    "strategy": strategy,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    (args.output_dir / "extraction.json").write_text(
        json.dumps({"status": "FAIL", "failed_attempts": errors}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"status": "FAIL", "failed_attempts": errors}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
