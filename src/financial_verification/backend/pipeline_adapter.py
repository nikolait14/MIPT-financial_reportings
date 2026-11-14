from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


ProgressCallback = Callable[[str, int, str], None]


@dataclass
class PipelineOutput:
    registry: dict[str, Any]
    reconciliation: dict[str, Any]
    extraction: dict[str, Any]
    artifacts_dir: Path


class VerificationRunner(Protocol):
    def run(
        self,
        *,
        workspace: Path,
        context: dict[str, Any],
        rules_path: Path,
        progress: ProgressCallback,
    ) -> PipelineOutput: ...


class PipelineExecutionError(RuntimeError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


class LocalPipelineRunner:
    def __init__(self, stage_timeout_seconds: int = 600) -> None:
        self.stage_timeout_seconds = stage_timeout_seconds

    def _execute(self, stage: str, arguments: list[str]) -> None:
        try:
            completed = subprocess.run(
                [sys.executable, "-m", *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.stage_timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise PipelineExecutionError(
                stage,
                f"Pipeline stage exceeded {self.stage_timeout_seconds} seconds",
            ) from error
        if completed.returncode == 0:
            return
        diagnostic = (completed.stderr or completed.stdout or "unknown error").strip()
        raise PipelineExecutionError(stage, diagnostic[-4000:])

    def run(
        self,
        *,
        workspace: Path,
        context: dict[str, Any],
        rules_path: Path,
        progress: ProgressCallback,
    ) -> PipelineOutput:
        input_dir = workspace / "input"
        artifacts_dir = workspace / "artifacts"
        context_path = workspace / "context.json"
        context_path.write_text(
            json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report_path = input_dir / context["report_file"]
        if not report_path.exists():
            raise PipelineExecutionError(
                "VALIDATION", f"Configured report does not exist: {context['report_file']}"
            )

        source_ocr_dir = artifacts_dir / "case19-registry" / "ocr"
        registry_dir = artifacts_dir / "case19-registry" / "results"
        report_ocr_dir = artifacts_dir / "linux-report-ocr"
        reconciliation_dir = artifacts_dir / "case19-reconciliation"

        progress("SOURCE_OCR", 10, "Распознаются первичные PDF и изображения")
        self._execute(
            "SOURCE_OCR",
            [
                "financial_verification.source_ocr_pipeline",
                "--input-dir",
                str(input_dir),
                "--context",
                str(context_path),
                "--output-dir",
                str(source_ocr_dir),
                "--provider",
                "tesseract",
            ],
        )

        progress("REPORT_OCR", 35, "Извлекается проверяемая строка КФЗ")
        self._execute(
            "REPORT_OCR",
            [
                "financial_verification.report_ocr_pipeline",
                "--input-pdf",
                str(report_path),
                "--context",
                str(context_path),
                "--output-dir",
                str(report_ocr_dir),
            ],
        )

        progress("DOCUMENT_REGISTRY", 60, "Проверяются документы, дубли и реквизиты")
        self._execute(
            "DOCUMENT_REGISTRY",
            [
                "financial_verification.document_registry",
                "--input-dir",
                str(input_dir),
                "--context",
                str(context_path),
                "--ocr-dir",
                str(source_ocr_dir),
                "--output-dir",
                str(registry_dir),
            ],
        )

        progress("RECONCILIATION", 80, "Классифицируются и сверяются операции")
        self._execute(
            "RECONCILIATION",
            [
                "financial_verification.reconciliation",
                "--registry",
                str(registry_dir / "document-registry.json"),
                "--input-dir",
                str(input_dir),
                "--ocr-dir",
                str(source_ocr_dir),
                "--context",
                str(context_path),
                "--rules",
                str(rules_path),
                "--report-ocr",
                str(report_ocr_dir / "report-ocr.json"),
                "--output-dir",
                str(reconciliation_dir),
            ],
        )

        progress("RESULT", 95, "Формируются доказательства и итоговый результат")
        try:
            registry = json.loads(
                (registry_dir / "document-registry.json").read_text(encoding="utf-8")
            )
            reconciliation = json.loads(
                (reconciliation_dir / "reconciliation.json").read_text(
                    encoding="utf-8"
                )
            )
            extraction = json.loads(
                (report_ocr_dir / "extraction.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise PipelineExecutionError("RESULT", str(error)) from error
        return PipelineOutput(
            registry=registry,
            reconciliation=reconciliation,
            extraction=extraction,
            artifacts_dir=artifacts_dir,
        )
