"""Validated, frontend-oriented content blocks for agent responses."""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any


MAX_COLUMNS = 20
MAX_ROWS = 100
MAX_TITLE_LENGTH = 160
MAX_TEXT_LENGTH = 1_000
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")
_COLUMN_TYPES = {"text", "number", "currency", "percent", "date", "ticker"}


class BlockValidationError(ValueError):
    """Raised when an API content block does not meet the public contract."""


def _json_value(value: Any) -> str | int | float | bool | None:
    if isinstance(value, float) and not math.isfinite(value):
        raise BlockValidationError("cell numbers must be finite")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
            raise BlockValidationError("cell text exceeds maximum length")
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise BlockValidationError("cell values must be JSON scalar values")


def _validate_cell_type(value: Any, column_type: str) -> None:
    if value is None:
        return
    if column_type in {"number", "currency", "percent"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BlockValidationError(f"{column_type} cells must be numbers")
    elif column_type == "text" and not isinstance(value, str):
        raise BlockValidationError("text cells must be strings")
    elif column_type == "date":
        if not isinstance(value, str):
            raise BlockValidationError("date cells must be ISO-8601 strings")
        try:
            date.fromisoformat(value[:10])
        except ValueError as exc:
            raise BlockValidationError("date cells must be ISO-8601 strings") from exc


def validate_table_block(candidate: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a version-1 table block before it reaches clients."""
    if not isinstance(candidate, dict) or candidate.get("type") != "table":
        raise BlockValidationError("block type must be table")
    if candidate.get("version", 1) != 1:
        raise BlockValidationError("unsupported table block version")

    title = candidate.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > MAX_TITLE_LENGTH):
        raise BlockValidationError("invalid table title")

    columns = candidate.get("columns")
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
        raise BlockValidationError("table must have between 1 and 20 columns")

    normalized_columns = []
    keys = set()
    for column in columns:
        if not isinstance(column, dict):
            raise BlockValidationError("column must be an object")
        key, label = column.get("key"), column.get("label")
        column_type = column.get("type", "text")
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key) or key in keys:
            raise BlockValidationError("column keys must be unique snake_case identifiers")
        if not isinstance(label, str) or not label or len(label) > MAX_TITLE_LENGTH:
            raise BlockValidationError("invalid column label")
        if column_type not in _COLUMN_TYPES:
            raise BlockValidationError("invalid column type")
        keys.add(key)
        normalized_columns.append({
            "key": key,
            "label": label,
            "type": column_type,
            "sortable": bool(column.get("sortable", True)),
        })

    rows = candidate.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise BlockValidationError("table has too many rows")
    normalized_rows = []
    for row in rows:
        if not isinstance(row, dict) or set(row) - keys:
            raise BlockValidationError("row contains unknown fields")
        normalized_row = {key: _json_value(value) for key, value in row.items()}
        for column in normalized_columns:
            value = normalized_row.get(column["key"])
            _validate_cell_type(value, column["type"])
            if column["type"] == "ticker" and column["key"] in normalized_row:
                ticker = value
                if ticker is not None and (not isinstance(ticker, str) or not _TICKER_RE.fullmatch(ticker)):
                    raise BlockValidationError("invalid ticker cell")
        normalized_rows.append(normalized_row)

    result = {"type": "table", "version": 1, "columns": normalized_columns, "rows": normalized_rows}
    if title:
        result["title"] = title
    return result


_FIELD_SPECS = (
    ("rank", "Rank", "number", ("rank",)),
    ("ticker", "Ticker", "ticker", ("ticker", "symbol")),
    ("current_price", "Current stock price", "currency", ("underlying_price", "current_price", "price")),
    ("strike", "Strike", "currency", ("strike",)),
    ("expiration", "Expiration", "date", ("expiration", "expiration_date")),
    ("dte", "DTE", "number", ("dte", "days_to_expiration")),
    ("delta", "Delta", "number", ("delta", "short_delta")),
    ("iv", "IV %", "percent", ("iv", "implied_volatility", "iv_pct")),
    ("premium_received", "Premium received", "currency", ("premium_received", "premium", "credit")),
    ("roi", "ROI %", "percent", ("roi", "roi_pct", "return_on_risk_pct", "premium_yield_pct")),
    ("cash_required", "Cash required", "currency", ("cash_required", "max_risk", "collateral")),
    ("breakeven", "Breakeven", "currency", ("breakeven", "break_even")),
    ("downside_buffer_pct", "Downside buffer %", "percent", ("downside_buffer_pct",)),
    ("contracts_affordable", "Contracts affordable", "number", ("contracts_affordable",)),
    ("estimated_monthly_income", "Estimated monthly income", "currency", ("estimated_monthly_income",)),
    ("stock_quality_score", "Stock quality score", "number", ("stock_quality_score", "quality_score")),
)


def _value_for_field(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    contract = row.get("best_contract")
    sources = (row, contract) if isinstance(contract, dict) else (row,)
    for source in sources:
        for alias in aliases:
            value = source.get(alias)
            if isinstance(value, (str, int, float, bool, Decimal)) or value is None:
                if value is not None:
                    return value
    return None


def _format_expiration_dte(expiration: Any, dte: Any) -> str | None:
    """Render the option term compactly when expiration and DTE are both known."""
    if expiration is not None and dte is not None:
        return f"{expiration} ({dte} DTE)"
    if expiration is not None:
        return str(expiration)
    if dte is not None:
        return f"{dte} DTE"
    return None


def table_block_from_tool_result(tool_name: str, tool_result: str) -> dict[str, Any] | None:
    """Build a safe table only from known structured tool-result collections."""
    try:
        payload = json.loads(tool_result)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if tool_name == "build_monthly_income_plan":
        rows_source = payload.get("allocated_put_ideas")
        title = "Monthly income plan"
    else:
        rows_source = payload.get("opportunities") or payload.get("ranked_candidates")
        title = "Ranked opportunities" if "opportunities" in payload else "Ranked candidates"
    if not isinstance(rows_source, list) or not rows_source:
        return None
    source_rows = [row for row in rows_source if isinstance(row, dict)][:MAX_ROWS]
    if not source_rows:
        return None

    selected = []
    for key, label, column_type, aliases in _FIELD_SPECS:
        if key == "rank" or any(_value_for_field(row, aliases) is not None for row in source_rows):
            selected.append((key, label, column_type, aliases))
    if not selected:
        return None

    selected_keys = {key for key, _label, _type, _aliases in selected}
    if {"expiration", "dte"}.issubset(selected_keys):
        expiration_index = next(index for index, field in enumerate(selected) if field[0] == "expiration")
        selected = [field for field in selected if field[0] not in {"expiration", "dte"}]
        selected.insert(expiration_index, ("expiration_dte", "Expiration / DTE", "text", ()))

    rows = []
    for index, source_row in enumerate(source_rows, start=1):
        row = {}
        for key, _label, _type, aliases in selected:
            if key == "rank":
                value = index
            elif key == "expiration_dte":
                value = _format_expiration_dte(
                    _value_for_field(source_row, ("expiration", "expiration_date")),
                    _value_for_field(source_row, ("dte", "days_to_expiration")),
                )
            else:
                value = _value_for_field(source_row, aliases)
            if value is not None:
                row[key] = value
        rows.append(row)
    return validate_table_block({
        "type": "table",
        "version": 1,
        "title": title,
        "columns": [
            {"key": key, "label": label, "type": column_type}
            for key, label, column_type, _aliases in selected
        ],
        "rows": rows,
    })
