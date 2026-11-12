from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from financial_verification.document_registry import Evidence, ExtractedContent
from financial_verification.reconciliation import (
    classify_content,
    extract_report_row,
    run,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_DIR / "artifacts/case19-registry/results/document-registry.json"
REPORT_OCR_PATH = PROJECT_DIR / "artifacts/kfz-row-extraction/300dpi/region-run-1.json"
RULES_PATH = PROJECT_DIR / "config/cost-article-rules.json"
CONTEXT_PATH = PROJECT_DIR / "config/case19-verification-context.json"


def content_for(text: str) -> ExtractedContent:
    lines = text.splitlines()
    return ExtractedContent(
        text=text,
        lines=lines,
        evidence_by_line=[
            Evidence("synthetic.txt", f"line:{index}", line)
            for index, line in enumerate(lines, 1)
        ],
    )


class ClassificationUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))

    def test_classification_uses_content(self) -> None:
        result = classify_content(
            content_for(
                "Справка без специального имени\n"
                "Административное сопровождение и ведение документооборота"
            ),
            self.rules,
        )
        self.assertEqual(result.status, "CLASSIFIED")
        self.assertEqual(result.article_code, "0900")
        self.assertTrue(result.evidence)

    def test_unknown_operation_is_sent_to_review(self) -> None:
        result = classify_content(content_for("Поставка неопределенного назначения"), self.rules)
        self.assertEqual(result.status, "REVIEW")
        self.assertEqual(result.reason_code, "NO_CLASSIFICATION_SIGNAL")
        self.assertIsNone(result.article_code)

    def test_equal_scores_are_ambiguous(self) -> None:
        rules = {
            "version": "test",
            "minimum_score": 1,
            "articles": {
                "A": {"name": "A", "signals": [{"id": "a", "pattern": "общий", "weight": 1}]},
                "B": {"name": "B", "signals": [{"id": "b", "pattern": "общий", "weight": 1}]},
            },
        }
        result = classify_content(content_for("Общий расход"), rules)
        self.assertEqual(result.status, "REVIEW")
        self.assertEqual(result.reason_code, "AMBIGUOUS_CLASSIFICATION")

    def test_report_parser_uses_raw_ocr_not_expected_values(self) -> None:
        payload = {
            "height": 200,
            "observations": [
                {"text": "10,00", "confidence": 1, "bboxPixels": [10, 20, 20, 20]},
                {"text": "20,00", "confidence": 1, "bboxPixels": [10, 120, 20, 20]},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "raw-ocr.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            fields = extract_report_row(path, ["first", "second"])
        self.assertEqual(fields["first"]["value"], "10.00")
        self.assertEqual(fields["second"]["value"], "20.00")


@unittest.skipUnless(
    REGISTRY_PATH.exists() and REPORT_OCR_PATH.exists(),
    "Run the registry and KФЗ OCR pipelines first",
)
class Case19ReconciliationIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = run(
            registry_path=REGISTRY_PATH,
            input_dir=PROJECT_DIR / "data/case-19/all",
            ocr_dir=PROJECT_DIR / "artifacts/case19-registry/ocr",
            context_path=CONTEXT_PATH,
            rules_path=RULES_PATH,
            report_ocr_path=REPORT_OCR_PATH,
        )
        cls.articles = {
            item["article_code"]: item
            for item in cls.payload["reconciliation"]["articles"]
        }

    def test_all_valid_operations_are_classified(self) -> None:
        self.assertEqual(len(self.payload["operations"]), 19)
        self.assertEqual(
            self.payload["reconciliation"]["conclusion"]["review_operations"], 0
        )
        counts = {
            code: sum(
                operation["classification"]["article_code"] == code
                for operation in self.payload["operations"]
            )
            for code in ("0200", "0300", "0800", "0900")
        }
        self.assertEqual(counts, {"0200": 5, "0300": 5, "0800": 4, "0900": 5})

    def test_article_totals_and_discrepancy(self) -> None:
        expected = {
            "0200": ("5558474.83", "5558474.83", "0.00", "CORRECT"),
            "0300": ("1671359.96", "1671359.96", "0.00", "CORRECT"),
            "0800": ("1145798.10", "1133298.10", "12500.00", "MISMATCH"),
            "0900": ("3204744.51", "3204744.51", "0.00", "CORRECT"),
        }
        for code, values in expected.items():
            item = self.articles[code]
            self.assertEqual(
                (
                    item["report_value"],
                    item["supported_value"],
                    item["delta_report_minus_supported"],
                    item["status"],
                ),
                values,
            )
        self.assertEqual(self.articles["0800"]["reason_code"], "REPORT_OVERSTATED")

    def test_total_and_conclusion(self) -> None:
        total = self.payload["reconciliation"]["total"]
        self.assertEqual(total["report_value"], "11580377.40")
        self.assertEqual(total["supported_value"], "11567877.40")
        self.assertEqual(total["delta_report_minus_supported"], "12500.00")
        self.assertEqual(total["reason_code"], "REPORT_OVERSTATED")
        self.assertEqual(
            self.payload["reconciliation"]["conclusion"]["status"],
            "REPORT_UNRELIABLE",
        )

    def test_filters_and_oracle_isolation(self) -> None:
        self.assertFalse(self.payload["oracle_used_by_working_pipeline"])
        names = {operation["file_name"] for operation in self.payload["operations"]}
        self.assertNotIn("!Правильные_ответы_и_разбор.xlsx", names)
        excluded = {
            item["file_name"]: item["reason_code"]
            for item in self.payload["excluded_source_documents"]
        }
        self.assertEqual(
            excluded,
            {
                "10_Акт_ADM-27_копия.pdf": "DUPLICATE_DOCUMENT",
                "14_Акт_ОРГ-0603.pdf": "OUT_OF_PERIOD",
                "15_Акт_ADM-75_15-05-2026.pdf": "WRONG_CONTRACT",
            },
        )

    def test_results_have_evidence_and_detail_abstentions(self) -> None:
        for item in self.payload["reconciliation"]["articles"]:
            self.assertTrue(item["report_evidence"])
            for source in item["sources"]:
                self.assertTrue(source["amount_evidence"])
                self.assertTrue(source["classification_evidence"])
        details = self.payload["reconciliation"]["unverified_details"]
        self.assertEqual(len(details), 9)
        self.assertTrue(all(item["status"] == "INSUFFICIENT_DATA" for item in details))
        self.assertTrue(all(item["reason_code"] == "INSUFFICIENT_DETAIL" for item in details))
        verified = {
            item["check_id"]: item
            for item in self.payload["reconciliation"]["verified_details"]
        }
        self.assertEqual(verified["base_wages"]["supported_value"], "5054470.26")
        self.assertEqual(verified["base_wages"]["status"], "CORRECT")
        combined = verified["additional_wages_and_leave_reserve"]
        self.assertEqual(combined["supported_value"], "504004.57")
        self.assertEqual(combined["report_value"], "504004.57")
        self.assertEqual(combined["status"], "CORRECT")


if __name__ == "__main__":
    unittest.main()
