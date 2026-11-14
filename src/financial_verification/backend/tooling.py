from __future__ import annotations

import json
import re
from dataclasses import asdict
from decimal import InvalidOperation
from pathlib import Path
from typing import Any

from financial_verification.document_registry import (
    extract_amount,
    extract_content,
    extract_contract,
    extract_date_or_period,
    extract_document_number,
)
from financial_verification.reconciliation import (
    ClassificationEvidence,
    ClassificationResult,
    Operation,
    parse_decimal,
    reconcile,
)
from financial_verification.source_ocr_pipeline import process_source_file


def extract_one_document(path: Path, workspace: Path) -> dict[str, Any]:
    ocr_dir = workspace / "ocr"
    if path.suffix.lower() in {".pdf", ".png"}:
        process_source_file(
            path, ocr_dir, provider_name="tesseract", dpi=300
        )
    content, method = extract_content(path, ocr_dir)
    document_number, number_evidence = extract_document_number(content, path.name)
    contract, contract_evidence = extract_contract(content)
    document_date, period_from, period_to, period_evidence = extract_date_or_period(
        content, path.name
    )
    amount, amount_evidence = extract_amount(content)
    return {
        "file_name": path.name,
        "extraction_method": method,
        "text": content.text,
        "lines": content.lines,
        "fields": {
            "document_number": document_number,
            "contract_number": contract,
            "document_date": document_date,
            "period_from": period_from,
            "period_to": period_to,
            "amount": f"{amount:.2f}" if amount is not None else None,
        },
        "evidence": {
            key: asdict(value)
            for key, value in {
                "document_number": number_evidence,
                "contract_number": contract_evidence,
                "date_or_period": period_evidence,
                "amount": amount_evidence,
            }.items()
            if value is not None
        },
    }


def annotate_fields(
    fields: list[dict[str, Any]], confidence_threshold: float
) -> list[dict[str, Any]]:
    annotations = []
    for field in fields:
        text = field["text"]
        target = {
            "type": "TEXT_SPAN",
            "start": 0,
            "end": len(text),
            "raw_text": text,
        }
        confidence = field.get("confidence")
        if confidence is not None and confidence < confidence_threshold:
            annotations.append(
                {
                    "annotation_id": f"{field['field_id']}:low-confidence",
                    "document_id": field.get("document_id", "inline"),
                    "category": "EXTRACTION_WARNING",
                    "severity": "WARNING",
                    "message": "Уверенность извлечения ниже заданного порога.",
                    "target": target,
                    "confidence": confidence,
                }
            )

        normalized: str | None = text.strip()
        if field["field_type"] == "money":
            try:
                normalized = f"{parse_decimal(text):.2f}"
            except InvalidOperation:
                normalized = None
                annotations.append(
                    {
                        "annotation_id": f"{field['field_id']}:invalid-money",
                        "document_id": field.get("document_id", "inline"),
                        "category": "EXTRACTION_WARNING",
                        "severity": "WARNING",
                        "message": "Значение не является надежно распознанной суммой.",
                        "target": target,
                    }
                )
        elif field["field_type"] == "contract":
            if not re.fullmatch(r"\d{2}/\d{2}/\d{2}-\d{2}", text.strip()):
                normalized = None
                annotations.append(
                    {
                        "annotation_id": f"{field['field_id']}:invalid-contract",
                        "document_id": field.get("document_id", "inline"),
                        "category": "EXTRACTION_WARNING",
                        "severity": "WARNING",
                        "message": "Номер договора не соответствует ожидаемому формату.",
                        "target": target,
                    }
                )

        expected = field.get("expected_value")
        if expected is not None and normalized is not None:
            normalized_expected = expected.strip()
            if field["field_type"] == "money":
                try:
                    normalized_expected = f"{parse_decimal(expected):.2f}"
                except InvalidOperation:
                    normalized_expected = expected.strip()
            if normalized != normalized_expected:
                annotations.append(
                    {
                        "annotation_id": f"{field['field_id']}:mismatch",
                        "document_id": field.get("document_id", "inline"),
                        "category": "BUSINESS_MISMATCH",
                        "severity": "ERROR",
                        "message": "Фактическое значение не совпадает с ожидаемым.",
                        "target": target,
                        "actual_value": normalized,
                        "expected_value": normalized_expected,
                    }
                )
    return annotations


def _classification_from_payload(payload: dict[str, Any]) -> ClassificationResult:
    return ClassificationResult(
        status=payload["status"],
        reason_code=payload.get("reason_code", "API_INPUT"),
        article_code=payload.get("article_code"),
        article_name=payload.get("article_name"),
        confidence=payload.get("confidence"),
        rule_set_version=payload.get("rule_set_version", "api"),
        score=int(payload.get("score", 0)),
        score_margin=int(payload.get("score_margin", 0)),
        scores=payload.get("scores", {}),
        evidence=[
            ClassificationEvidence(**item) for item in payload.get("evidence", [])
        ],
    )


def reconcile_payload(
    *,
    operations: list[dict[str, Any]],
    report_fields: dict[str, Any],
    context: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    domain_operations = [
        Operation(
            operation_id=item["operation_id"],
            document_id=item["document_id"],
            file_name=item["file_name"],
            document_number=item.get("document_number"),
            contract_number=item.get("contract_number"),
            period_from=item.get("period_from"),
            period_to=item.get("period_to"),
            amount=item.get("amount"),
            currency=item.get("currency", "RUB"),
            amount_evidence=item.get("amount_evidence"),
            classification=_classification_from_payload(item["classification"]),
            line_items=item.get("line_items", []),
            normalization_status=item.get("normalization_status", "NORMALIZED"),
            warnings=item.get("warnings", []),
        )
        for item in operations
    ]
    return reconcile(domain_operations, report_fields, context, rules)


def load_rules(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
