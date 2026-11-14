from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from financial_verification.document_registry import (
    build_base_records,
    extract_ocr,
    mark_duplicates,
    process_records,
)
from financial_verification.source_ocr_pipeline import cached_result_is_current


PROJECT_DIR = Path(__file__).resolve().parents[1]
RESULT_PATH = PROJECT_DIR / "artifacts/case19-registry/results/document-registry.json"


class DocumentRegistryUnitTest(unittest.TestCase):
    def test_ocr_words_are_grouped_into_visual_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.pdf"
            source.write_bytes(b"synthetic source")
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            ocr_dir = root / "ocr"
            ocr_dir.mkdir()
            payload = {
                "provider": "tesseract",
                "sourceSha256": source_hash,
                "renderDpi": 300,
                "observations": [
                    {"text": "Договор", "bboxPixels": [10, 10, 50, 12]},
                    {"text": "24/03/15-01", "bboxPixels": [70, 11, 90, 12]},
                    {"text": "Сумма", "bboxPixels": [10, 40, 40, 12]},
                    {"text": "300 000,00", "bboxPixels": [60, 39, 85, 12]},
                ],
            }
            ocr_path = ocr_dir / "source.json"
            ocr_path.write_text(json.dumps(payload), encoding="utf-8")

            content = extract_ocr(source, ocr_dir)

            self.assertEqual(
                content.lines,
                ["Договор 24/03/15-01", "Сумма 300 000,00"],
            )

    def test_ocr_cache_is_bound_to_source_hash_provider_and_dpi(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "result.json"
            output.write_text(
                json.dumps(
                    {
                        "sourceSha256": "current",
                        "provider": "tesseract",
                        "renderDpi": 300,
                        "observations": [{"text": "ok"}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(
                cached_result_is_current(output, "current", "tesseract", 300)
            )
            self.assertFalse(
                cached_result_is_current(output, "changed", "tesseract", 300)
            )
            self.assertFalse(
                cached_result_is_current(output, "current", "paddle", 300)
            )
            self.assertFalse(
                cached_result_is_current(output, "current", "tesseract", 400)
            )

    def test_stale_ocr_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.pdf"
            source.write_bytes(b"new source")
            ocr_dir = root / "ocr"
            ocr_dir.mkdir()
            (ocr_dir / "source.json").write_text(
                json.dumps(
                    {
                        "sourceSha256": "old-hash",
                        "observations": [
                            {"text": "Сумма 1,00", "bboxPixels": [0, 0, 10, 10]}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Stale OCR result"):
                extract_ocr(source, ocr_dir)


@unittest.skipUnless(RESULT_PATH.exists(), "Run scripts/run_case19_registry.sh first")
class DocumentRegistryIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        cls.by_name = {record["file_name"]: record for record in cls.payload["records"]}

    def test_inventory_and_oracle_isolation(self) -> None:
        self.assertEqual(self.payload["summary"]["total_files"], 26)
        oracle = self.by_name["!Правильные_ответы_и_разбор.xlsx"]
        self.assertEqual(oracle["role"], "ORACLE")
        self.assertEqual(oracle["decision"], "EXCLUDE")
        self.assertEqual(oracle["reason_code"], "ORACLE")

    def test_exact_duplicate(self) -> None:
        original = self.by_name["09_Акт_ADM-27_27-02-2026.pdf"]
        duplicate = self.by_name["10_Акт_ADM-27_копия.pdf"]
        self.assertEqual(original["sha256"], duplicate["sha256"])
        self.assertNotEqual(original["document_id"], duplicate["document_id"])
        self.assertEqual(duplicate["duplicate_of"], original["document_id"])
        self.assertEqual(duplicate["reason_code"], "DUPLICATE_DOCUMENT")
        self.assertEqual(duplicate["amount"], original["amount"])
        self.assertEqual(duplicate["document_date"], original["document_date"])
        self.assertEqual(duplicate["contract_number"], original["contract_number"])

    def test_period_and_contract_filters(self) -> None:
        out_of_period = self.by_name["14_Акт_ОРГ-0603.pdf"]
        wrong_contract = self.by_name["15_Акт_ADM-75_15-05-2026.pdf"]
        self.assertEqual(out_of_period["reason_code"], "OUT_OF_PERIOD")
        self.assertEqual(out_of_period["document_date"], "2026-06-03")
        self.assertEqual(wrong_contract["reason_code"], "WRONG_CONTRACT")
        self.assertEqual(wrong_contract["contract_number"], "24/03/16-02")

    def test_expected_amounts(self) -> None:
        expected = {
            "03_01_Страховые_взносы_Январь_2026.xlsx": "333000.00",
            "03_02_Страховые_взносы_Февраль_2026.xlsx": "335100.00",
            "03_03_Страховые_взносы_Март_2026.xlsx": "330540.00",
            "03_04_Страховые_взносы_Апрель_2026.xlsx": "337225.00",
            "03_05_Страховые_взносы_Май_2026.xlsx": "335494.96",
            "09_Акт_ADM-27_27-02-2026.pdf": "487500.00",
            "14_Акт_ОРГ-0603.pdf": "165000.00",
            "15_Акт_ADM-75_15-05-2026.pdf": "98000.00",
        }
        for file_name, amount in expected.items():
            self.assertEqual(self.by_name[file_name]["amount"], amount)

    def test_no_technical_errors(self) -> None:
        self.assertEqual(self.payload["summary"]["technical_errors"], 0)
        self.assertEqual(self.payload["summary"]["review"], 0)

    def test_corrupted_file_does_not_stop_other_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "01_valid.txt").write_text(
                "Документ: TEST-1\nДоговор: 24/03/15-01\n"
                "Дата: 10.04.2026\nСумма: 1 000,00 руб.",
                encoding="utf-8",
            )
            (root / "02_corrupted.docx").write_bytes(b"not a zip file")
            context = {
                "report_file": "report.pdf",
                "target_row_label": "target",
                "contract_number": "24/03/15-01",
                "period_from": "2026-01-01",
                "period_to": "2026-05-31",
                "oracle_patterns": [],
                "metadata_prefixes": [],
            }
            records = build_base_records(root, context)
            mark_duplicates(records)
            process_records(root, root / "ocr", context, records)
            by_name = {record.file_name: record for record in records}

            self.assertEqual(by_name["01_valid.txt"].decision, "INCLUDE")
            self.assertEqual(by_name["01_valid.txt"].reason_code, "VALID")
            self.assertEqual(by_name["02_corrupted.docx"].decision, "REVIEW")
            self.assertEqual(
                by_name["02_corrupted.docx"].processing_status, "EXTRACTION_ERROR"
            )
            self.assertEqual(
                by_name["02_corrupted.docx"].reason_code, "EXTRACTION_FAILED"
            )


if __name__ == "__main__":
    unittest.main()
