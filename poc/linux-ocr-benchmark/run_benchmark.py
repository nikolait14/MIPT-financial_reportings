#!/usr/bin/env python3
"""Benchmark Linux OCR providers on the target KФЗ row inside Docker."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from financial_verification.kfz_regions import prepare_regions
from financial_verification.ocr import OCRProvider, provider_by_name, write_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-pdf", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--providers", nargs="+", default=["paddle", "tesseract"])
    parser.add_argument("--dpis", nargs="+", type=int, default=[300, 400, 600])
    parser.add_argument("--repeats", type=int, default=3)
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


def center(observation: dict) -> tuple[float, float]:
    left, top, width, height = observation["bboxPixels"]
    return left + width / 2, top + height / 2


def text_in_bbox(observations: list[dict], bbox: list[int]) -> tuple[str, list[dict]]:
    left, top, right, bottom = bbox
    matches = []
    for observation in observations:
        center_x, center_y = center(observation)
        if left < center_x < right and top - 4 <= center_y <= bottom + 4:
            matches.append(observation)
    matches.sort(key=lambda item: item["bboxPixels"][0])
    return " ".join(item["text"] for item in matches), matches


def evaluate(regions: dict, ocr: dict, gold: dict[str, str], bbox_key: str) -> dict:
    cells = []
    for cell in regions["cells"]:
        raw, observations = text_in_bbox(ocr["observations"], cell[bbox_key])
        normalized = normalize_number(raw)
        expected = gold[cell["field_id"]]
        cells.append(
            {
                "field_id": cell["field_id"],
                "expected": expected,
                "raw": raw,
                "normalized": normalized,
                "exact_match": normalized == expected,
                "evidence": [item["bboxPixels"] for item in observations],
            }
        )
    errors = [cell for cell in cells if not cell["exact_match"]]
    return {
        "status": "PASS" if not errors else "FAIL",
        "matched": len(cells) - len(errors),
        "total": len(cells),
        "critical_errors": len(errors),
        "cells": cells,
    }


def recognize(provider: OCRProvider, image: Path, output: Path) -> tuple[dict, float]:
    started = time.perf_counter()
    result = provider.recognize(image)
    duration = time.perf_counter() - started
    write_result(result, output)
    return asdict(result), duration


def render_page(pdf: Path, dpi: int, output_prefix: Path) -> Path:
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
            str(output_prefix),
        ],
        check=True,
    )
    return output_prefix.with_suffix(".png")


def benchmark_provider(
    provider_name: str,
    provider: OCRProvider,
    args: argparse.Namespace,
    context: dict,
    gold: dict[str, str],
) -> dict:
    provider_result: dict[str, Any] = {
        "provider_key": provider_name,
        "provider": provider.name,
        "dpis": {},
    }
    for dpi in args.dpis:
        dpi_dir = args.output_dir / provider_name / f"{dpi}dpi"
        dpi_dir.mkdir(parents=True, exist_ok=True)
        page_path = render_page(args.input_pdf, dpi, dpi_dir / "page")
        dpi_result: dict[str, Any] = {"dpi": dpi, "runs": [], "preparation_status": "PENDING"}
        full_payloads = []
        try:
            for run_number in range(1, args.repeats + 1):
                payload, duration = recognize(
                    provider,
                    page_path,
                    dpi_dir / f"full-page-run-{run_number}.json",
                )
                full_payloads.append((payload, duration))
            regions = prepare_regions(
                page_path,
                full_payloads[0][0],
                context["target_row_label"],
                context["report_row_fields"],
                dpi_dir,
            )
            dpi_result["preparation_status"] = "PASS"
            contact_sheet = dpi_dir / "cells-contact-sheet.png"
            for run_number in range(1, args.repeats + 1):
                region_payload, region_duration = recognize(
                    provider,
                    contact_sheet,
                    dpi_dir / f"region-run-{run_number}.json",
                )
                full_evaluation = evaluate(
                    regions, full_payloads[run_number - 1][0], gold, "bbox"
                )
                region_evaluation = evaluate(regions, region_payload, gold, "slot_bbox")
                run_result = {
                    "run": run_number,
                    "full_page_duration_seconds": round(full_payloads[run_number - 1][1], 4),
                    "region_duration_seconds": round(region_duration, 4),
                    "full_page": full_evaluation,
                    "region_first": region_evaluation,
                }
                dpi_result["runs"].append(run_result)
                (dpi_dir / f"evaluation-run-{run_number}.json").write_text(
                    json.dumps(run_result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception as error:  # noqa: BLE001 - one provider/DPI must not stop the matrix
            dpi_result["preparation_status"] = "FAIL"
            dpi_result["error"] = f"{type(error).__name__}: {error}"

        dpi_result["region_pass_runs"] = sum(
            run["region_first"]["status"] == "PASS" for run in dpi_result["runs"]
        )
        dpi_result["full_page_pass_runs"] = sum(
            run["full_page"]["status"] == "PASS" for run in dpi_result["runs"]
        )
        durations = [run["region_duration_seconds"] for run in dpi_result["runs"]]
        dpi_result["median_region_seconds"] = (
            round(statistics.median(durations), 4) if durations else None
        )
        dpi_result["qualified"] = (
            dpi_result["preparation_status"] == "PASS"
            and dpi_result["region_pass_runs"] == args.repeats
        )
        provider_result["dpis"][str(dpi)] = dpi_result
    return provider_result


def select_providers(results: list[dict], repeats: int) -> dict:
    candidates = []
    for result in results:
        for dpi, dpi_result in result["dpis"].items():
            if dpi_result["qualified"]:
                candidates.append(
                    {
                        "provider_key": result["provider_key"],
                        "provider": result["provider"],
                        "dpi": int(dpi),
                        "median_region_seconds": dpi_result["median_region_seconds"],
                    }
                )
    candidates.sort(
        key=lambda item: (item["dpi"], item["median_region_seconds"], item["provider_key"])
    )
    return {
        "status": "PASS" if candidates else "FAIL",
        "primary": candidates[0] if candidates else None,
        "fallbacks": candidates[1:] if len(candidates) > 1 else [],
        "qualification_rule": f"region-first 15/15 in {repeats}/{repeats} runs and row preparation PASS",
    }


def write_markdown(payload: dict, path: Path) -> None:
    lines = [
        "# Linux OCR benchmark",
        "",
        f"- Overall: **{payload['selection']['status']}**.",
        f"- Rule: {payload['selection']['qualification_rule']}.",
        f"- Primary: `{payload['selection']['primary']}`.",
        "",
        "| Provider | DPI | Preparation | Full-page passes | Region-first passes | Median region time | Qualified |",
        "|---|---:|---|---:|---:|---:|---|",
    ]
    for provider in payload["providers"]:
        for dpi, result in provider["dpis"].items():
            lines.append(
                f"| {provider['provider']} | {dpi} | {result['preparation_status']} | "
                f"{result['full_page_pass_runs']}/{payload['repeats']} | "
                f"{result['region_pass_runs']}/{payload['repeats']} | "
                f"{result['median_region_seconds'] or '—'} | {result['qualified']} |"
            )
            if result.get("error"):
                lines.append(f"\nError `{provider['provider']}` {dpi} DPI: `{result['error']}`.\n")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    context = json.loads(args.context.read_text(encoding="utf-8"))
    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    results = []
    for provider_name in args.providers:
        try:
            provider = provider_by_name(provider_name)
            results.append(benchmark_provider(provider_name, provider, args, context, gold))
        except Exception as error:  # noqa: BLE001 - unavailable provider remains visible
            results.append(
                {
                    "provider_key": provider_name,
                    "provider": provider_name,
                    "initialization_error": f"{type(error).__name__}: {error}",
                    "dpis": {},
                }
            )
    selection = select_providers(results, args.repeats)
    payload = {
        "input_pdf": str(args.input_pdf),
        "target_row_label": context["target_row_label"],
        "dpis": args.dpis,
        "repeats": args.repeats,
        "gold_is_evaluation_only": True,
        "providers": results,
        "selection": selection,
    }
    (args.output_dir / "benchmark.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_markdown(payload, args.output_dir / "benchmark.md")
    print(json.dumps(selection, ensure_ascii=False))
    return 0 if selection["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
