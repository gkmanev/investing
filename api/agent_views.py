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
You are a long-term equity analyst assistant.

When asked about a stock or company, always call the analyze_stock tool first.

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

When asked whether a stock is a good Put/wheel candidate, call get_put_wheel_opportunity.
The tool queries live option data from the database and returns:
- option_data: the best current Put contract (strike, expiration, bid/ask, IV, ROI, delta)
- supporting context: RSI, quality score, classification, liquidity, next earnings date
- opportunity_assessment: a short structured verdict on Put/wheel attractiveness

When interpreting get_put_wheel_opportunity results:
- Good opportunity = ROI ≥ 2.5 % for the period,30 <= RSI <= 70,
  earnings not before the expiration date, liquidity GOOD, score ≥ 70
- Always cite the specific ROI %, IV %, strike, and expiration from the tool response.
- If the tool returns no option data for the symbol, say so clearly.
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

def _score_put_contract(
    contract,
    *,
    stock_price,
    rsi,
    quality_score,
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

    # DTE score — your preferred 25–40 DTE window
    if 25 <= dte <= 40:
        score += 20
        reasons.append("Expiration is inside the preferred 25–40 DTE window.")
    elif 20 <= dte <= 45:
        score += 12
        warnings.append("Expiration is close to the target DTE window, but not ideal.")
    else:
        warnings.append("Expiration is outside the preferred DTE range.")

    # Delta score
    if delta is not None:
        abs_delta = abs(delta)

        if 0.15 <= abs_delta <= 0.35:
            score += 20
            reasons.append("Delta is in a conservative put-selling range.")
        elif 0.35 < abs_delta <= 0.45:
            score += 10
            warnings.append("Delta is slightly aggressive for conservative put selling.")
        elif abs_delta < 0.15:
            score += 8
            warnings.append("Delta is very conservative, but income may be lower.")
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
        "score": score,
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
    next_earnings_date = _parse_date(sym.next_earnings_date)

    option_data = sym.option_data or {}
    put_contracts = _extract_put_contracts(option_data)

    if not put_contracts:
        return json.dumps({
            "symbol": symbol,
            "price": stock_price,
            "rsi": rsi,
            "quality_score": quality_score,
            "classification": sym.classification,
            "liquidity": sym.liquidity,
            "initial_suitability": sym.initial_suitability,
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
            "classification": sym.classification,
            "liquidity": sym.liquidity,
            "initial_suitability": sym.initial_suitability,
            "next_earnings_date": (
                next_earnings_date.isoformat()
                if next_earnings_date
                else None
            ),
            "error": "Put contracts were found, but none had enough valid data to evaluate."
        }, default=_json_default)

    evaluated = sorted(
        evaluated,
        key=lambda item: item["score"],
        reverse=True
    )

    best = evaluated[0]
    top_candidates = evaluated[:5]

    result = {
        "symbol": symbol,
        "price": stock_price,
        "rsi": rsi,
        "quality_score": quality_score,
        "classification": sym.classification,
        "liquidity": sym.liquidity,
        "initial_suitability": sym.initial_suitability,
        "next_earnings_date": (
            next_earnings_date.isoformat()
            if next_earnings_date
            else None
        ),

        "best_put_opportunity": best,
        "top_put_candidates": top_candidates,

        "strategy_rules_used": {
            "preferred_dte": "25–40 days",
            "preferred_delta": "0.15–0.35 absolute delta",
            "preferred_roi": "2.5%+",
            "avoid_earnings_before_expiration": True,
            "prefer_high_quality_score": "75+",
            "prefer_reasonable_bid_ask_spread": True,
        },

        "summary": {
            "rating": best["rating"],
            "score": best["score"],
            "best_strike": best["contract"]["strike"],
            "best_expiration": best["contract"]["expiration"],
            "best_dte": best["contract"]["dte"],
            "best_roi": best["contract"]["roi"],
            "best_delta": best["contract"]["delta"],
            "earnings_risk": best["earnings_before_expiration"],
        }
    }

    return json.dumps(result, default=_json_default)


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
                messages.append(message)

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
