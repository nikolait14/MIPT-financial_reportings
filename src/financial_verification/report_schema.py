"""Configuration-driven report schema helpers.

The numeric article codes are data from a KФЗ form, not a closed enum in the
application.  Legacy contexts may keep a list of strings; richer contexts may
attach an article code and a display name to each report column.
"""

from __future__ import annotations

from typing import Any


def report_field_ids(context: dict[str, Any]) -> list[str]:
    result = []
    for field in context["report_row_fields"]:
        field_id = field if isinstance(field, str) else field.get("field_id")
        if not isinstance(field_id, str) or not field_id:
            raise ValueError("Each report_row_fields item needs a non-empty field_id")
        result.append(field_id)
    if len(result) != len(set(result)):
        raise ValueError("report_row_fields contains duplicate field_id values")
    return result


def report_field_labels(context: dict[str, Any]) -> dict[str, str]:
    return {
        field["field_id"]: field["field_name"]
        for field in context["report_row_fields"]
        if isinstance(field, dict) and isinstance(field.get("field_name"), str)
    }


def article_report_fields(context: dict[str, Any]) -> dict[str, str]:
    """Return article -> field mapping from a legacy map and/or field metadata."""
    mapping = dict(context.get("article_report_fields", {}))
    known_fields = set(report_field_ids(context))
    for code, field_id in mapping.items():
        if field_id not in known_fields:
            raise ValueError(f"Article {code} refers to unknown report field {field_id}")
    for field in context["report_row_fields"]:
        if not isinstance(field, dict) or not field.get("article_code"):
            continue
        code = str(field["article_code"])
        field_id = field["field_id"]
        if code in mapping and mapping[code] != field_id:
            raise ValueError(f"Article {code} has conflicting report field mapping")
        mapping[code] = field_id
    return mapping


def article_display_name(article_code: str, context: dict[str, Any], rules: dict[str, Any]) -> str:
    configured = rules.get("articles", {}).get(article_code, {}).get("name")
    if isinstance(configured, str) and configured:
        return configured
    for field in context["report_row_fields"]:
        if isinstance(field, dict) and str(field.get("article_code")) == article_code:
            label = field.get("article_name") or field.get("field_name")
            if isinstance(label, str) and label:
                return label
    return f"Статья {article_code}"
