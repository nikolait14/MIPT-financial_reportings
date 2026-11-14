from __future__ import annotations

import json
import tempfile
import unittest
from decimal import InvalidOperation
from pathlib import Path

from financial_verification.document_registry import Evidence, ExtractedContent
from financial_verification.reconciliation import (
    ClassificationResult,
    Operation,
    ReconciliationError,
    classify_content,
    extract_report_row,
    markdown_report,
    parse_decimal,
    reconcile,
    run,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
REGISTRY_PATH = PROJECT_DIR / "artifacts/case19-registry/results/document-registry.json"
LINUX_REPORT_OCR_PATH = PROJECT_DIR / "artifacts/linux-report-ocr/report-ocr.json"
LEGACY_REPORT_OCR_PATH = PROJECT_DIR / "artifacts/kfz-row-extraction/300dpi/region-run-1.json"
REPORT_OCR_PATH = (
    LINUX_REPORT_OCR_PATH
    if LINUX_REPORT_OCR_PATH.exists()
    else LEGACY_REPORT_OCR_PATH
)
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

    def test_decimal_parser_rejects_ocr_letters(self) -> None:
        for raw in ("З4,14", "1O0,00", "100 руб."):
            with self.subTest(raw=raw), self.assertRaises(InvalidOperation):
                parse_decimal(raw)

    def test_report_parser_rejects_plausible_number_with_ocr_letter(self) -> None:
        payload = {
            "height": 100,
            "observations": [
                {"text": "1O0,00", "confidence": 1, "bboxPixels": [10, 20, 30, 20]},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid-ocr.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ReconciliationError):
                extract_report_row(path, ["amount"])


ARTICLE_FIELDS = {
    "0200": "wages_total",
    "0300": "insurance_total",
    "0800": "overhead_0800",
    "0900": "administrative_0900",
}


def synthetic_context(*, detail_checks: list[dict] | None = None) -> dict:
    return {
        "article_report_fields": ARTICLE_FIELDS,
        "total_report_field": "row_total",
        "detail_aggregation_checks": detail_checks or [],
        "insufficient_detail_fields": [],
    }


def synthetic_rules() -> dict:
    return {
        "articles": {
            code: {"name": f"Article {code}"} for code in ARTICLE_FIELDS
        }
    }


def synthetic_report_fields(
    article_values: dict[str, str], total: str, extra: dict[str, str] | None = None
) -> dict:
    values = {
        ARTICLE_FIELDS[code]: value for code, value in article_values.items()
    }
    values["row_total"] = total
    values.update(extra or {})
    return {
        field_id: {
            "field_id": field_id,
            "field_name": field_id,
            "value": value,
            "evidence": {"source": "synthetic", "raw": value},
        }
        for field_id, value in values.items()
    }


def synthetic_operation(
    operation_id: str,
    amount: str | None,
    article_code: str | None,
    *,
    line_items: list[dict] | None = None,
    reason_code: str | None = None,
    normalization_status: str = "NORMALIZED",
    warnings: list[str] | None = None,
) -> Operation:
    classified = article_code is not None
    return Operation(
        operation_id=operation_id,
        document_id=operation_id,
        file_name=f"{operation_id}.pdf",
        document_number=None,
        contract_number=None,
        period_from=None,
        period_to=None,
        amount=amount,
        currency="RUB",
        amount_evidence=None,
        classification=ClassificationResult(
            status="CLASSIFIED" if classified else "REVIEW",
            reason_code=(
                "RULE_MATCH" if classified else reason_code or "NO_CLASSIFICATION_SIGNAL"
            ),
            article_code=article_code,
            article_name=f"Article {article_code}" if classified else None,
            confidence="HIGH" if classified else None,
            rule_set_version="test",
            score=10 if classified else 0,
            score_margin=10 if classified else 0,
            scores={},
        ),
        line_items=line_items or [],
        normalization_status=normalization_status,
        warnings=warnings or [],
    )


class ReconciliationEdgeCaseTest(unittest.TestCase):
    def test_no_documents_is_insufficient_data_not_financial_mismatch(self) -> None:
        fields = synthetic_report_fields(
            {code: "25.00" for code in ARTICLE_FIELDS}, "100.00"
        )

        result = reconcile([], fields, synthetic_context(), synthetic_rules())

        for article in result["articles"]:
            self.assertEqual(article["status"], "INSUFFICIENT_DATA")
            self.assertEqual(article["reason_code"], "MISSING_DOCUMENT")
            self.assertIsNone(article["supported_value"])
            self.assertIsNone(article["delta_report_minus_supported"])
        self.assertEqual(result["total"]["status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["total"]["reason_code"], "MISSING_DOCUMENT")
        self.assertIsNone(result["total"]["supported_value"])
        self.assertIsNone(result["total"]["delta_report_minus_supported"])
        self.assertEqual(result["conclusion"]["status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["conclusion"]["reason_code"], "MISSING_DOCUMENT")
        report = markdown_report(
            {"reconciliation": result, "excluded_source_documents": []}
        )
        self.assertNotIn("None", report)
        self.assertIn("INSUFFICIENT_DATA / MISSING_DOCUMENT", report)

    def test_review_operation_makes_total_partial_and_not_mismatch(self) -> None:
        fields = synthetic_report_fields(
            {code: "25.00" for code in ARTICLE_FIELDS}, "100.00"
        )
        operations = [
            synthetic_operation(f"op-{code}", "25.00", code)
            for code in ARTICLE_FIELDS
        ]
        operations.append(synthetic_operation("review", "50.00", None))

        result = reconcile(operations, fields, synthetic_context(), synthetic_rules())

        self.assertTrue(
            all(item["status"] == "INSUFFICIENT_DATA" for item in result["articles"])
        )
        total = result["total"]
        self.assertEqual(total["supported_value"], "100.00")
        self.assertTrue(total["supported_value_is_partial"])
        self.assertIsNone(total["delta_report_minus_supported"])
        self.assertEqual(total["status"], "INSUFFICIENT_DATA")
        self.assertEqual(total["reason_code"], "UNCLASSIFIED_OPERATIONS_PRESENT")
        self.assertEqual(result["conclusion"]["status"], "INSUFFICIENT_DATA")

    def test_one_missing_article_makes_total_partial_without_delta(self) -> None:
        fields = synthetic_report_fields(
            {code: "25.00" for code in ARTICLE_FIELDS}, "100.00"
        )
        operations = [
            synthetic_operation(f"op-{code}", "25.00", code)
            for code in ("0200", "0300", "0800")
        ]

        result = reconcile(operations, fields, synthetic_context(), synthetic_rules())

        missing = next(
            item for item in result["articles"] if item["article_code"] == "0900"
        )
        self.assertEqual(missing["status"], "INSUFFICIENT_DATA")
        self.assertEqual(missing["reason_code"], "MISSING_DOCUMENT")
        total = result["total"]
        self.assertEqual(total["supported_value"], "75.00")
        self.assertTrue(total["supported_value_is_partial"])
        self.assertIsNone(total["delta_report_minus_supported"])
        self.assertEqual(total["status"], "INSUFFICIENT_DATA")
        self.assertEqual(total["reason_code"], "MISSING_DOCUMENT")
        self.assertEqual(result["conclusion"]["reason_code"], "MISSING_DOCUMENT")

    def test_technical_review_has_distinct_reason_and_is_visible_in_report(self) -> None:
        fields = synthetic_report_fields(
            {code: "25.00" for code in ARTICLE_FIELDS}, "100.00"
        )
        operations = [
            synthetic_operation(f"op-{code}", "25.00", code)
            for code in ARTICLE_FIELDS
        ]
        operations.append(
            synthetic_operation(
                "damaged",
                None,
                None,
                reason_code="EXTRACTION_FAILED",
                normalization_status="EXTRACTION_ERROR",
                warnings=["damaged PDF"],
            )
        )

        result = reconcile(operations, fields, synthetic_context(), synthetic_rules())
        payload = {
            "operations": [
                {
                    **operation.__dict__,
                    "classification": operation.classification.__dict__,
                }
                for operation in operations
            ],
            "reconciliation": result,
            "excluded_source_documents": [],
        }

        self.assertEqual(
            result["conclusion"]["reason_code"],
            "SOURCE_DOCUMENT_PROCESSING_INCOMPLETE",
        )
        self.assertEqual(
            result["total"]["reason_code"],
            "SOURCE_DOCUMENT_PROCESSING_INCOMPLETE",
        )
        report = markdown_report(payload)
        self.assertIn("damaged.pdf", report)
        self.assertIn("EXTRACTION_FAILED", report)
        self.assertIn("damaged PDF", report)

    def test_empty_detail_sources_are_insufficient_not_mismatch(self) -> None:
        fields = synthetic_report_fields(
            {code: "25.00" for code in ARTICLE_FIELDS},
            "100.00",
            {"detail_a": "10.00"},
        )
        operations = [
            synthetic_operation(f"op-{code}", "25.00", code)
            for code in ARTICLE_FIELDS
        ]
        context = synthetic_context(
            detail_checks=[
                {
                    "check_id": "detail",
                    "check_name": "Detail",
                    "source_line_item_codes": ["0201"],
                    "report_field_ids": ["detail_a"],
                }
            ]
        )

        result = reconcile(operations, fields, context, synthetic_rules())

        detail = result["verified_details"][0]
        self.assertEqual(detail["status"], "INSUFFICIENT_DATA")
        self.assertEqual(detail["reason_code"], "MISSING_DETAIL_SOURCE")
        self.assertIsNone(detail["supported_value"])
        self.assertIsNone(detail["delta_report_minus_supported"])

    def test_report_article_sum_is_checked_against_row_total(self) -> None:
        fields = synthetic_report_fields(
            {code: "25.00" for code in ARTICLE_FIELDS}, "99.00"
        )
        operations = [
            synthetic_operation(f"op-{code}", "25.00", code)
            for code in ARTICLE_FIELDS
        ]

        result = reconcile(operations, fields, synthetic_context(), synthetic_rules())

        consistency = result["report_consistency"]
        self.assertEqual(consistency["article_sum"], "100.00")
        self.assertEqual(consistency["row_total"], "99.00")
        self.assertEqual(consistency["delta_row_total_minus_article_sum"], "-1.00")
        self.assertEqual(consistency["status"], "MISMATCH")
        self.assertEqual(consistency["reason_code"], "REPORT_TOTAL_INCONSISTENT")
        self.assertEqual(result["conclusion"]["status"], "REPORT_UNRELIABLE")


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
