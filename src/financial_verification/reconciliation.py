#!/usr/bin/env python3
"""Classify normalized operations and reconcile them with a report row."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from financial_verification.document_registry import ExtractedContent, extract_content


MONEY_QUANTUM = Decimal("0.01")
FIELD_LABELS = {
    "labor_intensity": "Трудоемкость",
    "materials": "Материальные затраты",
    "wages_total": "Оплата труда, всего",
    "wages_without_reserve": "Оплата труда без резерва",
    "base_wages": "Основная заработная плата",
    "additional_wages": "Дополнительная заработная плата",
    "leave_reserve": "Резерв отпусков",
    "insurance_total": "Страховые взносы, всего",
    "insurance_without_reserve": "Взносы без резерва",
    "insurance_base_wages": "Взносы на основную заработную плату",
    "insurance_additional_wages": "Взносы на дополнительную заработную плату",
    "insurance_leave_reserve": "Взносы на резерв отпусков",
    "overhead_0800": "Общепроизводственные затраты",
    "administrative_0900": "Общехозяйственные затраты",
    "row_total": "Итого по строке",
}


@dataclass
class ClassificationEvidence:
    signal_id: str
    weight: int
    source: str
    locator: str
    raw: str
    bbox: list[int] | None = None


@dataclass
class ClassificationResult:
    status: str
    reason_code: str
    article_code: str | None
    article_name: str | None
    confidence: str | None
    rule_set_version: str
    score: int
    score_margin: int
    scores: dict[str, int]
    evidence: list[ClassificationEvidence] = field(default_factory=list)


@dataclass
class Operation:
    operation_id: str
    document_id: str
    file_name: str
    document_number: str | None
    contract_number: str | None
    period_from: str | None
    period_to: str | None
    amount: str | None
    currency: str
    amount_evidence: dict[str, Any] | None
    classification: ClassificationResult
    line_items: list[dict[str, Any]] = field(default_factory=list)
    normalization_status: str = "NORMALIZED"
    warnings: list[str] = field(default_factory=list)


class ReconciliationError(RuntimeError):
    """Raised when a required artifact is missing or structurally invalid."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--ocr-dir", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--report-ocr", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def decimal_string(value: Decimal) -> str:
    return f"{value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP):.2f}"


def parse_decimal(raw: str) -> Decimal:
    value = raw.strip().replace("\u00a0", " ")
    if not value or re.search(r"[^0-9\s,\.\-+]", value):
        raise InvalidOperation(f"Invalid characters in number {raw!r}")
    value = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)*", value):
        raise InvalidOperation(f"Invalid number format {raw!r}")
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    elif "," in value:
        value = value.replace(",", ".")
    elif value.count(".") > 1:
        parts = value.split(".")
        value = "".join(parts[:-1]) + "." + parts[-1]
    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def normalized_text(value: str) -> str:
    return value.lower().replace("ё", "е")


def signal_evidence(
    content: ExtractedContent, pattern: str, signal_id: str, weight: int
) -> ClassificationEvidence | None:
    compiled = re.compile(pattern, re.IGNORECASE)
    for line, evidence in zip(content.lines, content.evidence_by_line):
        if compiled.search(normalized_text(line)):
            return ClassificationEvidence(
                signal_id=signal_id,
                weight=weight,
                source=evidence.source,
                locator=evidence.locator,
                raw=evidence.raw,
                bbox=evidence.bbox,
            )
    if compiled.search(normalized_text(content.text)):
        match = compiled.search(normalized_text(content.text))
        assert match is not None
        start = max(0, match.start() - 80)
        end = min(len(content.text), match.end() + 80)
        return ClassificationEvidence(
            signal_id=signal_id,
            weight=weight,
            source="normalized_document_text",
            locator=f"characters:{start}-{end}",
            raw=content.text[start:end].replace("\n", " "),
        )
    return None


def classify_content(content: ExtractedContent, rules: dict[str, Any]) -> ClassificationResult:
    scores: dict[str, int] = {}
    evidence_by_article: dict[str, list[ClassificationEvidence]] = {}
    for article_code, article in rules["articles"].items():
        matches = []
        for signal in article["signals"]:
            evidence = signal_evidence(
                content, signal["pattern"], signal["id"], int(signal["weight"])
            )
            if evidence:
                matches.append(evidence)
        scores[article_code] = sum(item.weight for item in matches)
        evidence_by_article[article_code] = matches

    ranking = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_code, top_score = ranking[0]
    second_score = ranking[1][1] if len(ranking) > 1 else 0
    margin = top_score - second_score
    minimum = int(rules["minimum_score"])

    if top_score < minimum:
        return ClassificationResult(
            status="REVIEW",
            reason_code="NO_CLASSIFICATION_SIGNAL",
            article_code=None,
            article_name=None,
            confidence=None,
            rule_set_version=rules["version"],
            score=top_score,
            score_margin=margin,
            scores=scores,
        )
    if top_score == second_score:
        return ClassificationResult(
            status="REVIEW",
            reason_code="AMBIGUOUS_CLASSIFICATION",
            article_code=None,
            article_name=None,
            confidence=None,
            rule_set_version=rules["version"],
            score=top_score,
            score_margin=0,
            scores=scores,
        )

    confidence = "HIGH" if top_score >= 5 and margin >= 3 else "MEDIUM"
    return ClassificationResult(
        status="CLASSIFIED",
        reason_code="RULE_MATCH",
        article_code=top_code,
        article_name=rules["articles"][top_code]["name"],
        confidence=confidence,
        rule_set_version=rules["version"],
        score=top_score,
        score_margin=margin,
        scores=scores,
        evidence=evidence_by_article[top_code],
    )


def extract_line_items(content: ExtractedContent) -> list[dict[str, Any]]:
    """Extract explicit cost-code rows without inferring an unavailable split."""
    row_pattern = re.compile(
        r"\b(0\d{3})\b\s*\|\s*([^|]+?)\s*\|\s*([0-9]+(?:[.,][0-9]+)?)\s*(?:\||$)"
    )
    items = []
    for line, evidence in zip(content.lines, content.evidence_by_line):
        match = row_pattern.search(line)
        if not match:
            continue
        items.append(
            {
                "code": match.group(1),
                "description": match.group(2).strip(),
                "amount": decimal_string(parse_decimal(match.group(3))),
                "evidence": {
                    "source": evidence.source,
                    "locator": evidence.locator,
                    "raw": evidence.raw,
                    "bbox": evidence.bbox,
                },
            }
        )
    return items


def build_operations(
    registry: dict[str, Any],
    input_dir: Path,
    ocr_dir: Path,
    rules: dict[str, Any],
) -> list[Operation]:
    operations = []
    for record in registry["records"]:
        if record["role"] != "SOURCE_DOCUMENT":
            continue
        if record["decision"] == "REVIEW":
            operations.append(
                Operation(
                    operation_id=f"{record['document_id']}:review",
                    document_id=record["document_id"],
                    file_name=record["file_name"],
                    document_number=record["document_number"],
                    contract_number=record["contract_number"],
                    period_from=record["period_from"],
                    period_to=record["period_to"],
                    amount=record["amount"],
                    currency="RUB",
                    amount_evidence=record["evidence"].get("amount"),
                    classification=ClassificationResult(
                        status="REVIEW",
                        reason_code=record["reason_code"],
                        article_code=None,
                        article_name=None,
                        confidence=None,
                        rule_set_version=rules["version"],
                        score=0,
                        score_margin=0,
                        scores={},
                    ),
                    normalization_status=record["processing_status"],
                    warnings=record["warnings"],
                )
            )
            continue
        if record["decision"] != "INCLUDE":
            continue
        if record["amount"] is None:
            raise ReconciliationError(f"Included document has no amount: {record['file_name']}")
        amount = decimal_string(Decimal(record["amount"]))
        warnings = []
        try:
            content, _ = extract_content(input_dir / record["file_name"], ocr_dir)
            classification = classify_content(content, rules)
            line_items = extract_line_items(content)
            normalization_status = "NORMALIZED"
        except Exception as error:  # noqa: BLE001 - isolate a single document
            classification = ClassificationResult(
                status="REVIEW",
                reason_code="NORMALIZATION_FAILED",
                article_code=None,
                article_name=None,
                confidence=None,
                rule_set_version=rules["version"],
                score=0,
                score_margin=0,
                scores={},
            )
            normalization_status = "ERROR"
            line_items = []
            warnings.append(str(error))
        operations.append(
            Operation(
                operation_id=f"{record['document_id']}:1",
                document_id=record["document_id"],
                file_name=record["file_name"],
                document_number=record["document_number"],
                contract_number=record["contract_number"],
                period_from=record["period_from"],
                period_to=record["period_to"],
                amount=amount,
                currency="RUB",
                amount_evidence=record["evidence"].get("amount"),
                classification=classification,
                line_items=line_items,
                normalization_status=normalization_status,
                warnings=warnings,
            )
        )
    return operations


def observation_center(observation: dict[str, Any]) -> tuple[float, float]:
    left, top, width, height = observation["bboxPixels"]
    return left + width / 2, top + height / 2


def extract_report_row(report_ocr_path: Path, field_order: list[str]) -> dict[str, Any]:
    payload = json.loads(report_ocr_path.read_text(encoding="utf-8"))
    image_height = int(payload["height"])
    slot_height = image_height / len(field_order)
    fields: dict[str, Any] = {}

    for index, field_id in enumerate(field_order):
        top = index * slot_height
        bottom = (index + 1) * slot_height
        matches = []
        for observation in payload["observations"]:
            center_x, center_y = observation_center(observation)
            if top <= center_y < bottom:
                matches.append((center_x, observation))
        matches.sort(key=lambda item: item[0])
        raw = " ".join(item[1]["text"] for item in matches)
        if not raw:
            raise ReconciliationError(f"Report cell {index} ({field_id}) has no OCR value")
        try:
            value = parse_decimal(raw)
        except InvalidOperation as error:
            raise ReconciliationError(
                f"Report cell {index} ({field_id}) is not numeric: {raw!r}"
            ) from error
        fields[field_id] = {
            "field_id": field_id,
            "field_name": FIELD_LABELS.get(field_id, field_id),
            "value": decimal_string(value),
            "evidence": {
                "source": str(report_ocr_path),
                "locator": f"contact_sheet_slot:{index}",
                "raw": raw,
                "observations": [
                    {
                        "bbox": item[1]["bboxPixels"],
                        "confidence": item[1].get("confidence"),
                    }
                    for item in matches
                ],
            },
        }
    return fields


def comparison_status(report_value: Decimal, supported_value: Decimal) -> tuple[str, str, str]:
    delta = report_value - supported_value
    if delta == 0:
        return "CORRECT", "EXACT_MATCH", "NONE"
    if delta > 0:
        return "MISMATCH", "REPORT_OVERSTATED", "OVERSTATEMENT"
    return "MISMATCH", "REPORT_UNDERSTATED", "UNDERSTATEMENT"


def reconcile(
    operations: list[Operation],
    report_fields: dict[str, Any],
    context: dict[str, Any],
    rules: dict[str, Any],
) -> dict[str, Any]:
    classified = [op for op in operations if op.classification.status == "CLASSIFIED"]
    review = [op for op in operations if op.classification.status != "CLASSIFIED"]
    technical_review = any(
        op.classification.reason_code
        in {"EXTRACTION_FAILED", "EXTRACTION_INCOMPLETE", "NORMALIZATION_FAILED"}
        for op in review
    )
    review_reason = (
        "SOURCE_DOCUMENT_PROCESSING_INCOMPLETE"
        if technical_review
        else "UNCLASSIFIED_OPERATIONS_PRESENT"
    )
    article_results = []

    for article_code, report_field_id in context["article_report_fields"].items():
        sources = [op for op in classified if op.classification.article_code == article_code]
        supported = sum((Decimal(op.amount) for op in sources if op.amount is not None), Decimal("0"))
        report_value = Decimal(report_fields[report_field_id]["value"])
        if not sources:
            status = "INSUFFICIENT_DATA"
            reason_code = "MISSING_DOCUMENT"
            direction = "UNKNOWN"
            supported_value = None
            delta = None
            supported_value_is_partial = False
        elif review:
            status = "INSUFFICIENT_DATA"
            reason_code = review_reason
            direction = "UNKNOWN"
            supported_value = decimal_string(supported)
            delta = None
            supported_value_is_partial = True
        else:
            status, reason_code, direction = comparison_status(report_value, supported)
            supported_value = decimal_string(supported)
            delta = decimal_string(report_value - supported)
            supported_value_is_partial = False
        article_results.append(
            {
                "article_code": article_code,
                "article_name": rules["articles"][article_code]["name"],
                "report_field_id": report_field_id,
                "report_value": decimal_string(report_value),
                "supported_value": supported_value,
                "supported_value_is_partial": supported_value_is_partial,
                "delta_report_minus_supported": delta,
                "status": status,
                "reason_code": reason_code,
                "direction": direction,
                "source_count": len(sources),
                "sources": [
                    {
                        "operation_id": op.operation_id,
                        "document_id": op.document_id,
                        "file_name": op.file_name,
                        "amount": op.amount,
                        "amount_evidence": op.amount_evidence,
                        "classification_evidence": [
                            asdict(item) for item in op.classification.evidence
                        ],
                    }
                    for op in sources
                ],
                "report_evidence": report_fields[report_field_id]["evidence"],
                "formula": " + ".join(op.amount for op in sources if op.amount is not None) or None,
            }
        )

    total_supported = sum(
        (Decimal(op.amount) for op in classified if op.amount is not None), Decimal("0")
    )
    total_field_id = context["total_report_field"]
    total_report = Decimal(report_fields[total_field_id]["value"])
    missing_article_sources = any(item["source_count"] == 0 for item in article_results)
    if review:
        total_status = "INSUFFICIENT_DATA"
        total_reason = review_reason
        total_direction = "UNKNOWN"
        total_value = decimal_string(total_supported) if classified else None
        total_delta = None
        total_is_partial = True
    elif missing_article_sources:
        total_status = "INSUFFICIENT_DATA"
        total_reason = "MISSING_DOCUMENT"
        total_direction = "UNKNOWN"
        total_value = decimal_string(total_supported) if classified else None
        total_delta = None
        total_is_partial = bool(classified)
    else:
        total_status, total_reason, total_direction = comparison_status(
            total_report, total_supported
        )
        total_value = decimal_string(total_supported)
        total_delta = decimal_string(total_report - total_supported)
        total_is_partial = False
    total_result = {
        "report_field_id": total_field_id,
        "report_value": decimal_string(total_report),
        "supported_value": total_value,
        "supported_value_is_partial": total_is_partial,
        "delta_report_minus_supported": total_delta,
        "status": total_status,
        "reason_code": total_reason,
        "direction": total_direction,
        "source_count": len(classified),
        "source_operation_ids": [op.operation_id for op in classified],
        "unclassified_operation_ids": [op.operation_id for op in review],
        "report_evidence": report_fields[total_field_id]["evidence"],
        "formula": " + ".join(
            op.amount for op in classified if op.amount is not None
        )
        or None,
    }

    report_article_sum = sum(
        (
            Decimal(report_fields[field_id]["value"])
            for field_id in context["article_report_fields"].values()
        ),
        Decimal("0"),
    )
    report_consistency_delta = total_report - report_article_sum
    report_consistency = {
        "article_field_ids": list(context["article_report_fields"].values()),
        "total_field_id": total_field_id,
        "article_sum": decimal_string(report_article_sum),
        "row_total": decimal_string(total_report),
        "delta_row_total_minus_article_sum": decimal_string(report_consistency_delta),
        "status": "CORRECT" if report_consistency_delta == 0 else "MISMATCH",
        "reason_code": (
            "REPORT_TOTAL_CONSISTENT"
            if report_consistency_delta == 0
            else "REPORT_TOTAL_INCONSISTENT"
        ),
    }

    verified_details = []
    for check in context.get("detail_aggregation_checks", []):
        code_set = set(check["source_line_item_codes"])
        source_items = [
            {
                "operation_id": operation.operation_id,
                "document_id": operation.document_id,
                "file_name": operation.file_name,
                **line_item,
            }
            for operation in classified
            for line_item in operation.line_items
            if line_item["code"] in code_set
        ]
        supported = sum(
            (Decimal(item["amount"]) for item in source_items), Decimal("0")
        )
        report_value = sum(
            (Decimal(report_fields[field_id]["value"]) for field_id in check["report_field_ids"]),
            Decimal("0"),
        )
        if not source_items:
            status = "INSUFFICIENT_DATA"
            reason_code = "MISSING_DETAIL_SOURCE"
            direction = "UNKNOWN"
            supported_value = None
            delta = None
            supported_value_is_partial = False
        elif review:
            status = "INSUFFICIENT_DATA"
            reason_code = review_reason
            direction = "UNKNOWN"
            supported_value = decimal_string(supported)
            delta = None
            supported_value_is_partial = True
        else:
            status, reason_code, direction = comparison_status(report_value, supported)
            supported_value = decimal_string(supported)
            delta = decimal_string(report_value - supported)
            supported_value_is_partial = False
        verified_details.append(
            {
                "check_id": check["check_id"],
                "check_name": check["check_name"],
                "source_line_item_codes": check["source_line_item_codes"],
                "report_field_ids": check["report_field_ids"],
                "report_value": decimal_string(report_value),
                "supported_value": supported_value,
                "supported_value_is_partial": supported_value_is_partial,
                "delta_report_minus_supported": delta,
                "status": status,
                "reason_code": reason_code,
                "direction": direction,
                "source_count": len(source_items),
                "sources": source_items,
                "report_evidence": [
                    report_fields[field_id]["evidence"]
                    for field_id in check["report_field_ids"]
                ],
                "formula": " + ".join(item["amount"] for item in source_items) or None,
            }
        )

    detail_results = [
        {
            "field_id": field_id,
            "field_name": report_fields[field_id]["field_name"],
            "report_value": report_fields[field_id]["value"],
            "supported_value": None,
            "delta_report_minus_supported": None,
            "status": "INSUFFICIENT_DATA",
            "reason_code": "INSUFFICIENT_DETAIL",
            "report_evidence": report_fields[field_id]["evidence"],
        }
        for field_id in context["insufficient_detail_fields"]
    ]

    mismatches = [item for item in article_results if item["status"] == "MISMATCH"]
    detail_mismatches = [item for item in verified_details if item["status"] == "MISMATCH"]
    insufficient_articles = [
        item for item in article_results if item["status"] == "INSUFFICIENT_DATA"
    ]
    if report_consistency["status"] == "MISMATCH":
        conclusion_status = "REPORT_UNRELIABLE"
        conclusion_reason = "REPORT_TOTAL_INCONSISTENT"
    elif review:
        conclusion_status = "INSUFFICIENT_DATA"
        conclusion_reason = review_reason
    elif insufficient_articles:
        conclusion_status = "INSUFFICIENT_DATA"
        conclusion_reason = "MISSING_DOCUMENT"
    elif mismatches or detail_mismatches or total_status == "MISMATCH":
        conclusion_status = "REPORT_UNRELIABLE"
        conclusion_reason = "CONFIRMED_FINANCIAL_DISCREPANCY"
    else:
        conclusion_status = "RELIABLE_FOR_CHECKED_FIELDS"
        conclusion_reason = "NO_CONFIRMED_DISCREPANCY"

    return {
        "articles": article_results,
        "total": total_result,
        "report_consistency": report_consistency,
        "verified_details": verified_details,
        "unverified_details": detail_results,
        "conclusion": {
            "status": conclusion_status,
            "reason_code": conclusion_reason,
            "classified_operations": len(classified),
            "review_operations": len(review),
            "article_mismatches": len(mismatches),
            "insufficient_articles": len(insufficient_articles),
            "detail_mismatches": len(detail_mismatches),
            "checked_articles": len(article_results) - len(insufficient_articles),
            "unverified_detail_fields": len(detail_results),
        },
    }


def source_exclusions(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "document_id": record["document_id"],
            "file_name": record["file_name"],
            "reason_code": record["reason_code"],
            "duplicate_of": record["duplicate_of"],
            "document_date": record["document_date"],
            "contract_number": record["contract_number"],
            "amount": record["amount"],
            "evidence": record["evidence"],
        }
        for record in registry["records"]
        if record["role"] == "SOURCE_DOCUMENT" and record["decision"] == "EXCLUDE"
    ]


def markdown_report(payload: dict[str, Any]) -> str:
    result = payload["reconciliation"]

    def shown(value: Any) -> str:
        return "—" if value is None else str(value)

    def shown_supported(item: dict[str, Any]) -> str:
        value = shown(item.get("supported_value"))
        return f"{value} (частично)" if item.get("supported_value_is_partial") else value

    lines = [
        "# Сверка операций с КФЗ — кейс 19",
        "",
        f"- Итоговое заключение: **{result['conclusion']['status']}**.",
        f"- Классифицировано операций: {result['conclusion']['classified_operations']}.",
        f"- Операций на ручной проверке: {result['conclusion']['review_operations']}.",
        f"- Непроверенных детализированных полей: {result['conclusion']['unverified_detail_fields']}.",
        "- Денежные значения сравниваются точно до копейки; средняя дельта не используется.",
        "",
        "## Проверяемые статьи",
        "",
        "| Статья | По КФЗ | По документам | Дельта | Статус | Источники |",
        "|---|---:|---:|---:|---|---:|",
    ]
    for item in result["articles"]:
        lines.append(
            f"| {item['article_code']} — {item['article_name']} | {item['report_value']} | "
            f"{shown_supported(item)} | {shown(item['delta_report_minus_supported'])} | "
            f"{item['status']} / {item['reason_code']} | {item['source_count']} |"
        )
    total = result["total"]
    lines.append(
        f"| **Итого** | **{total['report_value']}** | **{shown_supported(total)}** | "
        f"**{shown(total['delta_report_minus_supported'])}** | **{total['status']} / "
        f"{total['reason_code']}** | **{total['source_count']}** |"
    )

    consistency = result["report_consistency"]
    lines.extend(
        [
            "",
            "## Внутренняя арифметика отчета",
            "",
            "| Сумма статей | Итоговая строка | Дельта | Статус |",
            "|---:|---:|---:|---|",
            f"| {consistency['article_sum']} | {consistency['row_total']} | "
            f"{consistency['delta_row_total_minus_article_sum']} | "
            f"{consistency['status']} / {consistency['reason_code']} |",
        ]
    )

    lines.extend(["", "## Цепочки доказательств", ""])
    for item in result["articles"]:
        lines.append(f"### {item['article_code']} — {item['article_name']}")
        lines.append("")
        if item["formula"]:
            suffix = " (частичный итог)" if item["supported_value_is_partial"] else ""
            lines.append(
                f"Формула: `{item['formula']} = {item['supported_value']}`{suffix}."
            )
        else:
            lines.append("Формула не строится: подтверждающие документы отсутствуют.")
        lines.append("")
        for source in item["sources"]:
            signal_ids = ", ".join(
                evidence["signal_id"] for evidence in source["classification_evidence"]
            )
            locator = (source["amount_evidence"] or {}).get("locator", "—")
            lines.append(
                f"- `{source['file_name']}`: {source['amount']} RUB; сумма — `{locator}`; "
                f"классификация — `{signal_ids}`."
            )
        lines.append("")

    lines.extend(
        [
            "## Исключенные исходные документы",
            "",
            "| Файл | Причина | Сумма |",
            "|---|---|---:|",
        ]
    )
    for item in payload["excluded_source_documents"]:
        lines.append(
            f"| {item['file_name']} | {item['reason_code']} | {item['amount'] or '—'} |"
        )

    review_operations = [
        operation
        for operation in payload.get("operations", [])
        if operation["classification"]["status"] != "CLASSIFIED"
    ]
    lines.extend(
        [
            "",
            "## Операции на ручной или технической проверке",
            "",
        ]
    )
    if review_operations:
        lines.extend(
            [
                "| Файл | Состояние | Причина | Предупреждения |",
                "|---|---|---|---|",
            ]
        )
        for operation in review_operations:
            warnings = "; ".join(operation["warnings"]) or "—"
            lines.append(
                f"| {operation['file_name']} | {operation['normalization_status']} | "
                f"{operation['classification']['reason_code']} | {warnings} |"
            )
    else:
        lines.append("Таких операций нет.")

    lines.extend(
        [
            "",
            "## Проверенная внутренняя детализация",
            "",
            "| Проверка | По КФЗ | По документам | Дельта | Статус |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for item in result["verified_details"]:
        lines.append(
            f"| {item['check_name']} | {item['report_value']} | {shown_supported(item)} | "
            f"{shown(item['delta_report_minus_supported'])} | {item['status']} / "
            f"{item['reason_code']} |"
        )

    lines.extend(
        [
            "",
            "## Непроверенные детализированные показатели",
            "",
            "Эти значения присутствуют в КФЗ, но доступные документы не дают достаточной "
            "самостоятельной детализации. Они не считаются финансовыми расхождениями.",
            "",
            "| Поле | Значение КФЗ | Статус | Причина |",
            "|---|---:|---|---|",
        ]
    )
    for item in result["unverified_details"]:
        lines.append(
            f"| {item['field_name']} | {item['report_value']} | {item['status']} | "
            f"{item['reason_code']} |"
        )
    return "\n".join(lines) + "\n"


def write_outputs(payload: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "reconciliation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "reconciliation.md").write_text(
        markdown_report(payload), encoding="utf-8"
    )
    columns = [
        "operation_id",
        "document_id",
        "file_name",
        "document_number",
        "period_from",
        "period_to",
        "amount",
        "currency",
        "normalization_status",
        "classification_status",
        "article_code",
        "article_name",
        "confidence",
        "reason_code",
        "score",
        "score_margin",
        "signal_ids",
    ]
    with (output_dir / "operation-ledger.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for operation in payload["operations"]:
            classification = operation["classification"]
            writer.writerow(
                {
                    "operation_id": operation["operation_id"],
                    "document_id": operation["document_id"],
                    "file_name": operation["file_name"],
                    "document_number": operation["document_number"],
                    "period_from": operation["period_from"],
                    "period_to": operation["period_to"],
                    "amount": operation["amount"],
                    "currency": operation["currency"],
                    "normalization_status": operation["normalization_status"],
                    "classification_status": classification["status"],
                    "article_code": classification["article_code"],
                    "article_name": classification["article_name"],
                    "confidence": classification["confidence"],
                    "reason_code": classification["reason_code"],
                    "score": classification["score"],
                    "score_margin": classification["score_margin"],
                    "signal_ids": ";".join(
                        item["signal_id"] for item in classification["evidence"]
                    ),
                }
            )


def run(
    registry_path: Path,
    input_dir: Path,
    ocr_dir: Path,
    context_path: Path,
    rules_path: Path,
    report_ocr_path: Path,
) -> dict[str, Any]:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    context = json.loads(context_path.read_text(encoding="utf-8"))
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    if registry["verification_context"]["case_id"] != context["case_id"]:
        raise ReconciliationError("Registry and verification context belong to different cases")

    operations = build_operations(registry, input_dir, ocr_dir, rules)
    report_fields = extract_report_row(report_ocr_path, context["report_row_fields"])
    report_ocr_payload = json.loads(report_ocr_path.read_text(encoding="utf-8"))
    report_provider = report_ocr_payload.get("provider", "unknown")
    result = reconcile(operations, report_fields, context, rules)
    return {
        "case_id": context["case_id"],
        "target_row_label": context["target_row_label"],
        "rule_set_version": rules["version"],
        "oracle_used_by_working_pipeline": False,
        "registry_summary": registry["summary"],
        "report_extraction": {
            "method": f"region_first_{report_provider}",
            "provider": report_provider,
            "source": str(report_ocr_path),
            "fields": report_fields,
        },
        "operations": [asdict(operation) for operation in operations],
        "excluded_source_documents": source_exclusions(registry),
        "reconciliation": result,
    }


def main() -> int:
    args = parse_args()
    payload = run(
        registry_path=args.registry,
        input_dir=args.input_dir,
        ocr_dir=args.ocr_dir,
        context_path=args.context,
        rules_path=args.rules,
        report_ocr_path=args.report_ocr,
    )
    write_outputs(payload, args.output_dir)
    print(
        json.dumps(
            {
                "status": payload["reconciliation"]["conclusion"]["status"],
                "operations": len(payload["operations"]),
                "review": payload["reconciliation"]["conclusion"]["review_operations"],
                "article_mismatches": payload["reconciliation"]["conclusion"][
                    "article_mismatches"
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
