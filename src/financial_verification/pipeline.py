#!/usr/bin/env python3
"""Run the complete case verification workflow in one reproducible process."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--artifacts-dir", required=True, type=Path)
    return parser.parse_args()


def run_step(name: str, arguments: list[str]) -> dict[str, Any]:
    print(json.dumps({"step": name, "status": "RUNNING"}, ensure_ascii=False), flush=True)
    completed = subprocess.run([sys.executable, "-m", *arguments], check=False)
    result = {"step": name, "return_code": completed.returncode}
    print(
        json.dumps(
            {**result, "status": "PASS" if completed.returncode == 0 else "FAIL"},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result


def main() -> int:
    args = parse_args()
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)
    context = json.loads(args.context.read_text(encoding="utf-8"))
    report_pdf = args.input_dir / context["report_file"]
    if not report_pdf.exists():
        raise SystemExit(f"Configured report file does not exist: {report_pdf}")

    source_ocr_dir = args.artifacts_dir / "case19-registry" / "ocr"
    registry_dir = args.artifacts_dir / "case19-registry" / "results"
    report_ocr_dir = args.artifacts_dir / "linux-report-ocr"
    reconciliation_dir = args.artifacts_dir / "case19-reconciliation"

    steps = []
    steps.append(
        run_step(
            "source_ocr",
            [
                "financial_verification.source_ocr_pipeline",
                "--input-dir",
                str(args.input_dir),
                "--context",
                str(args.context),
                "--output-dir",
                str(source_ocr_dir),
                "--provider",
                "tesseract",
            ],
        )
    )
    steps.append(
        run_step(
            "report_ocr",
            [
                "financial_verification.report_ocr_pipeline",
                "--input-pdf",
                str(report_pdf),
                "--context",
                str(args.context),
                "--output-dir",
                str(report_ocr_dir),
            ],
        )
    )
    if steps[-1]["return_code"] != 0:
        return write_summary(args.artifacts_dir, steps, status="FAIL")

    steps.append(
        run_step(
            "document_registry",
            [
                "financial_verification.document_registry",
                "--input-dir",
                str(args.input_dir),
                "--context",
                str(args.context),
                "--ocr-dir",
                str(source_ocr_dir),
                "--output-dir",
                str(registry_dir),
            ],
        )
    )
    registry_path = registry_dir / "document-registry.json"
    if not registry_path.exists():
        return write_summary(args.artifacts_dir, steps, status="FAIL")

    steps.append(
        run_step(
            "reconciliation",
            [
                "financial_verification.reconciliation",
                "--registry",
                str(registry_path),
                "--input-dir",
                str(args.input_dir),
                "--ocr-dir",
                str(source_ocr_dir),
                "--context",
                str(args.context),
                "--rules",
                str(args.rules),
                "--report-ocr",
                str(report_ocr_dir / "report-ocr.json"),
                "--output-dir",
                str(reconciliation_dir),
            ],
        )
    )
    reconciliation_path = reconciliation_dir / "reconciliation.json"
    if steps[-1]["return_code"] != 0 or not reconciliation_path.exists():
        return write_summary(args.artifacts_dir, steps, status="FAIL")

    payload = json.loads(reconciliation_path.read_text(encoding="utf-8"))
    return write_summary(
        args.artifacts_dir,
        steps,
        status="PASS",
        result={
            "conclusion": payload["reconciliation"]["conclusion"],
            "reconciliation": str(reconciliation_path),
            "human_report": str(reconciliation_dir / "reconciliation.md"),
        },
    )


def write_summary(
    artifacts_dir: Path,
    steps: list[dict[str, Any]],
    *,
    status: str,
    result: dict[str, Any] | None = None,
) -> int:
    payload = {"status": status, "steps": steps, "result": result}
    (artifacts_dir / "pipeline-summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
