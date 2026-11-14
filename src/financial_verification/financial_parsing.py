"""Strict, shared parsing primitives for financial values.

The module deliberately accepts only a complete numeric literal.  Callers that
work with prose must first identify the candidate span and retain its source
evidence; silently deleting OCR letters (``1O0`` -> ``10``) is prohibited.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


MONEY_QUANTUM = Decimal("0.01")
# OCR may omit a thousands separator (``11 580377,40``).  Interior spaces are
# therefore formatting only, but every non-space character still has to be a
# digit, one sign or one two-digit fractional separator.  We never substitute
# lookalike letters such as O/0 or З/3.
_LITERAL_RE = re.compile(r"[+-]?\d(?:[\d \u00a0]*\d)?(?:[,.]\d{2})?")


def parse_money_literal(raw: str) -> Decimal:
    """Parse an unadorned amount without guessing its formatting or OCR text."""
    value = raw.strip().replace("\u00a0", " ")
    if not value or not _LITERAL_RE.fullmatch(value):
        raise InvalidOperation(f"Invalid money literal {raw!r}")
    normalized = value.replace(" ", "").replace(",", ".")
    return Decimal(normalized).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def money_spans(text: str) -> list[re.Match[str]]:
    """Return literal-shaped values; semantic filtering belongs to the caller."""
    return list(_LITERAL_RE.finditer(text))
