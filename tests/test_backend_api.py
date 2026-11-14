from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from financial_verification.backend.api import create_app
from financial_verification.backend.config import ApiSettings
from financial_verification.backend.pipeline_adapter import (
    LocalPipelineRunner,
    PipelineExecutionError,
    PipelineOutput,
)
from financial_verification.backend.service import VerificationJobManager, utc_now


PROJECT_DIR = Path(__file__).resolve().parents[1]


def api_context() -> dict:
    return {
        "report_file": "report.pdf",
        "target_row_label": "3 этап, 2026 г.",
        "contract_number": "24/03/15-01",
        "period_from": "2026-01-01",
        "period_to": "2026-05-31",
        "case_id": "api-test",
    }


def fixture_output(workspace: Path) -> PipelineOutput:
    report = {
        "document_id": "report-id",
        "file_name": "report.pdf",
        "extension": ".pdf",
        "size_bytes": 6,
        "sha256": "report-sha",
        "role": "REPORT",
        "processing_status": "EXCLUDED_FROM_WORKING_SET",
        "decision": "EXCLUDE",
        "reason_code": "REPORT",
        "duplicate_of": None,
        "document_number": None,
        "contract_number": None,
        "document_date": None,
        "period_from": None,
        "period_to": None,
        "amount": None,
        "extraction_method": None,
        "evidence": {},
        "warnings": [],
    }
    source = {
        "document_id": "source-id",
        "file_name": "source.pdf",
        "extension": ".pdf",
        "size_bytes": 6,
        "sha256": "source-sha",
        "role": "SOURCE_DOCUMENT",
        "processing_status": "PROCESSED",
        "decision": "INCLUDE",
        "reason_code": "VALID",
        "duplicate_of": None,
        "document_number": "1",
        "contract_number": "24/03/15-01",
        "document_date": "2026-01-10",
        "period_from": "2026-01-10",
        "period_to": "2026-01-10",
        "amount": "90.00",
        "extraction_method": "ocr:tesseract",
        "evidence": {
            "amount": {
                "source": "source.pdf",
                "locator": "page:1",
                "raw": "Сумма: 90,00",
                "bbox": [10, 20, 30, 10],
            }
        },
        "warnings": [],
    }
    registry = {
        "summary": {
            "total_files": 2,
            "source_documents": 1,
            "included": 1,
            "excluded": 1,
            "review": 0,
            "technical_errors": 0,
        },
        "records": [report, source],
    }
    report_fields = {
        "wages_total": {
            "field_id": "wages_total",
            "field_name": "Оплата труда",
            "value": "100.00",
            "evidence": {"raw": "100,00"},
        },
        "row_total": {
            "field_id": "row_total",
            "field_name": "Итого",
            "value": "100.00",
            "evidence": {"raw": "100,00"},
        },
    }
    article = {
        "article_code": "0200",
        "article_name": "Оплата труда",
        "report_field_id": "wages_total",
        "report_value": "100.00",
        "supported_value": "90.00",
        "supported_value_is_partial": False,
        "delta_report_minus_supported": "10.00",
        "status": "MISMATCH",
        "reason_code": "REPORT_OVERSTATED",
        "direction": "OVERSTATEMENT",
        "source_count": 1,
        "sources": [
            {
                "operation_id": "source-id:1",
                "document_id": "source-id",
                "file_name": "source.pdf",
                "amount": "90.00",
                "amount_evidence": source["evidence"]["amount"],
                "classification_evidence": [],
            }
        ],
        "report_evidence": {"raw": "100,00"},
        "formula": "90.00",
    }
    reconciliation = {
        "case_id": "api-test",
        "target_row_label": "3 этап, 2026 г.",
        "rule_set_version": "test",
        "registry_summary": registry["summary"],
        "report_extraction": {
            "provider": "stub",
            "fields": report_fields,
        },
        "operations": [],
        "excluded_source_documents": [],
        "reconciliation": {
            "articles": [article],
            "total": {
                "report_field_id": "row_total",
                "report_value": "100.00",
                "supported_value": "90.00",
                "supported_value_is_partial": False,
                "delta_report_minus_supported": "10.00",
                "status": "MISMATCH",
                "reason_code": "REPORT_OVERSTATED",
                "direction": "OVERSTATEMENT",
                "source_count": 1,
                "source_operation_ids": ["source-id:1"],
                "unclassified_operation_ids": [],
                "report_evidence": {"raw": "100,00"},
                "formula": "90.00",
            },
            "report_consistency": {
                "article_sum": "100.00",
                "row_total": "100.00",
                "delta_row_total_minus_article_sum": "0.00",
                "status": "CORRECT",
                "reason_code": "REPORT_TOTAL_CONSISTENT",
            },
            "verified_details": [],
            "unverified_details": [],
            "conclusion": {
                "status": "REPORT_UNRELIABLE",
                "reason_code": "CONFIRMED_FINANCIAL_DISCREPANCY",
                "classified_operations": 1,
                "review_operations": 0,
                "article_mismatches": 1,
                "insufficient_articles": 0,
                "detail_mismatches": 0,
                "checked_articles": 1,
                "unverified_detail_fields": 0,
            },
        },
    }
    extraction = {
        "fields": [
            {
                "field_id": "wages_total",
                "raw": "100,00",
                "normalized": "100.00",
                "bbox": [10, 10, 30, 20],
                "observations": [{"confidence": 0.99}],
            },
            {
                "field_id": "row_total",
                "raw": "100,00",
                "normalized": "100.00",
                "bbox": [10, 30, 30, 40],
                "observations": [{"confidence": 0.99}],
            },
        ]
    }
    return PipelineOutput(
        registry=registry,
        reconciliation=reconciliation,
        extraction=extraction,
        artifacts_dir=workspace / "artifacts",
    )


class StubRunner:
    def run(self, *, workspace, context, rules_path, progress):
        progress("STUB", 50, "Stub pipeline")
        return fixture_output(workspace)


class FailingRunner:
    def run(self, *, workspace, context, rules_path, progress):
        progress("STUB", 50, "Stub failure")
        raise PipelineExecutionError("STUB", "controlled failure")


class BackendApiTest(unittest.TestCase):
    def make_settings(self, root: Path, *, persistent: bool = False, ttl: int = 60):
        config = root / "config"
        config.mkdir(parents=True, exist_ok=True)
        shutil.copy(
            PROJECT_DIR / "config/case19-verification-context.json",
            config / "case19-verification-context.json",
        )
        shutil.copy(
            PROJECT_DIR / "config/cost-article-rules.json",
            config / "cost-article-rules.json",
        )
        return ApiSettings(
            storage_mode="persistent" if persistent else "ephemeral",
            temporary_root=root / "temporary",
            storage_root=root / "storage",
            config_dir=config,
            result_ttl_seconds=ttl,
            retention_days=30,
            max_files=10,
            max_file_size_bytes=1024,
            max_total_size_bytes=4096,
            job_workers=1,
            pipeline_stage_timeout_seconds=600,
        )

    def create_job(self, client: TestClient) -> str:
        response = client.post(
            "/api/v1/verifications",
            data={"context": json.dumps(api_context(), ensure_ascii=False)},
            files=[
                ("files", ("report.pdf", b"report", "application/pdf")),
                ("files", ("source.pdf", b"source", "application/pdf")),
            ],
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["verification_id"]

    def wait(self, client: TestClient, verification_id: str) -> dict:
        for _ in range(100):
            response = client.get(f"/api/v1/verifications/{verification_id}")
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            if payload["status"] in {"COMPLETED", "FAILED"}:
                return payload
            time.sleep(0.01)
        self.fail("Verification did not finish")

    def test_ephemeral_verification_result_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(Path(directory))
            manager = VerificationJobManager(settings, runner=StubRunner())
            app = create_app(settings, manager)
            with TestClient(app) as client:
                capabilities = client.get("/api/v1/capabilities").json()
                self.assertEqual(capabilities["storage"]["mode"], "EPHEMERAL")
                verification_id = self.create_job(client)
                self.assertEqual(self.wait(client, verification_id)["status"], "COMPLETED")

                result = client.get(
                    f"/api/v1/verifications/{verification_id}/result"
                ).json()
                self.assertEqual(result["summary"]["delta"], "10.00")
                self.assertEqual(len(result["findings"]), 1)
                self.assertTrue(result["evidence"])
                self.assertTrue(
                    any(
                        item["category"] == "FINANCIAL_MISMATCH"
                        for item in result["annotations"]
                    )
                )
                self.assertFalse(settings.temporary_root.joinpath(verification_id).exists())

                content = client.get(
                    f"/api/v1/verifications/{verification_id}/documents/source-id/content"
                )
                self.assertEqual(content.status_code, 410)
                markdown = client.get(
                    f"/api/v1/verifications/{verification_id}/export?format=markdown"
                )
                self.assertEqual(markdown.status_code, 200)
                self.assertIn("Обнаруженные расхождения", markdown.text)
                events = client.get(
                    f"/api/v1/verifications/{verification_id}/events"
                )
                self.assertEqual(events.status_code, 200)
                self.assertIn("event: completed", events.text)
                openapi = client.get("/openapi.json")
                self.assertEqual(openapi.status_code, 200)
                self.assertIn(
                    "/api/v1/verifications",
                    openapi.json()["paths"],
                )

    def test_ephemeral_failed_job_also_cleans_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(Path(directory))
            manager = VerificationJobManager(settings, runner=FailingRunner())
            with TestClient(create_app(settings, manager)) as client:
                verification_id = self.create_job(client)
                status_payload = self.wait(client, verification_id)
                self.assertEqual(status_payload["status"], "FAILED")
                self.assertEqual(status_payload["error"]["code"], "PIPELINE_STAGE_FAILED")
                self.assertFalse(settings.temporary_root.joinpath(verification_id).exists())

    def test_ephemeral_startup_removes_orphaned_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(Path(directory))
            orphan = settings.temporary_root / "orphaned-run" / "input"
            orphan.mkdir(parents=True)
            orphan.joinpath("confidential.pdf").write_bytes(b"secret")
            manager = VerificationJobManager(settings, runner=StubRunner())
            try:
                self.assertEqual(list(settings.temporary_root.iterdir()), [])
            finally:
                manager.shutdown()

    def test_pipeline_timeout_is_reported_as_controlled_stage_error(self) -> None:
        runner = LocalPipelineRunner(stage_timeout_seconds=1)
        with mock.patch(
            "financial_verification.backend.pipeline_adapter.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ocr", timeout=1),
        ):
            with self.assertRaises(PipelineExecutionError) as raised:
                runner._execute("REPORT_OCR", ["unused"])
        self.assertEqual(raised.exception.stage, "REPORT_OCR")
        self.assertIn("exceeded 1 seconds", str(raised.exception))

    def test_expired_ephemeral_result_returns_410(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(Path(directory))
            manager = VerificationJobManager(settings, runner=StubRunner())
            with TestClient(create_app(settings, manager)) as client:
                verification_id = self.create_job(client)
                self.wait(client, verification_id)
                manager.get(verification_id).expires_at = utc_now() - timedelta(seconds=1)
                response = client.get(f"/api/v1/verifications/{verification_id}")
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response.json()["detail"]["code"], "VERIFICATION_EXPIRED")

    def test_persistent_result_survives_manager_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.make_settings(root, persistent=True)
            manager = VerificationJobManager(settings, runner=StubRunner())
            with TestClient(create_app(settings, manager)) as client:
                verification_id = self.create_job(client)
                self.wait(client, verification_id)
                content = client.get(
                    f"/api/v1/verifications/{verification_id}/documents/source-id/content"
                )
                self.assertEqual(content.status_code, 200)
                self.assertEqual(content.content, b"source")

            restored_manager = VerificationJobManager(settings, runner=StubRunner())
            with TestClient(create_app(settings, restored_manager)) as restored:
                status_response = restored.get(
                    f"/api/v1/verifications/{verification_id}"
                )
                self.assertEqual(status_response.status_code, 200)
                self.assertEqual(status_response.json()["status"], "COMPLETED")
                result = restored.get(
                    f"/api/v1/verifications/{verification_id}/result"
                )
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["summary"]["delta"], "10.00")
                rerun = restored.post(
                    f"/api/v1/verifications/{verification_id}/rerun"
                )
                self.assertEqual(rerun.status_code, 202, rerun.text)
                rerun_id = rerun.json()["verification_id"]
                self.assertEqual(self.wait(restored, rerun_id)["status"], "COMPLETED")
                rerun_result = restored.get(
                    f"/api/v1/verifications/{rerun_id}/result"
                )
                self.assertEqual(rerun_result.status_code, 200)
                self.assertEqual(rerun_result.json()["summary"]["delta"], "10.00")
                rerun_deleted = restored.delete(
                    f"/api/v1/verifications/{rerun_id}"
                )
                self.assertEqual(rerun_deleted.status_code, 204)
                deleted = restored.delete(f"/api/v1/verifications/{verification_id}")
                self.assertEqual(deleted.status_code, 204)
                self.assertFalse(settings.storage_root.joinpath(verification_id).exists())

    def test_persistent_retention_removes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.make_settings(root, persistent=True)
            manager = VerificationJobManager(settings, runner=StubRunner())
            with TestClient(create_app(settings, manager)) as client:
                verification_id = self.create_job(client)
                self.wait(client, verification_id)
                manager.get(verification_id).expires_at = utc_now() - timedelta(seconds=1)
                response = client.get(f"/api/v1/verifications/{verification_id}")
                self.assertEqual(response.status_code, 410)
                self.assertFalse(
                    settings.storage_root.joinpath(verification_id).exists()
                )

    def test_extract_tool_accepts_text_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(Path(directory))
            manager = VerificationJobManager(settings, runner=StubRunner())
            with TestClient(create_app(settings, manager)) as client:
                response = client.post(
                    "/api/v1/tools/extract",
                    files=[
                        (
                            "file",
                            (
                                "act.txt",
                                "Акт № 7 от 10.01.2026\nДоговор 24/03/15-01\nИтого 90,00",
                                "text/plain",
                            ),
                        )
                    ],
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["fields"]["amount"], "90.00")

    def test_annotation_tool_separates_extraction_and_business_problems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(Path(directory))
            manager = VerificationJobManager(settings, runner=StubRunner())
            with TestClient(create_app(settings, manager)) as client:
                response = client.post(
                    "/api/v1/tools/annotate",
                    json={
                        "fields": [
                            {
                                "field_id": "amount",
                                "text": "1O0,00",
                                "field_type": "money",
                                "confidence": 0.4,
                            },
                            {
                                "field_id": "contract",
                                "text": "24/03/16-02",
                                "field_type": "contract",
                                "expected_value": "24/03/15-01",
                            },
                        ]
                    },
                )
                self.assertEqual(response.status_code, 200)
                categories = {
                    item["category"] for item in response.json()["annotations"]
                }
                self.assertEqual(
                    categories, {"EXTRACTION_WARNING", "BUSINESS_MISMATCH"}
                )

    def test_upload_validation_rejects_unsafe_and_unsupported_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.make_settings(Path(directory))
            manager = VerificationJobManager(settings, runner=StubRunner())
            with TestClient(create_app(settings, manager)) as client:
                unsafe = client.post(
                    "/api/v1/verifications",
                    data={"context": json.dumps(api_context())},
                    files=[("files", ("../report.pdf", b"report", "application/pdf"))],
                )
                self.assertEqual(unsafe.status_code, 422)
                unsupported = client.post(
                    "/api/v1/verifications",
                    data={"context": json.dumps(api_context())},
                    files=[("files", ("report.exe", b"report", "application/octet-stream"))],
                )
                self.assertEqual(unsupported.status_code, 415)


if __name__ == "__main__":
    unittest.main()
