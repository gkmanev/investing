from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from api.agent_request import RequestContext
from api.models import Symbol


@dataclass
class AnswerValidationResult:
    is_valid: bool
    reasons: list[str] = field(default_factory=list)
    required_symbols: set[str] = field(default_factory=set)
    allowed_symbols: set[str] = field(default_factory=set)
    detected_symbols: set[str] = field(default_factory=set)

    @property
    def needs_repair(self) -> bool:
        return not self.is_valid


_SYMBOL_IGNORE_TOKENS = {
    "A",
    "AI",
    "AND",
    "ASK",
    "ATM",
    "BE",
    "BID",
    "BUY",
    "CALL",
    "CALLS",
    "CSP",
    "DTE",
    "ETF",
    "GO",
    "HOLD",
    "IV",
    "ITM",
    "LLM",
    "ME",
    "NO",
    "OTM",
    "PUT",
    "PUTS",
    "ROI",
    "RSI",
    "USD",
}


def _normalize_symbol(value: Any) -> str | None:
    symbol = str(value or "").strip().upper()
    if not symbol or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
        return None
    return symbol


def _extract_symbols_from_payload(payload: Any) -> set[str]:
    symbols: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            lower_key = str(key).lower()
            if lower_key in {"symbol", "ticker"}:
                normalized = _normalize_symbol(value)
                if normalized:
                    symbols.add(normalized)
            elif lower_key in {"symbols", "tickers"} and isinstance(value, list):
                for item in value:
                    normalized = _normalize_symbol(item)
                    if normalized:
                        symbols.add(normalized)
            else:
                symbols.update(_extract_symbols_from_payload(value))
    elif isinstance(payload, list):
        for item in payload:
            symbols.update(_extract_symbols_from_payload(item))
    return symbols


def _load_tool_payload(tool_result: str | None) -> Any:
    if not tool_result:
        return None
    try:
        return json.loads(tool_result)
    except Exception:
        return None


def _extract_known_symbols_from_text(text: str) -> set[str]:
    if not text:
        return set()

    candidates = {
        token.upper()
        for token in re.findall(r"\b[A-Z][A-Z0-9.\-]{0,9}\b", text)
        if token.upper() not in _SYMBOL_IGNORE_TOKENS
    }
    if not candidates:
        return set()

    try:
        return {
            str(symbol).upper()
            for symbol in Symbol.objects.filter(ticker__in=sorted(candidates)).values_list(
                "ticker",
                flat=True,
            )
        }
    except Exception:
        return set()


def validate_answer(
    *,
    answer: str,
    context: RequestContext,
    tool_name: str | None = None,
    tool_result: str | None = None,
) -> AnswerValidationResult:
    required_symbols = set(context.explicit_symbols)
    allowed_symbols = set(required_symbols)
    allowed_symbols.update(
        _normalize_symbol(position.get("symbol"))
        for position in context.positions
        if isinstance(position, dict)
    )
    allowed_symbols.discard(None)

    tool_payload = _load_tool_payload(tool_result)
    allowed_symbols.update(_extract_symbols_from_payload(tool_payload))
    detected_symbols = _extract_known_symbols_from_text(answer)

    reasons: list[str] = []

    if (
        context.active_intent in {"put_options", "covered_calls", "spreads"}
        and len(required_symbols) == 1
    ):
        required_symbol = next(iter(required_symbols))
        if required_symbol not in answer.upper():
            reasons.append(f"Answer omitted requested symbol {required_symbol}.")

    disallowed_symbols = detected_symbols - allowed_symbols
    if disallowed_symbols:
        reasons.append(
            "Answer referenced symbols outside the validated request scope: "
            + ", ".join(sorted(disallowed_symbols))
            + "."
        )

    if context.active_intent == "monthly_income_plan":
        if "ticker i" in answer.lower():
            reasons.append("Answer referenced invalid ticker I in a monthly income workflow.")

    return AnswerValidationResult(
        is_valid=not reasons,
        reasons=reasons,
        required_symbols=required_symbols,
        allowed_symbols=allowed_symbols,
        detected_symbols=detected_symbols,
    )


def build_answer_repair_prompt(
    *,
    validation: AnswerValidationResult,
    context: RequestContext,
) -> str:
    lines = [
        "Rewrite your previous answer using only the validated request scope and any tool results already present in the conversation.",
        "Do not introduce any new ticker, company, or symbol.",
    ]
    if validation.required_symbols:
        lines.append(
            "You must explicitly address these requested symbols: "
            + ", ".join(sorted(validation.required_symbols))
            + "."
        )
    if validation.allowed_symbols:
        lines.append(
            "You may mention only these validated symbols: "
            + ", ".join(sorted(validation.allowed_symbols))
            + "."
        )
    if validation.reasons:
        lines.append("Validation failures to fix: " + " ".join(validation.reasons))
    if context.active_intent == "monthly_income_plan":
        lines.append(
            "For monthly income plans, stay within the user's provided positions, budgets, and targets."
        )
    return " ".join(lines)


def build_answer_validation_fallback(
    *,
    validation: AnswerValidationResult,
    context: RequestContext,
) -> str:
    if validation.required_symbols:
        requested = ", ".join(sorted(validation.required_symbols))
        return (
            f"I couldn't validate the drafted answer reliably for {requested}. "
            "Please retry the request so I can answer strictly from the validated tool results."
        )
    if context.active_intent == "monthly_income_plan":
        return (
            "I couldn't validate the drafted monthly income plan reliably from the current tool results. "
            "Please retry after confirming your positions or cash budget."
        )
    return (
        "I couldn't validate the drafted answer reliably against the parsed request and tool results. "
        "Please retry the request."
    )
