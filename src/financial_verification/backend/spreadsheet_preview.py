"""Bounded, session-scoped XLSX grid data for the evidence viewer."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter, range_boundaries


MAX_ROWS = 120
MAX_COLUMNS = 40
MAX_ARCHIVE_ENTRIES = 2_000
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MIN_RATIO_CHECK_BYTES = 1024 * 1024
MAX_FORMULA_CELLS = 10_000
_SUM_FORMULA = re.compile(r"=SUM\(([^)]+)\)", re.IGNORECASE)


def _decimal_text(value: Decimal) -> str:
    """Format a calculated decimal as a plain spreadsheet number."""
    return format(value.normalize(), "f") if value != value.to_integral() else str(value.quantize(Decimal("1")))


def _validate_xlsx_archive(path: Path) -> None:
    """Reject archive shapes that are unsafe to hand to an XML parser."""
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
    except BadZipFile as error:
        raise ValueError("Некорректный XLSX-архив.") from error
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("XLSX содержит слишком много файлов.")

    uncompressed_total = 0
    for entry in entries:
        if entry.is_dir():
            continue
        uncompressed_total += entry.file_size
        if uncompressed_total > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("XLSX превышает лимит распакованного размера.")
        if (
            entry.file_size >= MIN_RATIO_CHECK_BYTES
            and entry.compress_size > 0
            and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise ValueError("XLSX имеет небезопасный коэффициент сжатия.")


def _formula_value(sheet: Any, formula: str, budget: list[int], depth: int = 0) -> str | None:
    """Evaluate the intentionally small formula subset used by source workbooks.

    Excel-generated files normally retain a cached calculated value.  Files
    created by openpyxl do not, so support a bounded recursive SUM fallback
    for the viewer without pretending to be a general Excel calculation engine.
    """
    if depth > 3:
        return None
    match = _SUM_FORMULA.fullmatch(formula.strip())
    if match is None:
        return None
    total = Decimal("0")
    try:
        for expression in re.split(r"[;,]", match.group(1)):
            min_col, min_row, max_col, max_row = range_boundaries(expression.strip())
            cell_count = (max_col - min_col + 1) * (max_row - min_row + 1)
            if cell_count > MAX_FORMULA_CELLS or cell_count > budget[0]:
                return None
            budget[0] -= cell_count
            for row in sheet.iter_rows(
                min_row=min_row, max_row=max_row, min_col=min_col, max_col=max_col
            ):
                for cell in row:
                    value = cell.value
                    if isinstance(value, str) and value.startswith("="):
                        value = _formula_value(sheet, value, budget, depth + 1)
                    if value is None:
                        return None
                    total += Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return _decimal_text(total)


def _display_value(
    sheet: Any, raw_value: Any, cached_value: Any, formula_budget: list[int]
) -> str:
    if cached_value is not None:
        return str(cached_value)
    if raw_value is None:
        return ""
    if isinstance(raw_value, str) and raw_value.startswith("="):
        calculated = _formula_value(sheet, raw_value, formula_budget)
        if calculated is not None:
            return calculated
    return str(raw_value)


def spreadsheet_preview(path: Path, sheet_name: str | None = None) -> dict[str, Any]:
    """Return a safe used-range slice, never an arbitrary workbook object."""
    _validate_xlsx_archive(path)
    # Read formulas and cached display values separately.  This mirrors Excel:
    # the grid shows the number while the formula bar reveals its expression.
    workbook = load_workbook(path, read_only=True, data_only=False)
    values_workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = workbook.sheetnames
        if not sheets:
            raise ValueError("Workbook has no sheets")
        selected = sheet_name or sheets[0]
        if selected not in sheets:
            raise KeyError(selected)
        sheet = workbook[selected]
        values_sheet = values_workbook[selected]
        min_col, min_row, max_col, max_row = range_boundaries(sheet.calculate_dimension())
        end_row = min(max_row, min_row + MAX_ROWS - 1)
        end_col = min(max_col, min_col + MAX_COLUMNS - 1)
        formula_budget = [MAX_FORMULA_CELLS]
        rows = []
        for row_offset, (row, value_row) in enumerate(
            zip(
                sheet.iter_rows(
                    min_row=min_row, max_row=end_row, min_col=min_col, max_col=end_col
                ),
                values_sheet.iter_rows(
                    min_row=min_row, max_row=end_row, min_col=min_col, max_col=end_col
                ),
            )
        ):
            rows.append(
                [
                    {
                        # ``read_only`` worksheets yield EmptyCell objects for
                        # sparse cells.  They intentionally have no coordinate;
                        # construct it from the bounded grid instead.
                        "coordinate": f"{get_column_letter(min_col + column_offset)}{min_row + row_offset}",
                        "value": _display_value(
                            sheet, cell.value, value_cell.value, formula_budget
                        ),
                        "formula": cell.value
                        if isinstance(cell.value, str) and cell.value.startswith("=")
                        else None,
                    }
                    for column_offset, (cell, value_cell) in enumerate(zip(row, value_row))
                ]
            )
        return {
            "sheet": selected,
            "sheets": sheets,
            "range": f"{get_column_letter(min_col)}{min_row}:{get_column_letter(end_col)}{end_row}",
            "truncated": end_row < max_row or end_col < max_col,
            "columns": [get_column_letter(column) for column in range(min_col, end_col + 1)],
            "row_numbers": list(range(min_row, end_row + 1)),
            "rows": rows,
        }
    finally:
        workbook.close()
        values_workbook.close()
