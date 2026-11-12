#!/usr/bin/env python3
"""Locate the target KФЗ row, split it into cells, and build an OCR contact sheet."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


FIELDS = [
    ("labor_intensity", "Трудоемкость", "34.14"),
    ("materials", "Материальные затраты", "0.00"),
    ("wages_total", "Оплата труда, всего", "5558474.83"),
    ("wages_without_reserve", "Оплата труда без резерва", "5248019.04"),
    ("base_wages", "Основная заработная плата", "5054470.26"),
    ("additional_wages", "Дополнительная заработная плата", "193548.78"),
    ("leave_reserve", "Резерв отпусков", "310455.79"),
    ("insurance_total", "Страховые взносы, всего", "1671359.96"),
    ("insurance_without_reserve", "Взносы без резерва", "1578126.37"),
    ("insurance_base_wages", "Взносы на основную заработную плату", "1526450.09"),
    ("insurance_additional_wages", "Взносы на дополнительную заработную плату", "51676.28"),
    ("insurance_leave_reserve", "Взносы на резерв отпусков", "93233.59"),
    ("overhead_0800", "Общепроизводственные затраты", "1145798.10"),
    ("administrative_0900", "Общехозяйственные затраты", "3204744.51"),
    ("row_total", "Итого по строке", "11580377.40"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--ocr-json", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def group_consecutive(values: np.ndarray) -> list[list[int]]:
    groups: list[list[int]] = []
    for value in values.tolist():
        if not groups or value > groups[-1][-1] + 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return groups


def find_anchor(observations: list[dict]) -> dict:
    candidates = []
    for observation in observations:
        normalized = re.sub(r"\s+", " ", observation["text"].lower().replace("ё", "е"))
        normalized = normalized.replace("зтап", "этап").replace("етап", "этап")
        if "2026" in normalized and "этап" in normalized and normalized.lstrip().startswith("3"):
            candidates.append(observation)
    if not candidates:
        raise RuntimeError("Target row label '3 этап, 2026 г.' was not found")
    candidates.sort(key=lambda item: item["bboxPixels"][1])
    return candidates[-1]


def find_horizontal_borders(gray: np.ndarray, center_y: float) -> tuple[int, int]:
    height, width = gray.shape
    x0, x1 = int(width * 0.01), int(width * 0.995)
    score = (gray[:, x0:x1] < 200).sum(axis=1)
    candidate_rows = np.where(score > (x1 - x0) * 0.55)[0]
    groups = group_consecutive(candidate_rows)
    centers = [int(round((group[0] + group[-1]) / 2)) for group in groups]
    above = [value for value in centers if value < center_y]
    below = [value for value in centers if value > center_y]
    if not above or not below:
        raise RuntimeError("Cannot locate horizontal borders around target row")
    return max(above), min(below)


def find_vertical_borders(gray: np.ndarray, row_top: int, row_bottom: int) -> list[int]:
    height, width = gray.shape
    row_height = max(1, row_bottom - row_top)
    y0 = max(0, row_top - row_height * 3)
    y1 = min(height, row_bottom + row_height * 2)
    region = gray[y0:y1, :]
    score = (region < 200).sum(axis=0)
    candidates = np.where(score > region.shape[0] * 0.60)[0]
    groups = group_consecutive(candidates)
    centers = [int(round((group[0] + group[-1]) / 2)) for group in groups]
    centers = [value for value in centers if width * 0.01 < value < width * 0.999]

    # The target row merges the first two report columns. It must therefore
    # have 16 cells: one label plus 15 values, bounded by 17 vertical lines.
    if len(centers) != 17:
        raise RuntimeError(f"Expected 17 vertical borders, found {len(centers)}: {centers}")
    return centers


def build_contact_sheet(
    image: Image.Image,
    cells: list[dict],
    output_path: Path,
) -> list[dict]:
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


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    image = Image.open(args.image).convert("RGB")
    gray = np.asarray(image.convert("L"))
    ocr = json.loads(args.ocr_json.read_text(encoding="utf-8"))
    anchor = find_anchor(ocr["observations"])
    left, top, width, height = anchor["bboxPixels"]
    center_y = top + height / 2

    row_top, row_bottom = find_horizontal_borders(gray, center_y)
    vertical_borders = find_vertical_borders(gray, row_top, row_bottom)

    label_bbox = [vertical_borders[0], row_top, vertical_borders[1], row_bottom]
    value_cells = []
    for index, (field_id, field_name, expected) in enumerate(FIELDS):
        cell_left = vertical_borders[index + 1]
        cell_right = vertical_borders[index + 2]
        value_cells.append(
            {
                "index": index,
                "field_id": field_id,
                "field_name": field_name,
                "expected": expected,
                "bbox": [cell_left, row_top, cell_right, row_bottom],
            }
        )

    contact_sheet_path = args.output_dir / "cells-contact-sheet.png"
    manifest = build_contact_sheet(image, value_cells, contact_sheet_path)

    evidence = image.copy()
    draw = ImageDraw.Draw(evidence)
    draw.rectangle(label_bbox, outline="blue", width=3)
    for cell in value_cells:
        draw.rectangle(cell["bbox"], outline="red", width=2)
    evidence.save(args.output_dir / "target-row-evidence.png")

    metadata = {
        "source_image": str(args.image),
        "image_size": [image.width, image.height],
        "target_label": "3 этап, 2026 г.",
        "anchor_observation": anchor,
        "row_bbox": [vertical_borders[0], row_top, vertical_borders[-1], row_bottom],
        "label_bbox": label_bbox,
        "vertical_borders": vertical_borders,
        "contact_sheet": str(contact_sheet_path),
        "cells": manifest,
    }
    (args.output_dir / "regions.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps({"row_bbox": metadata["row_bbox"], "cells": len(manifest)}))


if __name__ == "__main__":
    main()
