from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from api.models import Symbol


@dataclass
class RequestContext:
    raw_query: str
    current_intent: str | None = None
    active_intent: str | None = None
    explicit_symbols: list[str] = field(default_factory=list)
    ambiguous_symbols: list[str] = field(default_factory=list)
    positions: list[dict[str, Any]] = field(default_factory=list)
    monthly_income_target: float | None = None
    cash_budget: float | None = None
    price_filters: dict[str, float] = field(default_factory=dict)
    market_scan_requested: bool = False
    comparison_requested: bool = False
    max_dte: int | None = None
    max_delta: float | None = None
    min_roi: float | None = None
    max_risk: float | None = None
    directional_view: str | None = None
    risk_profile: str | None = None
    spread_type: str | None = None
    explanation_requested: bool = False
    actionable_analysis_requested: bool = False
    semantic_parse_used: bool = False
    source_user_messages: list[str] = field(default_factory=list)


@dataclass
class RouteDecision:
    kind: str
    tool_name: str | None = None
    tool_args: dict[str, Any] = field(default_factory=dict)
    clarification_message: str | None = None
    reason: str | None = None
    defaults_applied: list[str] = field(default_factory=list)


COMMON_WORD_TICKERS = frozenset(
    {
        "A",
        "I",
        "AM",
        "AT",
        "BE",
        "BY",
        "DO",
        "GO",
        "IN",
        "IS",
        "IT",
        "ME",
        "MY",
        "NO",
        "OF",
        "ON",
        "OR",
        "SO",
        "TO",
        "UP",
        "US",
        "WE",
    }
)


def looks_like_common_word(symbol: str) -> bool:
    return bool(symbol) and symbol.upper() in COMMON_WORD_TICKERS


def to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def to_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def parse_scaled_number(number_text: Any, suffix_text: Any = None) -> float | None:
    value = to_float(number_text)
    if value is None:
        return None

    suffix = str(suffix_text or "").strip().lower()
    if suffix == "k":
        return value * 1_000
    if suffix == "m":
        return value * 1_000_000
    return value


def extract_numeric_value(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"(-?\d+(?:\.\d+)?)\s*([kKmM])?", text)
    if not match:
        return None
    return parse_scaled_number(match.group(1), match.group(2))


def normalize_delta_value(value: Any) -> float | None:
    numeric = extract_numeric_value(value)
    if numeric is None:
        return None
    if numeric > 1:
        numeric = numeric / 100 if numeric <= 100 else numeric
    numeric = abs(numeric)
    if 0 < numeric <= 1:
        return numeric
    return None


def resolve_cash_secured_budget(*values: float | None) -> float | None:
    candidates = [value for value in values if value is not None and value > 0]
    if not candidates:
        return None
    return min(candidates)


def extract_user_messages_from_history(
    history: list[dict[str, Any]] | None,
) -> list[str]:
    if not history:
        return []

    messages = []
    for item in history:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            messages.append(content.strip())
    return messages


def extract_intent_from_query(query: str) -> str | None:
    if not query:
        return None

    normalized_query = " ".join(str(query).split()).lower()

    if re.search(
        r"\b(monthly income plan|income plan|reliable income|consistent income|portfolio income|monthly income target)\b",
        normalized_query,
    ):
        return "monthly_income_plan"

    if re.search(
        r"\b(cash[-\s]?secured puts?|csp\b|wheel strategy|wheel\b|sell(?:ing)? puts?|put ideas?|put opportunities?|short puts?|puts?)\b",
        normalized_query,
    ):
        return "put_options"

    if re.search(
        r"\b(covered calls?|sell(?:ing)? calls? against|call[-\s]?away|call income)\b",
        normalized_query,
    ):
        return "covered_calls"

    if re.search(
        r"\b(credit spreads?|debit spreads?|vertical spreads?|bull put spreads?|bear call spreads?|bull call spreads?|bear put spreads?|iron condors?|iron butterflies?|spread ideas?|spread setups?|spread trades?|defined[-\s]?risk)\b",
        normalized_query,
    ):
        return "spreads"

    return None


def extract_ambiguous_common_word_symbols_from_query(query: str) -> list[str]:
    if not query:
        return []

    cue_pattern = re.compile(
        r"\b(for|ticker|tickers|symbol|symbols|stock|stocks|compare|versus|vs\.?|between|evaluate|analyze|screen|show|list|rank|idea|ideas|put|puts|call|calls|spread|spreads|wheel|covered)\b",
        re.IGNORECASE,
    )
    separator_pattern = re.compile(r"^\s*(?:,|and\b|or\b|vs\.?\b|versus\b)", re.IGNORECASE)

    ambiguous_symbols: list[str] = []
    seen: set[str] = set()

    for match in re.finditer(r"\b([A-Z]{1,3})\b", query):
        symbol = str(match.group(1) or "").strip().upper()
        if not looks_like_common_word(symbol) or symbol in seen:
            continue

        prefix = query[max(0, match.start() - 30):match.start()]
        suffix = query[match.end():match.end() + 20]
        is_explicit_candidate = bool(
            cue_pattern.search(prefix)
            or cue_pattern.search(suffix)
            or separator_pattern.search(suffix)
            or re.fullmatch(rf"\s*{re.escape(symbol)}\s*", query)
        )
        if not is_explicit_candidate:
            continue

        ambiguous_symbols.append(symbol)
        seen.add(symbol)

    return ambiguous_symbols


def query_requests_market_scan(query: str) -> bool:
    if not query:
        return False
    normalized_query = " ".join(str(query).split()).lower()
    return bool(
        re.search(
            r"\b(best|top|screen|scan|rank|ranking|ideas|opportunities|across the market|across all stocks|all tracked symbols)\b",
            normalized_query,
        )
    )


def query_requests_comparison(query: str) -> bool:
    if not query:
        return False
    normalized_query = " ".join(str(query).split()).lower()
    return bool(
        re.search(
            r"\b(compare|comparison|better|best among|rank|ranking|which one|which is better|vs\.?|versus)\b",
            normalized_query,
        )
    )


def query_requests_explanation(query: str) -> bool:
    if not query:
        return False
    normalized_query = " ".join(str(query).split()).lower()
    return bool(
        re.search(
            r"\b(what is|what are|how do|how does|how to|explain|when should|why|difference between|pros and cons|tax|taxes|risk|risks)\b",
            normalized_query,
        )
    )


def query_requests_actionable_analysis(query: str) -> bool:
    if not query:
        return False
    normalized_query = " ".join(str(query).split()).lower()
    return bool(
        re.search(
            r"\b(show|give|find|list|screen|scan|rank|best|top|ideas|opportunities|suggest|recommend|build|create|evaluate|analyze|compare|which|look for)\b",
            normalized_query,
        )
    )


def query_mentions_options_domain(query: str) -> bool:
    if not query:
        return False
    normalized_query = " ".join(str(query).split()).lower()
    return bool(
        re.search(
            r"\b(option|options|income|premium|cash|buying power|cash-secured|covered call|covered calls|put|puts|call|calls|wheel|spread|spreads|collateral|assignment)\b",
            normalized_query,
        )
    )


def extract_owned_positions_from_query(query: str) -> list[dict[str, Any]]:
    if not query:
        return []

    matches = list(
        re.finditer(
            r"\b([A-Za-z][A-Za-z0-9.\-]{0,9})\b\s*(?:at|@)\s*\$?\s*(\d+(?:\.\d+)?)",
            query,
        )
    )
    global_shares = None
    shares_each_match = re.search(r"\b(\d+(?:\.\d+)?)\s+shares?\s+each\b", query, re.IGNORECASE)
    if shares_each_match:
        global_shares = to_int(shares_each_match.group(1))

    positions = []
    seen_symbols = set()
    ownership_hint_pattern = re.compile(
        r"\b(own|owned|hold|holding|bought|position|positions|shares?)\b",
        re.IGNORECASE,
    )

    for match in matches:
        symbol = str(match.group(1) or "").strip().upper()
        cost_basis = to_float(match.group(2))
        if not symbol or cost_basis is None or symbol in seen_symbols:
            continue

        prefix = query[max(0, match.start() - 40):match.start()]
        if not positions and not ownership_hint_pattern.search(prefix):
            continue

        suffix = query[match.end():match.end() + 30]
        shares_owned = None
        shares_nearby_match = re.search(
            r"^\s*(?:,|-)?\s*(\d+(?:\.\d+)?)\s+shares?\b",
            suffix,
            re.IGNORECASE,
        )
        if shares_nearby_match:
            shares_owned = to_int(shares_nearby_match.group(1))
        elif global_shares is not None:
            shares_owned = global_shares

        position = {"symbol": symbol, "cost_basis": cost_basis}
        if shares_owned is not None:
            position["shares_owned"] = shares_owned
        positions.append(position)
        seen_symbols.add(symbol)

    quantity_patterns = [
        r"\b(?:own|owned|hold|holding|bought)\s+(\d+(?:\.\d+)?)\s+shares?\s+of\s+([A-Za-z][A-Za-z0-9.\-]{0,9})\b",
        r"\b([A-Za-z][A-Za-z0-9.\-]{0,9})\b\s*(?:,|-)?\s*(\d+(?:\.\d+)?)\s+shares?\b",
        r"\b(\d+(?:\.\d+)?)\s+shares?\s+of\s+([A-Za-z][A-Za-z0-9.\-]{0,9})\b",
    ]
    for pattern in quantity_patterns:
        for match in re.finditer(pattern, query, re.IGNORECASE):
            first = str(match.group(1) or "").strip()
            second = str(match.group(2) or "").strip()
            if re.fullmatch(r"\d+(?:\.\d+)?", first):
                shares_owned = to_int(first)
                symbol = second.upper()
            else:
                symbol = first.upper()
                shares_owned = to_int(second)

            if (
                not symbol
                or shares_owned is None
                or symbol in seen_symbols
                or looks_like_common_word(symbol)
            ):
                continue

            positions.append({"symbol": symbol, "shares_owned": shares_owned})
            seen_symbols.add(symbol)

    return positions


def extract_underlying_price_filters_from_query(query: str) -> dict[str, float]:
    if not query:
        return {}

    normalized_query = " ".join(str(query).split())
    amount_pattern = r"\$?\s*(\d+(?:\.\d+)?)\s*(?:\$|dollars?)?"
    scope_pattern = r"(?:stocks?|companies|tickers?|names|underlyings?)"

    range_patterns = [
        rf"\b{scope_pattern}\b(?:\s+\w+){{0,4}}\s+(?:priced\s+)?between\s*{amount_pattern}\s+and\s*{amount_pattern}\b",
        rf"\b{scope_pattern}\b(?:\s+\w+){{0,4}}\s+(?:priced\s+)?from\s*{amount_pattern}\s+to\s*{amount_pattern}\b",
        rf"\b(?:priced|trading)\s+between\s*{amount_pattern}\s+and\s*{amount_pattern}\b",
        rf"\b(?:priced|trading)\s+from\s*{amount_pattern}\s+to\s*{amount_pattern}\b",
    ]
    for pattern in range_patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        first_amount = to_float(match.group(1))
        second_amount = to_float(match.group(2))
        if first_amount is None or second_amount is None:
            continue
        min_price, max_price = sorted((first_amount, second_amount))
        return {"min_price": min_price, "max_price": max_price}

    max_patterns = [
        rf"\b{scope_pattern}\b(?:\s+\w+){{0,4}}\s+(?:priced\s+)?(?:below|under|less than|up to|at most|no more than)\s*{amount_pattern}\b",
        rf"\b(?:priced|trading)\s+(?:below|under|less than|up to|at most|no more than)\s*{amount_pattern}\b",
    ]
    for pattern in max_patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        max_price = to_float(match.group(1))
        if max_price is not None:
            return {"max_price": max_price}

    min_patterns = [
        rf"\b{scope_pattern}\b(?:\s+\w+){{0,4}}\s+(?:priced\s+)?(?:above|over|more than|greater than|at least|no less than)\s*{amount_pattern}\b",
        rf"\b(?:priced|trading)\s+(?:above|over|more than|greater than|at least|no less than)\s*{amount_pattern}\b",
    ]
    for pattern in min_patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        min_price = to_float(match.group(1))
        if min_price is not None:
            return {"min_price": min_price}

    return {}


def extract_explicit_symbols_from_query(query: str) -> list[str]:
    if not query:
        return []

    candidate_matches = list(re.finditer(r"\b([A-Za-z][A-Za-z0-9.\-]{0,9})\b", query))
    if not candidate_matches:
        return []

    ignored_tokens = {
        "A", "AN", "AND", "AT", "BEST", "BUY", "CALL", "CALLS", "CSP", "DO",
        "DTE", "EXPIRATION", "FOR", "GIVE", "HAVE", "I", "IDEA", "IDEAS",
        "INCOME", "IV", "LIST", "ME", "MONTHLY", "MY", "OF", "ON", "OR",
        "PLAN", "PUT", "PUTS", "ROI", "SCREEN", "SELL", "SHOW", "STRIKE",
        "TARGET", "THE", "TO", "WHEEL", "WITH",
    }

    ordered_candidates = []
    seen_candidates = set()
    uppercase_fallback_candidates = set()

    for match in candidate_matches:
        raw_token = str(match.group(1) or "").strip()
        upper_token = raw_token.upper()
        if not upper_token or upper_token in ignored_tokens or upper_token in seen_candidates:
            continue
        seen_candidates.add(upper_token)
        ordered_candidates.append(upper_token)
        if (
            len(raw_token) >= 2
            and raw_token == upper_token
            and re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", raw_token)
        ):
            uppercase_fallback_candidates.add(upper_token)

    if not ordered_candidates:
        return []

    try:
        matched_symbols = {
            str(symbol).upper()
            for symbol in Symbol.objects.filter(
                ticker__in=ordered_candidates
            ).values_list("ticker", flat=True)
        }
    except Exception:
        matched_symbols = set()

    if matched_symbols:
        return [symbol for symbol in ordered_candidates if symbol in matched_symbols]

    return [
        symbol for symbol in ordered_candidates if symbol in uppercase_fallback_candidates
    ]


def extract_monthly_income_target_from_query(query: str) -> float | None:
    if not query:
        return None
    normalized_query = " ".join(str(query).split())
    amount_pattern = r"(\$?\s*\d+(?:\.\d+)?\s*[kKmM]?\s*(?:\$|dollars?)?)"
    patterns = [
        rf"\bmonthly income target\b(?:\s+is|\s+of)?\s*{amount_pattern}",
        rf"{amount_pattern}\s+\bmonthly income target\b",
        rf"\btarget\b(?:\s+is|\s+of)?\s*{amount_pattern}\s*(?:per month|monthly)?",
        rf"{amount_pattern}\s*(?:per month|monthly)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        amount = extract_numeric_value(match.group(1))
        if amount is not None and amount > 0:
            return amount
    return None


def extract_cash_budget_from_query(query: str) -> float | None:
    if not query:
        return None
    normalized_query = " ".join(str(query).split())
    amount_pattern = r"(\$?\s*\d+(?:\.\d+)?\s*[kKmM]?\s*(?:\$|dollars?)?)"
    patterns = [
        rf"\b(?:available cash|cash available|buying power|max cash required|cash budget|collateral budget)\b(?:\s+is|\s+of|\s+around|\s+about)?\s*{amount_pattern}",
        rf"\b(?:allocate|use|deploy)\b\s*{amount_pattern}\s*\b(?:for|into)\b(?:\s+cash-secured puts?| options?)?",
        rf"{amount_pattern}\s*\b(?:available cash|buying power|cash budget|collateral budget)\b",
        rf"\b(?:have|got)\b\s*{amount_pattern}\s*(?:in\s+)?\b(?:cash|buying power)\b",
        rf"\b(?:cash|buying power)\b(?:\s+of|\s+is|\s+around|\s+about)?\s*{amount_pattern}",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        amount = extract_numeric_value(match.group(1))
        if amount is not None and amount > 0:
            return amount
    return None


def extract_max_dte_from_query(query: str) -> int | None:
    if not query:
        return None
    normalized_query = " ".join(str(query).split())
    patterns = [
        r"\bmax(?:imum)?\s+dte\b(?:\s+of|\s+is|\s+under|\s+below|\s+up to)?\s*(\d+)\b",
        r"\b(?:under|below|up to|at most|no more than|max)\s*(\d+)\s*dte\b",
        r"\b(?:under|below|up to|at most|no more than|max)\s*(\d+)\s*days?(?:\s+to\s+expiration)?\b",
        r"\b(\d+)\s*dte\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        value = to_int(match.group(1))
        if value is not None and value > 0:
            return value
    return None


def extract_max_delta_from_query(query: str) -> float | None:
    if not query:
        return None
    normalized_query = " ".join(str(query).split())
    patterns = [
        r"\bmax(?:imum)?\s+delta\b(?:\s+of|\s+is|\s+under|\s+below|\s+up to)?\s*(-?\d+(?:\.\d+)?)\b",
        r"\bdelta\b(?:\s+under|\s+below|\s+up to|\s+at most)?\s*(-?\d+(?:\.\d+)?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        value = to_float(match.group(1))
        if value is None:
            continue
        if value > 1:
            value = value / 100 if value <= 100 else value
        value = abs(value)
        if 0 < value <= 1:
            return value
    return None


def extract_min_roi_from_query(query: str) -> float | None:
    if not query:
        return None
    normalized_query = " ".join(str(query).split())
    patterns = [
        r"\bmin(?:imum)?\s+(?:roi|yield|premium yield|return on investment)\b(?:\s+of|\s+is|\s+above|\s+over|\s+at least)?\s*(\d+(?:\.\d+)?)\s*%?",
        r"\b(?:roi|yield|premium yield)\b(?:\s+above|\s+over|\s+at least)?\s*(\d+(?:\.\d+)?)\s*%?",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        value = to_float(match.group(1))
        if value is not None and value > 0:
            return value
    return None


def extract_max_risk_from_query(query: str) -> float | None:
    if not query:
        return None
    normalized_query = " ".join(str(query).split())
    amount_pattern = r"\$?\s*(\d+(?:\.\d+)?)\s*(?:\$|dollars?)?"
    patterns = [
        rf"\bmax(?:imum)?\s+risk\b(?:\s+of|\s+is|\s+under|\s+below|\s+up to)?\s*{amount_pattern}\b",
        rf"\b(?:risk|loss)\b(?:\s+under|\s+below|\s+up to|\s+at most|<)\s*{amount_pattern}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        value = to_float(match.group(1))
        if value is not None and value > 0:
            return value
    return None


def extract_directional_view_from_query(query: str) -> str | None:
    if not query:
        return None
    normalized_query = " ".join(str(query).split()).lower()
    if re.search(r"\b(neutral|sideways|range[-\s]?bound)\b", normalized_query):
        return "neutral"
    if re.search(r"\b(bearish|downside|bear call|bear put)\b", normalized_query):
        return "bearish"
    if re.search(r"\b(bullish|upside|bull put|bull call)\b", normalized_query):
        return "bullish"
    return None


def extract_risk_profile_from_query(query: str) -> str | None:
    if not query:
        return None
    normalized_query = " ".join(str(query).split()).lower()
    if re.search(r"\b(conservative|safer|low[-\s]?risk|high probability)\b", normalized_query):
        return "conservative"
    if re.search(r"\b(aggressive|higher risk|speculative)\b", normalized_query):
        return "aggressive"
    if re.search(r"\b(balanced|moderate)\b", normalized_query):
        return "balanced"
    return None


def extract_spread_type_from_query(query: str) -> str | None:
    if not query:
        return None
    normalized_query = " ".join(str(query).split()).lower()
    if "bull put" in normalized_query:
        return "bull_put_credit_spread"
    if "bear call" in normalized_query:
        return "bear_call_credit_spread"
    if "bull call" in normalized_query:
        return "bull_call_debit_spread"
    if "bear put" in normalized_query:
        return "bear_put_debit_spread"
    if "iron condor" in normalized_query:
        return "iron_condor"
    if "iron butterfly" in normalized_query:
        return "iron_butterfly"
    if "credit spread" in normalized_query or "defined-risk income" in normalized_query:
        return "auto"
    if "debit spread" in normalized_query:
        return "auto"
    return None


def merge_positions(
    existing_positions: list[dict[str, Any]],
    new_positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged_by_symbol: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for position in [*(existing_positions or []), *(new_positions or [])]:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or "").strip().upper()
        if not symbol or looks_like_common_word(symbol):
            continue
        if symbol not in merged_by_symbol:
            merged_by_symbol[symbol] = {"symbol": symbol}
            order.append(symbol)
        for key, value in position.items():
            if key != "symbol" and value is not None:
                merged_by_symbol[symbol][key] = value
    return [merged_by_symbol[symbol] for symbol in order]


def build_request_context(
    query: str,
    history: list[dict[str, Any]] | None = None,
) -> RequestContext:
    user_messages = extract_user_messages_from_history(history)
    source_messages = [*user_messages, query]
    current_intent = extract_intent_from_query(query)
    active_intent = current_intent
    explicit_symbols: list[str] = []
    ambiguous_symbols = extract_ambiguous_common_word_symbols_from_query(query)
    positions: list[dict[str, Any]] = []
    monthly_income_target = None
    cash_budget = None
    max_dte = None
    max_delta = None
    min_roi = None
    max_risk = None
    directional_view = None
    risk_profile = None
    spread_type = None

    for message in source_messages:
        message_intent = extract_intent_from_query(message)
        if message_intent:
            active_intent = message_intent

        message_symbols = extract_explicit_symbols_from_query(message)
        if message_symbols:
            explicit_symbols = message_symbols

        message_positions = extract_owned_positions_from_query(message)
        if message_positions:
            positions = merge_positions(positions, message_positions)

        if (value := extract_monthly_income_target_from_query(message)) is not None:
            monthly_income_target = value
        if (value := extract_cash_budget_from_query(message)) is not None:
            cash_budget = value
        if (value := extract_max_dte_from_query(message)) is not None:
            max_dte = value
        if (value := extract_max_delta_from_query(message)) is not None:
            max_delta = value
        if (value := extract_min_roi_from_query(message)) is not None:
            min_roi = value
        if (value := extract_max_risk_from_query(message)) is not None:
            max_risk = value
        if (value := extract_directional_view_from_query(message)) is not None:
            directional_view = value
        if (value := extract_risk_profile_from_query(message)) is not None:
            risk_profile = value
        if (value := extract_spread_type_from_query(message)) is not None:
            spread_type = value

    return RequestContext(
        raw_query=query,
        current_intent=current_intent,
        active_intent=active_intent,
        explicit_symbols=explicit_symbols,
        ambiguous_symbols=ambiguous_symbols,
        positions=positions,
        monthly_income_target=monthly_income_target,
        cash_budget=cash_budget,
        price_filters=extract_underlying_price_filters_from_query(query),
        market_scan_requested=query_requests_market_scan(query),
        comparison_requested=query_requests_comparison(query),
        max_dte=max_dte,
        max_delta=max_delta,
        min_roi=min_roi,
        max_risk=max_risk,
        directional_view=directional_view,
        risk_profile=risk_profile,
        spread_type=spread_type,
        explanation_requested=query_requests_explanation(query),
        actionable_analysis_requested=query_requests_actionable_analysis(query),
        source_user_messages=source_messages,
    )


def has_usable_covered_call_position(positions: list[dict[str, Any]]) -> bool:
    for position in positions or []:
        shares_owned = to_int(position.get("shares_owned"))
        if shares_owned is not None and shares_owned >= 100:
            return True
    return False


def build_monthly_income_plan_clarification(context: RequestContext) -> str:
    parts = ["Need one more input to build the monthly income plan."]
    if context.monthly_income_target is not None:
        target_display = (
            int(context.monthly_income_target)
            if float(context.monthly_income_target).is_integer()
            else round(context.monthly_income_target, 2)
        )
        parts.append(f"I noted the monthly income target as `${target_display}`.")
    parts.append(
        "Provide either your owned positions with ticker and share count, or the amount of available cash / buying power you want to allocate to cash-secured puts."
    )
    return " ".join(parts)


def build_put_clarification(context: RequestContext) -> str:
    if context.market_scan_requested or context.price_filters:
        return (
            "Need one more detail to run the put scan cleanly. "
            "Specify whether you want the best cash-secured puts across the market, or name one or more tickers to evaluate directly."
        )
    return (
        "Need a ticker or a clear market-wide scan request before evaluating put opportunities. "
        "You can name one ticker, provide several tickers to compare, or ask for the best puts across the market."
    )


def build_ambiguous_symbol_clarification(
    context: RequestContext,
) -> str:
    symbols = ", ".join(context.ambiguous_symbols)
    return (
        f"I found ambiguous ticker text: {symbols}. "
        "These can be valid tickers, but they also commonly appear as ordinary words. "
        "Please confirm the exact ticker symbol before I run the analysis."
    )


def build_covered_call_clarification(context: RequestContext) -> str:
    if context.positions and not has_usable_covered_call_position(context.positions):
        return (
            "Need share counts before evaluating covered calls. "
            "Provide a ticker with at least 100 shares owned, or ask for a market-wide covered call scan instead."
        )
    return (
        "Need a ticker, owned position, or a clear market-wide scan request before evaluating covered calls. "
        "You can name one ticker, provide several tickers to compare, share your owned positions with share counts, or ask for the best covered calls across the market."
    )


def build_spread_clarification(context: RequestContext) -> str:
    return (
        "Need a ticker or a clear market-wide scan request before evaluating spread opportunities. "
        "You can name one ticker, provide several tickers to compare, or ask for the best spreads across the market."
    )


def route_request(context: RequestContext) -> RouteDecision:
    if context.ambiguous_symbols and context.active_intent in {
        "put_options",
        "covered_calls",
        "spreads",
    }:
        return RouteDecision(
            "clarification",
            clarification_message=build_ambiguous_symbol_clarification(context),
            reason="ambiguous_common_word_symbol_requires_confirmation",
        )

    if context.active_intent == "monthly_income_plan":
        if not has_usable_covered_call_position(context.positions) and context.cash_budget is None:
            return RouteDecision(
                kind="clarification",
                clarification_message=build_monthly_income_plan_clarification(context),
                reason="monthly_income_plan_missing_positions_or_cash",
            )
        tool_args: dict[str, Any] = {}
        if context.monthly_income_target is not None:
            tool_args["monthly_income_target"] = context.monthly_income_target
        if context.positions:
            tool_args["positions"] = context.positions
        if context.cash_budget is not None:
            tool_args["account_size"] = context.cash_budget
            tool_args["max_cash_required"] = context.cash_budget
        return RouteDecision("tool", "build_monthly_income_plan", tool_args, reason="deterministic_monthly_income_plan")

    if context.active_intent == "put_options":
        tool_args: dict[str, Any] = {}
        if context.cash_budget is not None:
            tool_args["account_size"] = context.cash_budget
            tool_args["max_cash_required"] = context.cash_budget
        if len(context.explicit_symbols) == 1:
            tool_args["symbol"] = context.explicit_symbols[0]
            return RouteDecision("tool", "get_put_wheel_opportunity", tool_args, reason="deterministic_single_put_symbol")
        if len(context.explicit_symbols) > 1:
            tool_args["symbols"] = context.explicit_symbols
            return RouteDecision("tool", "compare_put_candidates", tool_args, reason="deterministic_multi_put_compare")
        if context.market_scan_requested or context.price_filters or context.cash_budget is not None:
            tool_args.update(context.price_filters)
            return RouteDecision("tool", "scan_put_opportunities", tool_args, reason="deterministic_put_scan")
        return RouteDecision("clarification", clarification_message=build_put_clarification(context), reason="put_request_missing_symbol_or_scan_scope")

    if context.active_intent == "covered_calls":
        route_symbols = context.explicit_symbols or [
            str(position.get("symbol") or "").strip().upper()
            for position in context.positions
            if str(position.get("symbol") or "").strip()
        ]
        route_symbols = [symbol for symbol in route_symbols if symbol and not looks_like_common_word(symbol)]

        if len(route_symbols) > 1 or (context.comparison_requested and len(route_symbols) >= 1):
            tool_args: dict[str, Any] = {"symbols": route_symbols}
            if context.max_delta is not None:
                tool_args["max_delta"] = context.max_delta
            if context.min_roi is not None:
                tool_args["min_roi"] = context.min_roi
            return RouteDecision("tool", "compare_covered_call_candidates", tool_args, reason="deterministic_covered_call_compare")

        if len(route_symbols) == 1:
            symbol = route_symbols[0]
            matching_position = next(
                (
                    position
                    for position in context.positions
                    if str(position.get("symbol") or "").strip().upper() == symbol
                ),
                None,
            )
            tool_args = {"symbol": symbol}
            if matching_position is not None:
                if to_int(matching_position.get("shares_owned")) is not None:
                    tool_args["shares_owned"] = to_int(matching_position.get("shares_owned"))
                if to_float(matching_position.get("cost_basis")) is not None:
                    tool_args["cost_basis"] = to_float(matching_position.get("cost_basis"))
            if context.max_dte is not None:
                tool_args["max_dte"] = context.max_dte
            if context.min_roi is not None:
                tool_args["min_roi"] = context.min_roi
            return RouteDecision("tool", "get_covered_call_opportunity", tool_args, reason="deterministic_single_covered_call")

        if context.market_scan_requested:
            tool_args = {}
            if context.max_dte is not None:
                tool_args["max_dte"] = context.max_dte
            if context.max_delta is not None:
                tool_args["max_delta"] = context.max_delta
            if context.min_roi is not None:
                tool_args["min_roi"] = context.min_roi
            return RouteDecision("tool", "scan_covered_call_opportunities", tool_args, reason="deterministic_covered_call_scan")

        return RouteDecision("clarification", clarification_message=build_covered_call_clarification(context), reason="covered_call_missing_symbol_position_or_scan_scope")

    if context.active_intent == "spreads":
        tool_args: dict[str, Any] = {}
        if context.spread_type is not None:
            tool_args["spread_type"] = context.spread_type
        if context.directional_view is not None:
            tool_args["directional_view"] = context.directional_view
        if context.risk_profile is not None:
            tool_args["risk_profile"] = context.risk_profile
        if context.max_dte is not None:
            tool_args["max_dte"] = context.max_dte
        if context.max_risk is not None:
            tool_args["max_risk"] = context.max_risk

        if len(context.explicit_symbols) > 1:
            tool_args["symbols"] = context.explicit_symbols
            return RouteDecision("tool", "compare_spread_candidates", tool_args, reason="deterministic_spread_compare")
        if len(context.explicit_symbols) == 1:
            tool_args["symbol"] = context.explicit_symbols[0]
            tool_name = "compare_spread_candidates" if context.comparison_requested else "get_spread_opportunity"
            return RouteDecision("tool", tool_name, tool_args, reason="deterministic_single_spread")
        if context.market_scan_requested:
            return RouteDecision("tool", "scan_spread_opportunities", tool_args, reason="deterministic_spread_scan")
        return RouteDecision("clarification", clarification_message=build_spread_clarification(context), reason="spread_missing_symbol_or_scan_scope")

    return RouteDecision("llm", reason="fallback_to_general_agent")


def build_clarification_if_needed(context: RequestContext) -> RouteDecision | None:
    if context.ambiguous_symbols and context.active_intent in {
        "put_options",
        "covered_calls",
        "spreads",
    }:
        return RouteDecision(
            "clarification",
            clarification_message=build_ambiguous_symbol_clarification(context),
            reason="ambiguous_common_word_symbol_requires_confirmation",
        )

    if context.active_intent == "monthly_income_plan":
        if not has_usable_covered_call_position(context.positions) and context.cash_budget is None:
            return RouteDecision(
                kind="clarification",
                clarification_message=build_monthly_income_plan_clarification(context),
                reason="monthly_income_plan_missing_positions_or_cash",
            )
    return None


def _build_covered_call_route_symbols(context: RequestContext) -> list[str]:
    route_symbols = context.explicit_symbols or [
        str(position.get("symbol") or "").strip().upper()
        for position in context.positions
        if str(position.get("symbol") or "").strip()
    ]
    return [symbol for symbol in route_symbols if symbol and not looks_like_common_word(symbol)]


def _is_credit_or_auto_spread(spread_type: str | None) -> bool:
    return spread_type in {
        None,
        "",
        "auto",
        "bull_put_credit_spread",
        "bear_call_credit_spread",
        "iron_condor",
        "iron_butterfly",
    }


def apply_tool_defaults(
    tool_name: str,
    tool_args: dict[str, Any],
    context: RequestContext,
) -> tuple[dict[str, Any], list[str]]:
    merged_args = dict(tool_args or {})
    defaults_applied: list[str] = []

    if tool_name in {
        "get_spread_opportunity",
        "scan_spread_opportunities",
        "compare_spread_candidates",
    }:
        if merged_args.get("spread_type") is None:
            merged_args["spread_type"] = "auto"
            defaults_applied.append("spread_type=auto")
        if merged_args.get("directional_view") is None:
            merged_args["directional_view"] = "auto"
            defaults_applied.append("directional_view=auto")
        if merged_args.get("risk_profile") is None:
            merged_args["risk_profile"] = "balanced"
            defaults_applied.append("risk_profile=balanced")
        if merged_args.get("max_dte") is None:
            default_max_dte = 45 if _is_credit_or_auto_spread(merged_args.get("spread_type")) else 60
            merged_args["max_dte"] = default_max_dte
            defaults_applied.append(f"max_dte={default_max_dte}")

    if tool_name == "scan_put_opportunities" and not (
        context.market_scan_requested or context.price_filters or context.cash_budget is not None
    ):
        defaults_applied.append("scope=market_scan")

    if tool_name == "scan_covered_call_opportunities" and not context.market_scan_requested:
        defaults_applied.append("scope=market_scan")

    if tool_name == "scan_spread_opportunities" and not context.market_scan_requested:
        defaults_applied.append("scope=market_scan")

    return merged_args, defaults_applied


def should_use_llm_request_parser(context: RequestContext) -> bool:
    if context.ambiguous_symbols:
        return False

    if context.current_intent is None and query_mentions_options_domain(context.raw_query):
        return True

    if context.current_intent is None and context.active_intent is None:
        return False

    if context.current_intent != context.active_intent:
        return True

    if context.active_intent == "monthly_income_plan":
        return not context.positions and context.cash_budget is None

    if context.active_intent == "put_options":
        return (
            not context.explicit_symbols
            and not context.market_scan_requested
            and not context.price_filters
            and context.cash_budget is None
        )

    if context.active_intent == "covered_calls":
        return (
            not context.explicit_symbols
            and not context.positions
            and not context.market_scan_requested
        )

    if context.active_intent == "spreads":
        return (
            not context.explicit_symbols
            and not context.market_scan_requested
            and context.spread_type is None
            and context.directional_view is None
        )

    return False


def _normalize_semantic_intent(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"monthly_income_plan", "put_options", "covered_calls", "spreads"}:
        return normalized
    return None


def _normalize_semantic_symbols(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []

    normalized_symbols: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if not symbol or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            continue
        if symbol in seen:
            continue
        normalized_symbols.append(symbol)
        seen.add(symbol)
    return normalized_symbols


def _normalize_semantic_positions(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []

    positions: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        symbol = str(value.get("symbol") or "").strip().upper()
        if not symbol or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            continue
        if looks_like_common_word(symbol):
            continue

        position: dict[str, Any] = {"symbol": symbol}
        shares_owned = to_int(extract_numeric_value(value.get("shares_owned")))
        if shares_owned is not None and shares_owned > 0:
            position["shares_owned"] = shares_owned
        cost_basis = extract_numeric_value(value.get("cost_basis"))
        if cost_basis is not None and cost_basis > 0:
            position["cost_basis"] = cost_basis
        positions.append(position)
    return positions


def merge_request_context_with_semantic_parse(
    base_context: RequestContext,
    parsed_payload: dict[str, Any] | None,
) -> RequestContext:
    if not isinstance(parsed_payload, dict):
        return base_context

    parsed_intent = _normalize_semantic_intent(parsed_payload.get("intent"))
    parsed_symbols = _normalize_semantic_symbols(parsed_payload.get("explicit_symbols"))
    parsed_positions = _normalize_semantic_positions(parsed_payload.get("positions"))
    parsed_monthly_income_target = extract_numeric_value(parsed_payload.get("monthly_income_target"))
    parsed_cash_budget = extract_numeric_value(parsed_payload.get("cash_budget"))
    parsed_max_dte = to_int(extract_numeric_value(parsed_payload.get("max_dte")))
    parsed_max_delta = normalize_delta_value(parsed_payload.get("max_delta"))
    parsed_min_roi = extract_numeric_value(parsed_payload.get("min_roi"))
    parsed_max_risk = extract_numeric_value(parsed_payload.get("max_risk"))

    parsed_directional_view = str(parsed_payload.get("directional_view") or "").strip().lower()
    if parsed_directional_view not in {"bullish", "bearish", "neutral", "auto"}:
        parsed_directional_view = ""

    parsed_risk_profile = str(parsed_payload.get("risk_profile") or "").strip().lower()
    if parsed_risk_profile not in {"conservative", "balanced", "aggressive"}:
        parsed_risk_profile = ""

    parsed_spread_type = str(parsed_payload.get("spread_type") or "").strip().lower()
    if parsed_spread_type not in {
        "",
        "auto",
        "bull_put_credit_spread",
        "bear_call_credit_spread",
        "bull_call_debit_spread",
        "bear_put_debit_spread",
        "iron_condor",
        "iron_butterfly",
    }:
        parsed_spread_type = ""

    explanation_requested = bool(parsed_payload.get("explanation_requested"))
    actionable_analysis_requested = bool(parsed_payload.get("actionable_analysis_requested"))
    market_scan_requested = bool(parsed_payload.get("market_scan_requested"))
    comparison_requested = bool(parsed_payload.get("comparison_requested"))

    return replace(
        base_context,
        current_intent=base_context.current_intent or parsed_intent,
        active_intent=parsed_intent or base_context.active_intent,
        explicit_symbols=base_context.explicit_symbols or parsed_symbols,
        positions=merge_positions(base_context.positions, parsed_positions),
        monthly_income_target=base_context.monthly_income_target if base_context.monthly_income_target is not None else parsed_monthly_income_target,
        cash_budget=base_context.cash_budget if base_context.cash_budget is not None else parsed_cash_budget,
        market_scan_requested=base_context.market_scan_requested or market_scan_requested,
        comparison_requested=base_context.comparison_requested or comparison_requested,
        max_dte=base_context.max_dte if base_context.max_dte is not None else parsed_max_dte,
        max_delta=base_context.max_delta if base_context.max_delta is not None else parsed_max_delta,
        min_roi=base_context.min_roi if base_context.min_roi is not None else parsed_min_roi,
        max_risk=base_context.max_risk if base_context.max_risk is not None else parsed_max_risk,
        directional_view=base_context.directional_view or (parsed_directional_view or None),
        risk_profile=base_context.risk_profile or (parsed_risk_profile or None),
        spread_type=base_context.spread_type or (parsed_spread_type or None),
        explanation_requested=base_context.explanation_requested or explanation_requested,
        actionable_analysis_requested=base_context.actionable_analysis_requested or actionable_analysis_requested,
        semantic_parse_used=True,
    )


def decide_action(context: RequestContext) -> RouteDecision:
    clarification = build_clarification_if_needed(context)
    if clarification is not None:
        return clarification

    if context.active_intent == "monthly_income_plan":
        tool_args: dict[str, Any] = {}
        if context.monthly_income_target is not None:
            tool_args["monthly_income_target"] = context.monthly_income_target
        if context.positions:
            tool_args["positions"] = context.positions
        if context.cash_budget is not None:
            tool_args["account_size"] = context.cash_budget
            tool_args["max_cash_required"] = context.cash_budget
        return RouteDecision("tool", "build_monthly_income_plan", tool_args, reason="policy_monthly_income_plan")

    if context.active_intent == "put_options":
        tool_args: dict[str, Any] = {}
        if context.cash_budget is not None:
            tool_args["account_size"] = context.cash_budget
            tool_args["max_cash_required"] = context.cash_budget
        if len(context.explicit_symbols) == 1:
            tool_args["symbol"] = context.explicit_symbols[0]
            return RouteDecision("tool", "get_put_wheel_opportunity", tool_args, reason="policy_single_put_symbol")
        if len(context.explicit_symbols) > 1:
            tool_args["symbols"] = context.explicit_symbols
            return RouteDecision("tool", "compare_put_candidates", tool_args, reason="policy_multi_put_compare")
        if context.market_scan_requested or context.price_filters or context.cash_budget is not None or context.actionable_analysis_requested:
            tool_args.update(context.price_filters)
            tool_args, defaults_applied = apply_tool_defaults("scan_put_opportunities", tool_args, context)
            return RouteDecision("tool", "scan_put_opportunities", tool_args, reason="policy_put_scan", defaults_applied=defaults_applied)
        if context.explanation_requested:
            return RouteDecision("llm", reason="policy_put_explanation")
        return RouteDecision("llm", reason="policy_put_fallback")

    if context.active_intent == "covered_calls":
        route_symbols = _build_covered_call_route_symbols(context)

        if len(route_symbols) > 1 or (context.comparison_requested and len(route_symbols) >= 1):
            tool_args: dict[str, Any] = {"symbols": route_symbols}
            if context.max_delta is not None:
                tool_args["max_delta"] = context.max_delta
            if context.min_roi is not None:
                tool_args["min_roi"] = context.min_roi
            return RouteDecision("tool", "compare_covered_call_candidates", tool_args, reason="policy_covered_call_compare")

        if len(route_symbols) == 1:
            symbol = route_symbols[0]
            matching_position = next(
                (
                    position
                    for position in context.positions
                    if str(position.get("symbol") or "").strip().upper() == symbol
                ),
                None,
            )
            tool_args = {"symbol": symbol}
            if matching_position is not None:
                if to_int(matching_position.get("shares_owned")) is not None:
                    tool_args["shares_owned"] = to_int(matching_position.get("shares_owned"))
                if to_float(matching_position.get("cost_basis")) is not None:
                    tool_args["cost_basis"] = to_float(matching_position.get("cost_basis"))
            if context.max_dte is not None:
                tool_args["max_dte"] = context.max_dte
            if context.min_roi is not None:
                tool_args["min_roi"] = context.min_roi
            return RouteDecision("tool", "get_covered_call_opportunity", tool_args, reason="policy_single_covered_call")

        if context.market_scan_requested or context.actionable_analysis_requested:
            tool_args = {}
            if context.max_dte is not None:
                tool_args["max_dte"] = context.max_dte
            if context.max_delta is not None:
                tool_args["max_delta"] = context.max_delta
            if context.min_roi is not None:
                tool_args["min_roi"] = context.min_roi
            tool_args, defaults_applied = apply_tool_defaults("scan_covered_call_opportunities", tool_args, context)
            return RouteDecision("tool", "scan_covered_call_opportunities", tool_args, reason="policy_covered_call_scan", defaults_applied=defaults_applied)

        if context.explanation_requested:
            return RouteDecision("llm", reason="policy_covered_call_explanation")
        return RouteDecision("llm", reason="policy_covered_call_fallback")

    if context.active_intent == "spreads":
        tool_args: dict[str, Any] = {}
        if context.spread_type is not None:
            tool_args["spread_type"] = context.spread_type
        if context.directional_view is not None:
            tool_args["directional_view"] = context.directional_view
        if context.risk_profile is not None:
            tool_args["risk_profile"] = context.risk_profile
        if context.max_dte is not None:
            tool_args["max_dte"] = context.max_dte
        if context.max_risk is not None:
            tool_args["max_risk"] = context.max_risk

        if len(context.explicit_symbols) > 1:
            tool_args["symbols"] = context.explicit_symbols
            tool_args, defaults_applied = apply_tool_defaults("compare_spread_candidates", tool_args, context)
            return RouteDecision("tool", "compare_spread_candidates", tool_args, reason="policy_spread_compare", defaults_applied=defaults_applied)
        if len(context.explicit_symbols) == 1:
            tool_args["symbol"] = context.explicit_symbols[0]
            tool_name = "compare_spread_candidates" if context.comparison_requested else "get_spread_opportunity"
            tool_args, defaults_applied = apply_tool_defaults(tool_name, tool_args, context)
            return RouteDecision("tool", tool_name, tool_args, reason="policy_single_spread", defaults_applied=defaults_applied)
        if context.market_scan_requested or context.actionable_analysis_requested:
            tool_args, defaults_applied = apply_tool_defaults("scan_spread_opportunities", tool_args, context)
            return RouteDecision("tool", "scan_spread_opportunities", tool_args, reason="policy_spread_scan", defaults_applied=defaults_applied)
        if context.explanation_requested:
            return RouteDecision("llm", reason="policy_spread_explanation")
        return RouteDecision("llm", reason="policy_spread_fallback")

    return RouteDecision("llm", reason="fallback_to_general_agent")


def parse_request(
    query: str,
    history: list[dict[str, Any]] | None = None,
) -> RequestContext:
    return build_request_context(query, history)


def route_request(context: RequestContext) -> RouteDecision:
    return decide_action(context)


def augment_tool_args_from_query(tool_name: str, tool_args: dict, user_query: str) -> dict:
    merged_args = dict(tool_args or {})
    request_context = build_request_context(user_query)
    price_filters = extract_underlying_price_filters_from_query(user_query)
    cash_budget = extract_cash_budget_from_query(user_query)
    max_dte = extract_max_dte_from_query(user_query)
    max_delta = extract_max_delta_from_query(user_query)
    min_roi = extract_min_roi_from_query(user_query)
    max_risk = extract_max_risk_from_query(user_query)
    directional_view = extract_directional_view_from_query(user_query)
    risk_profile = extract_risk_profile_from_query(user_query)
    spread_type = extract_spread_type_from_query(user_query)

    if tool_name == "scan_put_opportunities" and price_filters:
        merged_args.update(price_filters)
    if tool_name in {
        "get_put_wheel_opportunity",
        "scan_put_opportunities",
        "compare_put_candidates",
        "build_monthly_income_plan",
    } and cash_budget is not None:
        merged_args.setdefault("account_size", cash_budget)
        merged_args.setdefault("max_cash_required", cash_budget)

    if tool_name in {"scan_put_opportunities", "compare_put_candidates"}:
        if max_delta is not None:
            merged_args.setdefault("max_delta", max_delta)
        if min_roi is not None:
            merged_args.setdefault("min_roi", min_roi)
        if max_dte is not None and tool_name == "scan_put_opportunities":
            merged_args.setdefault("max_dte", max_dte)

    if tool_name == "build_monthly_income_plan":
        monthly_income_target = extract_monthly_income_target_from_query(user_query)
        if monthly_income_target is not None and merged_args.get("monthly_income_target") is None:
            merged_args["monthly_income_target"] = monthly_income_target

    if tool_name in {
        "get_covered_call_opportunity",
        "scan_covered_call_opportunities",
        "compare_covered_call_candidates",
    }:
        if max_dte is not None:
            merged_args.setdefault("max_dte", max_dte)
        if max_delta is not None:
            merged_args.setdefault("max_delta", max_delta)
        if min_roi is not None:
            merged_args.setdefault("min_roi", min_roi)

    if tool_name in {
        "get_spread_opportunity",
        "scan_spread_opportunities",
        "compare_spread_candidates",
    }:
        if spread_type is not None:
            merged_args.setdefault("spread_type", spread_type)
        if directional_view is not None:
            merged_args.setdefault("directional_view", directional_view)
        if risk_profile is not None:
            merged_args.setdefault("risk_profile", risk_profile)
        if max_dte is not None:
            merged_args.setdefault("max_dte", max_dte)
        if max_risk is not None:
            merged_args.setdefault("max_risk", max_risk)

    merged_args, _ = apply_tool_defaults(tool_name, merged_args, request_context)
    return merged_args
