#!/usr/bin/env python3
"""Cross-platform OCR providers with one Apple-Vision-compatible result schema."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from PIL import Image


@dataclass
class OCRCandidate:
    text: str
    confidence: float


@dataclass
class OCRObservation:
    text: str
    confidence: float
    bboxNormalized: list[float]
    bboxPixels: list[int]
    alternatives: list[OCRCandidate] = field(default_factory=list)


@dataclass
class OCRResult:
    image: str
    width: int
    height: int
    recognitionLanguages: list[str]
    provider: str
    observations: list[OCRObservation]


class OCRProvider(Protocol):
    name: str

    def recognize(self, image_path: Path, *, numeric: bool = False) -> OCRResult: ...


def normalized_bbox(left: int, top: int, width: int, height: int, image_width: int, image_height: int) -> list[float]:
    """Return Apple Vision-style normalized coordinates with a lower-left origin."""
    return [
        left / image_width,
        1.0 - ((top + height) / image_height),
        width / image_width,
        height / image_height,
    ]


class TesseractOCRProvider:
    name = "tesseract-5-rus-eng"

    def __init__(self, page_segmentation_mode: int = 11) -> None:
        self.page_segmentation_mode = page_segmentation_mode

    def recognize(self, image_path: Path, *, numeric: bool = False) -> OCRResult:
        with Image.open(image_path) as image:
            image_width, image_height = image.size
        command = [
            "tesseract",
            str(image_path),
            "stdout",
            "-l",
            "rus+eng",
            "--oem",
            "1",
            "--psm",
            str(self.page_segmentation_mode),
        ]
        if numeric:
            command.extend(
                [
                    "-c",
                    "tessedit_char_whitelist=0123456789.,-",
                ]
            )
        command.append("tsv")
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        observations = []
        for row in csv.DictReader(completed.stdout.splitlines(), delimiter="\t"):
            text = (row.get("text") or "").strip()
            if not text:
                continue
            try:
                confidence = max(0.0, float(row["conf"]) / 100.0)
                left = int(row["left"])
                top = int(row["top"])
                width = int(row["width"])
                height = int(row["height"])
            except (KeyError, TypeError, ValueError):
                continue
            observations.append(
                OCRObservation(
                    text=text,
                    confidence=confidence,
                    bboxNormalized=normalized_bbox(
                        left, top, width, height, image_width, image_height
                    ),
                    bboxPixels=[left, top, width, height],
                    alternatives=[OCRCandidate(text=text, confidence=confidence)],
                )
            )
        return OCRResult(
            image=str(image_path),
            width=image_width,
            height=image_height,
            recognitionLanguages=["ru", "en"],
            provider=self.name,
            observations=observations,
        )


class PaddleOCRProvider:
    name = "paddleocr-3.7.0-ru"

    def __init__(self) -> None:
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR  # imported lazily for the lightweight CLI

        self.engine = PaddleOCR(
            lang="ru",
            device=os.environ.get("PADDLE_DEVICE", "cpu"),
            text_detection_model_name="PP-OCRv5_mobile_det",
            text_recognition_model_name="eslav_PP-OCRv5_mobile_rec",
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,
            cpu_threads=int(os.environ.get("PADDLE_CPU_THREADS", "2")),
        )

    @staticmethod
    def _result_payload(result: Any) -> dict[str, Any]:
        value = getattr(result, "json", result)
        if callable(value):
            value = value()
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise TypeError(f"Unsupported PaddleOCR result type: {type(value)!r}")
        return value.get("res", value)

    def recognize(self, image_path: Path, *, numeric: bool = False) -> OCRResult:
        with Image.open(image_path) as image:
            image_width, image_height = image.size
        observations = []
        for result in self.engine.predict(input=str(image_path)):
            payload = self._result_payload(result)
            texts = payload.get("rec_texts", [])
            scores = payload.get("rec_scores", [])
            boxes = payload.get("rec_boxes", [])
            polygons = payload.get("dt_polys", [])
            for index, text in enumerate(texts):
                text = str(text).strip()
                if not text:
                    continue
                confidence = float(scores[index]) if index < len(scores) else 0.0
                if index < len(boxes):
                    left, top, right, bottom = [int(round(float(value))) for value in boxes[index]]
                elif index < len(polygons):
                    points = polygons[index]
                    xs = [float(point[0]) for point in points]
                    ys = [float(point[1]) for point in points]
                    left, top = int(round(min(xs))), int(round(min(ys)))
                    right, bottom = int(round(max(xs))), int(round(max(ys)))
                else:
                    left, top, right, bottom = 0, 0, image_width, image_height
                width = max(1, right - left)
                height = max(1, bottom - top)
                observations.append(
                    OCRObservation(
                        text=text,
                        confidence=confidence,
                        bboxNormalized=normalized_bbox(
                            left, top, width, height, image_width, image_height
                        ),
                        bboxPixels=[left, top, width, height],
                        alternatives=[OCRCandidate(text=text, confidence=confidence)],
                    )
                )
        return OCRResult(
            image=str(image_path),
            width=image_width,
            height=image_height,
            recognitionLanguages=["ru"],
            provider=self.name,
            observations=observations,
        )


def provider_by_name(name: str) -> OCRProvider:
    if name == "tesseract":
        return TesseractOCRProvider()
    if name == "paddle":
        return PaddleOCRProvider()
    raise ValueError(f"Unknown OCR provider: {name}")


def write_result(result: OCRResult, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True, choices=("paddle", "tesseract"))
    parser.add_argument("--image", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--numeric", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    provider = provider_by_name(args.provider)
    if args.warmup:
        print(json.dumps({"provider": provider.name, "status": "READY"}))
        return 0
    if not args.image or not args.output:
        raise SystemExit("--image and --output are required unless --warmup is used")
    result = provider.recognize(args.image, numeric=args.numeric)
    write_result(result, args.output)
    print(
        json.dumps(
            {"provider": result.provider, "observations": len(result.observations)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
