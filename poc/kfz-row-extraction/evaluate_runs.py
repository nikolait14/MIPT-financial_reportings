#!/usr/bin/env python3
"""Evaluate full-page and region OCR runs using exact per-cell matching."""

from __future__ import annotations

import argparse
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--regions", required=True, type=Path)
    parser.add_argument("--full-page-json", required=True, type=Path)
    parser.add_argument("--contact-sheet-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def normalize_number(text: str) -> str | None:
    value = text.strip().replace("\u00a0", " ")
    value = re.sub(r"[^0-9,.-]", "", value.replace(" ", ""))
    if not value:
        return None
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    elif value.count(".") > 1:
        parts = value.split(".")
        value = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return f"{Decimal(value):.2f}"
    except InvalidOperation:
        return None


def observation_center(observation: dict) -> tuple[float, float]:
    left, top, width, height = observation["bboxPixels"]
    return left + width / 2, top + height / 2


def find_text_for_bbox(observations: list[dict], bbox: list[int]) -> tuple[str, list[dict]]:
    left, top, right, bottom = bbox
    matches = []
    for observation in observations:
        center_x, center_y = observation_center(observation)
        if left < center_x < right and top - 4 <= center_y <= bottom + 4:
            matches.append(observation)
    matches.sort(key=lambda item: item["bboxPixels"][0])
    return " ".join(item["text"] for item in matches), matches


def evaluate_full_page(regions: dict, ocr: dict) -> list[dict]:
    rows = []
    for cell in regions["cells"]:
        raw, observations = find_text_for_bbox(ocr["observations"], cell["bbox"])
        normalized = normalize_number(raw)
        rows.append(
            {
                "field_id": cell["field_id"],
                "field_name": cell["field_name"],
                "expected": cell["expected"],
                "raw": raw,
                "normalized": normalized,
                "exact_match": normalized == cell["expected"],
                "evidence": [item["bboxPixels"] for item in observations],
            }
        )
    return rows


def evaluate_contact_sheet(regions: dict, ocr: dict) -> list[dict]:
    rows = []
    for cell in regions["cells"]:
        raw, observations = find_text_for_bbox(ocr["observations"], cell["slot_bbox"])
        normalized = normalize_number(raw)
        rows.append(
            {
                "field_id": cell["field_id"],
                "field_name": cell["field_name"],
                "expected": cell["expected"],
                "raw": raw,
                "normalized": normalized,
                "exact_match": normalized == cell["expected"],
                "evidence": [item["bboxPixels"] for item in observations],
            }
        )
    return rows


def result_block(name: str, rows: list[dict]) -> dict:
    errors = [row for row in rows if not row["exact_match"]]
    return {
        "method": name,
        "status": "PASS" if not errors else "FAIL",
        "matched": len(rows) - len(errors),
        "total": len(rows),
        "critical_errors": len(errors),
        "cells": rows,
    }


def write_markdown(result: dict, path: Path) -> None:
    lines = [
        "# Результат локального OCR прогона",
        "",
        f"- Full page: **{result['full_page']['status']}**, "
        f"{result['full_page']['matched']}/{result['full_page']['total']} точных совпадений.",
        f"- Region first: **{result['region_first']['status']}**, "
        f"{result['region_first']['matched']}/{result['region_first']['total']} точных совпадений.",
        "- Средние ошибки не рассчитываются.",
        "",
        "| Метод | Поле | Эталон | Распознано | Нормализовано | Статус |",
        "|---|---|---:|---|---:|---|",
    ]
    for method_key in ("full_page", "region_first"):
        method = result[method_key]
        for row in method["cells"]:
            status = "EXACT_MATCH" if row["exact_match"] else "MISMATCH"
            lines.append(
                f"| {method['method']} | {row['field_name']} | {row['expected']} | "
                f"{row['raw'] or '—'} | {row['normalized'] or '—'} | {status} |"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    regions = json.loads(args.regions.read_text(encoding="utf-8"))
    full_page_ocr = json.loads(args.full_page_json.read_text(encoding="utf-8"))
    contact_ocr = json.loads(args.contact_sheet_json.read_text(encoding="utf-8"))

    result = {
        "full_page": result_block("full_page_vision", evaluate_full_page(regions, full_page_ocr)),
        "region_first": result_block(
            "region_first_vision", evaluate_contact_sheet(regions, contact_ocr)
        ),
    }
    result["overall_status"] = (
        "PASS"
        if result["full_page"]["status"] == "PASS"
        and result["region_first"]["status"] == "PASS"
        else "FAIL"
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(result, args.output.with_suffix(".md"))
    print(
        json.dumps(
            {
                "overall_status": result["overall_status"],
                "full_page": result["full_page"]["status"],
                "region_first": result["region_first"]["status"],
            }
        )
    )


if __name__ == "__main__":
    main()
