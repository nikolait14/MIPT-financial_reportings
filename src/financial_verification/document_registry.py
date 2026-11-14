#!/usr/bin/env python3
"""Build a deterministic document registry for a verification case."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import statistics
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries

from financial_verification.financial_parsing import money_spans, parse_money_literal


DATE_RE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)")
DATE_RANGE_RE = re.compile(
    r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4})\s*(?:—|–|-)\s*"
    r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})(?!\d)"
)
CONTRACT_RE = re.compile(r"(?<!\d)(\d{2}/\d{2}/\d{2}-\d{2})(?!\d)")
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
    confidence: float | None = None


@dataclass
class ExtractedContent:
    text: str
    lines: list[str]
    evidence_by_line: list[Evidence]
    amount: Decimal | None = None
    amount_evidence: Evidence | None = None
    extraction_issues: list[str] = field(default_factory=list)


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
    possible_duplicate_of: str | None = None
    possible_duplicate_match_fields: list[str] = field(default_factory=list)
    document_number: str | None = None
    contract_number: str | None = None
    document_date: str | None = None
    period_from: str | None = None
    period_to: str | None = None
    amount: str | None = None
    currency: str | None = None
    extraction_method: str | None = None
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--ocr-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--fail-on-technical-errors", action="store_true")
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
    try:
        return parse_money_literal(raw)
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
    source_hash = payload.get("sourceSha256")
    if source_hash and source_hash != sha256_file(path):
        raise ValueError(f"Stale OCR result for {path.name}: source SHA-256 changed")
    lines = []
    evidence = []
    page_payloads = payload.get("pages") or [{"page": 1, **payload}]
    for page_payload in page_payloads:
        page_number = int(page_payload.get("page", 1))
        observations = [
            item for item in page_payload["observations"] if item.get("text", "").strip()
        ]
        heights = [item["bboxPixels"][3] for item in observations if item["bboxPixels"][3] > 0]
        tolerance = max(5.0, (statistics.median(heights) if heights else 10.0) * 0.6)
        visual_lines: list[list[dict[str, Any]]] = []
        for observation in sorted(
            observations,
            key=lambda item: (item["bboxPixels"][1] + item["bboxPixels"][3] / 2, item["bboxPixels"][0]),
        ):
            center_y = observation["bboxPixels"][1] + observation["bboxPixels"][3] / 2
            matching_line = min(
                visual_lines,
                key=lambda line: abs(center_y - statistics.median(item["bboxPixels"][1] + item["bboxPixels"][3] / 2 for item in line)),
                default=None,
            )
            if matching_line is not None:
                line_center = statistics.median(item["bboxPixels"][1] + item["bboxPixels"][3] / 2 for item in matching_line)
                if abs(center_y - line_center) <= tolerance:
                    matching_line.append(observation)
                    continue
            visual_lines.append([observation])
        for line in sorted(visual_lines, key=lambda row: statistics.median(item["bboxPixels"][1] + item["bboxPixels"][3] / 2 for item in row)):
            line.sort(key=lambda item: item["bboxPixels"][0])
            text = " ".join(item["text"].strip() for item in line)
            left = min(item["bboxPixels"][0] for item in line)
            top = min(item["bboxPixels"][1] for item in line)
            right = max(item["bboxPixels"][0] + item["bboxPixels"][2] for item in line)
            bottom = max(item["bboxPixels"][1] + item["bboxPixels"][3] for item in line)
            lines.append(text)
            confidences = [
                float(item["confidence"])
                for item in line
                if item.get("confidence") is not None
            ]
            evidence.append(
                Evidence(
                    path.name,
                    f"page:{page_number}",
                    text,
                    [left, top, right - left, bottom - top],
                    min(confidences) if confidences else None,
                )
            )
    return ExtractedContent("\n".join(lines), lines, evidence)


def extract_pdf_text(path: Path) -> ExtractedContent | None:
    """Read a searchable PDF before using OCR; retain page-level locators."""
    try:
        completed = subprocess.run(
            ["pdftotext", "-bbox-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
        root = ET.fromstring(completed.stdout)
    except (OSError, subprocess.CalledProcessError):
        return None
    except ET.ParseError:
        return None
    lines: list[str] = []
    evidence: list[Evidence] = []
    namespace = {"pdf": "http://www.w3.org/1999/xhtml"}
    pages = root.findall(".//page") or root.findall(".//pdf:page", namespace)
    for page_number, page in enumerate(pages, start=1):
        page_width = float(page.attrib.get("width", "0"))
        page_height = float(page.attrib.get("height", "0"))
        page_lines = page.findall(".//line") or page.findall(".//pdf:line", namespace)
        for line_number, line in enumerate(page_lines, start=1):
            words = line.findall("word") or line.findall("pdf:word", namespace)
            text = " ".join((word.text or "").strip() for word in words).strip()
            if not text:
                continue
            lines.append(text)
            try:
                left = min(float(word.attrib["xMin"]) for word in words)
                top = min(float(word.attrib["yMin"]) for word in words)
                right = max(float(word.attrib["xMax"]) for word in words)
                bottom = max(float(word.attrib["yMax"]) for word in words)
                bbox = (
                    [left / page_width, top / page_height, (right - left) / page_width, (bottom - top) / page_height]
                    if page_width > 0 and page_height > 0
                    else [int(left), int(top), int(right - left), int(bottom - top)]
                )
            except (KeyError, ValueError):
                bbox = None
            evidence.append(Evidence(path.name, f"page:{page_number}:line:{line_number}", text, bbox))
    # A few isolated glyphs are not a usable text layer and should not prevent
    # OCR from reading a scan with a broken hidden layer.
    meaningful = sum(char.isalnum() for line in lines for char in line)
    return ExtractedContent("\n".join(lines), lines, evidence) if meaningful >= 20 else None


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


def _set_issue(content: ExtractedContent, reason_code: str) -> None:
    if reason_code not in content.extraction_issues:
        content.extraction_issues.append(reason_code)


def extract_date_or_period(content: ExtractedContent, file_name: str) -> tuple[str | None, str | None, str | None, Evidence | None]:
    """Extract a labelled document date or reporting period without choosing first date.

    A document can contain a signing date, a service period and an invoice date.
    Unlabelled conflicting dates are intentionally routed to REVIEW instead of
    becoming a random period filter.
    """
    labelled_date_markers = (
        "дата документа",
        "дата акта",
        "дата составления",
        "дата счета",
        "от ",
    )
    period_markers = ("за период", "период", "отчетный период", "оказанных услуг")
    date_matches = list(DATE_RE.finditer(content.text))
    unique_values = {
        value
        for match in date_matches
        if (value := iso_date(*match.groups())) is not None
    }
    single_document_date = next(iter(unique_values)) if len(unique_values) == 1 else None
    # A labelled service period takes precedence over the date the document was
    # created, even if the document date occurs earlier on the page.
    for line, evidence in zip(content.lines, content.evidence_by_line):
        lower = line.lower().replace("ё", "е")
        range_match = DATE_RANGE_RE.search(line)
        if range_match and any(marker in lower for marker in period_markers):
            start = iso_date(*range_match.groups()[:3])
            end = iso_date(*range_match.groups()[3:])
            if start and end and start <= end:
                return single_document_date, start, end, evidence
            _set_issue(content, "INVALID_DATE_RANGE")
            return None, None, None, evidence
        period = month_period(line)
        if period and any(marker in lower for marker in period_markers + ("за ",)):
            return single_document_date, period[0], period[1], evidence

    for line, evidence in zip(content.lines, content.evidence_by_line):
        lower = line.lower().replace("ё", "е")
        match = DATE_RE.search(line)
        if match and any(marker in lower for marker in labelled_date_markers):
            value = iso_date(*match.groups())
            if value:
                return value, value, value, evidence

    # A single full date without a label is still unambiguous.  Several dates
    # need a label: first occurrence is not a business rule.
    if len(unique_values) == 1:
        value = next(iter(unique_values))
        raw_match = next(match for match in date_matches if iso_date(*match.groups()) == value)
        return value, value, value, find_evidence(content, raw_match.group(0))
    if len(unique_values) > 1:
        _set_issue(content, "AMBIGUOUS_DATE_OR_PERIOD")
        return None, None, None, None

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


def _date_span_overlaps(line: str, start: int, end: int) -> bool:
    return any(start < match.end() and end > match.start() for match in DATE_RE.finditer(line))


def extract_amount(content: ExtractedContent) -> tuple[Decimal | None, Evidence | None]:
    """Extract one defensible amount or emit an explicit REVIEW-worthy issue.

    We do not use "largest number wins": VAT and subtotal documents often have
    several perfectly valid values.  The selected value must be uniquely best
    by an explicit total/payment label.
    """
    if content.amount is not None:
        return content.amount, content.amount_evidence

    keywords = ("итого", "сумма", "стоимость", "оплате", "всего", "к перечислению")
    strongest_keywords = ("итого", "всего", "к оплате", "к перечислению")
    candidates: list[tuple[int, Decimal, str, Evidence | None]] = []
    explicit_currency: set[str] = set()
    previous_line_has_amount_label = False
    for line in content.lines:
        line_lower = line.lower()
        unit_markers = [
            ("million", Decimal("1000000"))
            for marker in ("млн руб", "миллион рублей")
            if marker in line_lower
        ] + [
            ("thousand", Decimal("1000"))
            for marker in ("тыс. руб", "тысяч рублей")
            if marker in line_lower
        ]
        if len(unit_markers) > 1:
            _set_issue(content, "AMBIGUOUS_AMOUNT_UNIT")
            return None, None
        multiplier = unit_markers[0][1] if unit_markers else Decimal("1")
        if any(marker in line_lower for marker in ("usd", "долл", "$")):
            explicit_currency.add("USD")
        if any(marker in line_lower for marker in ("eur", "евро", "€")):
            explicit_currency.add("EUR")
        if any(marker in line_lower for marker in ("руб", "rur", "rub", "₽")):
            explicit_currency.add("RUB")
        score = 1
        if any(keyword in line_lower for keyword in keywords):
            score += 10
        # DOCX tables commonly put "Сумма" in a header row and the value in
        # the following row.  Preserve that structure instead of falling back
        # to the largest unlabelled number in the document.
        if previous_line_has_amount_label:
            score += 10
        if any(keyword in line_lower for keyword in strongest_keywords):
            score += 10
        has_non_date_money = False
        for match in money_spans(line):
            if _date_span_overlaps(line, match.start(), match.end()):
                continue
            # A hyphen in an identifier (for example KЛ-31) is not a negative
            # financial value.  A real negative amount starts a token.
            if (
                match.group(0).startswith("-")
                and match.start() > 0
                and line[match.start() - 1].isalnum()
            ):
                continue
            value = normalize_decimal(match.group(0))
            if value is not None:
                has_non_date_money = True
                candidates.append(
                    (
                        score,
                        (value * multiplier).quantize(Decimal("0.01")),
                        match.group(0),
                        find_evidence(content, line),
                    )
                )
        previous_line_has_amount_label = (
            any(keyword in line_lower for keyword in keywords) and not has_non_date_money
        )

    if explicit_currency - {"RUB"}:
        _set_issue(content, "UNSUPPORTED_CURRENCY")
        return None, None
    if not candidates:
        _set_issue(content, "AMOUNT_NOT_EXTRACTED")
        return None, None
    if "ндс" in content.text.lower() and len({item[1] for item in candidates}) > 1:
        _set_issue(content, "AMBIGUOUS_VAT_AMOUNT")
        return None, None
    candidates.sort(key=lambda item: item[0], reverse=True)
    highest_score = candidates[0][0]
    top_candidates = [item for item in candidates if item[0] == highest_score]
    top_values = {item[1] for item in top_candidates}
    if len(top_values) > 1:
        _set_issue(
            content,
            "AMBIGUOUS_VAT_AMOUNT" if "ндс" in content.text.lower() else "AMBIGUOUS_AMOUNT",
        )
        return None, None
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
    if extension == ".pdf":
        native = extract_pdf_text(path)
        if native is not None:
            return native, "native_pdf_text"
        ocr_path = ocr_dir / f"{path.stem}.json"
        payload = json.loads(ocr_path.read_text(encoding="utf-8")) if ocr_path.exists() else {}
        provider = payload.get("provider", "legacy")
        return extract_ocr(path, ocr_dir), f"ocr:{provider}"
    if extension == ".png":
        ocr_path = ocr_dir / f"{path.stem}.json"
        payload = json.loads(ocr_path.read_text(encoding="utf-8")) if ocr_path.exists() else {}
        provider = payload.get("provider", "legacy")
        return extract_ocr(path, ocr_dir), f"ocr:{provider}"
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


def _possible_duplicate_match_fields(
    original: DocumentRecord, candidate: DocumentRecord
) -> list[str]:
    """Return strong business-identity matches, never a fuzzy auto-decision."""
    matches = []
    for attribute, label in (
        ("document_number", "document_number"),
        ("contract_number", "contract_number"),
        ("amount", "amount"),
    ):
        value = getattr(original, attribute)
        if value and value == getattr(candidate, attribute):
            matches.append(label)
    same_date = original.document_date and original.document_date == candidate.document_date
    same_period = (
        original.period_from
        and original.period_to
        and original.period_from == candidate.period_from
        and original.period_to == candidate.period_to
    )
    if same_date:
        matches.append("document_date")
    elif same_period:
        matches.append("period")
    return matches


def mark_possible_duplicates(records: list[DocumentRecord]) -> None:
    """Route re-saved document candidates to REVIEW without excluding them.

    Binary-identical files have already been excluded by ``mark_duplicates``.
    Here we require four independent strong requisites before treating two
    differently encoded files as possible copies.  A human confirms the pair;
    the candidate is never silently discarded or summed.
    """
    originals: list[DocumentRecord] = []
    for record in records:
        if (
            record.role != "SOURCE_DOCUMENT"
            or record.reason_code == "DUPLICATE_DOCUMENT"
        ):
            continue
        if record.decision != "INCLUDE":
            continue
        matched_original = next(
            (
                original
                for original in originals
                if len(_possible_duplicate_match_fields(original, record)) == 4
            ),
            None,
        )
        if matched_original is not None:
            record.possible_duplicate_of = matched_original.document_id
            record.possible_duplicate_match_fields = _possible_duplicate_match_fields(
                matched_original, record
            )
            record.decision = "REVIEW"
            record.reason_code = "POSSIBLE_DUPLICATE_DOCUMENT"
            record.warnings.append("POSSIBLE_DUPLICATE_DOCUMENT")
            continue
        originals.append(record)


def apply_manual_possible_duplicate_decisions(
    records: list[DocumentRecord], context: dict[str, Any]
) -> None:
    """Apply only explicit INCLUDE/EXCLUDE decisions to REVIEW candidates."""
    decisions = context.get("possible_duplicate_decisions", {})
    if not isinstance(decisions, dict):
        return
    for record in records:
        decision = decisions.get(record.file_name)
        if record.reason_code != "POSSIBLE_DUPLICATE_DOCUMENT" or decision not in {
            "INCLUDE",
            "EXCLUDE",
        }:
            continue
        record.decision = decision
        record.reason_code = (
            "MANUAL_POSSIBLE_DUPLICATE_INCLUDED"
            if decision == "INCLUDE"
            else "MANUAL_POSSIBLE_DUPLICATE_EXCLUDED"
        )
        record.warnings.append(record.reason_code)


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
        if (
            record.period_from < context["period_from"]
            or record.period_to > context["period_to"]
        ):
            record.decision = "REVIEW"
            record.reason_code = "PARTIALLY_OUT_OF_PERIOD"
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
            "currency",
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
            record.currency = "RUB" if amount is not None else None
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
            record.warnings.extend(content.extraction_issues)
            if record.warnings:
                record.processing_status = "PARTIALLY_PROCESSED"
                record.decision = "REVIEW"
                record.reason_code = content.extraction_issues[0] if content.extraction_issues else "EXTRACTION_INCOMPLETE"
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
        "currency",
        "decision",
        "reason_code",
        "duplicate_of",
        "possible_duplicate_of",
        "possible_duplicate_match_fields",
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
    mark_possible_duplicates(records)
    apply_manual_possible_duplicate_decisions(records, context)
    write_outputs(records, context, args.output_dir)
    summary = json.loads((args.output_dir / "document-registry.json").read_text())["summary"]
    print(json.dumps(summary, ensure_ascii=False))
    if args.fail_on_technical_errors and summary["technical_errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
