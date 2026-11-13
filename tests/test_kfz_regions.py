import unittest

from financial_verification.kfz_regions import find_anchor
from financial_verification.report_ocr_pipeline import normalize_number


def observation(text: str, left: int, top: int, width: int = 40, height: int = 18) -> dict:
    return {
        "text": text,
        "confidence": 0.9,
        "bboxPixels": [left, top, width, height],
        "bboxNormalized": [],
        "alternatives": [],
    }


class KfzRegionTests(unittest.TestCase):
    def test_find_anchor_rebuilds_fragmented_tesseract_line(self) -> None:
        observations = [
            observation("3", 10, 100, 10),
            observation("этап,", 25, 104, 50),
            observation("2026", 82, 100, 48),
            observation("г.:", 137, 104, 22),
            observation("34,14", 220, 103, 55),
            observation("ВСЕГО", 10, 140, 70),
        ]

        anchor = find_anchor(observations, "3 этап, 2026 г.")

        self.assertIn("3 этап, 2026 г.:", anchor["text"])
        self.assertEqual(anchor["composedFrom"], 5)
        self.assertEqual(anchor["bboxPixels"], [10, 100, 265, 22])

    def test_number_normalization_does_not_change_digits(self) -> None:
        self.assertEqual(normalize_number("5 558 474,83"), "5558474.83")
        self.assertEqual(normalize_number("1.145.798,10"), "1145798.10")
        self.assertIsNone(normalize_number("нет данных"))


if __name__ == "__main__":
    unittest.main()
