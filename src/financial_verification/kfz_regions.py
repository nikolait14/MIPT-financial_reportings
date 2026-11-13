#!/usr/bin/env python3
"""Locate a target KФЗ row and build OCR-friendly cell crops."""

from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def group_consecutive(values: np.ndarray) -> list[list[int]]:
    groups: list[list[int]] = []
    for value in values.tolist():
        if not groups or value > groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return groups


def normalized_label(value: str) -> str:
    value = re.sub(r"\s+", " ", value.lower().replace("ё", "е"))
    return value.replace("зтап", "этап").replace("етап", "этап")


def find_anchor(observations: list[dict], target_label: str) -> dict:
    target = normalized_label(target_label)
    target_years = re.findall(r"20\d{2}", target)
    target_stage = re.search(r"\b(\d+)\s*этап", target)
    candidates = []
    for observation in observations:
        value = normalized_label(observation["text"])
        has_year = not target_years or target_years[0] in value
        has_stage = "этап" in value
        has_number = not target_stage or value.lstrip().startswith(target_stage.group(1))
        if has_year and has_stage and has_number:
            candidates.append(observation)
    if not candidates:
        # Tesseract commonly emits a table-row label as separate words. Rebuild
        # visual lines before matching; this is based only on geometry and the
        # requested label, never on expected monetary values.
        heights = [item["bboxPixels"][3] for item in observations if item["bboxPixels"][3] > 0]
        tolerance = max(8.0, (statistics.median(heights) if heights else 10.0) * 0.8)
        lines: list[list[dict]] = []
        for observation in sorted(
            observations,
            key=lambda item: (
                item["bboxPixels"][1] + item["bboxPixels"][3] / 2,
                item["bboxPixels"][0],
            ),
        ):
            _, top, _, height = observation["bboxPixels"]
            center_y = top + height / 2
            for line in lines:
                line_centers = [
                    item["bboxPixels"][1] + item["bboxPixels"][3] / 2 for item in line
                ]
                if abs(center_y - statistics.median(line_centers)) <= tolerance:
                    line.append(observation)
                    break
            else:
                lines.append([observation])
        for line in lines:
            line.sort(key=lambda item: item["bboxPixels"][0])
            value = normalized_label(" ".join(item["text"] for item in line))
            has_year = not target_years or target_years[0] in value
            has_stage = "этап" in value
            has_number = not target_stage or bool(
                re.search(rf"(?:^|\s){re.escape(target_stage.group(1))}\s*этап", value)
            )
            if not (has_year and has_stage and has_number):
                continue
            left = min(item["bboxPixels"][0] for item in line)
            top = min(item["bboxPixels"][1] for item in line)
            right = max(item["bboxPixels"][0] + item["bboxPixels"][2] for item in line)
            bottom = max(item["bboxPixels"][1] + item["bboxPixels"][3] for item in line)
            confidence = min(float(item.get("confidence", 0.0)) for item in line)
            candidates.append(
                {
                    "text": " ".join(item["text"] for item in line),
                    "confidence": confidence,
                    "bboxPixels": [left, top, right - left, bottom - top],
                    "bboxNormalized": [],
                    "alternatives": [],
                    "composedFrom": len(line),
                }
            )
    if not candidates:
        raise RuntimeError(f"Target row label was not found: {target_label!r}")
    candidates.sort(key=lambda item: item["bboxPixels"][1])
    return candidates[-1]


def find_horizontal_borders(gray: np.ndarray, center_y: float) -> tuple[int, int]:
    _, width = gray.shape
    x0, x1 = int(width * 0.01), int(width * 0.995)
    score = (gray[:, x0:x1] < 200).sum(axis=1)
    candidate_rows = np.where(score > (x1 - x0) * 0.55)[0]
    centers = [
        int(round((group[0] + group[-1]) / 2))
        for group in group_consecutive(candidate_rows)
    ]
    above = [value for value in centers if value < center_y]
    below = [value for value in centers if value > center_y]
    if not above or not below:
        raise RuntimeError("Cannot locate horizontal borders around target row")
    return max(above), min(below)


def find_vertical_borders(
    gray: np.ndarray, row_top: int, row_bottom: int, expected_count: int
) -> list[int]:
    height, width = gray.shape
    row_height = max(1, row_bottom - row_top)
    y0 = max(0, row_top - row_height * 3)
    y1 = min(height, row_bottom + row_height * 2)
    region = gray[y0:y1, :]
    score = (region < 200).sum(axis=0)
    candidates = np.where(score > region.shape[0] * 0.60)[0]
    centers = [
        int(round((group[0] + group[-1]) / 2))
        for group in group_consecutive(candidates)
    ]
    centers = [value for value in centers if width * 0.01 < value < width * 0.999]
    if len(centers) != expected_count:
        raise RuntimeError(
            f"Expected {expected_count} vertical borders, found {len(centers)}: {centers}"
        )
    return centers


def build_contact_sheet(image: Image.Image, cells: list[dict], output_path: Path) -> list[dict]:
    slot_width = 900
    slot_height = 120
    margin = 12
    sheet = Image.new("RGB", (slot_width, slot_height * len(cells)), "white")
    manifest = []
    for index, cell in enumerate(cells):
        left, top, right, bottom = cell["bbox"]
        crop = image.crop((left + 2, top + 1, right - 2, bottom - 1)).convert("RGB")
        scale = min(6.0, (slot_width - margin * 2) / max(1, crop.width))
        resized = crop.resize(
            (max(1, int(crop.width * scale)), max(1, int(crop.height * scale))),
            Image.Resampling.LANCZOS,
        )
        slot_top = index * slot_height
        paste_x = (slot_width - resized.width) // 2
        paste_y = slot_top + (slot_height - resized.height) // 2
        sheet.paste(resized, (paste_x, paste_y))
        item = dict(cell)
        item["slot_bbox"] = [0, slot_top, slot_width, slot_top + slot_height]
        item["rendered_crop_bbox"] = [
            paste_x,
            paste_y,
            paste_x + resized.width,
            paste_y + resized.height,
        ]
        manifest.append(item)
    sheet.save(output_path)
    return manifest


def prepare_regions(
    image_path: Path,
    ocr_payload: dict,
    target_label: str,
    field_order: list[str],
    output_dir: Path,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    image = Image.open(image_path).convert("RGB")
    gray = np.asarray(image.convert("L"))
    anchor = find_anchor(ocr_payload["observations"], target_label)
    _, top, _, height = anchor["bboxPixels"]
    row_top, row_bottom = find_horizontal_borders(gray, top + height / 2)
    vertical_borders = find_vertical_borders(
        gray, row_top, row_bottom, expected_count=len(field_order) + 2
    )
    label_bbox = [vertical_borders[0], row_top, vertical_borders[1], row_bottom]
    value_cells = [
        {
            "index": index,
            "field_id": field_id,
            "bbox": [
                vertical_borders[index + 1],
                row_top,
                vertical_borders[index + 2],
                row_bottom,
            ],
        }
        for index, field_id in enumerate(field_order)
    ]
    contact_sheet_path = output_dir / "cells-contact-sheet.png"
    manifest = build_contact_sheet(image, value_cells, contact_sheet_path)

    evidence = image.copy()
    draw = ImageDraw.Draw(evidence)
    draw.rectangle(label_bbox, outline="blue", width=3)
    for cell in value_cells:
        draw.rectangle(cell["bbox"], outline="red", width=2)
    evidence.save(output_dir / "target-row-evidence.png")

    metadata = {
        "source_image": str(image_path),
        "image_size": [image.width, image.height],
        "target_label": target_label,
        "anchor_observation": anchor,
        "row_bbox": [vertical_borders[0], row_top, vertical_borders[-1], row_bottom],
        "label_bbox": label_bbox,
        "vertical_borders": vertical_borders,
        "contact_sheet": str(contact_sheet_path),
        "cells": manifest,
    }
    (output_dir / "regions.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--ocr-json", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    context = json.loads(args.context.read_text(encoding="utf-8"))
    ocr = json.loads(args.ocr_json.read_text(encoding="utf-8"))
    metadata = prepare_regions(
        args.image,
        ocr,
        context["target_row_label"],
        context["report_row_fields"],
        args.output_dir,
    )
    print(json.dumps({"row_bbox": metadata["row_bbox"], "cells": len(metadata["cells"])}))


if __name__ == "__main__":
    main()
