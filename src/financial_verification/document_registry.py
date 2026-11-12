#!/usr/bin/env python3
"""Build a deterministic document registry for a verification case."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries


DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)")
CONTRACT_RE = re.compile(r"(?<!\d)(\d{2}/\d{2}/\d{2}-\d{2})(?!\d)")
AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[ \u00a0]\d{3})*[,.]\d{2})(?!\d)")
DOC_NUMBER_RE = re.compile(r"(?:№|N[º°oО]?|No)\s*([A-Za-zА-Яа-я0-9-]+)", re.IGNORECASE)

MONTHS = {
    "январь": 1,
    "января": 1,
    "февраль": 2,
    "февраля": 2,
    "март": 3,
    "марта": 3,
    "апрель": 4,
    "апреля": 4,
    "май": 5,
    "мая": 5,
    "июнь": 6,
    "июня": 6,
    "июль": 7,
    "июля": 7,
    "август": 8,
    "августа": 8,
    "сентябрь": 9,
    "сентября": 9,
    "октябрь": 10,
    "октября": 10,
    "ноябрь": 11,
    "ноября": 11,
    "декабрь": 12,
    "декабря": 12,
}


@dataclass
class Evidence:
    source: str
    locator: str
    raw: str
    bbox: list[int] | None = None


@dataclass
class ExtractedContent:
    text: str
    lines: list[str]
    evidence_by_line: list[Evidence]
    amount: Decimal | None = None
    amount_evidence: Evidence | None = None


@dataclass
class DocumentRecord:
    document_id: str
    file_name: str
    extension: str
    size_bytes: int
    sha256: str
    role: str
    processing_status: str
    decision: str
    reason_code: str
    duplicate_of: str | None = None
    document_number: str | None = None
    contract_number: str | None = None
    document_date: str | None = None
    period_from: str | None = None
    period_to: str | None = None
    amount: str | None = None
    extraction_method: str | None = None
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--ocr-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def role_for_file(file_name: str, context: dict[str, Any]) -> str:
    if file_name == context["report_file"]:
        return "REPORT"
    if file_name in context.get("oracle_patterns", []):
        return "ORACLE"
    if any(file_name.startswith(prefix) for prefix in context.get("metadata_prefixes", [])):
        return "METADATA"
    return "SOURCE_DOCUMENT"


def normalize_decimal(raw: str) -> Decimal | None:
    value = raw.replace("\u00a0", " ").replace(" ", "").replace(",", ".")
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def iso_date(day: str, month: str, year: str) -> str | None:
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def month_period(text: str) -> tuple[str, str] | None:
    normalized = text.lower().replace("ё", "е")
    for month_name, month_number in MONTHS.items():
        match = re.search(rf"\b{month_name}\s+(20\d{{2}})\b", normalized)
        if not match:
            continue
        year = int(match.group(1))
        if month_number == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month_number + 1, 1)
        last_day = date.fromordinal(next_month.toordinal() - 1)
        return date(year, month_number, 1).isoformat(), last_day.isoformat()
    return None


def extract_docx(path: Path) -> ExtractedContent:
    document = Document(path)
    lines: list[str] = []
    evidence: list[Evidence] = []
    paragraph_number = 0
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            paragraph_number += 1
            lines.append(text)
            evidence.append(Evidence(path.name, f"paragraph:{paragraph_number}", text))
    for table_index, table in enumerate(document.tables, start=1):
        for row_index, row in enumerate(table.rows, start=1):
            values = [cell.text.strip() for cell in row.cells]
            text = " | ".join(value for value in values if value)
            if text:
                lines.append(text)
                evidence.append(Evidence(path.name, f"table:{table_index}:row:{row_index}", text))
    return ExtractedContent("\n".join(lines), lines, evidence)


def evaluate_sum_formula(workbook, sheet, formula: str) -> Decimal | None:
    match = re.fullmatch(r"=SUM\(([^)]+)\)", formula.strip(), re.IGNORECASE)
    if not match:
        return None
    min_col, min_row, max_col, max_row = range_boundaries(match.group(1))
    total = Decimal("0")
    for row in sheet.iter_rows(
        min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col
    ):
        for cell in row:
            if isinstance(cell.value, (int, float)):
                total += Decimal(str(cell.value))
            else:
                return None
    return total


def extract_xlsx(path: Path) -> ExtractedContent:
    workbook = load_workbook(path, data_only=False, read_only=False)
    lines: list[str] = []
    evidence: list[Evidence] = []
    total_amount: Decimal | None = None
    total_evidence: Evidence | None = None

    for sheet in workbook.worksheets:
        rows = list(sheet.iter_rows())
        contribution_column: int | None = None
        contribution_header_row: int | None = None
        for row in rows:
            for cell in row:
                if isinstance(cell.value, str) and "начислено взносов" in cell.value.lower():
                    contribution_column = cell.column
                    contribution_header_row = cell.row

        for row in rows:
            values = [cell.value for cell in row]
            if not any(value is not None for value in values):
                continue
            text_values = [str(value) for value in values if value is not None]
            text = " | ".join(text_values)
            lines.append(text)
            populated = [cell.coordinate for cell in row if cell.value is not None]
            locator = f"sheet:{sheet.title}:cells:{','.join(populated)}"
            row_evidence = Evidence(path.name, locator, text)
            evidence.append(row_evidence)

            if any(isinstance(value, str) and value.strip().upper() == "ИТОГО" for value in values):
                for cell in row:
                    if isinstance(cell.value, str) and cell.value.startswith("="):
                        evaluated = evaluate_sum_formula(workbook, sheet, cell.value)
                        if evaluated is not None:
                            total_amount = evaluated
                            total_evidence = Evidence(
                                path.name,
                                f"sheet:{sheet.title}:cell:{cell.coordinate}",
                                f"{cell.value} => {evaluated}",
                            )
                    elif isinstance(cell.value, (int, float)):
                        total_amount = Decimal(str(cell.value))
                        total_evidence = Evidence(
                            path.name,
                            f"sheet:{sheet.title}:cell:{cell.coordinate}",
                            str(cell.value),
                        )

        if total_amount is None and contribution_column and contribution_header_row:
            values = []
            cells = []
            for row_number in range(contribution_header_row + 1, sheet.max_row + 1):
                cell = sheet.cell(row=row_number, column=contribution_column)
                if isinstance(cell.value, (int, float)):
                    values.append(Decimal(str(cell.value)))
                    cells.append(cell.coordinate)
            if values:
                total_amount = sum(values, Decimal("0"))
                total_evidence = Evidence(
                    path.name,
                    f"sheet:{sheet.title}:cells:{','.join(cells)}",
                    " + ".join(str(value) for value in values) + f" = {total_amount}",
                )

    workbook.close()
    return ExtractedContent(
        "\n".join(lines), lines, evidence, amount=total_amount, amount_evidence=total_evidence
    )


def extract_ocr(path: Path, ocr_dir: Path) -> ExtractedContent:
    ocr_path = ocr_dir / f"{path.stem}.json"
    if not ocr_path.exists():
        raise FileNotFoundError(f"OCR result not found: {ocr_path}")
    payload = json.loads(ocr_path.read_text(encoding="utf-8"))
    observations = sorted(
        payload["observations"], key=lambda item: (item["bboxPixels"][1], item["bboxPixels"][0])
    )
    lines = [item["text"].strip() for item in observations if item["text"].strip()]
    evidence = [
        Evidence(path.name, "page:1", item["text"].strip(), item["bboxPixels"])
        for item in observations
        if item["text"].strip()
    ]
    return ExtractedContent("\n".join(lines), lines, evidence)


def find_evidence(content: ExtractedContent, raw: str) -> Evidence | None:
    for evidence in content.evidence_by_line:
        if raw in evidence.raw:
            return evidence
    return None


def extract_contract(content: ExtractedContent) -> tuple[str | None, Evidence | None]:
    match = CONTRACT_RE.search(content.text)
    if not match:
        return None, None
    return match.group(1), find_evidence(content, match.group(1))


def extract_date_or_period(content: ExtractedContent, file_name: str) -> tuple[str | None, str | None, str | None, Evidence | None]:
    match = DATE_RE.search(content.text)
    if match:
        value = iso_date(*match.groups())
        if value:
            return value, value, value, find_evidence(content, match.group(0))

    file_match = re.search(r"(\d{2})-(\d{2})-(20\d{2})", file_name)
    if file_match:
        value = iso_date(*file_match.groups())
        if value:
            return value, value, value, Evidence(file_name, "file_name", file_match.group(0))

    period = month_period(content.text) or month_period(file_name.replace("_", " "))
    if period:
        raw = next(
            (line for line in content.lines if month_period(line)),
            file_name,
        )
        return None, period[0], period[1], find_evidence(content, raw) or Evidence(file_name, "file_name", raw)
    return None, None, None, None


def extract_document_number(content: ExtractedContent, file_name: str) -> tuple[str | None, Evidence | None]:
    match = DOC_NUMBER_RE.search(content.text)
    if match:
        return match.group(1), find_evidence(content, match.group(0))

    labeled = re.search(r"Документ:\s*([^\s|]+)", content.text, re.IGNORECASE)
    if labeled:
        return labeled.group(1), find_evidence(content, labeled.group(0))

    fallback = re.sub(r"^\d{2}_", "", Path(file_name).stem)
    return fallback, Evidence(file_name, "file_name", fallback)


def extract_amount(content: ExtractedContent) -> tuple[Decimal | None, Evidence | None]:
    if content.amount is not None:
        return content.amount, content.amount_evidence

    keywords = ("итого", "сумма", "стоимость", "оплате", "всего")
    candidates: list[tuple[int, Decimal, str, Evidence | None]] = []
    for line in content.lines:
        line_lower = line.lower()
        score = 1 + (10 if any(keyword in line_lower for keyword in keywords) else 0)
        for match in AMOUNT_RE.finditer(line):
            value = normalize_decimal(match.group(1))
            if value is not None:
                candidates.append((score, value, match.group(1), find_evidence(content, line)))

    if not candidates:
        return None, None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, value, raw, evidence = candidates[0]
    if evidence is None:
        evidence = Evidence("unknown", "text", raw)
    return value, evidence


def evidence_dict(evidence: Evidence | None) -> dict[str, Any] | None:
    return asdict(evidence) if evidence else None


def extract_content(path: Path, ocr_dir: Path) -> tuple[ExtractedContent, str]:
    extension = path.suffix.lower()
    if extension == ".docx":
        return extract_docx(path), "native_docx"
    if extension == ".xlsx":
        return extract_xlsx(path), "native_xlsx"
    if extension in {".pdf", ".png"}:
        return extract_ocr(path, ocr_dir), "apple_vision_ocr"
    if extension == ".txt":
        text = path.read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        evidence = [Evidence(path.name, f"line:{index}", line) for index, line in enumerate(lines, 1)]
        return ExtractedContent(text, lines, evidence), "plain_text"
    raise ValueError(f"Unsupported format: {extension}")


def build_base_records(input_dir: Path, context: dict[str, Any]) -> list[DocumentRecord]:
    records = []
    for path in sorted(item for item in input_dir.iterdir() if item.is_file()):
        digest = sha256_file(path)
        unique_id = hashlib.sha256(f"{path.name}\0{digest}".encode("utf-8")).hexdigest()[:16]
        records.append(
            DocumentRecord(
                document_id=unique_id,
                file_name=path.name,
                extension=path.suffix.lower(),
                size_bytes=path.stat().st_size,
                sha256=digest,
                role=role_for_file(path.name, context),
                processing_status="REGISTERED",
                decision="PENDING",
                reason_code="PENDING",
            )
        )
    return records


def mark_duplicates(records: list[DocumentRecord]) -> None:
    first_by_hash: dict[str, DocumentRecord] = {}
    for record in records:
        if record.role != "SOURCE_DOCUMENT":
            continue
        original = first_by_hash.get(record.sha256)
        if original is None:
            first_by_hash[record.sha256] = record
            continue
        record.duplicate_of = original.document_id
        record.processing_status = "PROCESSED"
        record.decision = "EXCLUDE"
        record.reason_code = "DUPLICATE_DOCUMENT"


def apply_context_decision(record: DocumentRecord, context: dict[str, Any]) -> None:
    if record.role == "ORACLE":
        record.processing_status = "EXCLUDED_FROM_WORKING_SET"
        record.decision = "EXCLUDE"
        record.reason_code = "ORACLE"
        return
    if record.role == "REPORT":
        record.processing_status = "EXCLUDED_FROM_SOURCE_DOCUMENTS"
        record.decision = "EXCLUDE"
        record.reason_code = "REPORT"
        return
    if record.role == "METADATA":
        record.processing_status = "EXCLUDED_FROM_SOURCE_DOCUMENTS"
        record.decision = "EXCLUDE"
        record.reason_code = "METADATA"
        return
    if record.reason_code == "DUPLICATE_DOCUMENT":
        return
    if record.contract_number and record.contract_number != context["contract_number"]:
        record.decision = "EXCLUDE"
        record.reason_code = "WRONG_CONTRACT"
        return
    if record.period_from and record.period_to:
        if record.period_to < context["period_from"] or record.period_from > context["period_to"]:
            record.decision = "EXCLUDE"
            record.reason_code = "OUT_OF_PERIOD"
            return
    record.decision = "INCLUDE"
    record.reason_code = "VALID"


def hydrate_duplicate_fields(records: list[DocumentRecord]) -> None:
    by_id = {record.document_id: record for record in records}
    for record in records:
        if not record.duplicate_of:
            continue
        original = by_id[record.duplicate_of]
        for attribute in (
            "document_number",
            "contract_number",
            "document_date",
            "period_from",
            "period_to",
            "amount",
            "extraction_method",
        ):
            setattr(record, attribute, getattr(original, attribute))
        record.evidence = dict(original.evidence)


def process_records(
    input_dir: Path, ocr_dir: Path, context: dict[str, Any], records: list[DocumentRecord]
) -> None:
    for record in records:
        apply_context_decision(record, context)
        if record.role != "SOURCE_DOCUMENT" or record.reason_code == "DUPLICATE_DOCUMENT":
            continue

        path = input_dir / record.file_name
        try:
            content, method = extract_content(path, ocr_dir)
            number, number_ev = extract_document_number(content, record.file_name)
            contract, contract_ev = extract_contract(content)
            document_date, period_from, period_to, date_ev = extract_date_or_period(
                content, record.file_name
            )
            amount, amount_ev = extract_amount(content)

            record.document_number = number
            record.contract_number = contract
            record.document_date = document_date
            record.period_from = period_from
            record.period_to = period_to
            record.amount = f"{amount:.2f}" if amount is not None else None
            record.extraction_method = method
            record.processing_status = "PROCESSED"
            record.evidence = {
                key: value
                for key, value in {
                    "document_number": evidence_dict(number_ev),
                    "contract_number": evidence_dict(contract_ev),
                    "date_or_period": evidence_dict(date_ev),
                    "amount": evidence_dict(amount_ev),
                }.items()
                if value is not None
            }
            if contract is None:
                record.warnings.append("CONTRACT_NOT_EXTRACTED")
            if period_from is None:
                record.warnings.append("DATE_OR_PERIOD_NOT_EXTRACTED")
            if amount is None:
                record.warnings.append("AMOUNT_NOT_EXTRACTED")
            if record.warnings:
                record.processing_status = "PARTIALLY_PROCESSED"
                record.decision = "REVIEW"
                record.reason_code = "EXTRACTION_INCOMPLETE"
            else:
                apply_context_decision(record, context)
        except Exception as error:  # noqa: BLE001 - boundary must isolate file errors
            record.processing_status = "EXTRACTION_ERROR"
            record.decision = "REVIEW"
            record.reason_code = "EXTRACTION_FAILED"
            record.warnings.append(str(error))

    hydrate_duplicate_fields(records)


def write_outputs(records: list[DocumentRecord], context: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "verification_context": context,
        "summary": {
            "total_files": len(records),
            "source_documents": sum(record.role == "SOURCE_DOCUMENT" for record in records),
            "included": sum(record.decision == "INCLUDE" for record in records),
            "excluded": sum(record.decision == "EXCLUDE" for record in records),
            "review": sum(record.decision == "REVIEW" for record in records),
            "technical_errors": sum(record.processing_status == "EXTRACTION_ERROR" for record in records),
        },
        "records": [asdict(record) for record in records],
    }
    (output_dir / "document-registry.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    columns = [
        "file_name",
        "role",
        "document_number",
        "document_date",
        "period_from",
        "period_to",
        "contract_number",
        "amount",
        "decision",
        "reason_code",
        "duplicate_of",
        "processing_status",
    ]
    with (output_dir / "document-registry.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            source = asdict(record)
            writer.writerow({column: source[column] for column in columns})

    lines = [
        "# Реестр документов кейса 19",
        "",
        f"- Всего файлов: {payload['summary']['total_files']}.",
        f"- Первичных и расчетных документов: {payload['summary']['source_documents']}.",
        f"- Включено: {payload['summary']['included']}.",
        f"- Исключено: {payload['summary']['excluded']}.",
        f"- Требует проверки: {payload['summary']['review']}.",
        f"- Технических ошибок: {payload['summary']['technical_errors']}.",
        "",
        "| Файл | Дата или период | Договор | Сумма | Решение | Причина |",
        "|---|---|---|---:|---|---|",
    ]
    for record in records:
        period = record.document_date or (
            f"{record.period_from} — {record.period_to}" if record.period_from else "—"
        )
        lines.append(
            f"| {record.file_name} | {period} | {record.contract_number or '—'} | "
            f"{record.amount or '—'} | {record.decision} | {record.reason_code} |"
        )
    (output_dir / "document-registry.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    context = json.loads(args.context.read_text(encoding="utf-8"))
    records = build_base_records(args.input_dir, context)
    mark_duplicates(records)
    process_records(args.input_dir, args.ocr_dir, context, records)
    write_outputs(records, context, args.output_dir)
    summary = json.loads((args.output_dir / "document-registry.json").read_text())["summary"]
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["technical_errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
