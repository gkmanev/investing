import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List

from django.conf import settings
from openai import OpenAI, OpenAIError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from api.helper import FinancialMetricsCalculator
from api.models import Symbol


SYSTEM_PROMPT = """
You are a long-term equity analyst and options trading assistant.

Always format your responses using Markdown. Use **bold** for emphasis, `## headers` to separate sections, bullet lists for flags/signals, and tables for ranked comparisons or multi-ticker data. Never return plain prose where a table or list would be clearer.

If the user asks about long-term business quality, fundamentals, moat, financial health, balance sheet, margins, ROIC, FCF, or whether the company is good to own, call analyze_stock.

The tool returns a structured report with:
- long_term_quality_score (0–100)
- classification: "High-quality compounder", "Quality (selective)", or "Higher risk"
- risk_flags: list of concrete concerns
- positive_signals: list of strengths
- Sections: cash_flow, fcf_margin, balance_sheet, profitability, capital_efficiency,
  per_share, interest_coverage, key_metrics

When interpreting results:
- Score ≥ 80 → High-quality compounder (strong buy candidate for long-term)
- Score 65–79 → Quality but selective entry point matters
- Score < 65 → Higher risk, explain why from the flags

Always cite specific numbers from the report (ROIC %, margins, score, flags).
Do not invent or estimate figures not present in the tool response.
Never give direct buy/sell recommendations — frame as analytical observations.

If the analyze_stock tool returns an error, report the exact error message to the user without rephrasing or softening it.

If the user asks about selling puts, wheel strategy, CSP, income, assignment comfort, option strike, expiration, delta, ROI, IV, or whether a specific ticker is good for put selling now, call get_put_wheel_opportunity.
- Always cite the specific ROI %, IV %, volume, current stock price, strike, delta, expiration, stock technical score, stock quality score, and put opportunity rating/score from the tool response.
- Use `technical_score` only for the stock's technical rating (`Strong Buy`, `Buy`, `Neutral`, `Sell`, `Strong Sell`).
- Use `stock_quality_score` / `quality_score` only for the underlying stock's quality score. 
- Use `rating` / `score` only for the evaluated put contract opportunity. Never call the opportunity score a technical score.
- If the tool returns no option data for the symbol, say so clearly.

If the user asks about covered calls, selling calls against owned shares, call income, call-away risk, monthly covered calls, or which call to sell on a stock they own, call get_covered_call_opportunity.
- Always cite the specific premium yield %, annualized yield %, strike, expiration, DTE, delta, IV %, bid/ask or mid premium, upside to strike %, call-away risk, stock technical score, stock quality score, and covered call rating/score from the tool response.
- Use `technical_score` only for the stock's technical rating (`Strong Buy`, `Buy`, `Neutral`, `Sell`, `Strong Sell`).
- Use `stock_quality_score` / `quality_score` only for the underlying stock's quality score.
- Use `covered_call_score`, `score`, or `rating` only for the evaluated covered call opportunity.
- If the user provided a cost basis, comment on whether the recommended strike stays above it.
- If the tool returns earnings or ex-dividend warnings, mention them explicitly.
- If the user is explicit about their intention (keep shares, maximize premium, sell at a target price, continue the wheel, or avoid assignment for tax reasons), use `covered_call_strategy` to reflect that goal.
- If the user just wants extra income and does not express a stronger preference, default to `covered_call_strategy="balanced_income"`.
- Map user intent to strategy like this:
  - keep shares, low-risk income, long-term hold, or "I'll roll if needed" -> `keep_shares_conservative`
  - decent income without actively trying to sell -> `balanced_income`
  - okay being called away, maximize premium, income over upside -> `high_premium_ok_called`
  - sell at a target price or "can I get paid to exit?" -> `exit_at_target_price` and include `target_exit_price`
  - assigned from a short put and wants to continue the wheel -> `wheel_continuation`; include `assigned_price`, `premium_received_from_put`, and `cost_basis` if known
  - avoid assignment, avoid taxable sale if possible, or tax-sensitive -> `tax_sensitive`
- For `exit_at_target_price`, explicitly mention `effective_exit_price` and `gain_if_called_from_cost_basis` when available.
- For `wheel_continuation`, explicitly mention `wheel_cost_basis_before_call` and `adjusted_cost_basis_after_call` when available.
- If the response includes strategy warnings about lower premium for share retention or higher call-away risk for premium capture, surface those warnings clearly.


If the user asks for best ideas for PUTs, Wheels and CSP - Cash Secured Puts across the market, top candidates, screeners, scans, or ranked opportunities, call scan_put_opportunities.
- The tool scans all tracked symbols and returns the highest-scoring cash-secured put contracts ranked by opportunity score.
- Optional filters: limit (number of results), min_roi (%), max_dte (days to expiration), min_price, max_price, max_delta.

When interpreting scan_put_opportunities results:
- Present results as a ranked list with ticker, strike, expiration, IV %, ROI %, stock quality score, stock technical score, delta, and put opportunity rating/score.
- Highlight any warnings (wide spreads, low liquidity, earnings risk) for each candidate.
- Use `technical_score` only for the stock's technical rating and `stock_quality_score` / `quality_score` only for the underlying stock's quality score.
- Use `rating` / `score` only for the evaluated put contract opportunity.
- End with a short conclusion paragraph that comments on the overall ROI range across the presented candidates and the strength of their fundamentals (quality scores and classifications). Note any standouts — highest ROI, strongest fundamentals, or any concerns worth flagging.

If the user asks to compare multiple tickers for put selling, cash-secured puts, CSP, option income, assignment comfort, or the wheel strategy, call compare_put_candidates.

Use compare_put_candidates when the user provides two or more tickers and wants to know which one is better, safer, more attractive, more conservative, or more suitable for the wheel strategy.

Examples:
- "Compare MSFT, AXP, and JNJ for wheel."
- "Which is better for selling puts: NVDA or AMD?"
- "Rank these tickers for CSP: KO, PG, PEP, MCD."
- "Compare these stocks for monthly income."
- "Which one has the best assignment comfort?"

Do not call scan_put_opportunities if the user gives a specific list of tickers.
Use scan_put_opportunities only when the user asks for the best candidates across the whole market or all tracked symbols.

When interpreting compare_put_candidates results:
- Present the results as a ranked comparison.
- Clearly separate option attractiveness from assignment comfort.
- Do not choose only by ROI. A high ROI with poor fundamentals, weak technicals, wide spreads, or earnings risk should not be ranked as conservative.
- Prefer candidates with strong underlying quality, acceptable technical trend, reasonable delta, sufficient downside buffer, good liquidity, and no near-term earnings risk.
- If a candidate has high premium but poor assignment comfort, say that clearly.

Always cite the specific numbers returned by the tool:
- ticker
- current stock price
- strike
- expiration
- DTE
- delta
- IV %
- ROI %
- downside buffer %
- volume and open interest, if available
- bid/ask spread, if available
- stock quality score
- technical score
- opportunity score/rating
- assignment comfort score/label, if available
- warnings

Use technical_score only for the stock's technical rating.
Use stock_quality_score or quality_score only for the underlying company's quality score.
Use opportunity_score or rating only for the evaluated put contract opportunity.
Use assignment_comfort_score only for comfort with owning the stock if assigned.

Final answer format for compare_put_candidates:
1. Short verdict: which ticker looks best overall and why.
2. Ranked table of candidates.
3. Notes on each candidate:
   - best use case
   - assignment comfort
   - main risk
4. Bottom line:
   - best conservative candidate
   - best premium candidate
   - candidate to avoid or keep on watchlist

If the tool returns no option data for one or more symbols, include those symbols in a separate "No usable option data" section and do not invent values.


"""

# Tools the agent can call (maps to your Django business logic)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "analyze_stock",
            "description": (
                "Fetch and analyze the long-term investment quality of a publicly traded company. "
                "Returns a full financial report including: FCF metrics, profitability, balance sheet health, "
                "ROIC, per-share growth, interest coverage, a quality score (0–100), "
                "classification (High-quality compounder / Quality / Higher risk), "
                "and a list of risk flags and positive signals. "
                "Use this whenever the user asks about a stock, company financials, or investment quality. Or fundamentals, or PUT/wheel option suitability etc... "
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Stock ticker symbol e.g. AAPL, MSFT, NVDA",
                    }
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_put_wheel_opportunity",
            "description": (
                        "Fetch current option data from the database for a symbol and evaluate the best available "
                        "cash-secured put / wheel opportunity. The function ranks available put contracts using "
                        "DTE, delta, ROI, downside buffer, bid/ask spread, liquidity, RSI, quality score, and earnings risk. "
                        "Returns the best put contract, top candidates, rating, score, warnings, and supporting context. "
                        "Use this when the user asks about put selling, cash-secured puts, the wheel strategy, "
                        "or option income opportunities for a specific ticker."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Stock ticker symbol e.g. AAPL, MSFT, NVDA",
                    }
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_covered_call_opportunity",
            "description": (
                "Fetch current call option data for a symbol and evaluate the best covered call opportunities. "
                "Ranks call contracts using DTE, delta, ROI, annualized yield, upside to strike, bid/ask spread, "
                "liquidity, IV, technical score, stock quality score, earnings risk, ex-dividend risk, and "
                "call-away risk. Use this when the user asks which covered call to sell for a specific stock."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Stock ticker symbol, e.g. AAPL, MSFT, NVDA",
                    },
                    "shares_owned": {
                        "type": "integer",
                        "description": "Number of shares owned. Optional. Default assumes 100 shares.",
                    },
                    "cost_basis": {
                        "type": "number",
                        "description": "User's average cost basis per share. Optional.",
                    },
                    "assigned_price": {
                        "type": "number",
                        "description": "Assigned share price from a short put. Optional. Useful for wheel continuation.",
                    },
                    "premium_received_from_put": {
                        "type": "number",
                        "description": "Premium already collected from the short put that led to assignment. Optional.",
                    },
                    "target_delta": {
                        "type": "number",
                        "description": "Preferred call delta, e.g. 0.20, 0.30, 0.40. Optional.",
                    },
                    "max_dte": {
                        "type": "integer",
                        "description": "Maximum days to expiration. Optional.",
                    },
                    "min_roi": {
                        "type": "number",
                        "description": "Minimum premium yield / ROI percentage. Optional.",
                    },
                    "style": {
                        "type": "string",
                        "enum": ["conservative", "balanced", "income", "aggressive"],
                        "description": "Covered call style. Optional.",
                    },
                    "covered_call_strategy": {
                        "type": "string",
                        "enum": [
                            "keep_shares_conservative",
                            "balanced_income",
                            "high_premium_ok_called",
                            "exit_at_target_price",
                            "wheel_continuation",
                            "tax_sensitive",
                        ],
                        "description": (
                            "Intent-based covered call strategy. Use this when the user expresses a clear goal "
                            "such as keeping shares, maximizing premium, exiting near a target price, continuing "
                            "the wheel, or minimizing assignment probability."
                        ),
                    },
                    "target_exit_price": {
                        "type": "number",
                        "description": (
                            "Desired stock sale price per share. Optional, but required when "
                            "`covered_call_strategy` is `exit_at_target_price`."
                        ),
                    },
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_put_opportunities",
            "description": (
                "Scan all tracked symbols in the database and return the best cash-secured put (CSP) "
                "opportunities ranked by composite score. Use this when the user asks for today's best puts, "
                "top put opportunities across all stocks, or wants to compare puts across multiple tickers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of top results to return. Default 10.",
                    },
                    "min_score": {
                        "type": "number",
                        "description": "Minimum composite score (0–100) to include. Default 50 (filters out Avoid-rated contracts).",
                    },
                    "min_roi": {
                        "type": "number",
                        "description": "Minimum ROI percentage to include. Optional.",
                    },
                    "max_dte": {
                        "type": "integer",
                        "description": "Maximum days to expiration to include. Optional.",
                    },
                    "min_price": {
                        "type": "number",
                        "description": "Minimum underlying stock price to include. Optional.",
                    },
                    "max_price": {
                        "type": "number",
                        "description": "Maximum underlying stock price to include. Optional.",
                    },
                    "max_delta": {
                        "type": "number",
                        "description": "Maximum absolute delta to include (for example 0.30). Optional.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_put_candidates",
            "description": "Compare put/wheel opportunities for multiple tickers and rank them by option attractiveness, assignment comfort, risk, and liquidity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of ticker symbols, e.g. ['MSFT', 'JNJ', 'NVDA']",
                    },
                    "max_delta": {
                        "type": "number",
                        "description": "Maximum absolute delta, e.g. 0.30",
                    },
                    "min_roi": {
                        "type": "number",
                        "description": "Minimum ROI percentage",
                    },
                    "min_quality_score": {
                        "type": "number",
                        "description": "Minimum stock quality score",
                    },
                },
                "required": ["symbols"],
            },
        },
    }
]

class FMPClient:
    def __init__(self):
        self.fmp_api_key = getattr(settings, "FINANCIAL_MODELING_API_KEY", "")
        if not self.fmp_api_key:
            raise ValueError("FINANCIAL_MODELING_API_KEY is missing in Django settings")
        self.fmp_base_url = "https://financialmodelingprep.com/stable"

    def _fetch_json(self, url: str) -> Any:
        max_attempts = 4
        wait = 15
        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    body = response.read()
                return json.loads(body.decode("utf-8")) if body else None
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = e.headers.get("Retry-After")
                    sleep_secs = int(retry_after) if retry_after else wait
                    if attempt == max_attempts:
                        raise Exception(f"FMP request failed (429) after {max_attempts} attempts")
                    time.sleep(sleep_secs)
                    wait *= 2
                else:
                    raise Exception(f"FMP request failed ({e.code}): {e.reason}")

    def fetch_financial_data(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()
        base = self.fmp_base_url
        key = self.fmp_api_key
        statements = {
            "balance_sheet": f"{base}/balance-sheet-statement?symbol={symbol}&apikey={key}",
            "income_statement": f"{base}/income-statement?symbol={symbol}&apikey={key}",
            "cash_flow": f"{base}/cash-flow-statement?symbol={symbol}&apikey={key}",
        }
        financial_data: Dict[str, Any] = {}
        missing_statements: List[str] = []
        for stmt_key, path in statements.items():
            statement_data = self._fetch_json(path)
            financial_data[stmt_key] = statement_data
            if not statement_data:
                missing_statements.append(stmt_key)
            time.sleep(1.5)
        if not financial_data or missing_statements:
            missing = ", ".join(missing_statements) if missing_statements else "all statements"
            raise Exception(f"Incomplete financial data for {symbol}; missing: {missing}")
        return financial_data


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _to_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _to_int(value):
    try:
        if value is None or value == "":
            return None
        return int(float(value))
    except Exception:
        return None


def _parse_date(value):
    if not value:
        return None

    if isinstance(value, date):
        return value

    if isinstance(value, datetime):
        return value.date()

    try:
        s = str(value).replace("Z", "+00:00")
        # Handle compact YYYYMMDD integer format (e.g. 20260618)
        if re.fullmatch(r"\d{8}", s):
            return datetime.strptime(s, "%Y%m%d").date()
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def _extract_put_contracts(option_data):
    """
    Supports several common structures:

    1. option_data = {"puts": [...]}
    2. option_data = {"contracts": [...]}
    3. option_data = [{"type": "put", ...}, ...]
    4. option_data = {"2026-06-19": {"puts": [...]}}
    5. option_data = {single contract dict with "strike", "option_type", ...}
    """

    contracts = []

    if not option_data:
        return contracts

    if isinstance(option_data, list):
        raw_contracts = option_data

    elif isinstance(option_data, dict):
        raw_contracts = []

        # Single contract stored directly as a dict
        if "strike" in option_data and not any(
            isinstance(option_data.get(k), list) for k in ("puts", "contracts", "options")
        ):
            raw_contracts = [option_data]

        elif isinstance(option_data.get("puts"), list):
            raw_contracts.extend(option_data["puts"])

        if isinstance(option_data.get("contracts"), list):
            raw_contracts.extend(option_data["contracts"])

        if isinstance(option_data.get("options"), list):
            raw_contracts.extend(option_data["options"])

        # Expiration-keyed structure (skip keys already handled above)
        _KNOWN_KEYS = {"puts", "contracts", "options"}
        for exp_key, exp_value in option_data.items():
            if exp_key in _KNOWN_KEYS:
                continue
            if isinstance(exp_value, dict) and isinstance(exp_value.get("puts"), list):
                for contract in exp_value["puts"]:
                    if isinstance(contract, dict):
                        c = dict(contract)
                        c.setdefault("expiration", exp_key)
                        raw_contracts.append(c)

            elif isinstance(exp_value, list):
                for contract in exp_value:
                    if isinstance(contract, dict):
                        c = dict(contract)
                        c.setdefault("expiration", exp_key)
                        raw_contracts.append(c)
    else:
        return contracts

    # Expand alternatives nested inside each contract
    extras = []
    for c in raw_contracts:
        if isinstance(c, dict):
            for alt in c.get("alternatives") or []:
                if isinstance(alt, dict):
                    extras.append(alt)
    raw_contracts.extend(extras)

    for c in raw_contracts:
        if not isinstance(c, dict):
            continue

        contract_type = str(
            c.get("type")
            or c.get("option_type")
            or c.get("contract_type")
            or c.get("right")
            or ""
        ).lower()

        # If type is missing, assume it is a put only if it came from a "puts" list.
        if contract_type and contract_type not in ["put", "p"]:
            continue

        expiration = (
            c.get("expiration")
            or c.get("expiration_date")
            or c.get("expiry")
            or c.get("exp")
            or c.get("date")
        )

        strike = _to_float(c.get("strike") or c.get("strike_price"))
        bid = _to_float(c.get("bid"))
        ask = _to_float(c.get("ask"))
        last = _to_float(c.get("last") or c.get("last_price"))
        mid = _to_float(c.get("mid") or c.get("mark"))

        if mid is None and bid is not None and ask is not None and ask > 0:
            mid = round((bid + ask) / 2, 4)

        delta = _to_float(c.get("delta"))
        iv = _to_float(c.get("iv") or c.get("implied_volatility"))
        volume = _to_int(c.get("volume"))
        open_interest = _to_int(c.get("open_interest") or c.get("oi"))

        contracts.append({
            "raw": c,
            "expiration": expiration,
            "expiration_date": _parse_date(expiration),
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "last": last,
            "mid": mid,
            "delta": delta,
            "iv": iv,
            "volume": volume,
            "open_interest": open_interest,
        })
    return contracts


def _extract_call_contracts(option_data):
    """
    Mirrors _extract_put_contracts but filters for call options.

    Supports the same structures:
    1. option_data = {"calls": [...]}
    2. option_data = {"contracts": [...]}
    3. option_data = [{"type": "call", ...}, ...]
    4. option_data = {"2026-06-19": {"calls": [...]}}
    5. option_data = {single contract dict with "strike", "option_type": "call", ...}
    """

    contracts = []

    if not option_data:
        return contracts

    if isinstance(option_data, list):
        raw_contracts = option_data

    elif isinstance(option_data, dict):
        raw_contracts = []

        if "strike" in option_data and not any(
            isinstance(option_data.get(k), list) for k in ("calls", "contracts", "options")
        ):
            raw_contracts = [option_data]

        elif isinstance(option_data.get("calls"), list):
            raw_contracts.extend(option_data["calls"])

        if isinstance(option_data.get("contracts"), list):
            raw_contracts.extend(option_data["contracts"])

        if isinstance(option_data.get("options"), list):
            raw_contracts.extend(option_data["options"])

        _KNOWN_KEYS = {"calls", "contracts", "options"}
        for exp_key, exp_value in option_data.items():
            if exp_key in _KNOWN_KEYS:
                continue
            if isinstance(exp_value, dict) and isinstance(exp_value.get("calls"), list):
                for contract in exp_value["calls"]:
                    if isinstance(contract, dict):
                        c = dict(contract)
                        c.setdefault("expiration", exp_key)
                        raw_contracts.append(c)

            elif isinstance(exp_value, list):
                for contract in exp_value:
                    if isinstance(contract, dict):
                        c = dict(contract)
                        c.setdefault("expiration", exp_key)
                        raw_contracts.append(c)
    else:
        return contracts

    extras = []
    for c in raw_contracts:
        if isinstance(c, dict):
            for alt in c.get("alternatives") or []:
                if isinstance(alt, dict):
                    extras.append(alt)
    raw_contracts.extend(extras)

    for c in raw_contracts:
        if not isinstance(c, dict):
            continue

        contract_type = str(
            c.get("type")
            or c.get("option_type")
            or c.get("contract_type")
            or c.get("right")
            or ""
        ).lower()

        if contract_type and contract_type not in ["call", "c"]:
            continue

        expiration = (
            c.get("expiration")
            or c.get("expiration_date")
            or c.get("expiry")
            or c.get("exp")
            or c.get("date")
        )

        strike = _to_float(c.get("strike") or c.get("strike_price"))
        bid = _to_float(c.get("bid"))
        ask = _to_float(c.get("ask"))
        last = _to_float(c.get("last") or c.get("last_price"))
        mid = _to_float(c.get("mid") or c.get("mark"))

        if mid is None and bid is not None and ask is not None and ask > 0:
            mid = round((bid + ask) / 2, 4)

        delta = _to_float(c.get("delta"))
        iv = _to_float(c.get("iv") or c.get("implied_volatility"))
        volume = _to_int(c.get("volume"))
        open_interest = _to_int(c.get("open_interest") or c.get("oi"))

        contracts.append({
            "raw": c,
            "expiration": expiration,
            "expiration_date": _parse_date(expiration),
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "last": last,
            "mid": mid,
            "delta": delta,
            "iv": iv,
            "volume": volume,
            "open_interest": open_interest,
        })
    return contracts


def _dedupe_preserve_order(items):
    seen = set()
    output = []
    for item in items or []:
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def _normalize_covered_call_style(style):
    value = str(style or "balanced").strip().lower()
    if value in {"conservative", "balanced", "income", "aggressive"}:
        return value
    return "balanced"


def _normalize_covered_call_strategy(strategy):
    value = str(strategy or "").strip().lower()
    valid = {
        "keep_shares_conservative",
        "balanced_income",
        "high_premium_ok_called",
        "exit_at_target_price",
        "wheel_continuation",
        "tax_sensitive",
    }
    if value in valid:
        return value
    return None


def _covered_call_profile(style):
    profiles = {
        "conservative": {
            "target_delta": 0.20,
            "delta_min": 0.15,
            "delta_max": 0.25,
            "preferred_min_dte": 21,
            "preferred_max_dte": 45,
            "allow_atm": False,
            "allow_itm": False,
        },
        "balanced": {
            "target_delta": 0.30,
            "delta_min": 0.25,
            "delta_max": 0.35,
            "preferred_min_dte": 21,
            "preferred_max_dte": 45,
            "allow_atm": False,
            "allow_itm": False,
        },
        "income": {
            "target_delta": 0.38,
            "delta_min": 0.35,
            "delta_max": 0.50,
            "preferred_min_dte": 10,
            "preferred_max_dte": 45,
            "allow_atm": True,
            "allow_itm": False,
        },
        "aggressive": {
            "target_delta": 0.52,
            "delta_min": 0.45,
            "delta_max": 0.60,
            "preferred_min_dte": 7,
            "preferred_max_dte": 35,
            "allow_atm": True,
            "allow_itm": True,
        },
    }
    return profiles[_normalize_covered_call_style(style)]


def _covered_call_strategy_profile(strategy):
    strategy = _normalize_covered_call_strategy(strategy)
    if strategy is None:
        return None

    profiles = {
        "keep_shares_conservative": {
            "strategy": strategy,
            "filter_source": "strategy",
            "mapped_style": "conservative",
            "target_delta": 0.18,
            "delta_min": 0.10,
            "delta_max": 0.25,
            "preferred_min_dte": 21,
            "preferred_max_dte": 45,
            "min_upside_pct": 5.0,
            "max_upside_pct": None,
            "require_otm": True,
            "require_above_cost_basis": False,
            "allow_atm": False,
            "allow_itm": False,
        },
        "balanced_income": {
            "strategy": strategy,
            "filter_source": "strategy",
            "mapped_style": "balanced",
            "target_delta": 0.30,
            "delta_min": 0.20,
            "delta_max": 0.35,
            "preferred_min_dte": 21,
            "preferred_max_dte": 45,
            "min_upside_pct": None,
            "max_upside_pct": None,
            "require_otm": True,
            "require_above_cost_basis": False,
            "allow_atm": False,
            "allow_itm": False,
        },
        "high_premium_ok_called": {
            "strategy": strategy,
            "filter_source": "strategy",
            "mapped_style": "aggressive",
            "target_delta": 0.45,
            "delta_min": 0.35,
            "delta_max": 0.60,
            "preferred_min_dte": 14,
            "preferred_max_dte": 35,
            "min_upside_pct": -1.0,
            "max_upside_pct": 4.0,
            "require_otm": False,
            "require_above_cost_basis": False,
            "allow_atm": True,
            "allow_itm": False,
        },
        "exit_at_target_price": {
            "strategy": strategy,
            "filter_source": "strategy",
            "mapped_style": "balanced",
            "target_delta": 0.30,
            "delta_min": 0.10,
            "delta_max": 0.60,
            "preferred_min_dte": 7,
            "preferred_max_dte": 60,
            "min_upside_pct": None,
            "max_upside_pct": None,
            "require_otm": False,
            "require_above_cost_basis": False,
            "allow_atm": True,
            "allow_itm": True,
        },
        "wheel_continuation": {
            "strategy": strategy,
            "filter_source": "strategy",
            "mapped_style": "balanced",
            "target_delta": 0.30,
            "delta_min": 0.20,
            "delta_max": 0.40,
            "preferred_min_dte": 14,
            "preferred_max_dte": 45,
            "min_upside_pct": 1.0,
            "max_upside_pct": None,
            "require_otm": True,
            "require_above_cost_basis": True,
            "allow_atm": False,
            "allow_itm": False,
        },
        "tax_sensitive": {
            "strategy": strategy,
            "filter_source": "strategy",
            "mapped_style": "conservative",
            "target_delta": 0.15,
            "delta_min": 0.10,
            "delta_max": 0.20,
            "preferred_min_dte": 21,
            "preferred_max_dte": 45,
            "min_upside_pct": 5.0,
            "max_upside_pct": None,
            "require_otm": True,
            "require_above_cost_basis": False,
            "allow_atm": False,
            "allow_itm": False,
        },
    }
    return profiles[strategy]


def _resolve_covered_call_filters(*, style, covered_call_strategy):
    strategy_profile = _covered_call_strategy_profile(covered_call_strategy)
    if strategy_profile is not None:
        return strategy_profile

    style_profile = dict(_covered_call_profile(style))
    style_profile.update({
        "strategy": None,
        "filter_source": "style",
        "mapped_style": _normalize_covered_call_style(style),
        "min_upside_pct": None,
        "max_upside_pct": None,
        "require_otm": False,
        "require_above_cost_basis": False,
        "allow_atm": True,
        "allow_itm": True,
    })
    return style_profile


def _default_covered_call_strategy(style):
    if style:
        return None
    return "balanced_income"


def _classify_moneyness(stock_price, strike):
    if stock_price is None or strike is None or stock_price <= 0:
        return None
    ratio = strike / stock_price
    if ratio >= 1.01:
        return "OTM"
    if ratio <= 0.99:
        return "ITM"
    return "ATM"


def _label_call_away_risk(*, delta, upside_to_strike_pct, moneyness, technical_score):
    abs_delta = abs(delta) if delta is not None else None
    upside = upside_to_strike_pct if upside_to_strike_pct is not None else None

    if moneyness == "ITM":
        return "High"
    if abs_delta is not None and abs_delta >= 0.50:
        return "High"
    if upside is not None and upside < 2:
        return "High"
    if technical_score == "Strong Buy" and abs_delta is not None and abs_delta >= 0.35:
        return "High"

    if (
        (abs_delta is not None and abs_delta >= 0.30)
        or (upside is not None and upside < 5)
        or technical_score in {"Buy", "Strong Buy"}
        or moneyness == "ATM"
    ):
        return "Moderate"

    return "Low"


def _extract_ex_dividend_info(option_data):
    if not option_data:
        return {"data_available": False, "ex_dividend_date": None, "dividend_amount": None}

    sources = []
    if isinstance(option_data, dict):
        sources.append(option_data)
        for value in option_data.values():
            if isinstance(value, dict):
                sources.append(value)
            elif isinstance(value, list):
                sources.extend(item for item in value if isinstance(item, dict))
    elif isinstance(option_data, list):
        sources.extend(item for item in option_data if isinstance(item, dict))

    for source in sources:
        ex_div_date = _parse_date(
            source.get("ex_dividend_date")
            or source.get("exDividendDate")
            or source.get("dividend_ex_date")
            or source.get("ex_div_date")
        )
        dividend_amount = _to_float(
            source.get("dividend_amount")
            or source.get("cash_dividend_amount")
            or source.get("dividendAmount")
            or source.get("dividend")
        )
        if ex_div_date or dividend_amount is not None:
            return {
                "data_available": True,
                "ex_dividend_date": ex_div_date,
                "dividend_amount": dividend_amount,
            }

    return {"data_available": False, "ex_dividend_date": None, "dividend_amount": None}


def _build_ex_dividend_risk(
    *,
    expiration_date,
    stock_price,
    strike,
    mid,
    delta,
    option_data,
):
    info = _extract_ex_dividend_info(option_data)
    ex_dividend_date = info["ex_dividend_date"]
    dividend_amount = info["dividend_amount"]
    has_ex_dividend = False
    early_assignment_risk = "Unknown" if not info["data_available"] else "Low"

    if ex_dividend_date and expiration_date and ex_dividend_date <= expiration_date:
        has_ex_dividend = True
        intrinsic_value = max((stock_price or 0) - (strike or 0), 0)
        extrinsic_value = None
        if mid is not None:
            extrinsic_value = max(mid - intrinsic_value, 0)

        abs_delta = abs(delta) if delta is not None else None

        if intrinsic_value > 0 and dividend_amount is not None and extrinsic_value is not None:
            if dividend_amount >= extrinsic_value:
                early_assignment_risk = "High"
            elif dividend_amount >= extrinsic_value * 0.6:
                early_assignment_risk = "Moderate"
            else:
                early_assignment_risk = "Low"
        elif intrinsic_value > 0 and abs_delta is not None and abs_delta >= 0.65:
            early_assignment_risk = "Moderate"
        else:
            early_assignment_risk = "Low"

    return {
        "data_available": info["data_available"],
        "has_ex_dividend_before_expiration": has_ex_dividend if info["data_available"] else None,
        "ex_dividend_date": ex_dividend_date.isoformat() if ex_dividend_date else None,
        "dividend_amount": dividend_amount,
        "early_assignment_risk": early_assignment_risk,
    }


def _score_covered_call_contract(
    contract,
    *,
    stock_price,
    shares_owned,
    cost_basis,
    assigned_price,
    premium_received_from_put,
    target_delta,
    filter_profile,
    covered_call_strategy,
    quality_score,
    technical_score,
    next_earnings_date,
    option_data,
    today,
):
    strike = contract["strike"]
    expiration_date = contract["expiration_date"]
    bid = contract["bid"]
    ask = contract["ask"]
    mid = contract["mid"]
    delta = contract["delta"]
    volume = contract["volume"]
    open_interest = contract["open_interest"]
    iv = contract["iv"]

    if not stock_price or not strike or not expiration_date or mid is None:
        return None

    dte = (expiration_date - today).days
    if dte <= 0:
        return None

    if mid <= 0:
        return None

    premium_yield_pct = (mid / stock_price) * 100
    annualized_yield_pct = premium_yield_pct * (365 / dte)
    upside_to_strike_pct = ((strike - stock_price) / stock_price) * 100
    spread_pct = None
    if bid is not None and ask is not None and ask > 0:
        spread_pct = ((ask - bid) / ask) * 100

    moneyness = _classify_moneyness(stock_price, strike)
    call_away_risk = _label_call_away_risk(
        delta=delta,
        upside_to_strike_pct=upside_to_strike_pct,
        moneyness=moneyness,
        technical_score=technical_score,
    )
    earnings_before_exp = bool(
        next_earnings_date and today <= next_earnings_date <= expiration_date
    )
    ex_dividend_risk = _build_ex_dividend_risk(
        expiration_date=expiration_date,
        stock_price=stock_price,
        strike=strike,
        mid=mid,
        delta=delta,
        option_data=option_data,
    )

    preferred_delta = (
        target_delta if target_delta is not None else filter_profile["target_delta"]
    )
    covered_share_lots = shares_owned // 100
    wheel_cost_basis_before_call = None
    adjusted_cost_basis_after_call = None
    if assigned_price is not None and premium_received_from_put is not None:
        wheel_cost_basis_before_call = assigned_price - premium_received_from_put
        adjusted_cost_basis_after_call = wheel_cost_basis_before_call - mid

    effective_exit_price = round(strike + mid, 2)
    max_gain_if_called = round((((strike - stock_price) + mid) * 100) * covered_share_lots, 2)
    max_gain_if_called_pct = round((((strike - stock_price) + mid) / stock_price) * 100, 2)
    breakeven_after_premium = round(stock_price - mid, 2)
    gain_if_called_from_cost_basis = None
    gain_if_called_from_cost_basis_pct = None
    if cost_basis is not None:
        gain_if_called_from_cost_basis = round(
            ((effective_exit_price - cost_basis) * 100) * covered_share_lots,
            2,
        )
        if cost_basis > 0:
            gain_if_called_from_cost_basis_pct = round(
                ((effective_exit_price - cost_basis) / cost_basis) * 100,
                2,
            )

    score = 0
    reasons = []
    warnings = []
    score_breakdown = {}

    premium_score = 0
    if premium_yield_pct >= 2.0:
        premium_score += 14
        reasons.append("Premium yield is strong for a covered call.")
    elif premium_yield_pct >= 1.25:
        premium_score += 12
        reasons.append("Premium yield is solid for a monthly-style covered call.")
    elif premium_yield_pct >= 0.8:
        premium_score += 9
    elif premium_yield_pct >= 0.5:
        premium_score += 6
    else:
        premium_score += 3
        warnings.append("Premium yield is light.")

    if annualized_yield_pct >= 18:
        premium_score += 6
    elif annualized_yield_pct >= 12:
        premium_score += 5
    elif annualized_yield_pct >= 8:
        premium_score += 3
    elif annualized_yield_pct >= 5:
        premium_score += 1

    if filter_profile["preferred_min_dte"] <= dte <= filter_profile["preferred_max_dte"]:
        premium_score += 1
    elif dte > 75:
        warnings.append("DTE is long, which ties up shares for longer.")

    premium_score = min(premium_score, 20)
    score += premium_score
    score_breakdown["premium_yield_roi"] = premium_score

    risk_score = 0
    abs_delta = abs(delta) if delta is not None else None
    if abs_delta is None:
        risk_score += 8
        warnings.append("Delta is missing, so call-away risk is harder to judge.")
    else:
        if abs_delta <= 0.20:
            risk_score += 20
            reasons.append("Delta implies relatively low call-away risk.")
        elif abs_delta <= 0.30:
            risk_score += 17
        elif abs_delta <= 0.40:
            risk_score += 13
            warnings.append("Delta suggests moderate call-away risk.")
        elif abs_delta <= 0.50:
            risk_score += 8
            warnings.append("Delta is aggressive for a covered call.")
        else:
            risk_score += 4
            warnings.append("Delta implies elevated call-away risk.")

        if preferred_delta is not None:
            delta_gap = abs(abs_delta - preferred_delta)
            if delta_gap <= 0.05:
                risk_score += 2
            elif delta_gap >= 0.15:
                risk_score -= 2

    if dte > 60:
        risk_score -= 2
    elif dte < 7:
        risk_score -= 1

    risk_score = max(0, min(risk_score, 20))
    score += risk_score
    score_breakdown["delta_call_away_risk"] = risk_score

    upside_score = 0
    if upside_to_strike_pct >= 8:
        upside_score += 15
        reasons.append("Strike leaves ample upside before shares are called away.")
    elif upside_to_strike_pct >= 5:
        upside_score += 12
    elif upside_to_strike_pct >= 3:
        upside_score += 8
    elif upside_to_strike_pct >= 1:
        upside_score += 4
        warnings.append("Strike is fairly close to the current stock price.")
    elif upside_to_strike_pct >= 0:
        upside_score += 2
        warnings.append("Very limited upside to the strike.")
    else:
        warnings.append("Call strike is below the current stock price.")

    if cost_basis is not None and strike < cost_basis:
        upside_score = min(upside_score, 2)
        warnings.append("Strike is below the provided cost basis.")
    score += upside_score
    score_breakdown["upside_to_strike"] = upside_score

    liquidity_score = 0
    if volume is not None:
        if volume >= 500:
            liquidity_score += 5
        elif volume >= 100:
            liquidity_score += 4
        elif volume >= 20:
            liquidity_score += 2
        else:
            warnings.append("Option volume is low.")

    if open_interest is not None:
        if open_interest >= 1000:
            liquidity_score += 5
        elif open_interest >= 250:
            liquidity_score += 4
        elif open_interest >= 50:
            liquidity_score += 2
        else:
            warnings.append("Open interest is low.")

    if spread_pct is not None:
        if spread_pct <= 5:
            liquidity_score += 5
            reasons.append("Bid/ask spread is tight.")
        elif spread_pct <= 10:
            liquidity_score += 4
        elif spread_pct <= 20:
            liquidity_score += 2
            warnings.append("Spread is wider than preferred.")
        else:
            warnings.append("Spread is wide.")
    else:
        warnings.append("Bid/ask spread is unavailable.")

    liquidity_score = min(liquidity_score, 15)
    score += liquidity_score
    score_breakdown["liquidity"] = liquidity_score

    quality_component = 0
    if quality_score is not None:
        if quality_score >= 85:
            quality_component = 10
            reasons.append("Underlying stock quality is high.")
        elif quality_score >= 75:
            quality_component = 8
        elif quality_score >= 65:
            quality_component = 6
        elif quality_score >= 55:
            quality_component = 3
            warnings.append("Underlying quality score is only middling.")
        else:
            warnings.append("Underlying quality score is weak.")
    score += quality_component
    score_breakdown["stock_quality_score"] = quality_component

    technical_component = 0
    if technical_score == "Neutral":
        technical_component = 10
        reasons.append("Neutral technical trend lowers immediate call-away pressure.")
    elif technical_score == "Sell":
        technical_component = 7
    elif technical_score == "Buy":
        technical_component = 4
        warnings.append("Bullish technical trend raises call-away risk.")
    elif technical_score == "Strong Buy":
        technical_component = 2
        warnings.append("Strong bullish technical trend raises call-away risk.")
    elif technical_score == "Strong Sell":
        technical_component = 3
        warnings.append("Weak technical trend may pressure the stock price.")
    score += technical_component
    score_breakdown["technical_trend"] = technical_component

    event_component = 10
    if earnings_before_exp:
        event_component -= 7
        warnings.append("Earnings occur before expiration.")
    else:
        reasons.append("No earnings date detected before expiration.")

    if ex_dividend_risk["data_available"]:
        if ex_dividend_risk["has_ex_dividend_before_expiration"]:
            if ex_dividend_risk["early_assignment_risk"] == "High":
                event_component -= 5
                warnings.append("Ex-dividend date before expiration raises early assignment risk.")
            elif ex_dividend_risk["early_assignment_risk"] == "Moderate":
                event_component -= 3
                warnings.append("Ex-dividend date before expiration adds some early assignment risk.")
            else:
                event_component -= 1
        else:
            reasons.append("No ex-dividend date detected before expiration.")
    else:
        warnings.append("Ex-dividend data is unavailable.")
        event_component -= 1

    event_component = max(0, min(event_component, 10))
    score += event_component
    score_breakdown["earnings_dividend_risk"] = event_component

    strategy_component = 0
    if covered_call_strategy == "keep_shares_conservative":
        warnings.append("Premium will be lower because you are prioritizing share retention.")
        if abs_delta is not None and abs_delta <= 0.18:
            strategy_component += 2
        if upside_to_strike_pct >= 8:
            strategy_component += 2
        elif upside_to_strike_pct >= 5:
            strategy_component += 1
        if cost_basis is not None and strike >= cost_basis:
            strategy_component += 2
        if not earnings_before_exp:
            strategy_component += 1
        if (
            ex_dividend_risk["data_available"]
            and (
                not ex_dividend_risk["has_ex_dividend_before_expiration"]
                or ex_dividend_risk["early_assignment_risk"] == "Low"
            )
        ):
            strategy_component += 1
    elif covered_call_strategy == "balanced_income":
        if premium_yield_pct >= 1.0:
            strategy_component += 1
        if 2 <= upside_to_strike_pct <= 8:
            strategy_component += 1
        if abs_delta is not None and 0.20 <= abs_delta <= 0.35:
            strategy_component += 1
        if (volume or 0) >= 100 and (open_interest or 0) >= 250:
            strategy_component += 1
        if (quality_score or 0) >= 75:
            strategy_component += 1
    elif covered_call_strategy == "high_premium_ok_called":
        warnings.append(
            "This gives more premium but materially increases the chance your shares are called away."
        )
        if premium_yield_pct >= 1.5:
            strategy_component += 3
        elif premium_yield_pct >= 1.0:
            strategy_component += 2
        if (volume or 0) >= 100 and (open_interest or 0) >= 250:
            strategy_component += 1
        if max_gain_if_called_pct >= 4:
            strategy_component += 2
        if cost_basis is None or strike >= cost_basis:
            strategy_component += 1
    elif covered_call_strategy == "wheel_continuation":
        if adjusted_cost_basis_after_call is not None and strike >= adjusted_cost_basis_after_call:
            strategy_component += 3
        elif cost_basis is not None and strike >= cost_basis:
            strategy_component += 2
        if abs_delta is not None and 0.20 <= abs_delta <= 0.35:
            strategy_component += 1
        if not earnings_before_exp:
            strategy_component += 1
    elif covered_call_strategy == "tax_sensitive":
        if abs_delta is not None and abs_delta <= 0.15:
            strategy_component += 2
        if upside_to_strike_pct >= 8:
            strategy_component += 2
        elif upside_to_strike_pct >= 5:
            strategy_component += 1
        if (
            ex_dividend_risk["data_available"]
            and ex_dividend_risk["early_assignment_risk"] == "Low"
        ):
            strategy_component += 1

    strategy_component = max(0, min(strategy_component, 8))
    score += strategy_component
    score_breakdown["strategy_fit"] = strategy_component

    score = max(0, min(100, round(score)))
    if score >= 80:
        rating = "Excellent"
    elif score >= 70:
        rating = "Good"
    elif score >= 60:
        rating = "Acceptable"
    else:
        rating = "Avoid"

    premium_income = round(mid * 100 * covered_share_lots, 2)
    return {
        "contract": {
            "expiration": expiration_date.isoformat(),
            "dte": dte,
            "strike": strike,
            "moneyness": moneyness,
            "delta": delta,
            "iv": iv,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "volume": volume,
            "open_interest": open_interest,
            "premium_income": premium_income,
            "premium_yield_pct": round(premium_yield_pct, 2),
            "annualized_yield_pct": round(annualized_yield_pct, 2),
            "upside_to_strike_pct": round(upside_to_strike_pct, 2),
            "effective_exit_price": effective_exit_price,
            "max_gain_if_called": max_gain_if_called,
            "max_gain_if_called_pct": max_gain_if_called_pct,
            "breakeven_after_premium": breakeven_after_premium,
            "gain_if_called_from_cost_basis": gain_if_called_from_cost_basis,
            "gain_if_called_from_cost_basis_pct": gain_if_called_from_cost_basis_pct,
            "wheel_cost_basis_before_call": (
                round(wheel_cost_basis_before_call, 2)
                if wheel_cost_basis_before_call is not None
                else None
            ),
            "adjusted_cost_basis_after_call": (
                round(adjusted_cost_basis_after_call, 2)
                if adjusted_cost_basis_after_call is not None
                else None
            ),
            "call_away_risk": call_away_risk,
            "covered_call_score": score,
            "score": score,
            "rating": rating,
            "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
            "score_breakdown": score_breakdown,
            "reasons": reasons,
            "warnings": _dedupe_preserve_order(warnings),
        },
        "covered_call_score": score,
        "rating": rating,
        "earnings_before_expiration": earnings_before_exp,
        "ex_dividend_risk": ex_dividend_risk,
        "reasons": reasons,
        "warnings": _dedupe_preserve_order(warnings),
    }


def _score_put_contract(
    contract,
    *,
    stock_price,
    rsi,
    quality_score,
    technical_score,
    next_earnings_date,
    today
):
    strike = contract["strike"]
    expiration_date = contract["expiration_date"]
    bid = contract["bid"]
    ask = contract["ask"]
    mid = contract["mid"]
    delta = contract["delta"]
    volume = contract["volume"]
    open_interest = contract["open_interest"]
    iv = contract["iv"]

    if not stock_price or not strike or not expiration_date or not mid:
        return None

    dte = (expiration_date - today).days

    if dte <= 0:
        return None

    roi = (mid / strike) * 100
    downside_buffer = ((stock_price - strike) / stock_price) * 100

    spread_pct = None
    if bid is not None and ask is not None and ask > 0:
        spread_pct = ((ask - bid) / ask) * 100

    earnings_before_exp = False
    if next_earnings_date and today <= next_earnings_date <= expiration_date:
        earnings_before_exp = True

    score = 0
    reasons = []
    warnings = []
    

    # Delta score
    if delta is not None:
        abs_delta = abs(delta)

        if 0.18 <= abs_delta <= 0.29:
            score += 20
            reasons.append("Delta is in a conservative put-selling range.")
        elif 0.29 < abs_delta <= 0.34:
            score += 10
            warnings.append("Delta is slightly aggressive for conservative put selling.")

        else:
            warnings.append("Delta is too aggressive for a conservative wheel setup.")
    else:
        score += 6
        warnings.append("Delta is missing, so risk probability is harder to evaluate.")

    # ROI score
    if roi >= 3:
        score += 20
        reasons.append("ROI is strong for a cash-secured put.")
    elif roi >= 2.5:
        score += 17
        reasons.append("ROI meets the preferred 2.5%+ target.")
    elif roi >= 1.5:
        score += 10
        warnings.append("ROI is acceptable but below the preferred target.")
    else:
        warnings.append("ROI is weak for this strategy.")

    # Downside buffer
    if downside_buffer >= 10:
        score += 15
        reasons.append("Strike has a strong downside buffer below the current stock price.")
    elif downside_buffer >= 5:
        score += 10
        reasons.append("Strike has a reasonable downside buffer.")
    elif downside_buffer >= 0:
        score += 5
        warnings.append("Strike is close to the current price.")
    else:
        warnings.append("Strike is above the current stock price, which is not ideal for CSP selling.")

    # Liquidity
    liquidity_score = 0

    if volume is not None:
        if volume >= 100:
            liquidity_score += 5
        elif volume >= 20:
            liquidity_score += 3
        else:
            warnings.append("Option volume is low.")

    if open_interest is not None:
        if open_interest >= 500:
            liquidity_score += 5
        elif open_interest >= 100:
            liquidity_score += 3
        else:
            warnings.append("Open interest is low.")

    if spread_pct is not None:
        if spread_pct <= 10:
            liquidity_score += 5
            reasons.append("Bid/ask spread is reasonable.")
        elif spread_pct <= 25:
            liquidity_score += 2
            warnings.append("Bid/ask spread is somewhat wide.")
        else:
            warnings.append("Bid/ask spread is too wide.")

    score += min(liquidity_score, 15)

    # Fundamental quality
    if quality_score is not None:
        if quality_score >= 85:
            score += 10
            reasons.append("Company quality score is high.")
        elif quality_score >= 75:
            score += 7
            reasons.append("Company quality score is acceptable for wheel consideration.")
        elif quality_score >= 60:
            score += 3
            warnings.append("Company quality score is mediocre.")
        else:
            warnings.append("Company quality score is weak for wheel strategy.")

    # RSI / technical timing
    if rsi is not None:
        if 35 <= rsi <= 60:
            score += 5
            reasons.append("RSI suggests reasonable technical timing.")
        elif 60 < rsi <= 70:
            score += 2
            warnings.append("RSI is somewhat elevated.")
        elif rsi > 70:
            warnings.append("RSI is overbought, so entry timing may be poor.")
        elif rsi < 30:
            warnings.append("RSI is oversold, which may signal higher short-term risk.")

    # Technical score
    if technical_score == "Strong Buy":
        score += 10
        reasons.append("Technical score is Strong Buy.")
    elif technical_score == "Buy":
        score += 6
        reasons.append("Technical score is Buy.")
    elif technical_score == "Neutral":
        score += 2
    elif technical_score == "Sell":
        score -= 5
        warnings.append("Technical score is Sell.")
    elif technical_score == "Strong Sell":
        score -= 10
        warnings.append("Technical score is Strong Sell.")

    # Earnings risk
    if earnings_before_exp:
        score -= 15
        warnings.append("Earnings occur before expiration, increasing assignment and volatility risk.")
    else:
        score += 5
        reasons.append("No earnings date detected before expiration.")

    score = max(0, min(100, round(score)))

    if score >= 80:
        rating = "Good opportunity"
    elif score >= 65:
        rating = "Watchlist"
    elif score >= 50:
        rating = "Speculative"
    else:
        rating = "Avoid"

    return {
        "contract": {
            "strike": strike,
            "expiration": expiration_date.isoformat(),
            "dte": dte,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": contract["last"],
            "delta": delta,
            "iv": iv,
            "volume": volume,
            "open_interest": open_interest,
            "roi": round(roi, 2),
            "downside_buffer": round(downside_buffer, 2),
            "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
        },
        "cumulative_score": score,
        "rating": rating,
        "earnings_before_expiration": earnings_before_exp,
        "reasons": reasons,
        "warnings": warnings,
    }



def _handle_put_wheel_opportunity(symbol: str) -> str:
    if not symbol:
        return json.dumps({
            "error": "Missing required symbol"
        })

    symbol = symbol.strip().upper()

    if not re.match(r"^[A-Z0-9.\-]{1,10}$", symbol):
        return json.dumps({
            "error": "Invalid ticker symbol",
            "symbol": symbol
        })

    try:
        sym = Symbol.objects.filter(ticker__iexact=symbol).first()
    except Exception as e:
        return json.dumps({
            "error": "Database error while fetching symbol data",
            "details": str(e),
            "symbol": symbol
        })

    if sym is None:
        return json.dumps({
            "error": f"No data found in database for {symbol}",
            "symbol": symbol
        })

    today = date.today()

    stock_price = _to_float(sym.price)
    rsi = _to_float(sym.rsi)
    quality_score = _to_float(sym.score)
    technical_score = sym.technical_score
    next_earnings_date = _parse_date(sym.next_earnings_date)

    option_data = sym.option_data or {}
    put_contracts = _extract_put_contracts(option_data)

    if not put_contracts:
        return json.dumps({
            "symbol": symbol,
            "price": stock_price,
            "rsi": rsi,
            "quality_score": quality_score,
            "stock_quality_score": quality_score,
            "classification": sym.classification,
            "liquidity": sym.liquidity,
            "initial_suitability": sym.initial_suitability,
            "technical_score": technical_score,
            "next_earnings_date": (
                next_earnings_date.isoformat()
                if next_earnings_date
                else None
            ),
            "error": "No put contracts found in option_data."
        }, default=_json_default)

    evaluated = []

    for contract in put_contracts:
        scored = _score_put_contract(
            contract,
            stock_price=stock_price,
            rsi=rsi,
            quality_score=quality_score,
            technical_score=technical_score,
            next_earnings_date=next_earnings_date,
            today=today,
        )

        if scored:
            evaluated.append(scored)

    if not evaluated:
        return json.dumps({
            "symbol": symbol,
            "price": stock_price,
            "rsi": rsi,
            "quality_score": quality_score,
            "stock_quality_score": quality_score,
            "classification": sym.classification,
            "liquidity": sym.liquidity,
            "initial_suitability": sym.initial_suitability,
            "technical_score": technical_score,
            "next_earnings_date": (
                next_earnings_date.isoformat()
                if next_earnings_date
                else None
            ),
            "error": "Put contracts were found, but none had enough valid data to evaluate."
        }, default=_json_default)

    evaluated = sorted(
        evaluated,
        key=lambda item: item["cumulative_score"],
        reverse=True
    )

    best = evaluated[0]
    top_candidates = evaluated[:5]

    call_contracts = _extract_call_contracts(sym.call_data or {})
    calls_summary = []
    for c in call_contracts:
        if not c["strike"] or not c["mid"] or not c["expiration_date"]:
            continue
        dte = (c["expiration_date"] - today).days
        if dte <= 0:
            continue
        roi = round((c["mid"] / c["strike"]) * 100, 4)
        calls_summary.append({
            "strike": c["strike"],
            "expiration": c["expiration"],
            "dte": dte,
            "bid": c["bid"],
            "ask": c["ask"],
            "mid": c["mid"],
            "delta": c["delta"],
            "iv": c["iv"],
            "volume": c["volume"],
            "open_interest": c["open_interest"],
            "roi": roi,
        })

    result = {
        "symbol": symbol,
        "price": stock_price,
        "rsi": rsi,
        "quality_score": quality_score,
        "stock_quality_score": quality_score,
        "classification": sym.classification,
        "liquidity": sym.liquidity,
        "initial_suitability": sym.initial_suitability,
        "technical_score": technical_score,
        "next_earnings_date": (
            next_earnings_date.isoformat()
            if next_earnings_date
            else None
        ),

        "best_put_opportunity": best,
        "top_put_candidates": top_candidates,
        "call_contracts": calls_summary,

        "summary": {
            "rating": best["rating"],
            "opportunity_rating": best["rating"],
            "cumulative_score": best["cumulative_score"],
            "score": best["cumulative_score"],
            "opportunity_score": best["cumulative_score"],
            "best_strike": best["contract"]["strike"],
            "best_expiration": best["contract"]["expiration"],
            "best_dte": best["contract"]["dte"],
            "best_roi": best["contract"]["roi"],
            "best_delta": best["contract"]["delta"],
            "earnings_risk": best["earnings_before_expiration"],
            "quality_score": quality_score,
            "stock_quality_score": quality_score,
            "technical_score": technical_score,
        }
    }

    return json.dumps(result, default=_json_default)


def _handle_covered_call_opportunity(args: dict) -> str:
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return json.dumps({"error": "Missing required symbol"})

    if not re.match(r"^[A-Z0-9.\-]{1,10}$", symbol):
        return json.dumps({"error": "Invalid ticker symbol", "symbol": symbol})

    shares_owned = _to_int(args.get("shares_owned"))
    if shares_owned is None:
        shares_owned = 100
    if shares_owned < 100:
        return json.dumps({
            "error": "At least 100 shares are required to sell one standard covered call.",
            "symbol": symbol,
            "shares_owned": shares_owned,
        })

    cost_basis = _to_float(args.get("cost_basis"))
    assigned_price = _to_float(args.get("assigned_price"))
    premium_received_from_put = _to_float(args.get("premium_received_from_put"))
    target_delta = _to_float(args.get("target_delta"))
    max_dte = _to_int(args.get("max_dte"))
    min_roi = _to_float(args.get("min_roi"))
    raw_style = args.get("style")
    style = _normalize_covered_call_style(raw_style)
    covered_call_strategy = _normalize_covered_call_strategy(
        args.get("covered_call_strategy")
    )
    if covered_call_strategy is None:
        covered_call_strategy = _default_covered_call_strategy(
            raw_style if raw_style not in (None, "") else None
        )
    target_exit_price = _to_float(args.get("target_exit_price"))

    if covered_call_strategy == "exit_at_target_price" and target_exit_price is None:
        return json.dumps({
            "error": (
                "target_exit_price is required when covered_call_strategy is "
                "exit_at_target_price."
            ),
            "symbol": symbol,
            "covered_call_strategy": covered_call_strategy,
        })

    try:
        sym = Symbol.objects.filter(ticker__iexact=symbol).first()
    except Exception as e:
        return json.dumps({
            "error": "Database error while fetching symbol data",
            "details": str(e),
            "symbol": symbol,
        })

    if sym is None:
        return json.dumps({
            "error": f"No data found in database for {symbol}",
            "symbol": symbol,
        })

    today = date.today()
    stock_price = _to_float(sym.price)
    quality_score = _to_float(sym.score)
    technical_score = sym.technical_score
    next_earnings_date = _parse_date(sym.next_earnings_date)
    call_data = sym.call_data or sym.option_data or {}
    call_contracts = _extract_call_contracts(call_data)
    filter_profile = _resolve_covered_call_filters(
        style=style,
        covered_call_strategy=covered_call_strategy,
    )
    style_delta_min = filter_profile["delta_min"]
    style_delta_max = filter_profile["delta_max"]
    style_min_dte = filter_profile["preferred_min_dte"]
    style_max_dte = filter_profile["preferred_max_dte"]
    strategy_warnings = []

    if covered_call_strategy == "wheel_continuation":
        if assigned_price is None or premium_received_from_put is None:
            strategy_warnings.append(
                "Wheel continuation works best with assigned_price and premium_received_from_put so adjusted basis can be enforced precisely."
            )
        elif cost_basis is None:
            strategy_warnings.append(
                "Using adjusted wheel basis from assignment inputs because a standalone cost basis was not provided."
            )

    if not call_contracts:
        return json.dumps({
            "symbol": symbol,
            "current_price": stock_price,
            "shares_owned": shares_owned,
            "cost_basis": cost_basis,
            "assigned_price": assigned_price,
            "premium_received_from_put": premium_received_from_put,
            "stock_quality_score": quality_score,
            "quality_score": quality_score,
            "classification": sym.classification,
            "technical_score": technical_score,
            "covered_call_strategy": covered_call_strategy,
            "target_exit_price": target_exit_price,
            "next_earnings_date": next_earnings_date.isoformat() if next_earnings_date else None,
            "error": "No call contracts found in call_data.",
        }, default=_json_default)

    evaluated = []
    filtered_out = 0
    for contract in call_contracts:
        scored = _score_covered_call_contract(
            contract,
            stock_price=stock_price,
            shares_owned=shares_owned,
            cost_basis=cost_basis,
            assigned_price=assigned_price,
            premium_received_from_put=premium_received_from_put,
            target_delta=target_delta,
            filter_profile=filter_profile,
            covered_call_strategy=covered_call_strategy,
            quality_score=quality_score,
            technical_score=technical_score,
            next_earnings_date=next_earnings_date,
            option_data=call_data,
            today=today,
        )
        if scored is None:
            continue

        contract_delta = _to_float(scored["contract"]["delta"])
        contract_dte = _to_int(scored["contract"]["dte"])
        contract_strike = _to_float(scored["contract"]["strike"])
        upside_to_strike_pct = _to_float(scored["contract"]["upside_to_strike_pct"])
        abs_delta = abs(contract_delta) if contract_delta is not None else None

        if abs_delta is None or not (style_delta_min <= abs_delta <= style_delta_max):
            filtered_out += 1
            continue
        if contract_dte is None or not (style_min_dte <= contract_dte <= style_max_dte):
            filtered_out += 1
            continue
        contract_moneyness = scored["contract"].get("moneyness")
        if filter_profile["require_otm"] and contract_moneyness != "OTM":
            filtered_out += 1
            continue
        if not filter_profile.get("allow_itm", True) and contract_moneyness == "ITM":
            filtered_out += 1
            continue
        if not filter_profile.get("allow_atm", True) and contract_moneyness == "ATM":
            filtered_out += 1
            continue
        min_upside_pct = filter_profile.get("min_upside_pct")
        if min_upside_pct is not None and (
            upside_to_strike_pct is None or upside_to_strike_pct < min_upside_pct
        ):
            filtered_out += 1
            continue
        max_upside_pct = filter_profile.get("max_upside_pct")
        if max_upside_pct is not None and (
            upside_to_strike_pct is None or upside_to_strike_pct > max_upside_pct
        ):
            filtered_out += 1
            continue
        if (
            filter_profile["require_above_cost_basis"]
            and (
                scored["contract"].get("adjusted_cost_basis_after_call") is not None
                or cost_basis is not None
            )
            and (
                contract_strike is None
                or contract_strike
                < (
                    scored["contract"].get("adjusted_cost_basis_after_call")
                    if scored["contract"].get("adjusted_cost_basis_after_call") is not None
                    else cost_basis
                )
            )
        ):
            filtered_out += 1
            continue
        if covered_call_strategy == "exit_at_target_price":
            if contract_strike is None or contract_strike < target_exit_price:
                filtered_out += 1
                continue
            target_gap_abs_pct = abs(contract_strike - target_exit_price) / target_exit_price * 100
            scored["contract"]["target_exit_price"] = target_exit_price
            scored["contract"]["target_gap_abs_pct"] = round(target_gap_abs_pct, 2)

        contract_roi = scored["contract"]["premium_yield_pct"]
        if max_dte is not None and scored["contract"]["dte"] > max_dte:
            filtered_out += 1
            continue
        if min_roi is not None and contract_roi < min_roi:
            filtered_out += 1
            continue
        evaluated.append(scored)

    if not evaluated:
        return json.dumps({
            "symbol": symbol,
            "current_price": stock_price,
            "shares_owned": shares_owned,
            "cost_basis": cost_basis,
            "assigned_price": assigned_price,
            "premium_received_from_put": premium_received_from_put,
            "stock_quality_score": quality_score,
            "quality_score": quality_score,
            "classification": sym.classification,
            "technical_score": technical_score,
            "next_earnings_date": next_earnings_date.isoformat() if next_earnings_date else None,
            "filters_applied": {
                "style_delta_min": style_delta_min,
                "style_delta_max": style_delta_max,
                "style_min_dte": style_min_dte,
                "style_max_dte": style_max_dte,
                "filter_source": filter_profile["filter_source"],
                "covered_call_strategy": covered_call_strategy,
                "target_exit_price": target_exit_price,
                "target_delta": target_delta,
                "max_dte": max_dte,
                "min_roi": min_roi,
                "style": style,
            },
            "warnings": strategy_warnings,
            "filtered_out_contracts": filtered_out,
            "error": "Call contracts were found, but none passed the covered-call filters or had enough valid data to evaluate.",
        }, default=_json_default)

    if covered_call_strategy == "exit_at_target_price":
        evaluated.sort(
            key=lambda item: (
                item["contract"].get("target_gap_abs_pct")
                if item["contract"].get("target_gap_abs_pct") is not None
                else 999,
                -(item["covered_call_score"] or 0),
                -(item["contract"]["premium_yield_pct"] or 0),
            ),
        )
    else:
        evaluated.sort(
            key=lambda item: (
                item["covered_call_score"],
                item["contract"]["premium_yield_pct"],
                item["contract"]["upside_to_strike_pct"],
            ),
            reverse=True,
        )

    best = evaluated[0]
    top_candidates = [item["contract"] for item in evaluated[:5]]
    covered_share_lots = shares_owned // 100
    warnings = list(best.get("warnings") or [])

    if shares_owned % 100 != 0:
        warnings.append(
            f"Only {covered_share_lots} covered call contract(s) are fully covered by {shares_owned} shares."
        )
    if cost_basis is not None and best["contract"]["strike"] < cost_basis:
        warnings.append("Recommended strike is below the provided cost basis.")
    warnings.extend(strategy_warnings)

    result = {
        "symbol": symbol,
        "current_price": stock_price,
        "shares_owned": shares_owned,
        "covered_share_lots": covered_share_lots,
        "cost_basis": cost_basis,
        "assigned_price": assigned_price,
        "premium_received_from_put": premium_received_from_put,
        "stock_quality_score": quality_score,
        "quality_score": quality_score,
        "classification": sym.classification,
        "technical_score": technical_score,
        "next_earnings_date": next_earnings_date.isoformat() if next_earnings_date else None,
        "style": style,
        "covered_call_strategy": covered_call_strategy,
        "target_exit_price": target_exit_price,
        "target_delta": (
            target_delta if target_delta is not None else filter_profile["target_delta"]
        ),
        "best_contract": best["contract"],
        "top_candidates": top_candidates,
        "warnings": _dedupe_preserve_order(warnings),
        "ex_dividend_risk": best["ex_dividend_risk"],
        "summary": {
            "rating": best["rating"],
            "score": best["covered_call_score"],
            "covered_call_score": best["covered_call_score"],
            "best_strike": best["contract"]["strike"],
            "best_expiration": best["contract"]["expiration"],
            "best_dte": best["contract"]["dte"],
            "premium_yield_pct": best["contract"]["premium_yield_pct"],
            "annualized_yield_pct": best["contract"]["annualized_yield_pct"],
            "effective_exit_price": best["contract"].get("effective_exit_price"),
            "gain_if_called_from_cost_basis": best["contract"].get("gain_if_called_from_cost_basis"),
            "call_away_risk": best["contract"]["call_away_risk"],
        },
        "filters_applied": {
            "style_delta_min": style_delta_min,
            "style_delta_max": style_delta_max,
            "style_min_dte": style_min_dte,
            "style_max_dte": style_max_dte,
            "filter_source": filter_profile["filter_source"],
            "mapped_style": filter_profile["mapped_style"],
            "covered_call_strategy": covered_call_strategy,
            "target_exit_price": target_exit_price,
            "max_dte": max_dte,
            "min_roi": min_roi,
            "style": style,
        },
    }
    return json.dumps(result, default=_json_default)


def _handle_scan_put_opportunities(args: dict) -> str:
    limit = int(args.get("limit") or 10)
    min_score = float(args.get("min_score") or 50)
    min_roi = _to_float(args.get("min_roi"))
    max_dte = _to_int(args.get("max_dte"))
    min_price = _to_float(args.get("min_price"))
    max_price = _to_float(args.get("max_price"))
    max_delta = _to_float(args.get("max_delta"))
    
    today = date.today()

    try:
        symbols = Symbol.objects.exclude(option_data=None).filter(score__gte=65)
    except Exception as e:
        return json.dumps({"error": "Database error", "details": str(e)})

    results = []

    for sym in symbols:
        stock_price = _to_float(sym.price)
        if not stock_price:
            continue

        rsi = _to_float(sym.rsi)
        quality_score = _to_float(sym.score)
        technical_score = sym.technical_score
        next_earnings_date = _parse_date(sym.next_earnings_date)

        put_contracts = _extract_put_contracts(sym.option_data or {})
        if not put_contracts:
            continue

        best_scored = None
        for contract in put_contracts:
            scored = _score_put_contract(
                contract,
                stock_price=stock_price,
                rsi=rsi,
                quality_score=quality_score,
                technical_score=technical_score,
                next_earnings_date=next_earnings_date,
                today=today,
            )
            if scored is None:
                continue
            if (
                best_scored is None
                or scored["cumulative_score"] > best_scored["cumulative_score"]
            ):
                best_scored = scored

        if best_scored is None:
            continue

        c = best_scored["contract"]
        if best_scored["cumulative_score"] < min_score:
            continue
        if min_roi is not None and c["roi"] < min_roi:
            continue
        if max_dte is not None and c["dte"] > max_dte:
            continue
        if min_price is not None and stock_price < min_price:
            continue
        if max_price is not None and stock_price > max_price:
            continue
        if max_delta is not None:
            contract_delta = c.get("delta")
            if contract_delta is None or abs(contract_delta) > max_delta:
                continue

        results.append({
            "ticker": sym.ticker,
            "price": stock_price,
            "score": best_scored["cumulative_score"],
            "rating": best_scored["rating"],
            "strike": c["strike"],
            "expiration": c["expiration"],
            "dte": c["dte"],
            "roi": c["roi"],
            "delta": c["delta"],
            "iv": c["iv"],
            "downside_buffer": c["downside_buffer"],
            "earnings_risk": best_scored["earnings_before_expiration"],
            
            "stock_quality_score": quality_score,
            "technical_score": technical_score,
            "rsi": rsi,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:limit]

    return json.dumps({
        "scan_date": today.isoformat(),
        "total_symbols_scanned": symbols.count(),
        "results_returned": len(top),
        "filters_applied": {
            "min_score": min_score,
            "min_roi": min_roi,
            "max_dte": max_dte,
            "min_price": min_price,
            "max_price": max_price,
            "max_delta": max_delta,
        },
        "opportunities": top,
    }, default=_json_default)


def _handle_compare_put_candidates(args: dict) -> str:
    raw_symbols = args.get("symbols") or []
    if not isinstance(raw_symbols, list) or not raw_symbols:
        return json.dumps({"error": "symbols must be a non-empty list"})

    max_delta = _to_float(args.get("max_delta"))
    min_roi = _to_float(args.get("min_roi"))
    min_quality_score = _to_float(args.get("min_quality_score"))

    symbols = []
    for value in raw_symbols:
        if value is None:
            continue
        symbol = str(value).strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    if not symbols:
        return json.dumps({"error": "No valid symbols provided"})

    ranked_candidates = []
    skipped = []

    for symbol in symbols:
        payload = json.loads(_handle_put_wheel_opportunity(symbol))
        if payload.get("error"):
            skipped.append({"symbol": symbol, "error": payload["error"]})
            continue

        best = payload.get("best_put_opportunity") or {}
        contract = best.get("contract") or {}
        opportunity_score = best.get("cumulative_score")
        roi = _to_float(contract.get("roi"))
        delta = _to_float(contract.get("delta"))
        quality_score = _to_float(payload.get("quality_score"))

        if min_roi is not None and (roi is None or roi < min_roi):
            skipped.append({
                "symbol": symbol,
                "error": f"Best contract ROI below min_roi filter ({min_roi}).",
            })
            continue

        if max_delta is not None and (delta is None or abs(delta) > max_delta):
            skipped.append({
                "symbol": symbol,
                "error": f"Best contract delta exceeds max_delta filter ({max_delta}).",
            })
            continue

        if min_quality_score is not None and (
            quality_score is None or quality_score < min_quality_score
        ):
            skipped.append({
                "symbol": symbol,
                "error": (
                    "Underlying quality score below min_quality_score filter "
                    f"({min_quality_score})."
                ),
            })
            continue

        ranked_candidates.append({
            "symbol": symbol,
            "price": payload.get("price"),
            "classification": payload.get("classification"),
            "stock_quality_score": quality_score,
            "quality_score": quality_score,
            "technical_score": payload.get("technical_score"),
            "rsi": payload.get("rsi"),
            "comparison_score": opportunity_score,
            "opportunity_score": opportunity_score,
            "opportunity_rating": best.get("rating"),
            "rating": best.get("rating"),
            "assignment_comfort_score": quality_score,
            "best_contract": contract,
            "warnings": best.get("warnings") or [],
            "reasons": best.get("reasons") or [],
            "earnings_risk": best.get("earnings_before_expiration"),
        })

    ranked_candidates.sort(
        key=lambda item: (
            item.get("comparison_score") or 0,
            item.get("stock_quality_score") or 0,
            item.get("best_contract", {}).get("roi") or 0,
        ),
        reverse=True,
    )

    return json.dumps({
        "symbols_requested": symbols,
        "symbols_compared": len(ranked_candidates),
        "winner": ranked_candidates[0] if ranked_candidates else None,
        "ranked_candidates": ranked_candidates,
        "skipped": skipped,
        "filters_applied": {
            "max_delta": max_delta,
            "min_roi": min_roi,
            "min_quality_score": min_quality_score,
        },
    }, default=_json_default)


def handle_tool_call(tool_name: str, tool_args: dict) -> str:
    if tool_name == "analyze_stock":
        symbol = tool_args["symbol"]
        try:
            raw_data = FMPClient().fetch_financial_data(symbol)
            calculator = FinancialMetricsCalculator(raw_data)
            report = calculator.process()

            # Slim down the payload — drop verbose year-by-year arrays
            # to avoid bloating the context window unnecessarily
            report.pop("put_selling_guidance", None)
            for key in ("cash_flow", "fcf_margin", "balance_sheet", "profitability",
                        "capital_efficiency", "per_share", "interest_coverage", "key_metrics"):
                section = report.get(key, {})
                section.pop("fcf_by_year", None)
                section.pop("cash_conversion_ratios", None)
                section.pop("per_share_by_year", None)
                section.pop("roic_by_year", None)
                section.pop("fcf_margin_by_year", None)
                section.pop("interest_coverage_by_year", None)

            return json.dumps(report)

        except Exception as e:
            return json.dumps({"error": str(e), "symbol": symbol})

    if tool_name == "get_put_wheel_opportunity":
        return _handle_put_wheel_opportunity(tool_args["symbol"])

    if tool_name == "get_covered_call_opportunity":
        return _handle_covered_call_opportunity(tool_args)

    if tool_name == "scan_put_opportunities":
        return _handle_scan_put_opportunities(tool_args)

    if tool_name == "compare_put_candidates":
        return _handle_compare_put_candidates(tool_args)

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


class AgentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        query = request.data.get("query", "").strip()
        if not query:
            return Response({"error": "query is required"}, status=400)

        # Restore conversation history from the request (client sends it back)
        history = request.data.get("history", [])

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history,
            {"role": "user", "content": query},
        ]

        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

        try:
            # Agentic loop — keeps running until the model stops calling tools
            for _ in range(10):
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                )

                message = response.choices[0].message

                # No tool calls → final answer, we're done
                if not message.tool_calls:
                    return Response({
                        "answer": message.content,
                        "history": [
                            *history,
                            {"role": "user", "content": query},
                            {"role": "assistant", "content": message.content},
                        ],
                    })

                # Append the assistant's tool-call message to history
                messages.append(message.model_dump(exclude_none=True))

                # Execute each tool call and feed results back
                for tool_call in message.tool_calls:
                    result = handle_tool_call(
                        tool_call.function.name,
                        json.loads(tool_call.function.arguments),
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    })

            else:
                return Response({"error": "Agent loop exceeded maximum iterations"}, status=500)

        except OpenAIError as e:
            return Response({"error": f"AI service error: {str(e)}"}, status=502)
