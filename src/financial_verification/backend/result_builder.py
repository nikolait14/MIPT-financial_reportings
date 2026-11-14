from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _money(value: str | None) -> str:
    if value is None:
        return "—"
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value
    integral, fraction = f"{number:.2f}".split(".")
    grouped = f"{int(integral):,}".replace(",", " ")
    return f"{grouped},{fraction}"


def _normalize_xywh(
    bbox: list[float] | None, width: float | None, height: float | None
) -> list[float] | None:
    if not bbox or not width or not height or len(bbox) != 4:
        return None
    left, top, box_width, box_height = bbox
    return [
        round(left / width, 8),
        round(top / height, 8),
        round(box_width / width, 8),
        round(box_height / height, 8),
    ]


def _normalize_x1y1x2y2(
    bbox: list[float] | None, width: float | None, height: float | None
) -> list[float] | None:
    if not bbox or not width or not height or len(bbox) != 4:
        return None
    left, top, right, bottom = bbox
    return _normalize_xywh(
        [left, top, right - left, bottom - top], width, height
    )


def _locator_from_registry_evidence(
    evidence: dict[str, Any], dimensions: tuple[float, float] | None
) -> dict[str, Any]:
    locator = evidence.get("locator", "unknown")
    bbox = evidence.get("bbox")
    if locator.startswith("page:"):
        page = int(locator.split(":", 1)[1])
        width, height = dimensions or (None, None)
        return {
            "type": "PAGE_REGION",
            "page": page,
            "bbox": _normalize_xywh(bbox, width, height),
        }
    if locator.startswith("sheet:"):
        parts = locator.split(":")
        payload: dict[str, Any] = {"type": "SHEET_RANGE", "sheet": parts[1]}
        if "cell" in parts:
            payload["cell"] = parts[parts.index("cell") + 1]
        elif "cells" in parts:
            payload["cells"] = parts[parts.index("cells") + 1].split(",")
        return payload
    if locator.startswith("table:"):
        parts = locator.split(":")
        payload = {"type": "TABLE_ROW"}
        if len(parts) >= 2:
            payload["table"] = int(parts[1])
        if "row" in parts:
            payload["row"] = int(parts[parts.index("row") + 1])
        return payload
    if locator.startswith("paragraph:"):
        return {"type": "PARAGRAPH", "paragraph": int(locator.split(":")[1])}
    if locator == "file_name":
        return {"type": "FILE_NAME"}
    return {"type": "TEXT_REFERENCE", "locator": locator}


def _ocr_dimensions(artifacts_dir: Path, file_name: str) -> tuple[float, float] | None:
    path = artifacts_dir / "case19-registry" / "ocr" / f"{Path(file_name).stem}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return float(payload["width"]), float(payload["height"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def build_verification_result(
    *,
    verification_id: str,
    context: dict[str, Any],
    registry: dict[str, Any],
    reconciliation: dict[str, Any],
    extraction: dict[str, Any],
    artifacts_dir: Path,
    storage_mode: str,
    confidence_warning: float,
) -> dict[str, Any]:
    records = registry["records"]
    by_document_id = {record["document_id"]: record for record in records}
    report_record = next(record for record in records if record["role"] == "REPORT")
    content_access = "SERVER" if storage_mode == "PERSISTENT" else "CLIENT_FILE"

    documents = []
    evidence: list[dict[str, Any]] = []
    evidence_by_record_field: dict[tuple[str, str], str] = {}

    for record in records:
        document = {
            **{key: value for key, value in record.items() if key != "evidence"},
            "content_access": content_access,
        }
        if storage_mode == "PERSISTENT":
            document["content_url"] = (
                f"/api/v1/verifications/{verification_id}/documents/"
                f"{record['document_id']}/content"
            )
        documents.append(document)
        dimensions = _ocr_dimensions(artifacts_dir, record["file_name"])
        for field_name, item in record.get("evidence", {}).items():
            evidence_id = f"source:{record['document_id']}:{field_name}"
            evidence_by_record_field[(record["document_id"], field_name)] = evidence_id
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "document_id": record["document_id"],
                    "document_sha256": record["sha256"],
                    "field_id": field_name,
                    "locator": _locator_from_registry_evidence(item, dimensions),
                    "raw_text": item.get("raw"),
                    "normalized_value": record.get(field_name),
                    "confidence": None,
                    "extractor": record.get("extraction_method"),
                    "content_access": content_access,
                }
            )

    report_dimensions = None
    configured_regions = extraction.get("regions")
    regions_path = Path(configured_regions) if configured_regions else Path()
    if not configured_regions or not regions_path.exists():
        candidates = sorted((artifacts_dir / "linux-report-ocr").glob("*-*dpi/regions.json"))
        regions_path = candidates[0] if candidates else Path()
    if regions_path.exists():
        try:
            regions = json.loads(regions_path.read_text(encoding="utf-8"))
            report_dimensions = tuple(float(value) for value in regions["image_size"])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            report_dimensions = None

    extraction_fields = {
        field["field_id"]: field for field in extraction.get("fields", [])
    }
    report_evidence_ids: dict[str, str] = {}
    report_extracted_fields = reconciliation["report_extraction"]["fields"]
    for field_id, item in report_extracted_fields.items():
        evidence_id = f"report:{field_id}"
        report_evidence_ids[field_id] = evidence_id
        extracted = extraction_fields.get(field_id, {})
        observations = extracted.get("observations", [])
        confidences = [
            float(observation["confidence"])
            for observation in observations
            if observation.get("confidence") is not None
        ]
        width, height = report_dimensions or (None, None)
        evidence.append(
            {
                "evidence_id": evidence_id,
                "document_id": report_record["document_id"],
                "document_sha256": report_record["sha256"],
                "field_id": field_id,
                "locator": {
                    "type": "PAGE_REGION",
                    "page": 1,
                    "bbox": _normalize_x1y1x2y2(
                        extracted.get("bbox"), width, height
                    ),
                },
                "raw_text": extracted.get("raw", item["evidence"].get("raw")),
                "normalized_value": item["value"],
                "confidence": min(confidences) if confidences else None,
                "extractor": reconciliation["report_extraction"].get("provider"),
                "content_access": content_access,
            }
        )

    annotations: list[dict[str, Any]] = []
    for evidence_item in evidence:
        confidence = evidence_item.get("confidence")
        if confidence is not None and confidence < confidence_warning:
            annotations.append(
                {
                    "annotation_id": f"warning:{evidence_item['evidence_id']}",
                    "document_id": evidence_item["document_id"],
                    "category": "EXTRACTION_WARNING",
                    "severity": "WARNING",
                    "message": "Низкая уверенность распознавания критического поля.",
                    "target": evidence_item["locator"],
                    "raw_text": evidence_item.get("raw_text"),
                    "confidence": confidence,
                    "evidence_id": evidence_item["evidence_id"],
                }
            )

    articles = []
    findings = []
    for item in reconciliation["reconciliation"]["articles"]:
        source_evidence_ids = [
            evidence_by_record_field.get((source["document_id"], "amount"))
            for source in item["sources"]
        ]
        source_evidence_ids = [value for value in source_evidence_ids if value]
        article = {
            **{key: value for key, value in item.items() if key not in {"sources", "report_evidence"}},
            "report_evidence_id": report_evidence_ids[item["report_field_id"]],
            "source_evidence_ids": source_evidence_ids,
            "source_document_ids": [source["document_id"] for source in item["sources"]],
        }
        articles.append(article)
        if item["status"] != "MISMATCH":
            continue
        finding_id = f"finding:{item['article_code']}"
        title = (
            f"Завышение статьи {item['article_code']}"
            if item["direction"] == "OVERSTATEMENT"
            else f"Занижение статьи {item['article_code']}"
        )
        finding = {
            "finding_id": finding_id,
            "title": title,
            "article_code": item["article_code"],
            "article_name": item["article_name"],
            "severity": "ERROR",
            "status": item["status"],
            "reason_code": item["reason_code"],
            "report_value": item["report_value"],
            "supported_value": item["supported_value"],
            "delta": item["delta_report_minus_supported"],
            "direction": item["direction"],
            "formula": item["formula"],
            "explanation": (
                f"Сумма статьи {item['article_code']} в КФЗ не совпадает с суммой "
                f"{item['source_count']} подтверждающих документов."
            ),
            "report_evidence_id": report_evidence_ids[item["report_field_id"]],
            "source_evidence_ids": source_evidence_ids,
            "recommended_action": (
                "Проверить основание расхождения либо скорректировать КФЗ и "
                "повторно выполнить проверку."
            ),
        }
        findings.append(finding)
        report_evidence = next(
            evidence_item
            for evidence_item in evidence
            if evidence_item["evidence_id"] == finding["report_evidence_id"]
        )
        annotations.append(
            {
                "annotation_id": f"annotation:{item['article_code']}:report",
                "document_id": report_record["document_id"],
                "category": "FINANCIAL_MISMATCH",
                "severity": "ERROR",
                "message": (
                    f"Значение КФЗ отличается от подтвержденного на "
                    f"{_money(item['delta_report_minus_supported'])} руб."
                ),
                "target": report_evidence["locator"],
                "raw_text": report_evidence["raw_text"],
                "actual_value": item["report_value"],
                "expected_value": item["supported_value"],
                "finding_id": finding_id,
                "evidence_id": report_evidence["evidence_id"],
            }
        )

    for record in records:
        if record["role"] != "SOURCE_DOCUMENT":
            continue
        if record["decision"] == "EXCLUDE":
            annotations.append(
                {
                    "annotation_id": f"decision:{record['document_id']}",
                    "document_id": record["document_id"],
                    "category": "BUSINESS_DECISION",
                    "severity": "INFO",
                    "message": f"Документ исключен: {record['reason_code']}.",
                    "target": {"type": "DOCUMENT"},
                    "reason_code": record["reason_code"],
                }
            )
        elif record["decision"] == "REVIEW":
            annotations.append(
                {
                    "annotation_id": f"review:{record['document_id']}",
                    "document_id": record["document_id"],
                    "category": "EXTRACTION_WARNING",
                    "severity": "WARNING",
                    "message": "Документ требует ручной или технической проверки.",
                    "target": {"type": "DOCUMENT"},
                    "reason_code": record["reason_code"],
                    "warnings": record["warnings"],
                }
            )

    reconciliation_result = reconciliation["reconciliation"]
    total = reconciliation_result["total"]
    summary = {
        "conclusion": reconciliation_result["conclusion"],
        "report_total": total["report_value"],
        "supported_total": total["supported_value"],
        "delta": total["delta_report_minus_supported"],
        "total_status": total["status"],
        "total_reason_code": total["reason_code"],
        "total_files": registry["summary"]["total_files"],
        "source_documents": registry["summary"]["source_documents"],
        "included_documents": registry["summary"]["included"],
        "review_documents": registry["summary"]["review"],
        "technical_errors": registry["summary"]["technical_errors"],
        "findings": len(findings),
    }

    return {
        "schema_version": "1.0",
        "verification_id": verification_id,
        "status": "COMPLETED",
        "storage": {
            "mode": storage_mode,
            "documents_persisted": storage_mode == "PERSISTENT",
            "results_persisted": storage_mode == "PERSISTENT",
        },
        "context": context,
        "summary": summary,
        "findings": findings,
        "articles": articles,
        "documents": documents,
        "evidence": evidence,
        "annotations": annotations,
        "verified_details": reconciliation_result["verified_details"],
        "unverified": reconciliation_result["unverified_details"],
        "report_consistency": reconciliation_result["report_consistency"],
        "technical": {
            "rule_set_version": reconciliation["rule_set_version"],
            "report_provider": reconciliation["report_extraction"].get("provider"),
            "registry_summary": registry["summary"],
        },
    }


def render_informative_markdown(result: dict[str, Any]) -> str:
    context = result["context"]
    summary = result["summary"]
    conclusion = summary["conclusion"]
    if conclusion["status"] == "REPORT_UNRELIABLE":
        headline = "Обнаружено подтвержденное расхождение"
    elif conclusion["status"] == "INSUFFICIENT_DATA":
        headline = "Недостаточно данных для полного заключения"
    else:
        headline = "Проверенные показатели подтверждены"

    lines = [
        "# Заключение по результатам проверки КФЗ",
        "",
        f"> **Статус: {headline}**",
        "",
        "## Паспорт проверки",
        "",
        "| Параметр | Значение |",
        "|---|---|",
        f"| Проверяемый отчет | `{context['report_file']}` |",
        f"| Проверяемая строка | `{context['target_row_label']}` |",
        f"| Договор | `{context['contract_number']}` |",
        f"| Период | {context['period_from']}–{context['period_to']} |",
        f"| Получено файлов | {summary['total_files']} |",
        f"| Первичных и расчетных документов | {summary['source_documents']} |",
        "",
        "## Резюме",
        "",
        "| Показатель | Значение |",
        "|---|---:|",
        f"| Сумма по КФЗ | {_money(summary['report_total'])} руб. |",
        f"| Подтверждено документами | {_money(summary['supported_total'])} руб. |",
        f"| Расхождение | **{_money(summary['delta'])} руб.** |",
        f"| Обнаружено проблем | {summary['findings']} |",
        f"| Документов на проверке | {summary['review_documents']} |",
        f"| Технических ошибок | {summary['technical_errors']} |",
        "",
    ]

    if result["findings"]:
        lines.extend(["## Обнаруженные расхождения", ""])
        for index, finding in enumerate(result["findings"], 1):
            lines.extend(
                [
                    f"### {index}. {finding['article_code']} — {finding['article_name']}",
                    "",
                    f"- По КФЗ: **{_money(finding['report_value'])} руб.**",
                    f"- Подтверждено: **{_money(finding['supported_value'])} руб.**",
                    f"- Разница: **{_money(finding['delta'])} руб.**",
                    f"- Объяснение: {finding['explanation']}",
                    f"- Формула: `{finding['formula'] or '—'}`",
                    f"- Рекомендуемое действие: {finding['recommended_action']}",
                    "",
                ]
            )

    lines.extend(
        [
            "## Результаты по статьям",
            "",
            "| Статья | По КФЗ | Подтверждено | Разница | Результат |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for article in result["articles"]:
        label = {
            "CORRECT": "✅ Подтверждено",
            "MISMATCH": "❌ Расхождение",
            "INSUFFICIENT_DATA": "⚠️ Недостаточно данных",
        }.get(article["status"], article["status"])
        lines.append(
            f"| `{article['article_code']}` — {article['article_name']} | "
            f"{_money(article['report_value'])} | {_money(article['supported_value'])} | "
            f"{_money(article['delta_report_minus_supported'])} | {label} |"
        )

    lines.extend(
        [
            "",
            "## Полнота обработки",
            "",
            f"- Учтено документов: {summary['included_documents']}.",
            f"- Требует проверки: {summary['review_documents']}.",
            f"- Технических ошибок: {summary['technical_errors']}.",
            "",
            "## Исключенные документы",
            "",
            "| Документ | Причина | Сумма |",
            "|---|---|---:|",
        ]
    )
    excluded = [
        document
        for document in result["documents"]
        if document["role"] == "SOURCE_DOCUMENT" and document["decision"] == "EXCLUDE"
    ]
    if excluded:
        for document in excluded:
            lines.append(
                f"| `{document['file_name']}` | {document['reason_code']} | "
                f"{_money(document['amount'])} |"
            )
    else:
        lines.append("| — | Исключенных документов нет | — |")

    lines.extend(
        [
            "",
            "## Непроверенные показатели",
            "",
            "Недостаток данных не считается финансовым расхождением.",
            "",
            "| Показатель | Значение КФЗ | Причина |",
            "|---|---:|---|",
        ]
    )
    for item in result["unverified"]:
        lines.append(
            f"| {item['field_name']} | {_money(item['report_value'])} | "
            f"{item['reason_code']} |"
        )

    lines.extend(
        [
            "",
            "## Ограничение",
            "",
            "Заключение относится только к указанным договору, периоду и строке КФЗ. "
            "Автоматизированная проверка не заменяет юридическое или аудиторское "
            "заключение, если оно требуется нормативными документами.",
            "",
            "## Технические идентификаторы",
            "",
            f"- Verification ID: `{result['verification_id']}`",
            f"- Схема результата: `{result['schema_version']}`",
            f"- Машинный статус: `{conclusion['status']}`",
            f"- Код причины: `{conclusion['reason_code']}`",
        ]
    )
    return "\n".join(lines) + "\n"
