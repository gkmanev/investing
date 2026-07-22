import json
import logging
import os
import re
import time
import urllib.request
import urllib.error
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal
from itertools import combinations
from typing import Any, Dict, List

from anthropic import Anthropic
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from openai import OpenAI
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from api.entitlements import get_plan_context, get_plan_entitlements, serialize_plan_context
from api.helper import FinancialMetricsCalculator
from api.llm_usage import (
    calculate_usage_cost,
    extract_anthropic_usage_metrics,
    extract_openai_usage_metrics,
)
from api.models import AgentRun, Symbol, SymbolExpirationSnapshot


logger = logging.getLogger(__name__)

SOCIAL_GREETING_MESSAGE = (
    "Hi. Ask about stocks, fundamentals, options, covered calls, cash-secured puts, wheels, or spreads."
)
SOCIAL_ACKNOWLEDGEMENT_MESSAGE = "You're welcome."
SOCIAL_CONFIRMATION_MESSAGE = "Understood."
QUERY_TOO_LONG_MESSAGE = (
    "Your message is too long. Please shorten it and focus on one investing question about stocks, options, or fundamentals."
)


def _default_internal_plan_context() -> dict[str, Any]:
    return {
        "plan": "pro",
        "trial_days_left": None,
        "entitlements": dict(get_plan_entitlements()["pro"]),
        "has_full_access": True,
        "trial_expired": False,
        "subscription_active": True,
    }


def _resolve_runtime_plan_context(
    *,
    user=None,
    plan_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if plan_context:
        resolved = dict(plan_context)
        entitlements = resolved.get("entitlements") or {}
        if entitlements:
            resolved["entitlements"] = dict(entitlements)
            return resolved

    if user is not None and getattr(user, "is_authenticated", False):
        return get_plan_context(user)

    return _default_internal_plan_context()


def _local_day_bounds() -> tuple[datetime, datetime]:
    today = timezone.localdate()
    start = timezone.make_aware(datetime.combine(today, dt_time.min))
    end = start + timedelta(days=1)
    return start, end


def _count_daily_agent_queries(user) -> int:
    if user is None or not getattr(user, "is_authenticated", False):
        return 0
    start, end = _local_day_bounds()
    return AgentRun.objects.filter(
        user=user,
        created_at__gte=start,
        created_at__lt=end,
    ).count()


def _count_daily_analyze_stock_calls(user) -> int:
    if user is None or not getattr(user, "is_authenticated", False):
        return 0

    start, end = _local_day_bounds()
    count = 0
    for used_tools in AgentRun.objects.filter(
        user=user,
        created_at__gte=start,
        created_at__lt=end,
    ).values_list("used_tools_json", flat=True):
        if not isinstance(used_tools, list):
            continue
        count += sum(
            1
            for entry in used_tools
            if isinstance(entry, dict) and entry.get("name") == "analyze_stock"
        )
    return count


def _persist_used_tools(agent_run_id: int | None, used_tools: list[dict[str, Any]]) -> None:
    if agent_run_id is None:
        return
    AgentRun.objects.filter(pk=agent_run_id).update(
        used_tools_json=json.loads(json.dumps(used_tools, default=_json_default)),
        updated_at=timezone.now(),
    )


def _persist_llm_usage(agent_run_id: int | None, llm_usage: list[dict[str, Any]]) -> None:
    if agent_run_id is None:
        return
    AgentRun.objects.filter(pk=agent_run_id).update(
        llm_usage_json=json.loads(json.dumps(llm_usage, default=_json_default)),
        llm_usage_summary_json=calculate_usage_cost(llm_usage),
        updated_at=timezone.now(),
    )


def _trim_history_for_plan(
    conversation_history: list[dict[str, Any]],
    plan_context: dict[str, Any],
) -> list[dict[str, Any]]:
    max_history_items = (plan_context.get("entitlements") or {}).get("max_history_items")
    if max_history_items is None:
        return conversation_history
    return conversation_history[-max(0, int(max_history_items)) :]


def _clamp_scan_limit(requested_limit: int, plan_context: dict[str, Any]) -> int:
    max_scan_limit = (plan_context.get("entitlements") or {}).get("max_scan_limit")
    limit = max(1, int(requested_limit or 1))
    if max_scan_limit is None:
        return limit
    return min(limit, max(1, int(max_scan_limit)))


def _scan_pagination_error(
    *,
    plan_context: dict[str, Any],
    requested_limit: int | None = None,
    requested_offset: int | None = None,
) -> str:
    max_scan_limit = (plan_context.get("entitlements") or {}).get("max_scan_limit")
    max_extra_pages = (plan_context.get("entitlements") or {}).get("max_extra_pages")
    return json.dumps(
        {
            "error": "Additional scan pages are not available on the current plan.",
            "error_code": "scan_pagination_locked",
            "plan": plan_context.get("plan"),
            "trial_days_left": plan_context.get("trial_days_left"),
            "upgrade_available": True,
            "max_scan_limit": max_scan_limit,
            "max_extra_pages": max_extra_pages,
            "requested_limit": requested_limit,
            "requested_offset": requested_offset,
        },
        default=_json_default,
    )


def _scan_limit_applied_payload(
    *,
    plan_context: dict[str, Any],
    requested_limit: int,
    applied_limit: int,
) -> dict[str, Any]:
    return {
        "plan": plan_context.get("plan"),
        "trial_days_left": plan_context.get("trial_days_left"),
        "requested_limit": requested_limit,
        "applied_limit": applied_limit,
        "upgrade_available": plan_context.get("plan") == "free",
    }


SYSTEM_PROMPT = """
You are a long-term equity analyst and options trading assistant.

Scope handling:
- PutPulse specializes in stocks, stock fundamentals, options, wheel/CSP, covered calls, spreads, screening, and related portfolio income questions.
- If the user's request is clearly unrelated to those areas, briefly explain that PutPulse specializes in those topics and cannot help with the unrelated request.
- If the user's request is ambiguous, terse, or depends on prior conversation context, first interpret it in the most reasonable in-scope way instead of refusing.
- Do not over-refuse borderline requests that can reasonably be understood as stock, fundamentals, options, screening, or portfolio-income questions.
- If only part of the request is in scope, help with the in-scope part and briefly decline the rest.

Always format your responses using Markdown. Use **bold** for emphasis, `## headers` to separate sections, bullet lists for flags/signals, and tables for ranked comparisons or multi-ticker data. Never return plain prose where a table or list would be clearer.

For every ticker displayed from any tool response, always include a **Stock quality score** field or table column. Use `stock_quality_score` from the tool response (or `quality_score` only when that is the available alias). If the tool supplies neither value, display `N/A`; never omit the field.

For every ticker displayed from any tool response, always include a **Current stock price** field or table column. Use `underlying_price`, `current_price`, or `price` from the tool response. If the tool supplies none of those values, display `N/A`; never omit the field.

For tables that compare or rank ticker-based ideas, use this column order whenever the relevant fields are available: **Rank**, **Ticker**, **Current stock price**, **Strike**, **Expiration (DTE)**, **Delta**, **IV %**, **Premium received**, **ROI %**, **Cash required**, **Breakeven**, **Downside buffer %**, **Contracts affordable**, **Estimated monthly income**, **Stock quality score**. Keep **Stock quality score** as the final column. Omit option-specific columns only when the tool did not return a value for that metric; do not move the mandatory current stock price or stock quality score columns.

For follow-up screener refinements, rerun the relevant tool with the updated hard filters. Do not manually restate, prune, or partially reuse a previously rendered table when the user adds a new constraint.

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
- If the user provides account size, available cash, buying power, or a maximum cash-secured put budget, treat it as a hard collateral cap and pass it through as `account_size` or `max_cash_required`.
- Always cite the specific ROI %, IV %, volume, current stock price, strike, delta, expiration, cash required, premium received, breakeven, stock technical score, stock quality score, and put opportunity rating/score from the tool response.
- Include `contracts_affordable` whenever the tool returns it.
- Use `technical_score` only for the stock's technical rating (`Strong Buy`, `Buy`, `Neutral`, `Sell`, `Strong Sell`).
- Use `stock_quality_score` / `quality_score` only for the underlying stock's quality score. 
- Use `rating` / `score` only for the evaluated put contract opportunity. Never call the opportunity score a technical score.
- If the tool returns no option data for the symbol, say so clearly.

If the user asks about covered calls, selling calls against owned shares, call income, call-away risk, monthly covered calls, or which call to sell on a stock they own, call get_covered_call_opportunity.
- Always cite the specific premium yield %, annualized yield %, strike, expiration, DTE, delta, IV %, bid/ask or mid premium, upside to strike %, call-away risk, stock technical score, stock quality score, and covered call rating/score from the tool response.
- Use `technical_score` only for the stock's technical rating (`Strong Buy`, `Buy`, `Neutral`, `Sell`, `Strong Sell`).
- Use `stock_quality_score` / `quality_score` only for the underlying stock's quality score.
- Use `covered_call_score`, `score`, or `rating` only for the evaluated covered call opportunity.
- If the user says they own a stock "at 70", "at $70", or similar, treat that as `cost_basis` / entry price, not the current market price.
- Never state a current stock price from the user's entry price. Current price must come from tool output only.
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

If the user asks for a monthly income plan, reliable income plan, consistent income ideas, option income plan, or a portfolio income plan, call build_monthly_income_plan.
- If the user provides owned positions, pass them through as `positions` and prioritize covered calls on those holdings.
- If the user provides available cash, buying power, or a collateral budget, pass it as `account_size` or `max_cash_required` and include one or more cash-secured put / wheel ideas sized to fit that budget.
- If the user provides both owned positions and cash budget, return a mixed plan: covered calls on the owned positions plus a diversified CSP/wheel allocation for the cash portion when possible.
- If the user does not provide owned positions, do not pretend they own shares. Default to CSP/wheel ideas only.
- If the user provides a monthly target, use `monthly_income_target` and explicitly say whether the estimated normalized monthly income reaches that target.
- Use `estimated_monthly_income` only for the normalized monthly premium estimate derived from the specific contract DTE. Do not present it as guaranteed income.
- For every covered-call or CSP/wheel ticker shown in the plan, include its stock quality score in the displayed table or idea summary.

If the user asks about debit spreads, credit spreads, vertical spreads, bull put spreads, bear call spreads, bull call spreads, bear put spreads, iron condors, iron butterflies, or other defined-risk option trades for one ticker, call get_spread_opportunity.
- First infer:
  - directional_view: bullish, bearish, neutral, or auto
  - spread_type: credit spread, debit spread, a specific structure, or auto
  - risk_profile: conservative, balanced, or aggressive
  - max_risk, if the user provides it
- Do not block the answer if the user did not specify these. Use defaults and state the assumption clearly:
  - directional_view=`auto`
  - spread_type=`auto`
  - risk_profile=`balanced`
  - max_dte=45 for credit spreads
  - max_dte=60 for debit spreads
- Map user phrasing to tool settings like this:
  - defined-risk income -> credit spread
  - bullish but limited risk -> bull put credit spread or bull call debit spread
  - bearish but limited risk -> bear call credit spread or bear put debit spread
  - high probability trade -> credit spread with a lower-delta short strike
  - cheap bullish bet -> bull call debit spread
  - cheap bearish bet -> bear put debit spread
  - neutral income -> iron condor
  - high IV spread -> credit spread or iron condor
  - low IV directional trade -> debit spread
  - max risk below $500 -> set `max_risk`
- Always cite the ticker, current stock price, spread type, expiration, DTE, each leg (action, call/put, strike, bid, ask, mid, delta, IV, volume, open interest), net credit or debit, max profit, max loss, breakeven, return on risk or reward-to-risk, estimated probability of profit if available, downside/upside buffer, stock technical score, stock quality score, spread rating/score, and warnings from the tool response.
- Use `technical_score` only for the stock's technical rating and `stock_quality_score` / `quality_score` only for the underlying stock's quality score.
- Use `spread_score`, `score`, or `rating` only for the evaluated spread opportunity.
- If the user asks generally for the best spread, use `spread_type="auto"`.
- For credit spreads, emphasize max loss, breakeven, probability of profit, and return on risk. Do not rank only by credit received.
- For debit spreads, emphasize max loss, max profit, breakeven, reward-to-risk, and the directional thesis. Do not present debit spreads as income strategies.
- Never call a spread safe. Call it defined-risk, meaning the maximum theoretical loss is known before entry, excluding assignment and execution risk.
- If the tool returns earnings, liquidity, delta, or IV warnings, mention them explicitly.

If the user asks for best spread ideas across the market, such as best credit spreads today, conservative bull put spreads, bearish call credit spreads, defined-risk income trades, or spread scans with risk/return constraints, call scan_spread_opportunities.
- Use this only for market-wide spread scans across tracked symbols, not for a single ticker or a specific ticker list.
- If the user does not specify a direction or spread type, use auto mode and explain the assumption.
- Present results as a ranked list with ticker, spread type, expiration, DTE, strikes, net credit/debit, max profit, max loss, return on risk, estimated probability of profit, stock quality score, technical score, and spread rating/score.
- If the response includes earnings, liquidity, delta, or IV warnings, mention them explicitly.

If the user provides multiple tickers for spread ideas, asks which ticker has the better spread setup, or asks to compare spread types, call compare_spread_candidates.
- Use this when comparing multiple tickers, or when comparing two or more spread structures for one ticker.
- Do not use scan_spread_opportunities when the user provides a specific ticker list.
- Present the results as a ranked comparison and clearly separate income-oriented credit spreads from directional debit spreads.
- Do not choose only by headline credit, debit, or ROI. Factor in max loss, liquidity, earnings risk, underlying quality, technical alignment, and whether the spread structure matches the stated thesis.

If the user asks for best covered calls across the market, top covered call ideas, covered call screeners, scans, or ranked covered call opportunities across all tracked symbols, call scan_covered_call_opportunities.
- The tool scans all tracked symbols and returns the highest-scoring covered call opportunities ranked by covered call score.
- Optional filters: limit (number of results), min_roi (minimum premium yield %), max_delta, max_dte.

When interpreting scan_covered_call_opportunities results:
- Present results as a ranked list with ticker, strike, expiration, DTE, delta, IV %, premium yield %, annualized yield %, upside to strike %, call-away risk, stock quality score, stock technical score, and covered call rating/score.
- Highlight warnings for each candidate, especially earnings risk, ex-dividend risk, low liquidity, wide spreads, or elevated call-away risk.
- Use `technical_score` only for the stock's technical rating and `stock_quality_score` / `quality_score` only for the underlying stock's quality score.
- Use `covered_call_score`, `score`, or `rating` only for the evaluated covered call opportunity.
- End with a short conclusion paragraph that comments on the premium-yield range, underlying quality, and overall call-away risk tradeoff across the presented candidates.

If the user asks to compare multiple tickers for covered calls, call income across several stocks they own, or which owned stock has the best covered call right now, call compare_covered_call_candidates.

Use compare_covered_call_candidates when the user provides two or more tickers and wants to know which one has the better covered call setup, lower call-away risk, stronger income tradeoff, or is more suitable for covered-call income now.

When interpreting compare_covered_call_candidates results:
- Present the results as a ranked comparison.
- Clearly separate premium attractiveness from call-away risk and underlying stock quality.
- Do not choose only by premium yield. A high premium with weak fundamentals, poor liquidity, earnings risk, or aggressive delta should not be framed as the most conservative candidate.
- Prefer candidates with strong underlying quality, acceptable technical trend, reasonable delta, sufficient upside to strike, good liquidity, and no near-term earnings risk.
- If a candidate offers more premium but also materially higher call-away risk, say that clearly.

Always cite the specific numbers returned by the tool:
- ticker
- current stock price
- strike
- expiration
- DTE
- delta
- IV %
- premium yield %
- annualized yield %
- upside to strike %
- volume and open interest, if available
- bid/ask spread or mid premium, if available
- stock quality score
- technical score
- covered call score/rating
- call-away risk
- warnings


If the user asks for best ideas for PUTs, Wheels and CSP - Cash Secured Puts across the market, top candidates, screeners, scans, ranked opportunities, or generally asks which puts to suggest without naming a ticker, call scan_put_opportunities.
- The tool scans all tracked symbols and returns the highest-scoring cash-secured put contracts ranked by opportunity score.
- Call scan_put_opportunities again using the `next_offset` value from the previous response
- Do NOT call it with a higher limit
- If `next_offset` is null, tell the user there are no more results
- Never show results that were already shown in this conversation
 - Optional filters: limit (number of results), min_roi (%), max_dte (days to expiration), min_price, max_price, min_rsi, max_rsi, max_delta, and `account_size` / `max_cash_required` for CSP affordability.
 - If the user asks for companies, stocks, or tickers below / under a dollar threshold, map that to `max_price` on the underlying stock price. If they ask for above / over a threshold, map that to `min_price`.
 - If the user asks for oversold companies, map that to `max_rsi=30`. If the user asks for overbought companies, map that to `min_rsi=75`.

When interpreting scan_put_opportunities results:
- If the user provides a dollar budget for cash-secured puts, treat it as a hard affordability filter rather than a preference.
- Present results as a ranked list with ticker, strike, expiration, IV %, ROI %, cash required, premium received, breakeven, contracts affordable when available, stock quality score, stock technical score, delta, and put opportunity rating/score.
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
- If the user provides a dollar budget for cash-secured puts, treat it as a hard affordability filter rather than a preference.
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
- cash required
- premium received
- breakeven
- contracts affordable, if available
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
                    },
                    "account_size": {
                        "type": "number",
                        "description": "Optional total cash available for the cash-secured put, e.g. 10000.",
                    },
                    "max_cash_required": {
                        "type": "number",
                        "description": "Optional maximum collateral allowed for one cash-secured put position.",
                    },
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
                    },
                    "account_size": {
                        "type": "number",
                        "description": "Optional total cash available for the cash-secured put, e.g. 10000.",
                    },
                    "max_cash_required": {
                        "type": "number",
                        "description": "Optional maximum collateral allowed for one cash-secured put position.",
                    },
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
            "name": "build_monthly_income_plan",
            "description": (
                "Build an options income plan using covered calls on owned positions and/or "
                "cash-secured put / wheel ideas for available cash. Use this when the user asks "
                "for a monthly income plan, reliable income ideas, or consistent option income "
                "without specifying one exact tactic first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monthly_income_target": {
                        "type": "number",
                        "description": "Optional monthly income target in dollars.",
                    },
                    "account_size": {
                        "type": "number",
                        "description": "Optional total cash available for cash-secured puts.",
                    },
                    "max_cash_required": {
                        "type": "number",
                        "description": "Optional maximum collateral allowed for one cash-secured put position.",
                    },
                    "positions": {
                        "type": "array",
                        "description": "Optional owned stock positions to evaluate for covered calls.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "symbol": {
                                    "type": "string",
                                    "description": "Owned stock ticker, e.g. AAPL.",
                                },
                                "shares_owned": {
                                    "type": "integer",
                                    "description": "Number of shares owned. At least 100 is needed for one covered call.",
                                },
                                "cost_basis": {
                                    "type": "number",
                                    "description": "Average cost basis per share. Optional.",
                                },
                                "assigned_price": {
                                    "type": "number",
                                    "description": "Assigned share price from a short put. Optional.",
                                },
                                "premium_received_from_put": {
                                    "type": "number",
                                    "description": "Premium already collected from the short put that led to assignment. Optional.",
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
                                    "description": "Optional per-position covered call strategy override.",
                                },
                                "target_exit_price": {
                                    "type": "number",
                                    "description": "Optional target stock exit price. Required only for exit_at_target_price strategy.",
                                },
                            },
                            "required": ["symbol", "shares_owned"],
                        },
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of covered-call positions and alternative put ideas to include. Default 5.",
                    },
                    "min_put_roi": {
                        "type": "number",
                        "description": "Optional minimum ROI percentage for CSP ideas.",
                    },
                    "max_put_delta": {
                        "type": "number",
                        "description": "Optional maximum absolute delta for CSP ideas.",
                    },
                    "max_put_dte": {
                        "type": "integer",
                        "description": "Optional maximum DTE for CSP ideas.",
                    },
                    "min_call_roi": {
                        "type": "number",
                        "description": "Optional minimum premium yield percentage for covered calls.",
                    },
                    "max_call_delta": {
                        "type": "number",
                        "description": "Optional maximum absolute delta for covered calls.",
                    },
                    "max_call_dte": {
                        "type": "integer",
                        "description": "Optional maximum DTE for covered calls.",
                    },
                    "covered_call_style": {
                        "type": "string",
                        "enum": ["conservative", "balanced", "income", "aggressive"],
                        "description": "Default covered call style when a position-specific strategy is not supplied.",
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
                        "description": "Default covered call strategy applied to positions that do not specify one.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_spread_opportunity",
            "description": (
                "Fetch current option chain data for a symbol and evaluate defined-risk option spread opportunities. "
                "Supports bull put credit spreads, bear call credit spreads, bull call debit spreads, bear put debit spreads, "
                "iron condors, and iron butterflies. Ranks spreads using max profit, max loss, risk/reward, probability of profit, "
                "breakeven, liquidity, bid/ask spreads, IV, DTE, delta, stock quality score, technical score, earnings risk, "
                "and trend alignment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Stock ticker symbol, e.g. AAPL, MSFT, NVDA",
                    },
                    "spread_type": {
                        "type": "string",
                        "enum": [
                            "bull_put_credit_spread",
                            "bear_call_credit_spread",
                            "bull_call_debit_spread",
                            "bear_put_debit_spread",
                            "iron_condor",
                            "iron_butterfly",
                            "auto",
                        ],
                        "description": "Type of spread to evaluate. Use auto if the user does not specify a structure or just asks for the best spread.",
                    },
                    "directional_view": {
                        "type": "string",
                        "enum": ["bullish", "bearish", "neutral", "auto"],
                        "description": "User's market view. Use auto when it is not specified.",
                    },
                    "risk_profile": {
                        "type": "string",
                        "enum": ["conservative", "balanced", "aggressive"],
                        "description": "Risk preference. Defaults to balanced when not specified.",
                    },
                    "max_dte": {
                        "type": "integer",
                        "description": "Maximum days to expiration. When omitted, default to 45 for credit spreads and 60 for debit spreads.",
                    },
                    "min_credit": {
                        "type": "number",
                        "description": "Minimum credit received for credit spreads. Optional.",
                    },
                    "max_debit": {
                        "type": "number",
                        "description": "Maximum debit paid for debit spreads. Optional.",
                    },
                    "max_risk": {
                        "type": "number",
                        "description": "Maximum dollar risk per spread. Optional.",
                    },
                    "width": {
                        "type": "number",
                        "description": "Preferred strike width, e.g. 5 or 10. Optional.",
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
                "top put opportunities across all stocks, or wants to compare puts across multiple tickers. "
                "Results are paginated — default page size is 10. To get the next page, call this tool "
                "again with the `next_offset` value from the previous response. "
                "Never re-call with a larger limit to get more results."
            ),   
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of top results to return. Default 10.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                        "Pagination cursor. To get the next page, pass the `next_offset` value "
                        "returned in the previous response. Do NOT increase `limit` to get more results — "
                        "always use `offset` instead. Default 0."
                    ),
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
                    "min_rsi": {
                        "type": "number",
                        "description": "Minimum RSI to include on the underlying stock. Optional.",
                    },
                    "max_rsi": {
                        "type": "number",
                        "description": "Maximum RSI to include on the underlying stock. Optional.",
                    },
                    "max_delta": {
                        "type": "number",
                        "description": "Maximum absolute delta to include (for example 0.30). Optional.",
                    },
                    "account_size": {
                        "type": "number",
                        "description": "Optional total cash available for cash-secured puts, e.g. 10000.",
                    },
                    "max_cash_required": {
                        "type": "number",
                        "description": "Optional maximum collateral allowed for one cash-secured put position.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_spread_opportunities",
            "description": (
                "Scan all tracked symbols and option chains for defined-risk spread opportunities. "
                "Supports credit spreads, debit spreads, and neutral spreads. Use for market-wide scans, "
                "high-probability spreads, defined-risk income trades, or spread screens with max risk constraints."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spread_type": {
                        "type": "string",
                        "enum": [
                            "bull_put_credit_spread",
                            "bear_call_credit_spread",
                            "bull_call_debit_spread",
                            "bear_put_debit_spread",
                            "iron_condor",
                            "iron_butterfly",
                            "auto"
                        ],
                        "description": "Spread structure to scan for. Use auto when the user does not specify one."
                    },
                    "directional_view": {
                        "type": "string",
                        "enum": ["bullish", "bearish", "neutral", "auto"],
                        "description": "Directional thesis. Use auto when the user does not specify one."
                    },
                    "risk_profile": {
                        "type": "string",
                        "enum": ["conservative", "balanced", "aggressive"],
                        "description": "Risk preference for spread filters. Defaults to balanced."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results to return. Default 10."
                    },
                    "max_dte": {
                        "type": "integer",
                        "description": "Maximum days to expiration. Optional."
                    },
                    "min_return_on_risk_pct": {
                        "type": "number",
                        "description": "Minimum return on risk percentage for a candidate. Optional."
                    },
                    "min_probability_of_profit": {
                        "type": "number",
                        "description": "Minimum estimated probability of profit percentage. Optional."
                    },
                    "max_risk": {
                        "type": "number",
                        "description": "Maximum dollar loss per spread. Optional."
                    },
                    "min_quality_score": {
                        "type": "number",
                        "description": "Minimum stock quality score for the underlying (0-100). Optional."
                    },
                    "max_short_delta": {
                        "type": "number",
                        "description": "Maximum absolute delta of the short option leg, e.g. 0.30. Optional."
                    },
                    "exclude_earnings": {
                        "type": "boolean",
                        "description": "When true, exclude trades with earnings before expiration."
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "scan_covered_call_opportunities",
            "description": (
                "Scan all tracked symbols in the database and return the best covered call "
                "opportunities ranked by covered call score. Use this when the user asks for "
                "today's best covered calls, top covered call ideas across all stocks, or wants "
                "a market-wide covered call screener."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of top results to return. Default 10.",
                    },
                    "min_roi": {
                        "type": "number",
                        "description": "Minimum premium yield / ROI percentage to include. Optional.",
                    },
                    "max_delta": {
                        "type": "number",
                        "description": "Maximum absolute delta to include (for example 0.30). Optional.",
                    },
                    "max_dte": {
                        "type": "integer",
                        "description": "Maximum days to expiration to include. Optional.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_spread_candidates",
            "description": (
                "Compare defined-risk spread opportunities across multiple tickers, or compare "
                "multiple spread structures on one ticker. Supports credit spreads, debit spreads, "
                "iron condors, and iron butterflies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of ticker symbols to compare. Use one symbol if comparing spread types on the same stock.",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "Optional single ticker shortcut when comparing spread types on one stock.",
                    },
                    "spread_type": {
                        "type": "string",
                        "enum": [
                            "bull_put_credit_spread",
                            "bear_call_credit_spread",
                            "bull_call_debit_spread",
                            "bear_put_debit_spread",
                            "iron_condor",
                            "iron_butterfly",
                            "auto",
                        ],
                        "description": "Primary spread type to evaluate. Use auto when the user did not specify one.",
                    },
                    "spread_types": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "bull_put_credit_spread",
                                "bear_call_credit_spread",
                                "bull_call_debit_spread",
                                "bear_put_debit_spread",
                                "iron_condor",
                                "iron_butterfly",
                                "auto",
                            ],
                        },
                        "description": "Optional list of spread structures to compare in the same request.",
                    },
                    "directional_view": {
                        "type": "string",
                        "enum": ["bullish", "bearish", "neutral", "auto"],
                        "description": "Directional thesis. Use auto when not specified.",
                    },
                    "risk_profile": {
                        "type": "string",
                        "enum": ["conservative", "balanced", "aggressive"],
                        "description": "Risk preference. Defaults to balanced.",
                    },
                    "max_dte": {
                        "type": "integer",
                        "description": "Maximum days to expiration. When omitted, default to 45 for credit spreads and 60 for debit spreads.",
                    },
                    "min_credit": {
                        "type": "number",
                        "description": "Minimum credit received for credit spreads. Optional.",
                    },
                    "max_debit": {
                        "type": "number",
                        "description": "Maximum debit paid for debit spreads. Optional.",
                    },
                    "max_risk": {
                        "type": "number",
                        "description": "Maximum dollar risk per spread. Optional.",
                    },
                    "width": {
                        "type": "number",
                        "description": "Preferred strike width, e.g. 5 or 10. Optional.",
                    },
                    "min_return_on_risk_pct": {
                        "type": "number",
                        "description": "Minimum return-on-risk threshold. For debit spreads this is inferred from reward-to-risk.",
                    },
                    "min_probability_of_profit": {
                        "type": "number",
                        "description": "Minimum probability of profit threshold. Mostly relevant for credit spreads.",
                    },
                    "min_quality_score": {
                        "type": "number",
                        "description": "Minimum stock quality score for the underlying.",
                    },
                    "max_short_delta": {
                        "type": "number",
                        "description": "Maximum absolute short-leg delta.",
                    },
                    "exclude_earnings": {
                        "type": "boolean",
                        "description": "When true, exclude spreads where earnings occur before expiration.",
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
                    "account_size": {
                        "type": "number",
                        "description": "Optional total cash available for cash-secured puts, e.g. 10000.",
                    },
                    "max_cash_required": {
                        "type": "number",
                        "description": "Optional maximum collateral allowed for one cash-secured put position.",
                    },
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_covered_call_candidates",
            "description": (
                "Compare covered call opportunities for multiple tickers and rank them by "
                "income attractiveness, call-away risk, liquidity, and underlying stock quality."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of ticker symbols, e.g. ['AAPL', 'MSFT', 'NVDA']",
                    },
                    "max_delta": {
                        "type": "number",
                        "description": "Maximum absolute delta, e.g. 0.30",
                    },
                    "min_roi": {
                        "type": "number",
                        "description": "Minimum premium yield percentage",
                    },
                },
                "required": ["symbols"],
            },
        },
    }
]

ANTHROPIC_TOOLS = [
    {
        "name": tool["function"]["name"],
        "description": tool["function"]["description"],
        "input_schema": tool["function"]["parameters"],
    }
    for tool in TOOLS
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


def _truncate_text_for_log(value: Any, max_chars: int = 500) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}..."


def _log_agent_response(run_id: int | None, query: str, answer: str) -> None:
    logger.info(
        "Agent response run_id=%s query=%r answer=%r",
        run_id,
        _truncate_text_for_log(query),
        _truncate_text_for_log(answer),
    )


def _to_float(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _normalize_iv_percent(value):
    iv = _to_float(value)
    if iv is None:
        return None

    # TradingView snapshots may store IV as a fraction (for example 1.05 = 105%),
    # while the rest of the app expects percentage points.
    if 0 < abs(iv) < 10:
        return round(iv * 100, 4)

    return round(iv, 4)


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


def _resolve_cash_secured_budget(*values):
    candidates = [value for value in values if value is not None and value > 0]
    if not candidates:
        return None
    return min(candidates)


def _extract_owned_positions_from_query(query: str) -> list[dict[str, Any]]:
    if not query:
        return []

    matches = list(
        re.finditer(
            r"\b([A-Za-z][A-Za-z0-9.\-]{0,9})\b\s*(?:at|@)\s*\$?\s*(\d+(?:\.\d+)?)",
            query,
        )
    )
    if not matches:
        return []

    global_shares = None
    shares_each_match = re.search(r"\b(\d+(?:\.\d+)?)\s+shares?\s+each\b", query, re.IGNORECASE)
    if shares_each_match:
        global_shares = _to_int(shares_each_match.group(1))

    positions = []
    seen_symbols = set()
    ownership_hint_pattern = re.compile(
        r"\b(own|owned|hold|holding|bought|position|positions|shares?)\b",
        re.IGNORECASE,
    )

    for match in matches:
        symbol = str(match.group(1) or "").strip().upper()
        cost_basis = _to_float(match.group(2))
        if not symbol or cost_basis is None:
            continue
        if symbol in seen_symbols:
            continue

        prefix = query[max(0, match.start() - 40):match.start()]
        if not positions and not ownership_hint_pattern.search(prefix):
            continue

        suffix = query[match.end():match.end() + 30]
        shares_owned = None
        shares_nearby_match = re.search(r"^\s*(?:,|-)?\s*(\d+(?:\.\d+)?)\s+shares?\b", suffix, re.IGNORECASE)
        if shares_nearby_match:
            shares_owned = _to_int(shares_nearby_match.group(1))
        elif global_shares is not None:
            shares_owned = global_shares

        position = {
            "symbol": symbol,
            "cost_basis": cost_basis,
        }
        if shares_owned is not None:
            position["shares_owned"] = shares_owned

        positions.append(position)
        seen_symbols.add(symbol)

    return positions


def _extract_underlying_price_filters_from_query(query: str) -> dict[str, float]:
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
        first_amount = _to_float(match.group(1))
        second_amount = _to_float(match.group(2))
        if first_amount is None or second_amount is None:
            continue
        min_price, max_price = sorted((first_amount, second_amount))
        return {
            "min_price": min_price,
            "max_price": max_price,
        }

    max_patterns = [
        rf"\b{scope_pattern}\b(?:\s+\w+){{0,4}}\s+(?:priced\s+)?(?:below|under|less than|up to|at most|no more than)\s*{amount_pattern}\b",
        rf"\b(?:priced|trading)\s+(?:below|under|less than|up to|at most|no more than)\s*{amount_pattern}\b",
        rf"^\s*(?:provide|show|list|find|screen|give me)?(?:\s+the)?(?:\s+{scope_pattern})?(?:\s+(?:that are|which are))?\s*(?:priced\s+)?(?:below|under|less than|up to|at most|no more than)\s*{amount_pattern}\s*$",
    ]
    for pattern in max_patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        max_price = _to_float(match.group(1))
        if max_price is not None:
            return {"max_price": max_price}

    min_patterns = [
        rf"\b{scope_pattern}\b(?:\s+\w+){{0,4}}\s+(?:priced\s+)?(?:above|over|more than|greater than|at least|no less than)\s*{amount_pattern}\b",
        rf"\b(?:priced|trading)\s+(?:above|over|more than|greater than|at least|no less than)\s*{amount_pattern}\b",
        rf"^\s*(?:provide|show|list|find|screen|give me)?(?:\s+the)?(?:\s+{scope_pattern})?(?:\s+(?:that are|which are))?\s*(?:priced\s+)?(?:above|over|more than|greater than|at least|no less than)\s*{amount_pattern}\s*$",
    ]
    for pattern in min_patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        min_price = _to_float(match.group(1))
        if min_price is not None:
            return {"min_price": min_price}

    return {}


def _extract_rsi_filters_from_query(query: str) -> dict[str, float]:
    if not query:
        return {}

    normalized_query = " ".join(str(query).split()).lower()
    has_oversold = bool(re.search(r"\boversold\b", normalized_query))
    has_overbought = bool(re.search(r"\boverbought?\b", normalized_query))

    if has_oversold and not has_overbought:
        return {"max_rsi": 30.0}
    if has_overbought and not has_oversold:
        return {"min_rsi": 75.0}
    return {}


def _parse_money_amount(raw_value):
    if raw_value is None:
        return None
    try:
        return float(str(raw_value).replace(",", "").strip())
    except Exception:
        return None


def _extract_cash_budget_from_query(query: str) -> dict[str, float]:
    if not query:
        return {}

    normalized_query = " ".join(str(query).split())
    amount_pattern = r"\$?\s*(?P<amount>\d[\d,]*(?:\.\d+)?)\s*\$?"
    account_budget_terms = (
        r"available\s+cash|cash\s+to\s+deploy|buying\s+power|account\s+size|"
        r"cash\s+budget|capital\s+to\s+deploy|capital|cash\s+account|cash\s+acct|cash\s+balance"
    )

    max_cash_patterns = [
        rf"\b(?:max(?:imum)?\s+)?(?:csp\s+)?collateral\b[^.\n]*?{amount_pattern}\b",
        rf"\bmax(?:imum)?\s+(?:cash\s+required|cash-secured put budget|put budget|per-position budget|position size)\b[^.\n]*?{amount_pattern}\b",
        rf"\b{amount_pattern}\b[^.\n]*?\b(?:max(?:imum)?\s+)?(?:csp\s+)?collateral\b",
    ]
    for pattern in max_cash_patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        amount = _parse_money_amount(match.group("amount"))
        if amount is not None and amount > 0:
            return {"max_cash_required": amount}

    account_size_patterns = [
        rf"\b(?:i\s+have|have|with|using|for\s+my|in\s+my|my)\b[^.\n]*?{amount_pattern}[^.\n]*?\b(?:cash|budget|{account_budget_terms})\b",
        rf"\b(?:{account_budget_terms})\b[^.\n]*?{amount_pattern}\b",
        rf"\b{amount_pattern}\b[^.\n]*?\b(?:cash|budget|{account_budget_terms})\b",
    ]
    for pattern in account_size_patterns:
        match = re.search(pattern, normalized_query, re.IGNORECASE)
        if not match:
            continue
        amount = _parse_money_amount(match.group("amount"))
        if amount is not None and amount > 0:
            return {"account_size": amount}

    return {}


def _build_structured_query_context(query: str) -> str:
    positions = _extract_owned_positions_from_query(query)
    price_filters = _extract_underlying_price_filters_from_query(query)
    rsi_filters = _extract_rsi_filters_from_query(query)
    cash_budget = _extract_cash_budget_from_query(query)
    if not positions and not price_filters and not rsi_filters and not cash_budget:
        return ""

    lines = []
    if positions:
        lines.append("Structured holdings extracted from the user's message:")
        for index, position in enumerate(positions, start=1):
            line = (
                f"- position_{index}: symbol={position['symbol']}; "
                f"cost_basis={position['cost_basis']}"
            )
            if position.get("shares_owned") is not None:
                line += f"; shares_owned={position['shares_owned']}"
            lines.append(line)

        lines.append(
            "Important: treat `cost_basis` / entry price as separate from `current_price`. "
            "Any current stock price must come from tool data or stored market data, not from the entry price in the user's message."
        )

    if price_filters:
        if lines:
            lines.append("")
        lines.append("Structured screener filters extracted from the user's message:")
        if price_filters.get("min_price") is not None:
            lines.append(
                f"- min_price={price_filters['min_price']} (underlying stock price floor)"
            )
        if price_filters.get("max_price") is not None:
            lines.append(
                f"- max_price={price_filters['max_price']} (underlying stock price cap)"
            )
        lines.append(
            "Important: for market-wide stock or option scans, apply these underlying price filters as hard tool arguments."
        )

    if rsi_filters:
        if lines:
            lines.append("")
        if not price_filters:
            lines.append("Structured screener filters extracted from the user's message:")
        if rsi_filters.get("min_rsi") is not None:
            lines.append(
                f"- min_rsi={rsi_filters['min_rsi']} (underlying stock RSI floor)"
            )
        if rsi_filters.get("max_rsi") is not None:
            lines.append(
                f"- max_rsi={rsi_filters['max_rsi']} (underlying stock RSI cap)"
            )
        lines.append(
            "Important: for market-wide stock or option scans, apply these RSI filters as hard tool arguments."
        )

    if cash_budget:
        if lines:
            lines.append("")
        lines.append("Structured budget extracted from the user's message:")
        if cash_budget.get("account_size") is not None:
            lines.append(
                f"- account_size={cash_budget['account_size']} (total cash available to deploy)"
            )
        if cash_budget.get("max_cash_required") is not None:
            lines.append(
                f"- max_cash_required={cash_budget['max_cash_required']} (maximum collateral per position)"
            )
        lines.append(
            "Important: treat these cash constraints as hard tool arguments for income plans, CSP scans, and put comparisons."
        )

    return "\n".join(lines)


def _history_without_meta(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    visible_history = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "meta":
            continue
        visible_history.append(item)
    return visible_history


def _extract_history_tool_state(history: list[dict[str, Any]] | None) -> dict[str, Any]:
    for item in history or []:
        if not isinstance(item, dict) or item.get("role") != "meta":
            continue
        content = item.get("content")
        if not isinstance(content, dict):
            continue
        if content.get("type") != "tool_state":
            continue
        tools = content.get("tools")
        if isinstance(tools, dict):
            return json.loads(json.dumps(tools, default=_json_default))
    return {}


def _build_history_meta_entry(tool_state: dict[str, Any]) -> dict[str, Any] | None:
    if not tool_state:
        return None
    return {
        "role": "meta",
        "content": {
            "type": "tool_state",
            "tools": json.loads(json.dumps(tool_state, default=_json_default)),
        },
    }


def _is_show_more_follow_up(query: str) -> bool:
    normalized = " ".join(str(query or "").strip().lower().split())
    if not normalized:
        return False
    return bool(
        re.fullmatch(
            r"(show\s+)?(me\s+)?(more|more\s+results|additional|next|next\s+page|continue|show\s+next|show\s+more)",
            normalized,
        )
    )


def _get_simple_social_response(query: str) -> str | None:
    normalized = " ".join(str(query or "").strip().lower().split())
    if not normalized:
        return None

    if re.fullmatch(r"(hi|hello|hey|yo)[!.?]*", normalized):
        return SOCIAL_GREETING_MESSAGE

    if re.fullmatch(r"(thanks|thank you)[!.?]*", normalized):
        return SOCIAL_ACKNOWLEDGEMENT_MESSAGE

    if re.fullmatch(r"(ok|okay|cool|great|nice|got it|understood)[!.?]*", normalized):
        return SOCIAL_CONFIRMATION_MESSAGE

    return None


def _is_simple_social_query(query: str) -> bool:
    return _get_simple_social_response(query) is not None


def _get_fast_path_response(query: str) -> str | None:
    max_query_chars = max(1, int(getattr(settings, "AGENT_MAX_QUERY_CHARS", 12000)))
    if len(query) > max_query_chars:
        return QUERY_TOO_LONG_MESSAGE

    return _get_simple_social_response(query)


def _build_pagination_follow_up_context(
    query: str,
    history: list[dict[str, Any]] | None = None,
) -> str:
    if not _is_show_more_follow_up(query):
        return ""

    tool_state = _extract_history_tool_state(history)
    scan_state = tool_state.get("scan_put_opportunities")
    if not isinstance(scan_state, dict):
        return ""

    base_arguments = scan_state.get("base_arguments") or {}
    limit = scan_state.get("limit")
    next_offset = scan_state.get("next_offset")
    total_results_available = scan_state.get("total_results_available")

    lines = [
        "Structured pagination context from the previous scan:",
        "- previous_tool=scan_put_opportunities",
    ]
    if base_arguments:
        lines.append(
            f"- previous_filters={json.dumps(base_arguments, default=_json_default, sort_keys=True)}"
        )
    if limit is not None:
        lines.append(f"- previous_limit={limit}")
    lines.append(f"- next_offset={next_offset}")
    if total_results_available is not None:
        lines.append(f"- total_results_available={total_results_available}")
    lines.append(
        "Important: for this follow-up, continue the previous put scan using the saved next_offset instead of restarting from offset 0."
    )
    if next_offset is None:
        lines.append(
            "Important: next_offset is null, so there are no more scan results to show."
        )
    return "\n".join(lines)


def _prepare_agent_query(
    query: str,
    history: list[dict[str, Any]] | None = None,
) -> str:
    normalized_query = (query or "").strip()
    if not normalized_query:
        return normalized_query

    context_blocks = []

    structured_context = _build_structured_query_context(normalized_query)
    if structured_context:
        context_blocks.append(structured_context)

    pagination_context = _build_pagination_follow_up_context(
        normalized_query,
        history=history,
    )
    if pagination_context:
        context_blocks.append(pagination_context)

    if not context_blocks:
        return normalized_query

    return f"{normalized_query}\n\n" + "\n\n".join(context_blocks)


def _base_scan_put_arguments(tool_args: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in (tool_args or {}).items()
        if key != "offset"
    }


def _update_history_tool_state(
    tool_state: dict[str, Any],
    *,
    tool_name: str,
    tool_args: dict[str, Any],
    tool_result: str,
) -> dict[str, Any]:
    if tool_name != "scan_put_opportunities":
        return tool_state

    try:
        payload = json.loads(tool_result)
    except Exception:
        return tool_state

    if not isinstance(payload, dict):
        return tool_state

    opportunities = payload.get("opportunities")
    if not isinstance(opportunities, list):
        return tool_state

    returned_tickers = []
    for item in opportunities:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or "").strip().upper()
        if ticker:
            returned_tickers.append(ticker)

    previous_scan_state = tool_state.get("scan_put_opportunities")
    current_base_arguments = _base_scan_put_arguments(tool_args)
    current_offset = _to_int(payload.get("offset")) or 0

    shown_tickers = returned_tickers
    if (
        isinstance(previous_scan_state, dict)
        and previous_scan_state.get("base_arguments") == current_base_arguments
        and current_offset > 0
    ):
        shown_tickers = _dedupe_preserve_order(
            [
                *previous_scan_state.get("shown_tickers", []),
                *returned_tickers,
            ]
        )

    updated_state = dict(tool_state or {})
    updated_state["scan_put_opportunities"] = {
        "base_arguments": current_base_arguments,
        "limit": payload.get("limit"),
        "offset": payload.get("offset"),
        "next_offset": payload.get("next_offset"),
        "total_results_available": payload.get("total_results_available"),
        "shown_tickers": shown_tickers,
    }
    return updated_state


def _estimate_normalized_monthly_income(premium_amount, dte):
    premium_value = _to_float(premium_amount)
    dte_value = _to_int(dte)
    if premium_value is None:
        return None
    if dte_value is None or dte_value <= 0:
        return round(premium_value, 2)
    return round(premium_value * (30 / dte_value), 2)


def _select_diversified_put_allocation(put_ideas, total_budget=None, *, max_positions=3):
    if not put_ideas:
        return [], [], None

    max_positions = max(1, _to_int(max_positions) or 1)
    ranked_entries = []
    for index, item in enumerate(put_ideas):
        ranked_entries.append({
            "index": index,
            "idea": item,
            "cash_required": _to_float(item.get("cash_required")),
            "score": _to_float(item.get("score")) or 0,
            "estimated_monthly_income": _to_float(item.get("estimated_monthly_income")) or 0,
        })

    def _annotate_selected(entries, budget):
        selected = []
        remaining_cash = budget
        total_cash_required = 0.0
        total_monthly_income = 0.0

        for rank, entry in enumerate(sorted(entries, key=lambda candidate: candidate["index"]), start=1):
            cash_required = entry["cash_required"]
            if cash_required is not None:
                total_cash_required += cash_required
            total_monthly_income += entry["estimated_monthly_income"]

            idea = {
                **entry["idea"],
                "allocation_rank": rank,
                "allocated": True,
                "allocated_cash_required": cash_required,
            }
            if remaining_cash is not None and cash_required is not None:
                remaining_cash = round(max(remaining_cash - cash_required, 0), 2)
                idea["remaining_cash_after_allocation"] = remaining_cash
            else:
                idea["remaining_cash_after_allocation"] = None
            selected.append(idea)

        budget_value = _to_float(budget)
        return selected, {
            "positions_selected": len(selected),
            "diversified": len(selected) > 1,
            "budget": budget_value,
            "total_cash_required": round(total_cash_required, 2),
            "remaining_cash": (
                round(max((budget_value or 0) - total_cash_required, 0), 2)
                if budget_value is not None
                else None
            ),
            "estimated_monthly_income": round(total_monthly_income, 2),
        }

    budget_value = _to_float(total_budget)
    if budget_value is None or budget_value <= 0:
        selected, summary = _annotate_selected([ranked_entries[0]], None)
        alternatives = [
            {**entry["idea"], "allocated": False}
            for entry in ranked_entries[1:]
        ]
        return selected, alternatives, summary

    affordable_entries = [
        entry
        for entry in ranked_entries
        if entry["cash_required"] is not None
        and entry["cash_required"] > 0
        and entry["cash_required"] <= budget_value
    ]
    if not affordable_entries:
        return [], [], {
            "positions_selected": 0,
            "diversified": False,
            "budget": budget_value,
            "total_cash_required": 0.0,
            "remaining_cash": round(budget_value, 2),
            "estimated_monthly_income": 0.0,
        }

    combo_limit = min(max_positions, len(affordable_entries))
    best_combo = None
    best_metrics = None

    for position_count in range(1, combo_limit + 1):
        for combo in combinations(affordable_entries, position_count):
            combo_cash_required = round(
                sum(entry["cash_required"] or 0 for entry in combo),
                2,
            )
            if combo_cash_required > budget_value:
                continue

            metrics = (
                position_count,
                round(sum(entry["score"] for entry in combo), 4),
                round(sum(entry["estimated_monthly_income"] for entry in combo), 2),
                combo_cash_required,
                -sum(entry["index"] for entry in combo),
            )
            if best_metrics is None or metrics > best_metrics:
                best_metrics = metrics
                best_combo = combo

    if not best_combo:
        best_combo = (affordable_entries[0],)

    selected, summary = _annotate_selected(list(best_combo), budget_value)
    selected_indexes = {entry["index"] for entry in best_combo}
    alternatives = [
        {**entry["idea"], "allocated": False}
        for entry in affordable_entries
        if entry["index"] not in selected_indexes
    ]
    return selected, alternatives, summary


def _build_put_contract_cash_metrics(strike, mid, budget=None):
    if strike is None:
        return {
            "cash_required": None,
            "premium_received": None,
            "breakeven": None,
            "contracts_affordable": None,
        }

    cash_required = round(strike * 100, 2)
    premium_received = round(mid * 100, 2) if mid is not None else None
    breakeven = round(strike - mid, 2) if mid is not None else None
    contracts_affordable = None
    if budget is not None and cash_required > 0:
        contracts_affordable = int(budget // cash_required)

    return {
        "cash_required": cash_required,
        "premium_received": premium_received,
        "breakeven": breakeven,
        "contracts_affordable": contracts_affordable,
    }


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
        iv = _normalize_iv_percent(c.get("iv") or c.get("implied_volatility"))
        volume = _to_int(c.get("volume"))
        open_interest = _to_int(c.get("open_interest") or c.get("oi"))

        if not _is_tradeable_option_contract(bid=bid, ask=ask, volume=volume):
            continue

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
        iv = _normalize_iv_percent(c.get("iv") or c.get("implied_volatility"))
        volume = _to_int(c.get("volume"))
        open_interest = _to_int(c.get("open_interest") or c.get("oi"))

        if not _is_tradeable_option_contract(bid=bid, ask=ask, volume=volume):
            continue

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


def _get_expiration_snapshots_for_symbol(sym):
    if not getattr(sym, "pk", None):
        return []

    prefetched = getattr(sym, "_prefetched_objects_cache", {})
    if "expiration_snapshots" in prefetched:
        return list(prefetched["expiration_snapshots"])

    return list(
        SymbolExpirationSnapshot.objects.filter(symbol=sym).order_by("expiration_date")
    )


def _build_grouped_snapshot_payload(sym, *, option_type):
    grouped = {}
    for snapshot in _get_expiration_snapshots_for_symbol(sym):
        expiration_key = snapshot.expiration_date.isoformat()
        if option_type == "put":
            payload = snapshot.put_data
            if not payload and isinstance(snapshot.option_data, dict):
                payload = {"puts": [snapshot.option_data]}
        else:
            payload = snapshot.call_data

        if isinstance(payload, dict) and payload:
            grouped[expiration_key] = payload

    return grouped


def _get_symbol_put_contracts(sym):
    snapshot_payload = _build_grouped_snapshot_payload(sym, option_type="put")
    if snapshot_payload:
        return _extract_put_contracts(snapshot_payload)
    return _extract_put_contracts(sym.option_data or {})


def _get_symbol_call_payload(sym):
    snapshot_payload = _build_grouped_snapshot_payload(sym, option_type="call")
    if snapshot_payload:
        return snapshot_payload
    return sym.call_data or sym.option_data or {}


def _get_symbol_call_contracts(sym):
    return _extract_call_contracts(_get_symbol_call_payload(sym))


def _dedupe_preserve_order(items):
    seen = set()
    output = []
    for item in items or []:
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output


def _is_tradeable_option_contract(*, bid, ask, volume):
    if bid is not None and bid <= 0:
        return False
    if ask is not None and ask <= 0:
        return False
    if volume is not None and volume < 50:
        return False
    return True


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


def _normalize_spread_type(spread_type):
    value = str(spread_type or "auto").strip().lower()
    valid = {
        "bull_put_credit_spread",
        "bear_call_credit_spread",
        "bull_call_debit_spread",
        "bear_put_debit_spread",
        "iron_condor",
        "iron_butterfly",
        "auto",
    }
    if value in valid:
        return value
    return "auto"


def _normalize_directional_view(directional_view):
    value = str(directional_view or "auto").strip().lower()
    if value in {"bullish", "bearish", "neutral", "auto"}:
        return value
    return "auto"


def _normalize_risk_profile(risk_profile):
    value = str(risk_profile or "balanced").strip().lower()
    if value in {"conservative", "balanced", "aggressive"}:
        return value
    return "balanced"


def _spread_profile(risk_profile, overrides=None):
    profiles = {
        "conservative": {
            "credit_target_delta": 0.18,
            "neutral_target_delta": 0.15,
            "debit_target_delta": 0.58,
            "preferred_min_dte": 21,
            "preferred_max_dte": 45,
            "credit_iv_floor": 24,
            "debit_iv_ceiling": 30,
            "max_width": 10,
            "credit_max_dte": 45,
            "credit_short_delta_min": 0.15,
            "credit_short_delta_max": 0.30,
            "credit_min_return_on_risk_pct": 15,
            "credit_min_probability_of_profit": 65,
            "credit_min_open_interest": 100,
            "credit_max_bid_ask_spread_pct": 20,
            "credit_exclude_earnings_before_expiration": True,
            "debit_max_dte": 45,
            "debit_long_delta_min": 0.50,
            "debit_long_delta_max": 0.70,
            "debit_short_delta_min": 0.20,
            "debit_short_delta_max": 0.35,
            "debit_min_reward_to_risk": 1.6,
            "debit_max_debit_as_pct_of_width": 45,
        },
        "balanced": {
            "credit_target_delta": 0.25,
            "neutral_target_delta": 0.20,
            "debit_target_delta": 0.48,
            "preferred_min_dte": 21,
            "preferred_max_dte": 50,
            "credit_iv_floor": 20,
            "debit_iv_ceiling": 35,
            "max_width": 15,
            "credit_max_dte": 45,
            "credit_short_delta_min": 0.20,
            "credit_short_delta_max": 0.35,
            "credit_min_return_on_risk_pct": 18,
            "credit_min_probability_of_profit": 60,
            "credit_min_open_interest": 100,
            "credit_max_bid_ask_spread_pct": 20,
            "credit_exclude_earnings_before_expiration": True,
            "debit_max_dte": 60,
            "debit_long_delta_min": 0.45,
            "debit_long_delta_max": 0.70,
            "debit_short_delta_min": 0.20,
            "debit_short_delta_max": 0.40,
            "debit_min_reward_to_risk": 1.5,
            "debit_max_debit_as_pct_of_width": 50,
        },
        "aggressive": {
            "credit_target_delta": 0.35,
            "neutral_target_delta": 0.28,
            "debit_target_delta": 0.38,
            "preferred_min_dte": 14,
            "preferred_max_dte": 60,
            "credit_iv_floor": 16,
            "debit_iv_ceiling": 40,
            "max_width": 25,
            "credit_max_dte": 60,
            "credit_short_delta_min": 0.30,
            "credit_short_delta_max": 0.45,
            "credit_min_return_on_risk_pct": 25,
            "credit_min_probability_of_profit": 50,
            "credit_min_open_interest": 100,
            "credit_max_bid_ask_spread_pct": 25,
            "credit_exclude_earnings_before_expiration": False,
            "debit_max_dte": 60,
            "debit_long_delta_min": 0.40,
            "debit_long_delta_max": 0.75,
            "debit_short_delta_min": 0.15,
            "debit_short_delta_max": 0.45,
            "debit_min_reward_to_risk": 1.2,
            "debit_max_debit_as_pct_of_width": 60,
        },
    }
    profile = dict(profiles[_normalize_risk_profile(risk_profile)])
    if overrides:
        profile.update(overrides)
    return profile


def _round_if_number(value, digits=2):
    if value is None:
        return None
    return round(value, digits)


def _contract_mid(contract):
    mid = _to_float(contract.get("mid"))
    if mid is not None:
        return mid
    bid = _to_float(contract.get("bid"))
    ask = _to_float(contract.get("ask"))
    if bid is not None and ask is not None and ask > 0:
        return round((bid + ask) / 2, 4)
    return None


def _contract_spread_pct(contract):
    bid = _to_float(contract.get("bid"))
    ask = _to_float(contract.get("ask"))
    if bid is None or ask is None or ask <= 0:
        return None
    return ((ask - bid) / ask) * 100


def _spread_leg_payload(contract, *, action, option_type):
    return {
        "action": action,
        "option_type": option_type,
        "strike": _to_float(contract.get("strike")),
        "bid": _to_float(contract.get("bid")),
        "ask": _to_float(contract.get("ask")),
        "mid": _contract_mid(contract),
        "delta": _to_float(contract.get("delta")),
        "iv": _normalize_iv_percent(contract.get("iv")),
        "volume": _to_int(contract.get("volume")),
        "open_interest": _to_int(contract.get("open_interest")),
    }


def _option_liquidity_metrics(legs):
    volumes = [leg.get("volume") for leg in legs if leg.get("volume") is not None]
    open_interest = [
        leg.get("open_interest")
        for leg in legs
        if leg.get("open_interest") is not None
    ]
    leg_spreads = []
    for leg in legs:
        spread_pct = _contract_spread_pct(leg)
        if spread_pct is not None:
            leg_spreads.append(spread_pct)
    return {
        "min_volume": min(volumes) if volumes else None,
        "min_open_interest": min(open_interest) if open_interest else None,
        "avg_leg_spread_pct": (
            sum(leg_spreads) / len(leg_spreads) if leg_spreads else None
        ),
    }


def _spread_liquidity_component(legs):
    metrics = _option_liquidity_metrics(legs)
    score = 0
    reasons = []
    warnings = []

    min_volume = metrics["min_volume"]
    if min_volume is not None:
        if min_volume >= 500:
            score += 5
        elif min_volume >= 100:
            score += 4
        elif min_volume >= 20:
            score += 2
        else:
            warnings.append("Spread leg volume is low.")

    min_open_interest = metrics["min_open_interest"]
    if min_open_interest is not None:
        if min_open_interest >= 2000:
            score += 5
        elif min_open_interest >= 500:
            score += 4
        elif min_open_interest >= 100:
            score += 2
        else:
            warnings.append("Spread leg open interest is low.")

    avg_leg_spread_pct = metrics["avg_leg_spread_pct"]
    if avg_leg_spread_pct is not None:
        if avg_leg_spread_pct <= 5:
            score += 5
            reasons.append("Option bid/ask spreads are tight.")
        elif avg_leg_spread_pct <= 10:
            score += 4
        elif avg_leg_spread_pct <= 20:
            score += 2
            warnings.append("Spread liquidity is acceptable but not excellent.")
        else:
            warnings.append("Option bid/ask spreads are wide.")
    else:
        warnings.append("Bid/ask spread data is incomplete.")

    return min(score, 15), reasons, warnings, metrics


def _spread_quality_component(quality_score):
    score = 0
    reasons = []
    warnings = []
    if quality_score is not None:
        if quality_score >= 85:
            score = 10
            reasons.append("Underlying stock quality is high.")
        elif quality_score >= 75:
            score = 8
        elif quality_score >= 65:
            score = 6
        elif quality_score >= 55:
            score = 3
            warnings.append("Underlying quality score is only moderate.")
        else:
            warnings.append("Underlying quality score is weak.")
    return score, reasons, warnings


def _spread_technical_component(*, bias, technical_score):
    reasons = []
    warnings = []
    score = 0

    if bias == "bullish":
        if technical_score == "Strong Buy":
            score = 7
            reasons.append("Technical trend supports a bullish spread.")
        elif technical_score == "Buy":
            score = 6
        elif technical_score == "Neutral":
            score = 3
        elif technical_score == "Sell":
            score = 1
            warnings.append("Technical trend is not aligned with a bullish thesis.")
        elif technical_score == "Strong Sell":
            warnings.append("Technical trend is strongly against a bullish thesis.")
    elif bias == "bearish":
        if technical_score == "Strong Sell":
            score = 7
            reasons.append("Technical trend supports a bearish spread.")
        elif technical_score == "Sell":
            score = 6
        elif technical_score == "Neutral":
            score = 3
        elif technical_score == "Buy":
            score = 1
            warnings.append("Technical trend is not aligned with a bearish thesis.")
        elif technical_score == "Strong Buy":
            warnings.append("Technical trend is strongly against a bearish thesis.")
    else:
        if technical_score == "Neutral":
            score = 7
            reasons.append("Neutral technical trend suits a range-bound spread.")
        elif technical_score in {"Buy", "Sell"}:
            score = 4
        elif technical_score in {"Strong Buy", "Strong Sell"}:
            score = 2
            warnings.append("A strong trend reduces the appeal of a neutral spread.")

    return score, reasons, warnings


def _spread_dte_component(dte, profile):
    reasons = []
    warnings = []
    if profile["preferred_min_dte"] <= dte <= profile["preferred_max_dte"]:
        return 10, reasons, warnings
    if dte < 10:
        warnings.append("DTE is short, which increases gamma risk.")
        return 4, reasons, warnings
    if dte > 75:
        warnings.append("DTE is long, which ties up risk for longer.")
        return 4, reasons, warnings
    return 7, reasons, warnings


def _spread_iv_component(*, avg_iv, profile, trade_structure):
    reasons = []
    warnings = []
    if avg_iv is None:
        warnings.append("IV data is incomplete.")
        return 2, reasons, warnings

    if trade_structure == "credit":
        if avg_iv >= profile["credit_iv_floor"] + 8:
            reasons.append("IV is supportive for premium-selling spreads.")
            return 5, reasons, warnings
        if avg_iv >= profile["credit_iv_floor"]:
            return 4, reasons, warnings
        if avg_iv >= profile["credit_iv_floor"] - 5:
            warnings.append("IV is only average for a credit spread.")
            return 2, reasons, warnings
        warnings.append("IV is low for a premium-selling spread.")
        return 1, reasons, warnings

    if avg_iv <= max(profile["debit_iv_ceiling"] - 5, 1):
        reasons.append("IV is favorable for a debit spread.")
        return 5, reasons, warnings
    if avg_iv <= profile["debit_iv_ceiling"]:
        return 4, reasons, warnings
    if avg_iv <= profile["debit_iv_ceiling"] + 10:
        warnings.append("IV is somewhat elevated for a debit spread.")
        return 2, reasons, warnings
    warnings.append("IV is high for a debit spread.")
    return 1, reasons, warnings


def _spread_delta_fit_component(*, actual_delta, target_delta):
    reasons = []
    warnings = []
    if actual_delta is None:
        warnings.append("Delta data is incomplete for the spread.")
        return 4, reasons, warnings

    gap = abs(actual_delta - target_delta)
    if gap <= 0.05:
        reasons.append("Delta profile fits the requested risk style.")
        return 10, reasons, warnings
    if gap <= 0.10:
        return 7, reasons, warnings
    if gap <= 0.15:
        warnings.append("Delta is a bit outside the preferred risk range.")
        return 4, reasons, warnings
    warnings.append("Delta is materially outside the preferred risk range.")
    return 1, reasons, warnings


def _clamp_score(value, *, lower=0.0, upper=1.0):
    if value is None:
        return None
    return max(lower, min(upper, value))


def _linear_weighted_component(*, value, floor, ceiling, weight):
    if value is None:
        return 0
    if ceiling <= floor:
        return weight if value >= ceiling else 0
    normalized = _clamp_score((value - floor) / (ceiling - floor))
    return round(normalized * weight, 2)


def _credit_spread_filters(profile, *, requested_max_dte=None):
    profile_max_dte = profile.get("credit_max_dte")
    if requested_max_dte is None:
        effective_max_dte = profile_max_dte
    elif profile_max_dte is None:
        effective_max_dte = requested_max_dte
    else:
        effective_max_dte = min(requested_max_dte, profile_max_dte)

    short_delta_min = profile["credit_short_delta_min"]
    short_delta_max = profile["credit_short_delta_max"]
    return {
        "max_dte": effective_max_dte,
        "short_delta_min": short_delta_min,
        "short_delta_max": short_delta_max,
        "short_delta_target": round((short_delta_min + short_delta_max) / 2, 4),
        "min_return_on_risk_pct": profile["credit_min_return_on_risk_pct"],
        "min_probability_of_profit": profile["credit_min_probability_of_profit"],
        "min_open_interest": profile["credit_min_open_interest"],
        "max_bid_ask_spread_pct": profile["credit_max_bid_ask_spread_pct"],
        "exclude_earnings_before_expiration": profile[
            "credit_exclude_earnings_before_expiration"
        ],
        "allow_earnings": not profile["credit_exclude_earnings_before_expiration"],
    }


def _debit_spread_filters(profile, *, requested_max_dte=None):
    profile_max_dte = profile.get("debit_max_dte")
    if requested_max_dte is None:
        effective_max_dte = profile_max_dte
    elif profile_max_dte is None:
        effective_max_dte = requested_max_dte
    else:
        effective_max_dte = min(requested_max_dte, profile_max_dte)

    return {
        "max_dte": effective_max_dte,
        "long_delta_min": profile["debit_long_delta_min"],
        "long_delta_max": profile["debit_long_delta_max"],
        "short_delta_min": profile["debit_short_delta_min"],
        "short_delta_max": profile["debit_short_delta_max"],
        "long_delta_target": round(
            (profile["debit_long_delta_min"] + profile["debit_long_delta_max"]) / 2,
            4,
        ),
        "short_delta_target": round(
            (profile["debit_short_delta_min"] + profile["debit_short_delta_max"]) / 2,
            4,
        ),
        "min_reward_to_risk": profile["debit_min_reward_to_risk"],
        "max_debit_as_pct_of_width": profile["debit_max_debit_as_pct_of_width"],
    }


def _spread_rating(score):
    if score >= 80:
        return "Excellent"
    if score >= 70:
        return "Good"
    if score >= 60:
        return "Acceptable"
    return "Avoid"


def _average_iv_from_legs(legs, symbol_iv=None):
    ivs = []
    for leg in legs:
        normalized_leg_iv = _normalize_iv_percent(leg.get("iv"))
        if normalized_leg_iv is not None:
            ivs.append(normalized_leg_iv)
    if symbol_iv is not None:
        normalized_symbol_iv = _normalize_iv_percent(symbol_iv)
        if normalized_symbol_iv is not None:
            ivs.append(normalized_symbol_iv)
    if not ivs:
        return None
    return round(sum(ivs) / len(ivs), 2)


def _width_allowed(width_value, *, preferred_width, max_width):
    if width_value is None or width_value <= 0:
        return False
    if preferred_width is not None:
        return abs(width_value - preferred_width) <= 0.05
    return width_value <= max_width


def _group_contracts_by_expiration(contracts, *, today, max_dte=None):
    grouped = {}
    for contract in contracts:
        expiration_date = contract.get("expiration_date")
        if expiration_date is None:
            continue
        dte = (expiration_date - today).days
        if dte <= 0:
            continue
        if max_dte is not None and dte > max_dte:
            continue
        grouped.setdefault(expiration_date, []).append(contract)
    for expiration_date in grouped:
        grouped[expiration_date].sort(key=lambda item: item.get("strike") or 0)
    return grouped


def _resolve_spread_directional_view(*, directional_view, technical_score):
    normalized = _normalize_directional_view(directional_view)
    if normalized != "auto":
        return normalized
    if technical_score in {"Strong Buy", "Buy"}:
        return "bullish"
    if technical_score in {"Strong Sell", "Sell"}:
        return "bearish"
    return "neutral"


def _candidate_sort_key(candidate):
    return (
        candidate.get("spread_score") or 0,
        candidate.get("estimated_probability_of_profit") or 0,
        candidate.get("return_on_risk_pct")
        or candidate.get("reward_to_risk")
        or 0,
    )


def _spread_candidate_return_on_risk_pct(candidate):
    value = _to_float(candidate.get("return_on_risk_pct"))
    if value is not None:
        return round(value, 2)
    reward_to_risk = _to_float(candidate.get("reward_to_risk"))
    if reward_to_risk is None:
        return None
    return round(reward_to_risk * 100, 2)


def _spread_candidate_short_leg_deltas(candidate):
    deltas = []
    for leg in candidate.get("legs") or []:
        if str(leg.get("action") or "").lower() != "sell":
            continue
        delta = _to_float(leg.get("delta"))
        if delta is not None:
            deltas.append(round(abs(delta), 4))
    return deltas


def _spread_candidate_earnings_before_expiration(*, next_earnings_date, expiration):
    expiration_date = _parse_date(expiration)
    if not next_earnings_date or not expiration_date:
        return False
    return bool(date.today() <= next_earnings_date <= expiration_date)


def _filter_spread_candidates(
    evaluation,
    *,
    min_return_on_risk_pct=None,
    min_probability_of_profit=None,
    max_risk=None,
    max_short_delta=None,
    exclude_earnings=None,
):
    matched_candidates = []
    for candidate in evaluation["all_candidates"]:
        normalized_return_on_risk = _spread_candidate_return_on_risk_pct(candidate)
        probability_of_profit = _to_float(
            candidate.get("estimated_probability_of_profit")
        )
        candidate_max_loss = _to_float(candidate.get("max_loss"))
        short_leg_deltas = _spread_candidate_short_leg_deltas(candidate)
        max_candidate_short_delta = max(short_leg_deltas) if short_leg_deltas else None
        earnings_before_expiration = _spread_candidate_earnings_before_expiration(
            next_earnings_date=evaluation["next_earnings_date"],
            expiration=candidate.get("expiration"),
        )

        if min_return_on_risk_pct is not None and (
            normalized_return_on_risk is None
            or normalized_return_on_risk < min_return_on_risk_pct
        ):
            continue
        if min_probability_of_profit is not None and (
            probability_of_profit is None
            or probability_of_profit < min_probability_of_profit
        ):
            continue
        if max_risk is not None and (
            candidate_max_loss is None or candidate_max_loss > max_risk
        ):
            continue
        if max_short_delta is not None and (
            max_candidate_short_delta is None
            or max_candidate_short_delta > abs(max_short_delta)
        ):
            continue
        if exclude_earnings is True and earnings_before_expiration:
            continue

        enriched_candidate = dict(candidate)
        enriched_candidate["return_on_risk_pct"] = normalized_return_on_risk
        enriched_candidate["short_leg_deltas"] = short_leg_deltas
        enriched_candidate["max_short_delta"] = (
            round(max_candidate_short_delta, 4)
            if max_candidate_short_delta is not None
            else None
        )
        enriched_candidate["earnings_before_expiration"] = earnings_before_expiration
        matched_candidates.append(enriched_candidate)

    matched_candidates.sort(key=_candidate_sort_key, reverse=True)
    return matched_candidates


def _score_vertical_credit_spread(
    *,
    net_credit,
    width_value,
    buffer_pct,
    pop,
    dte,
    short_delta,
    avg_iv,
    legs,
    quality_score,
    technical_score,
    next_earnings_date,
    expiration_date,
    profile,
    bias,
):
    reasons = []
    warnings = []
    score = 0
    score_breakdown = {}
    filter_settings = _credit_spread_filters(profile)

    max_profit = net_credit * 100
    max_loss = (width_value - net_credit) * 100
    return_on_risk_pct = (max_profit / max_loss * 100) if max_loss > 0 else None
    abs_short_delta = abs(short_delta) if short_delta is not None else None
    earnings_before_expiration = bool(
        next_earnings_date
        and expiration_date
        and next_earnings_date <= expiration_date
        and date.today() <= next_earnings_date
    )

    return_component = _linear_weighted_component(
        value=return_on_risk_pct,
        floor=filter_settings["min_return_on_risk_pct"],
        ceiling=max(filter_settings["min_return_on_risk_pct"] + 20, 35),
        weight=20,
    )
    if return_on_risk_pct is not None and return_on_risk_pct >= 25:
        reasons.append("Return on risk is strong for a defined-risk credit spread.")
    elif return_on_risk_pct is not None and return_on_risk_pct < filter_settings["min_return_on_risk_pct"]:
        warnings.append("Return on risk is below the preferred minimum.")
    score += return_component
    score_breakdown["return_on_risk"] = return_component

    pop_component = _linear_weighted_component(
        value=pop,
        floor=filter_settings["min_probability_of_profit"],
        ceiling=85,
        weight=20,
    )
    if pop is not None and pop >= 72:
        reasons.append("Probability of profit is strong for a credit spread.")
    elif pop is not None and pop < filter_settings["min_probability_of_profit"]:
        warnings.append("Probability of profit is below the preferred minimum.")
    score += pop_component
    score_breakdown["probability_of_profit"] = pop_component

    buffer_component = _linear_weighted_component(
        value=buffer_pct,
        floor=2,
        ceiling=12,
        weight=15,
    )
    if buffer_pct is not None and buffer_pct >= 8:
        reasons.append("Short strike leaves a healthy buffer from the current price.")
    elif buffer_pct is not None and buffer_pct < 4:
        warnings.append("Short strike sits fairly close to the current price.")
    score += buffer_component
    score_breakdown["downside_buffer" if bias == "bullish" else "upside_buffer"] = (
        buffer_component
    )

    delta_component = 0
    if abs_short_delta is None:
        warnings.append("Delta data is incomplete for the spread.")
    else:
        delta_center = filter_settings["short_delta_target"]
        delta_distance = abs(abs_short_delta - delta_center)
        max_distance = max(
            delta_center - filter_settings["short_delta_min"],
            filter_settings["short_delta_max"] - delta_center,
            0.0001,
        )
        delta_component = round(
            _clamp_score(1 - (delta_distance / max_distance)) * 15,
            2,
        )
        if delta_distance <= 0.03:
            reasons.append("Short strike delta fits the requested credit-spread risk range.")
        elif abs_short_delta > filter_settings["short_delta_max"]:
            warnings.append("Short strike delta is too aggressive for the preferred risk range.")
        elif abs_short_delta < filter_settings["short_delta_min"]:
            warnings.append("Short strike delta is below the preferred premium range.")
    score += delta_component
    score_breakdown["short_strike_delta"] = delta_component

    liquidity_component = 0
    liquidity_metrics = _option_liquidity_metrics(legs)
    min_volume = liquidity_metrics["min_volume"]
    min_open_interest = liquidity_metrics["min_open_interest"]
    avg_leg_spread_pct = liquidity_metrics["avg_leg_spread_pct"]

    oi_component = _linear_weighted_component(
        value=min_open_interest,
        floor=filter_settings["min_open_interest"],
        ceiling=max(filter_settings["min_open_interest"] * 8, 800),
        weight=6,
    )
    volume_component = _linear_weighted_component(
        value=min_volume,
        floor=20,
        ceiling=500,
        weight=4,
    )
    spread_component = 0
    if avg_leg_spread_pct is None:
        warnings.append("Bid/ask spread data is incomplete.")
    else:
        spread_component = round(
            _clamp_score(
                (filter_settings["max_bid_ask_spread_pct"] - avg_leg_spread_pct)
                / filter_settings["max_bid_ask_spread_pct"]
            )
            * 5,
            2,
        )
        if avg_leg_spread_pct <= 8:
            reasons.append("Option bid/ask spreads are tight.")
        elif avg_leg_spread_pct > filter_settings["max_bid_ask_spread_pct"]:
            warnings.append("Option bid/ask spreads are wider than the preferred maximum.")

    if min_open_interest is not None and min_open_interest < filter_settings["min_open_interest"]:
        warnings.append("Spread leg open interest is below the preferred minimum.")
    if min_volume is not None and min_volume < 20:
        warnings.append("Spread leg volume is low.")
    if avg_leg_spread_pct is not None and avg_leg_spread_pct > 10 and avg_leg_spread_pct <= filter_settings["max_bid_ask_spread_pct"]:
        warnings.append("Spread liquidity is acceptable but not excellent.")

    liquidity_component = round(oi_component + volume_component + spread_component, 2)
    score += liquidity_component
    score_breakdown["liquidity_spread_quality"] = liquidity_component

    technical_component = 0
    if bias == "bullish":
        if technical_score == "Strong Buy":
            technical_component = 10
            reasons.append("Technical trend supports a bullish spread.")
        elif technical_score == "Buy":
            technical_component = 8
        elif technical_score == "Neutral":
            technical_component = 5
        elif technical_score == "Sell":
            technical_component = 2
            warnings.append("Technical trend is not aligned with a bullish thesis.")
        elif technical_score == "Strong Sell":
            warnings.append("Technical trend is strongly against a bullish thesis.")
    else:
        if technical_score == "Strong Sell":
            technical_component = 10
            reasons.append("Technical trend supports a bearish spread.")
        elif technical_score == "Sell":
            technical_component = 8
        elif technical_score == "Neutral":
            technical_component = 5
        elif technical_score == "Buy":
            technical_component = 2
            warnings.append("Technical trend is not aligned with a bearish thesis.")
        elif technical_score == "Strong Buy":
            warnings.append("Technical trend is strongly against a bearish thesis.")
    score += technical_component
    score_breakdown["technical_trend_alignment"] = technical_component

    earnings_component = 5
    if earnings_before_expiration:
        earnings_component = 0
        warnings.append("Earnings before expiration.")
    score += earnings_component
    score_breakdown["earnings_risk_penalty"] = earnings_component

    if dte > profile["preferred_max_dte"]:
        warnings.append("DTE is long, which ties up risk for longer.")
    elif dte < 10:
        warnings.append("DTE is short, which increases gamma risk.")

    if avg_iv is not None and avg_iv < profile["credit_iv_floor"]:
        warnings.append("IV is low for a premium-selling spread.")

    if quality_score is not None:
        if quality_score >= 85:
            reasons.append("Underlying stock quality is high.")
        elif quality_score < 65:
            warnings.append("Underlying quality score is below the preferred range for assignment comfort.")

    if short_delta is not None and abs(short_delta) >= 0.30:
        warnings.append("Short strike delta is moderately high.")

    return {
        "spread_score": max(0, min(100, round(score))),
        "score_breakdown": score_breakdown,
        "reasons": _dedupe_preserve_order(reasons),
        "warnings": _dedupe_preserve_order(warnings),
        "return_on_risk_pct": (
            round(return_on_risk_pct, 2) if return_on_risk_pct is not None else None
        ),
        "liquidity_metrics": liquidity_metrics,
        "earnings_before_expiration": earnings_before_expiration,
        "filters_used": filter_settings,
    }


def _score_vertical_debit_spread(
    *,
    net_debit,
    width_value,
    move_to_breakeven_pct,
    pop,
    dte,
    long_delta,
    avg_iv,
    legs,
    quality_score,
    technical_score,
    next_earnings_date,
    expiration_date,
    profile,
    bias,
):
    reasons = []
    warnings = []
    score = 0
    score_breakdown = {}
    filter_settings = _debit_spread_filters(profile)

    max_profit = (width_value - net_debit) * 100
    max_loss = net_debit * 100
    reward_to_risk = (max_profit / max_loss) if max_loss > 0 else None
    long_delta_component = 0
    if long_delta is None:
        warnings.append("Delta data is incomplete for the long option.")
    else:
        abs_long_delta = abs(long_delta)
        target = filter_settings["long_delta_target"]
        max_distance = max(
            target - filter_settings["long_delta_min"],
            filter_settings["long_delta_max"] - target,
            0.0001,
        )
        long_delta_component = round(
            _clamp_score(1 - (abs(abs_long_delta - target) / max_distance)) * 5,
            2,
        )
        if abs_long_delta < filter_settings["long_delta_min"]:
            warnings.append("Long strike delta is below the preferred debit-spread range.")
        elif abs_long_delta > filter_settings["long_delta_max"]:
            warnings.append("Long strike delta is above the preferred debit-spread range.")

    return_component = _linear_weighted_component(
        value=reward_to_risk,
        floor=filter_settings["min_reward_to_risk"],
        ceiling=max(filter_settings["min_reward_to_risk"] + 1.0, 2.5),
        weight=25,
    )
    if reward_to_risk is not None and reward_to_risk >= 1.8:
        reasons.append("Reward-to-risk is strong for a debit spread.")
    elif reward_to_risk is not None and reward_to_risk < filter_settings["min_reward_to_risk"]:
        warnings.append("Reward-to-risk is below the preferred minimum.")
    score += return_component
    score_breakdown["reward_to_risk_ratio"] = return_component

    breakeven_component = _linear_weighted_component(
        value=(
            max(0, 8 - move_to_breakeven_pct)
            if move_to_breakeven_pct is not None
            else None
        ),
        floor=1,
        ceiling=7,
        weight=20,
    )
    if move_to_breakeven_pct is not None and move_to_breakeven_pct <= 3:
        reasons.append("The stock does not need to move much to reach breakeven.")
    elif move_to_breakeven_pct is not None and move_to_breakeven_pct > 7:
        warnings.append("The stock needs a sizable move to reach breakeven.")
    score += breakeven_component
    score_breakdown["breakeven_distance"] = breakeven_component

    technical_component = 0
    if bias == "bullish":
        if technical_score == "Strong Buy":
            technical_component = 20
            reasons.append("Technical trend strongly supports a bullish debit spread.")
        elif technical_score == "Buy":
            technical_component = 16
        elif technical_score == "Neutral":
            technical_component = 8
        elif technical_score == "Sell":
            technical_component = 2
            warnings.append("Technical trend does not support a bullish debit spread.")
        elif technical_score == "Strong Sell":
            warnings.append("Technical trend is strongly against a bullish debit spread.")
    else:
        if technical_score == "Strong Sell":
            technical_component = 20
            reasons.append("Technical trend strongly supports a bearish debit spread.")
        elif technical_score == "Sell":
            technical_component = 16
        elif technical_score == "Neutral":
            technical_component = 8
        elif technical_score == "Buy":
            technical_component = 2
            warnings.append("Technical trend does not support a bearish debit spread.")
        elif technical_score == "Strong Buy":
            warnings.append("Technical trend is strongly against a bearish debit spread.")
    score += technical_component
    score_breakdown["technical_trend_alignment"] = technical_component

    liquidity_component, liq_reasons, liq_warnings, liquidity_metrics = (
        _spread_liquidity_component(legs)
    )
    score += liquidity_component
    score_breakdown["liquidity"] = liquidity_component
    reasons.extend(liq_reasons)
    warnings.extend(liq_warnings)

    dte_component = 0
    if filter_settings["max_dte"] is not None and dte > filter_settings["max_dte"]:
        warnings.append("DTE is above the preferred debit-spread limit.")
        dte_component = 2
    elif 21 <= dte <= 45:
        dte_component = 10
    elif 14 <= dte <= filter_settings["max_dte"]:
        dte_component = 8
    elif dte < 10:
        dte_component = 3
        warnings.append("DTE is short, which increases gamma risk.")
    else:
        dte_component = 6
    score += dte_component
    score_breakdown["dte_suitability"] = dte_component

    iv_component = 0
    if avg_iv is None:
        warnings.append("IV data is incomplete.")
    elif avg_iv <= max(profile["debit_iv_ceiling"] - 8, 1):
        iv_component = 10
        reasons.append("IV is favorable for a debit spread.")
    elif avg_iv <= profile["debit_iv_ceiling"]:
        iv_component = 8
    elif avg_iv <= profile["debit_iv_ceiling"] + 5:
        iv_component = 4
        warnings.append("IV is somewhat elevated for a debit spread.")
    else:
        iv_component = 1
        warnings.append("IV is high for a debit spread.")
    score += iv_component
    score_breakdown["iv_environment"] = iv_component

    if quality_score is not None and quality_score >= 85:
        reasons.append("Underlying stock quality is high.")
    elif quality_score is not None and quality_score < 60:
        warnings.append("Underlying quality score is weak.")

    if (
        next_earnings_date
        and expiration_date
        and next_earnings_date <= expiration_date
        and date.today() <= next_earnings_date
    ):
        warnings.append("Earnings before expiration.")

    return {
        "spread_score": max(0, min(100, round(score))),
        "score_breakdown": score_breakdown,
        "reasons": _dedupe_preserve_order(reasons),
        "warnings": _dedupe_preserve_order(warnings),
        "reward_to_risk": round(reward_to_risk, 2) if reward_to_risk is not None else None,
        "liquidity_metrics": liquidity_metrics,
        "filters_used": filter_settings,
        "long_delta_fit_component": long_delta_component,
    }


def _score_neutral_credit_spread(
    *,
    net_credit,
    width_value,
    inner_buffer_pct,
    pop,
    dte,
    avg_short_delta,
    avg_iv,
    legs,
    quality_score,
    technical_score,
    next_earnings_date,
    expiration_date,
    profile,
):
    reasons = []
    warnings = []
    score = 0
    score_breakdown = {}

    max_profit = net_credit * 100
    max_loss = (width_value - net_credit) * 100
    return_on_risk_pct = (max_profit / max_loss * 100) if max_loss > 0 else None

    delta_component, delta_reasons, delta_warnings = _spread_delta_fit_component(
        actual_delta=avg_short_delta,
        target_delta=profile["neutral_target_delta"],
    )
    score += delta_component
    score_breakdown["delta_fit"] = delta_component
    reasons.extend(delta_reasons)
    warnings.extend(delta_warnings)

    return_component = 0
    if return_on_risk_pct is not None:
        if return_on_risk_pct >= 30:
            return_component = 18
        elif return_on_risk_pct >= 22:
            return_component = 15
        elif return_on_risk_pct >= 16:
            return_component = 11
        elif return_on_risk_pct >= 10:
            return_component = 7
        else:
            return_component = 3
            warnings.append("Credit received is light relative to the risk range.")
    score += return_component
    score_breakdown["return_on_risk"] = return_component

    edge_component = 0
    if pop is not None:
        if pop >= 70:
            edge_component += 10
            reasons.append("Probability of profit is solid for a neutral spread.")
        elif pop >= 60:
            edge_component += 8
        elif pop >= 50:
            edge_component += 5
        else:
            edge_component += 2
            warnings.append("Probability of profit is only moderate for a neutral setup.")
    if inner_buffer_pct is not None:
        if inner_buffer_pct >= 8:
            edge_component += 8
        elif inner_buffer_pct >= 5:
            edge_component += 6
        elif inner_buffer_pct >= 2:
            edge_component += 4
        else:
            edge_component += 1
            warnings.append("The short strikes sit close to the current stock price.")
    score += min(edge_component, 18)
    score_breakdown["probability_and_range"] = min(edge_component, 18)

    liquidity_component, liq_reasons, liq_warnings, liquidity_metrics = (
        _spread_liquidity_component(legs)
    )
    score += liquidity_component
    score_breakdown["liquidity"] = liquidity_component
    reasons.extend(liq_reasons)
    warnings.extend(liq_warnings)

    dte_component, dte_reasons, dte_warnings = _spread_dte_component(dte, profile)
    score += dte_component
    score_breakdown["dte"] = dte_component
    reasons.extend(dte_reasons)
    warnings.extend(dte_warnings)

    iv_component, iv_reasons, iv_warnings = _spread_iv_component(
        avg_iv=avg_iv,
        profile=profile,
        trade_structure="credit",
    )
    score += iv_component
    score_breakdown["iv_alignment"] = iv_component
    reasons.extend(iv_reasons)
    warnings.extend(iv_warnings)

    quality_component, quality_reasons, quality_warnings = _spread_quality_component(
        quality_score
    )
    score += quality_component
    score_breakdown["stock_quality_score"] = quality_component
    reasons.extend(quality_reasons)
    warnings.extend(quality_warnings)

    technical_component, technical_reasons, technical_warnings = (
        _spread_technical_component(bias="neutral", technical_score=technical_score)
    )
    score += technical_component
    score_breakdown["technical_alignment"] = technical_component
    reasons.extend(technical_reasons)
    warnings.extend(technical_warnings)

    earnings_component = 7
    if (
        next_earnings_date
        and expiration_date
        and next_earnings_date <= expiration_date
        and date.today() <= next_earnings_date
    ):
        earnings_component = 1
        warnings.append("Earnings before expiration.")
    score += earnings_component
    score_breakdown["earnings_risk"] = earnings_component

    return {
        "spread_score": max(0, min(100, round(score))),
        "score_breakdown": score_breakdown,
        "reasons": _dedupe_preserve_order(reasons),
        "warnings": _dedupe_preserve_order(warnings),
        "return_on_risk_pct": (
            round(return_on_risk_pct, 2) if return_on_risk_pct is not None else None
        ),
        "liquidity_metrics": liquidity_metrics,
    }


def _build_bull_put_credit_spreads(
    *,
    put_contracts,
    stock_price,
    quality_score,
    technical_score,
    next_earnings_date,
    symbol_iv,
    profile,
    today,
    preferred_width=None,
    max_dte=None,
    min_credit=None,
    max_risk=None,
):
    grouped_puts = _group_contracts_by_expiration(
        put_contracts,
        today=today,
        max_dte=_credit_spread_filters(profile, requested_max_dte=max_dte)["max_dte"],
    )
    candidates = []
    filter_settings = _credit_spread_filters(profile, requested_max_dte=max_dte)

    for expiration_date, contracts in grouped_puts.items():
        for short_index, short_put in enumerate(contracts):
            short_strike = _to_float(short_put.get("strike"))
            short_mid = _contract_mid(short_put)
            if short_strike is None or short_mid is None or short_mid <= 0:
                continue
            if stock_price is not None and short_strike >= stock_price:
                continue
            for long_put in contracts[:short_index]:
                long_strike = _to_float(long_put.get("strike"))
                long_mid = _contract_mid(long_put)
                if long_strike is None or long_mid is None:
                    continue
                width_value = short_strike - long_strike
                if not _width_allowed(
                    width_value,
                    preferred_width=preferred_width,
                    max_width=profile["max_width"],
                ):
                    continue
                net_credit = short_mid - long_mid
                if net_credit <= 0 or net_credit >= width_value:
                    continue
                max_loss = (width_value - net_credit) * 100
                if min_credit is not None and net_credit < min_credit:
                    continue
                if max_risk is not None and max_loss > max_risk:
                    continue

                short_delta = _to_float(short_put.get("delta"))
                pop = (
                    round((1 - abs(short_delta)) * 100, 2)
                    if short_delta is not None
                    else None
                )
                downside_buffer_pct = (
                    ((stock_price - short_strike) / stock_price) * 100
                    if stock_price
                    else None
                )
                return_on_risk_pct = (
                    (net_credit * 100) / max_loss * 100 if max_loss > 0 else None
                )
                min_open_interest = min(
                    value
                    for value in (
                        _to_int(short_put.get("open_interest")),
                        _to_int(long_put.get("open_interest")),
                    )
                    if value is not None
                ) if any(
                    value is not None
                    for value in (
                        _to_int(short_put.get("open_interest")),
                        _to_int(long_put.get("open_interest")),
                    )
                ) else None
                avg_leg_spread_pct = _option_liquidity_metrics([short_put, long_put])[
                    "avg_leg_spread_pct"
                ]
                earnings_before_expiration = bool(
                    next_earnings_date
                    and today <= next_earnings_date <= expiration_date
                )

                if short_delta is None:
                    continue
                abs_short_delta = abs(short_delta)
                if not (
                    filter_settings["short_delta_min"]
                    <= abs_short_delta
                    <= filter_settings["short_delta_max"]
                ):
                    continue
                if (
                    return_on_risk_pct is None
                    or return_on_risk_pct
                    < filter_settings["min_return_on_risk_pct"]
                ):
                    continue
                if (
                    pop is None
                    or pop < filter_settings["min_probability_of_profit"]
                ):
                    continue
                if (
                    min_open_interest is not None
                    and min_open_interest < filter_settings["min_open_interest"]
                ):
                    continue
                if (
                    avg_leg_spread_pct is None
                    or avg_leg_spread_pct > filter_settings["max_bid_ask_spread_pct"]
                ):
                    continue
                if (
                    filter_settings["exclude_earnings_before_expiration"]
                    and earnings_before_expiration
                ):
                    continue

                legs = [short_put, long_put]
                avg_iv = _average_iv_from_legs(legs, symbol_iv=symbol_iv)
                scoring = _score_vertical_credit_spread(
                    net_credit=net_credit,
                    width_value=width_value,
                    buffer_pct=downside_buffer_pct,
                    pop=pop,
                    dte=(expiration_date - today).days,
                    short_delta=short_delta,
                    avg_iv=avg_iv,
                    legs=legs,
                    quality_score=quality_score,
                    technical_score=technical_score,
                    next_earnings_date=next_earnings_date,
                    expiration_date=expiration_date,
                    profile=profile,
                    bias="bullish",
                )
                candidate = {
                    "spread_type": "bull_put_credit_spread",
                    "expiration": expiration_date.isoformat(),
                    "dte": (expiration_date - today).days,
                    "legs": [
                        _spread_leg_payload(short_put, action="sell", option_type="put"),
                        _spread_leg_payload(long_put, action="buy", option_type="put"),
                    ],
                    "width": round(width_value, 2),
                    "net_credit": round(net_credit, 2),
                    "max_profit": round(net_credit * 100, 2),
                    "max_loss": round(max_loss, 2),
                    "breakeven": round(short_strike - net_credit, 2),
                    "return_on_risk_pct": scoring["return_on_risk_pct"],
                    "downside_buffer_pct": _round_if_number(downside_buffer_pct),
                    "estimated_probability_of_profit": _round_if_number(pop),
                    "avg_iv": avg_iv,
                    "strategy_fit": "Bullish to neutral-bullish income trade",
                    "spread_score": scoring["spread_score"],
                    "score": scoring["spread_score"],
                    "rating": _spread_rating(scoring["spread_score"]),
                    "score_breakdown": scoring["score_breakdown"],
                    "reasons": scoring["reasons"],
                    "warnings": scoring["warnings"],
                    "liquidity_metrics": scoring["liquidity_metrics"],
                    "filters_used": scoring["filters_used"],
                }
                candidates.append(candidate)

    return candidates


def _build_bear_call_credit_spreads(
    *,
    call_contracts,
    stock_price,
    quality_score,
    technical_score,
    next_earnings_date,
    symbol_iv,
    profile,
    today,
    preferred_width=None,
    max_dte=None,
    min_credit=None,
    max_risk=None,
):
    grouped_calls = _group_contracts_by_expiration(
        call_contracts,
        today=today,
        max_dte=_credit_spread_filters(profile, requested_max_dte=max_dte)["max_dte"],
    )
    candidates = []
    filter_settings = _credit_spread_filters(profile, requested_max_dte=max_dte)

    for expiration_date, contracts in grouped_calls.items():
        for short_index, short_call in enumerate(contracts):
            short_strike = _to_float(short_call.get("strike"))
            short_mid = _contract_mid(short_call)
            if short_strike is None or short_mid is None or short_mid <= 0:
                continue
            if stock_price is not None and short_strike <= stock_price:
                continue
            for long_call in contracts[short_index + 1 :]:
                long_strike = _to_float(long_call.get("strike"))
                long_mid = _contract_mid(long_call)
                if long_strike is None or long_mid is None:
                    continue
                width_value = long_strike - short_strike
                if not _width_allowed(
                    width_value,
                    preferred_width=preferred_width,
                    max_width=profile["max_width"],
                ):
                    continue
                net_credit = short_mid - long_mid
                if net_credit <= 0 or net_credit >= width_value:
                    continue
                max_loss = (width_value - net_credit) * 100
                if min_credit is not None and net_credit < min_credit:
                    continue
                if max_risk is not None and max_loss > max_risk:
                    continue

                short_delta = _to_float(short_call.get("delta"))
                pop = (
                    round((1 - abs(short_delta)) * 100, 2)
                    if short_delta is not None
                    else None
                )
                upside_buffer_pct = (
                    ((short_strike - stock_price) / stock_price) * 100
                    if stock_price
                    else None
                )
                return_on_risk_pct = (
                    (net_credit * 100) / max_loss * 100 if max_loss > 0 else None
                )
                min_open_interest = min(
                    value
                    for value in (
                        _to_int(short_call.get("open_interest")),
                        _to_int(long_call.get("open_interest")),
                    )
                    if value is not None
                ) if any(
                    value is not None
                    for value in (
                        _to_int(short_call.get("open_interest")),
                        _to_int(long_call.get("open_interest")),
                    )
                ) else None
                avg_leg_spread_pct = _option_liquidity_metrics([short_call, long_call])[
                    "avg_leg_spread_pct"
                ]
                earnings_before_expiration = bool(
                    next_earnings_date
                    and today <= next_earnings_date <= expiration_date
                )

                if short_delta is None:
                    continue
                abs_short_delta = abs(short_delta)
                if not (
                    filter_settings["short_delta_min"]
                    <= abs_short_delta
                    <= filter_settings["short_delta_max"]
                ):
                    continue
                if (
                    return_on_risk_pct is None
                    or return_on_risk_pct
                    < filter_settings["min_return_on_risk_pct"]
                ):
                    continue
                if (
                    pop is None
                    or pop < filter_settings["min_probability_of_profit"]
                ):
                    continue
                if (
                    min_open_interest is not None
                    and min_open_interest < filter_settings["min_open_interest"]
                ):
                    continue
                if (
                    avg_leg_spread_pct is None
                    or avg_leg_spread_pct > filter_settings["max_bid_ask_spread_pct"]
                ):
                    continue
                if (
                    filter_settings["exclude_earnings_before_expiration"]
                    and earnings_before_expiration
                ):
                    continue

                legs = [short_call, long_call]
                avg_iv = _average_iv_from_legs(legs, symbol_iv=symbol_iv)
                scoring = _score_vertical_credit_spread(
                    net_credit=net_credit,
                    width_value=width_value,
                    buffer_pct=upside_buffer_pct,
                    pop=pop,
                    dte=(expiration_date - today).days,
                    short_delta=short_delta,
                    avg_iv=avg_iv,
                    legs=legs,
                    quality_score=quality_score,
                    technical_score=technical_score,
                    next_earnings_date=next_earnings_date,
                    expiration_date=expiration_date,
                    profile=profile,
                    bias="bearish",
                )
                candidate = {
                    "spread_type": "bear_call_credit_spread",
                    "expiration": expiration_date.isoformat(),
                    "dte": (expiration_date - today).days,
                    "legs": [
                        _spread_leg_payload(short_call, action="sell", option_type="call"),
                        _spread_leg_payload(long_call, action="buy", option_type="call"),
                    ],
                    "width": round(width_value, 2),
                    "net_credit": round(net_credit, 2),
                    "max_profit": round(net_credit * 100, 2),
                    "max_loss": round(max_loss, 2),
                    "breakeven": round(short_strike + net_credit, 2),
                    "return_on_risk_pct": scoring["return_on_risk_pct"],
                    "upside_buffer_pct": _round_if_number(upside_buffer_pct),
                    "estimated_probability_of_profit": _round_if_number(pop),
                    "avg_iv": avg_iv,
                    "strategy_fit": "Bearish to neutral-bearish income trade",
                    "spread_score": scoring["spread_score"],
                    "score": scoring["spread_score"],
                    "rating": _spread_rating(scoring["spread_score"]),
                    "score_breakdown": scoring["score_breakdown"],
                    "reasons": scoring["reasons"],
                    "warnings": scoring["warnings"],
                    "liquidity_metrics": scoring["liquidity_metrics"],
                    "filters_used": scoring["filters_used"],
                }
                candidates.append(candidate)

    return candidates


def _build_bull_call_debit_spreads(
    *,
    call_contracts,
    stock_price,
    quality_score,
    technical_score,
    next_earnings_date,
    symbol_iv,
    profile,
    today,
    preferred_width=None,
    max_dte=None,
    max_debit=None,
    max_risk=None,
):
    grouped_calls = _group_contracts_by_expiration(
        call_contracts,
        today=today,
        max_dte=_debit_spread_filters(profile, requested_max_dte=max_dte)["max_dte"],
    )
    candidates = []
    filter_settings = _debit_spread_filters(profile, requested_max_dte=max_dte)

    for expiration_date, contracts in grouped_calls.items():
        for long_index, long_call in enumerate(contracts):
            long_strike = _to_float(long_call.get("strike"))
            long_mid = _contract_mid(long_call)
            if long_strike is None or long_mid is None or long_mid <= 0:
                continue
            for short_call in contracts[long_index + 1 :]:
                short_strike = _to_float(short_call.get("strike"))
                short_mid = _contract_mid(short_call)
                if short_strike is None or short_mid is None:
                    continue
                width_value = short_strike - long_strike
                if not _width_allowed(
                    width_value,
                    preferred_width=preferred_width,
                    max_width=profile["max_width"],
                ):
                    continue
                net_debit = long_mid - short_mid
                if net_debit <= 0 or net_debit >= width_value:
                    continue
                max_loss = net_debit * 100
                if max_debit is not None and net_debit > max_debit:
                    continue
                if max_risk is not None and max_loss > max_risk:
                    continue

                long_delta = _to_float(long_call.get("delta"))
                short_delta = _to_float(short_call.get("delta"))
                pop = round(abs(long_delta) * 100, 2) if long_delta is not None else None
                breakeven = long_strike + net_debit
                move_to_breakeven_pct = (
                    ((breakeven - stock_price) / stock_price) * 100
                    if stock_price
                    else None
                )
                reward_to_risk = (
                    ((width_value - net_debit) * 100) / max_loss if max_loss > 0 else None
                )
                debit_as_pct_of_width = (net_debit / width_value * 100) if width_value > 0 else None

                if long_delta is None or short_delta is None:
                    continue
                abs_long_delta = abs(long_delta)
                abs_short_delta = abs(short_delta)
                if not (
                    filter_settings["long_delta_min"]
                    <= abs_long_delta
                    <= filter_settings["long_delta_max"]
                ):
                    continue
                if not (
                    filter_settings["short_delta_min"]
                    <= abs_short_delta
                    <= filter_settings["short_delta_max"]
                ):
                    continue
                if (
                    reward_to_risk is None
                    or reward_to_risk < filter_settings["min_reward_to_risk"]
                ):
                    continue
                if (
                    debit_as_pct_of_width is None
                    or debit_as_pct_of_width > filter_settings["max_debit_as_pct_of_width"]
                ):
                    continue

                legs = [long_call, short_call]
                avg_iv = _average_iv_from_legs(legs, symbol_iv=symbol_iv)
                scoring = _score_vertical_debit_spread(
                    net_debit=net_debit,
                    width_value=width_value,
                    move_to_breakeven_pct=move_to_breakeven_pct,
                    pop=pop,
                    dte=(expiration_date - today).days,
                    long_delta=long_delta,
                    avg_iv=avg_iv,
                    legs=legs,
                    quality_score=quality_score,
                    technical_score=technical_score,
                    next_earnings_date=next_earnings_date,
                    expiration_date=expiration_date,
                    profile=profile,
                    bias="bullish",
                )
                candidate = {
                    "spread_type": "bull_call_debit_spread",
                    "expiration": expiration_date.isoformat(),
                    "dte": (expiration_date - today).days,
                    "legs": [
                        _spread_leg_payload(long_call, action="buy", option_type="call"),
                        _spread_leg_payload(short_call, action="sell", option_type="call"),
                    ],
                    "width": round(width_value, 2),
                    "net_debit": round(net_debit, 2),
                    "max_profit": round((width_value - net_debit) * 100, 2),
                    "max_loss": round(max_loss, 2),
                    "breakeven": round(breakeven, 2),
                    "reward_to_risk": scoring["reward_to_risk"],
                    "move_to_breakeven_pct": _round_if_number(move_to_breakeven_pct),
                    "estimated_probability_of_profit": _round_if_number(pop),
                    "avg_iv": avg_iv,
                    "strategy_fit": "Bullish defined-risk upside trade",
                    "spread_score": scoring["spread_score"],
                    "score": scoring["spread_score"],
                    "rating": _spread_rating(scoring["spread_score"]),
                    "score_breakdown": scoring["score_breakdown"],
                    "reasons": scoring["reasons"],
                    "warnings": scoring["warnings"],
                    "liquidity_metrics": scoring["liquidity_metrics"],
                    "filters_used": scoring["filters_used"],
                }
                candidates.append(candidate)

    return candidates


def _build_bear_put_debit_spreads(
    *,
    put_contracts,
    stock_price,
    quality_score,
    technical_score,
    next_earnings_date,
    symbol_iv,
    profile,
    today,
    preferred_width=None,
    max_dte=None,
    max_debit=None,
    max_risk=None,
):
    grouped_puts = _group_contracts_by_expiration(
        put_contracts,
        today=today,
        max_dte=_debit_spread_filters(profile, requested_max_dte=max_dte)["max_dte"],
    )
    candidates = []
    filter_settings = _debit_spread_filters(profile, requested_max_dte=max_dte)

    for expiration_date, contracts in grouped_puts.items():
        for long_index, long_put in enumerate(contracts):
            long_strike = _to_float(long_put.get("strike"))
            long_mid = _contract_mid(long_put)
            if long_strike is None or long_mid is None or long_mid <= 0:
                continue
            for short_put in contracts[:long_index]:
                short_strike = _to_float(short_put.get("strike"))
                short_mid = _contract_mid(short_put)
                if short_strike is None or short_mid is None:
                    continue
                width_value = long_strike - short_strike
                if not _width_allowed(
                    width_value,
                    preferred_width=preferred_width,
                    max_width=profile["max_width"],
                ):
                    continue
                net_debit = long_mid - short_mid
                if net_debit <= 0 or net_debit >= width_value:
                    continue
                max_loss = net_debit * 100
                if max_debit is not None and net_debit > max_debit:
                    continue
                if max_risk is not None and max_loss > max_risk:
                    continue

                long_delta = _to_float(long_put.get("delta"))
                short_delta = _to_float(short_put.get("delta"))
                pop = round(abs(long_delta) * 100, 2) if long_delta is not None else None
                breakeven = long_strike - net_debit
                move_to_breakeven_pct = (
                    ((stock_price - breakeven) / stock_price) * 100
                    if stock_price
                    else None
                )
                reward_to_risk = (
                    ((width_value - net_debit) * 100) / max_loss if max_loss > 0 else None
                )
                debit_as_pct_of_width = (net_debit / width_value * 100) if width_value > 0 else None

                if long_delta is None or short_delta is None:
                    continue
                abs_long_delta = abs(long_delta)
                abs_short_delta = abs(short_delta)
                if not (
                    filter_settings["long_delta_min"]
                    <= abs_long_delta
                    <= filter_settings["long_delta_max"]
                ):
                    continue
                if not (
                    filter_settings["short_delta_min"]
                    <= abs_short_delta
                    <= filter_settings["short_delta_max"]
                ):
                    continue
                if (
                    reward_to_risk is None
                    or reward_to_risk < filter_settings["min_reward_to_risk"]
                ):
                    continue
                if (
                    debit_as_pct_of_width is None
                    or debit_as_pct_of_width > filter_settings["max_debit_as_pct_of_width"]
                ):
                    continue

                legs = [long_put, short_put]
                avg_iv = _average_iv_from_legs(legs, symbol_iv=symbol_iv)
                scoring = _score_vertical_debit_spread(
                    net_debit=net_debit,
                    width_value=width_value,
                    move_to_breakeven_pct=move_to_breakeven_pct,
                    pop=pop,
                    dte=(expiration_date - today).days,
                    long_delta=long_delta,
                    avg_iv=avg_iv,
                    legs=legs,
                    quality_score=quality_score,
                    technical_score=technical_score,
                    next_earnings_date=next_earnings_date,
                    expiration_date=expiration_date,
                    profile=profile,
                    bias="bearish",
                )
                candidate = {
                    "spread_type": "bear_put_debit_spread",
                    "expiration": expiration_date.isoformat(),
                    "dte": (expiration_date - today).days,
                    "legs": [
                        _spread_leg_payload(long_put, action="buy", option_type="put"),
                        _spread_leg_payload(short_put, action="sell", option_type="put"),
                    ],
                    "width": round(width_value, 2),
                    "net_debit": round(net_debit, 2),
                    "max_profit": round((width_value - net_debit) * 100, 2),
                    "max_loss": round(max_loss, 2),
                    "breakeven": round(breakeven, 2),
                    "reward_to_risk": scoring["reward_to_risk"],
                    "move_to_breakeven_pct": _round_if_number(move_to_breakeven_pct),
                    "estimated_probability_of_profit": _round_if_number(pop),
                    "avg_iv": avg_iv,
                    "strategy_fit": "Bearish defined-risk downside trade",
                    "spread_score": scoring["spread_score"],
                    "score": scoring["spread_score"],
                    "rating": _spread_rating(scoring["spread_score"]),
                    "score_breakdown": scoring["score_breakdown"],
                    "reasons": scoring["reasons"],
                    "warnings": scoring["warnings"],
                    "liquidity_metrics": scoring["liquidity_metrics"],
                    "filters_used": scoring["filters_used"],
                }
                candidates.append(candidate)

    return candidates


def _build_iron_condors(
    *,
    put_credit_candidates,
    call_credit_candidates,
    stock_price,
    quality_score,
    technical_score,
    next_earnings_date,
    profile,
    today,
    preferred_width=None,
    min_credit=None,
    max_risk=None,
):
    puts_by_exp = {}
    for candidate in put_credit_candidates:
        puts_by_exp.setdefault(candidate["expiration"], []).append(candidate)
    calls_by_exp = {}
    for candidate in call_credit_candidates:
        calls_by_exp.setdefault(candidate["expiration"], []).append(candidate)

    candidates = []
    for expiration, put_candidates in puts_by_exp.items():
        call_candidates = calls_by_exp.get(expiration) or []
        if not call_candidates:
            continue
        expiration_date = _parse_date(expiration)
        dte = (expiration_date - today).days if expiration_date else None
        for put_candidate in put_candidates:
            put_short = put_candidate["legs"][0]
            put_long = put_candidate["legs"][1]
            put_width = _to_float(put_candidate.get("width"))
            put_credit = _to_float(put_candidate.get("net_credit"))
            for call_candidate in call_candidates:
                call_short = call_candidate["legs"][0]
                call_long = call_candidate["legs"][1]
                call_width = _to_float(call_candidate.get("width"))
                call_credit = _to_float(call_candidate.get("net_credit"))
                if None in {put_width, call_width, put_credit, call_credit}:
                    continue
                if preferred_width is not None:
                    if (
                        abs(put_width - preferred_width) > 0.05
                        or abs(call_width - preferred_width) > 0.05
                    ):
                        continue
                short_put_strike = _to_float(put_short.get("strike"))
                short_call_strike = _to_float(call_short.get("strike"))
                if (
                    short_put_strike is None
                    or short_call_strike is None
                    or short_put_strike >= short_call_strike
                ):
                    continue
                net_credit = put_credit + call_credit
                max_width = max(put_width, call_width)
                if net_credit <= 0 or net_credit >= max_width:
                    continue
                max_loss = (max_width - net_credit) * 100
                if min_credit is not None and net_credit < min_credit:
                    continue
                if max_risk is not None and max_loss > max_risk:
                    continue
                put_delta = abs(_to_float(put_short.get("delta")) or 0)
                call_delta = abs(_to_float(call_short.get("delta")) or 0)
                pop = max(0, (1 - put_delta - call_delta) * 100)
                lower_buffer_pct = (
                    ((stock_price - short_put_strike) / stock_price) * 100
                    if stock_price
                    else None
                )
                upper_buffer_pct = (
                    ((short_call_strike - stock_price) / stock_price) * 100
                    if stock_price
                    else None
                )
                inner_buffer_pct = None
                if lower_buffer_pct is not None and upper_buffer_pct is not None:
                    inner_buffer_pct = min(lower_buffer_pct, upper_buffer_pct)
                legs = [
                    {
                        "strike": put_short["strike"],
                        "bid": put_short["bid"],
                        "ask": put_short["ask"],
                        "mid": put_short["mid"],
                        "delta": put_short["delta"],
                        "iv": put_short["iv"],
                        "volume": put_short["volume"],
                        "open_interest": put_short["open_interest"],
                    },
                    {
                        "strike": put_long["strike"],
                        "bid": put_long["bid"],
                        "ask": put_long["ask"],
                        "mid": put_long["mid"],
                        "delta": put_long["delta"],
                        "iv": put_long["iv"],
                        "volume": put_long["volume"],
                        "open_interest": put_long["open_interest"],
                    },
                    {
                        "strike": call_short["strike"],
                        "bid": call_short["bid"],
                        "ask": call_short["ask"],
                        "mid": call_short["mid"],
                        "delta": call_short["delta"],
                        "iv": call_short["iv"],
                        "volume": call_short["volume"],
                        "open_interest": call_short["open_interest"],
                    },
                    {
                        "strike": call_long["strike"],
                        "bid": call_long["bid"],
                        "ask": call_long["ask"],
                        "mid": call_long["mid"],
                        "delta": call_long["delta"],
                        "iv": call_long["iv"],
                        "volume": call_long["volume"],
                        "open_interest": call_long["open_interest"],
                    },
                ]
                avg_iv = _average_iv_from_legs(legs)
                scoring = _score_neutral_credit_spread(
                    net_credit=net_credit,
                    width_value=max_width,
                    inner_buffer_pct=inner_buffer_pct,
                    pop=pop,
                    dte=dte,
                    avg_short_delta=round((put_delta + call_delta) / 2, 4),
                    avg_iv=avg_iv,
                    legs=legs,
                    quality_score=quality_score,
                    technical_score=technical_score,
                    next_earnings_date=next_earnings_date,
                    expiration_date=expiration_date,
                    profile=profile,
                )
                candidates.append({
                    "spread_type": "iron_condor",
                    "expiration": expiration,
                    "dte": dte,
                    "legs": [
                        dict(put_short, action="sell", option_type="put"),
                        dict(put_long, action="buy", option_type="put"),
                        dict(call_short, action="sell", option_type="call"),
                        dict(call_long, action="buy", option_type="call"),
                    ],
                    "width": round(max_width, 2),
                    "put_width": round(put_width, 2),
                    "call_width": round(call_width, 2),
                    "net_credit": round(net_credit, 2),
                    "max_profit": round(net_credit * 100, 2),
                    "max_loss": round(max_loss, 2),
                    "breakeven": {
                        "low": round(short_put_strike - net_credit, 2),
                        "high": round(short_call_strike + net_credit, 2),
                    },
                    "breakeven_low": round(short_put_strike - net_credit, 2),
                    "breakeven_high": round(short_call_strike + net_credit, 2),
                    "return_on_risk_pct": scoring["return_on_risk_pct"],
                    "downside_buffer_pct": _round_if_number(lower_buffer_pct),
                    "upside_buffer_pct": _round_if_number(upper_buffer_pct),
                    "estimated_probability_of_profit": _round_if_number(pop),
                    "avg_iv": avg_iv,
                    "strategy_fit": "Neutral income trade",
                    "spread_score": scoring["spread_score"],
                    "score": scoring["spread_score"],
                    "rating": _spread_rating(scoring["spread_score"]),
                    "score_breakdown": scoring["score_breakdown"],
                    "reasons": scoring["reasons"],
                    "warnings": scoring["warnings"],
                    "liquidity_metrics": scoring["liquidity_metrics"],
                })

    return candidates


def _build_iron_butterflies(
    *,
    put_contracts,
    call_contracts,
    stock_price,
    quality_score,
    technical_score,
    next_earnings_date,
    symbol_iv,
    profile,
    today,
    preferred_width=None,
    max_dte=None,
    min_credit=None,
    max_risk=None,
):
    grouped_puts = _group_contracts_by_expiration(
        put_contracts,
        today=today,
        max_dte=max_dte,
    )
    grouped_calls = _group_contracts_by_expiration(
        call_contracts,
        today=today,
        max_dte=max_dte,
    )
    candidates = []

    for expiration_date, puts in grouped_puts.items():
        calls = grouped_calls.get(expiration_date) or []
        if not calls:
            continue
        put_by_strike = {round(_to_float(item.get("strike")) or 0, 4): item for item in puts}
        call_by_strike = {round(_to_float(item.get("strike")) or 0, 4): item for item in calls}
        common_short_strikes = sorted(set(put_by_strike.keys()) & set(call_by_strike.keys()))

        for center_key in common_short_strikes:
            short_put = put_by_strike[center_key]
            short_call = call_by_strike[center_key]
            center_strike = _to_float(short_put.get("strike"))
            if center_strike is None:
                continue
            for long_put in puts:
                long_put_strike = _to_float(long_put.get("strike"))
                if long_put_strike is None or long_put_strike >= center_strike:
                    continue
                put_width = center_strike - long_put_strike
                if not _width_allowed(
                    put_width,
                    preferred_width=preferred_width,
                    max_width=profile["max_width"],
                ):
                    continue
                target_call_strike = center_strike + put_width
                matching_calls = [
                    item
                    for item in calls
                    if abs((_to_float(item.get("strike")) or 0) - target_call_strike) <= 0.05
                ]
                for long_call in matching_calls:
                    short_put_mid = _contract_mid(short_put)
                    short_call_mid = _contract_mid(short_call)
                    long_put_mid = _contract_mid(long_put)
                    long_call_mid = _contract_mid(long_call)
                    if None in {short_put_mid, short_call_mid, long_put_mid, long_call_mid}:
                        continue
                    net_credit = short_put_mid + short_call_mid - long_put_mid - long_call_mid
                    if net_credit <= 0 or net_credit >= put_width:
                        continue
                    max_loss = (put_width - net_credit) * 100
                    if min_credit is not None and net_credit < min_credit:
                        continue
                    if max_risk is not None and max_loss > max_risk:
                        continue
                    short_put_delta = abs(_to_float(short_put.get("delta")) or 0)
                    short_call_delta = abs(_to_float(short_call.get("delta")) or 0)
                    pop = max(0, (1 - short_put_delta - short_call_delta) * 100)
                    inner_buffer_pct = (
                        (put_width / stock_price) * 100 if stock_price else None
                    )
                    legs = [short_put, long_put, short_call, long_call]
                    avg_iv = _average_iv_from_legs(legs, symbol_iv=symbol_iv)
                    scoring = _score_neutral_credit_spread(
                        net_credit=net_credit,
                        width_value=put_width,
                        inner_buffer_pct=inner_buffer_pct,
                        pop=pop,
                        dte=(expiration_date - today).days,
                        avg_short_delta=round((short_put_delta + short_call_delta) / 2, 4),
                        avg_iv=avg_iv,
                        legs=legs,
                        quality_score=quality_score,
                        technical_score=technical_score,
                        next_earnings_date=next_earnings_date,
                        expiration_date=expiration_date,
                        profile=profile,
                    )
                    candidates.append({
                        "spread_type": "iron_butterfly",
                        "expiration": expiration_date.isoformat(),
                        "dte": (expiration_date - today).days,
                        "legs": [
                            _spread_leg_payload(short_put, action="sell", option_type="put"),
                            _spread_leg_payload(long_put, action="buy", option_type="put"),
                            _spread_leg_payload(short_call, action="sell", option_type="call"),
                            _spread_leg_payload(long_call, action="buy", option_type="call"),
                        ],
                        "width": round(put_width, 2),
                        "net_credit": round(net_credit, 2),
                        "max_profit": round(net_credit * 100, 2),
                        "max_loss": round(max_loss, 2),
                        "breakeven": {
                            "low": round(center_strike - net_credit, 2),
                            "high": round(center_strike + net_credit, 2),
                        },
                        "breakeven_low": round(center_strike - net_credit, 2),
                        "breakeven_high": round(center_strike + net_credit, 2),
                        "return_on_risk_pct": scoring["return_on_risk_pct"],
                        "estimated_probability_of_profit": _round_if_number(pop),
                        "avg_iv": avg_iv,
                        "strategy_fit": "Neutral premium-selling trade for low movement",
                        "spread_score": scoring["spread_score"],
                        "score": scoring["spread_score"],
                        "rating": _spread_rating(scoring["spread_score"]),
                        "score_breakdown": scoring["score_breakdown"],
                        "reasons": scoring["reasons"],
                        "warnings": scoring["warnings"],
                        "liquidity_metrics": scoring["liquidity_metrics"],
                    })

    return candidates


def _evaluate_spread_candidates(
    *,
    sym,
    spread_type,
    directional_view,
    risk_profile,
    profile_overrides=None,
    max_dte,
    min_credit,
    max_debit,
    max_risk,
    preferred_width,
):
    today = date.today()
    stock_price = _to_float(sym.price)
    quality_score = _to_float(sym.score)
    technical_score = sym.technical_score
    next_earnings_date = _parse_date(sym.next_earnings_date)
    symbol_iv = _normalize_iv_percent(sym.option_iv)
    put_contracts = _get_symbol_put_contracts(sym)
    call_payload = _get_symbol_call_payload(sym)
    call_contracts = _extract_call_contracts(call_payload)
    profile = _spread_profile(risk_profile, overrides=profile_overrides)
    resolved_view = _resolve_spread_directional_view(
        directional_view=directional_view,
        technical_score=technical_score,
    )

    avg_iv = _average_iv_from_legs(
        [*put_contracts[:5], *call_contracts[:5]],
        symbol_iv=symbol_iv,
    )

    if spread_type == "auto":
        if resolved_view == "bullish":
            candidate_types = [
                "bull_put_credit_spread",
                "bull_call_debit_spread",
            ]
            if avg_iv is not None and avg_iv < profile["credit_iv_floor"]:
                candidate_types.reverse()
        elif resolved_view == "bearish":
            candidate_types = [
                "bear_call_credit_spread",
                "bear_put_debit_spread",
            ]
            if avg_iv is not None and avg_iv < profile["credit_iv_floor"]:
                candidate_types.reverse()
        else:
            candidate_types = ["iron_condor", "iron_butterfly"]
            if avg_iv is not None and avg_iv < profile["credit_iv_floor"]:
                candidate_types.reverse()
    else:
        candidate_types = [spread_type]

    bull_put_candidates = []
    bear_call_candidates = []
    evaluated_by_type = {}

    if any(
        spread_name in candidate_types
        for spread_name in {"bull_put_credit_spread", "iron_condor"}
    ):
        bull_put_candidates = _build_bull_put_credit_spreads(
            put_contracts=put_contracts,
            stock_price=stock_price,
            quality_score=quality_score,
            technical_score=technical_score,
            next_earnings_date=next_earnings_date,
            symbol_iv=symbol_iv,
            profile=profile,
            today=today,
            preferred_width=preferred_width,
            max_dte=max_dte,
            min_credit=min_credit,
            max_risk=max_risk,
        )
        evaluated_by_type["bull_put_credit_spread"] = bull_put_candidates

    if any(
        spread_name in candidate_types
        for spread_name in {"bear_call_credit_spread", "iron_condor"}
    ):
        bear_call_candidates = _build_bear_call_credit_spreads(
            call_contracts=call_contracts,
            stock_price=stock_price,
            quality_score=quality_score,
            technical_score=technical_score,
            next_earnings_date=next_earnings_date,
            symbol_iv=symbol_iv,
            profile=profile,
            today=today,
            preferred_width=preferred_width,
            max_dte=max_dte,
            min_credit=min_credit,
            max_risk=max_risk,
        )
        evaluated_by_type["bear_call_credit_spread"] = bear_call_candidates

    if "bull_call_debit_spread" in candidate_types:
        evaluated_by_type["bull_call_debit_spread"] = _build_bull_call_debit_spreads(
            call_contracts=call_contracts,
            stock_price=stock_price,
            quality_score=quality_score,
            technical_score=technical_score,
            next_earnings_date=next_earnings_date,
            symbol_iv=symbol_iv,
            profile=profile,
            today=today,
            preferred_width=preferred_width,
            max_dte=max_dte,
            max_debit=max_debit,
            max_risk=max_risk,
        )

    if "bear_put_debit_spread" in candidate_types:
        evaluated_by_type["bear_put_debit_spread"] = _build_bear_put_debit_spreads(
            put_contracts=put_contracts,
            stock_price=stock_price,
            quality_score=quality_score,
            technical_score=technical_score,
            next_earnings_date=next_earnings_date,
            symbol_iv=symbol_iv,
            profile=profile,
            today=today,
            preferred_width=preferred_width,
            max_dte=max_dte,
            max_debit=max_debit,
            max_risk=max_risk,
        )

    if "iron_condor" in candidate_types:
        evaluated_by_type["iron_condor"] = _build_iron_condors(
            put_credit_candidates=bull_put_candidates,
            call_credit_candidates=bear_call_candidates,
            stock_price=stock_price,
            quality_score=quality_score,
            technical_score=technical_score,
            next_earnings_date=next_earnings_date,
            profile=profile,
            today=today,
            preferred_width=preferred_width,
            min_credit=min_credit,
            max_risk=max_risk,
        )

    if "iron_butterfly" in candidate_types:
        evaluated_by_type["iron_butterfly"] = _build_iron_butterflies(
            put_contracts=put_contracts,
            call_contracts=call_contracts,
            stock_price=stock_price,
            quality_score=quality_score,
            technical_score=technical_score,
            next_earnings_date=next_earnings_date,
            symbol_iv=symbol_iv,
            profile=profile,
            today=today,
            preferred_width=preferred_width,
            max_dte=max_dte,
            min_credit=min_credit,
            max_risk=max_risk,
        )

    all_candidates = []
    for spread_name in candidate_types:
        all_candidates.extend(evaluated_by_type.get(spread_name) or [])

    all_candidates.sort(key=_candidate_sort_key, reverse=True)

    return {
        "stock_price": stock_price,
        "quality_score": quality_score,
        "technical_score": technical_score,
        "next_earnings_date": next_earnings_date,
        "resolved_view": resolved_view,
        "avg_iv": avg_iv,
        "profile": profile,
        "candidate_types": candidate_types,
        "all_candidates": all_candidates,
        "evaluated_by_type": evaluated_by_type,
        "put_contract_count": len(put_contracts),
        "call_contract_count": len(call_contracts),
    }


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
    iv = _normalize_iv_percent(contract["iv"])

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


def _evaluate_covered_call_symbol(
    sym,
    *,
    shares_owned,
    cost_basis,
    assigned_price,
    premium_received_from_put,
    target_delta,
    max_dte,
    min_roi,
    max_delta_filter,
    style,
    covered_call_strategy,
    target_exit_price,
):
    today = date.today()
    stock_price = _to_float(sym.price)
    quality_score = _to_float(sym.score)
    technical_score = sym.technical_score
    next_earnings_date = _parse_date(sym.next_earnings_date)
    call_data = _get_symbol_call_payload(sym)
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

    base_result = {
        "symbol": sym.ticker,
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
        "next_earnings_date": (
            next_earnings_date.isoformat() if next_earnings_date else None
        ),
    }

    if not call_contracts:
        base_result["error"] = "No call contracts found in call_data."
        return base_result

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
        upside_to_strike_pct = _to_float(
            scored["contract"]["upside_to_strike_pct"]
        )
        abs_delta = abs(contract_delta) if contract_delta is not None else None

        if max_delta_filter is not None and (
            abs_delta is None or abs_delta > max_delta_filter
        ):
            filtered_out += 1
            continue
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
                    if scored["contract"].get("adjusted_cost_basis_after_call")
                    is not None
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
            target_gap_abs_pct = (
                abs(contract_strike - target_exit_price) / target_exit_price * 100
            )
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
        base_result.update({
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
            "error": (
                "Call contracts were found, but none passed the covered-call "
                "filters or had enough valid data to evaluate."
            ),
        })
        return base_result

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

    return {
        "symbol": sym.ticker,
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
            "gain_if_called_from_cost_basis": best["contract"].get(
                "gain_if_called_from_cost_basis"
            ),
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


def _score_put_contract(
    contract,
    *,
    stock_price,
    rsi,
    quality_score,
    technical_score,
    next_earnings_date,
    today,
    budget=None,
):
    strike = contract["strike"]
    expiration_date = contract["expiration_date"]
    bid = contract["bid"]
    ask = contract["ask"]
    mid = contract["mid"]
    delta = contract["delta"]
    volume = contract["volume"]
    open_interest = contract["open_interest"]
    iv = _normalize_iv_percent(contract["iv"])

    if not stock_price or not strike or not expiration_date or not mid:
        return None

    dte = (expiration_date - today).days

    if dte <= 0:
        return None

    roi = (mid / strike) * 100
    downside_buffer = ((stock_price - strike) / stock_price) * 100
    cash_metrics = _build_put_contract_cash_metrics(strike, mid, budget=budget)

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
            "cash_required": cash_metrics["cash_required"],
            "premium_received": cash_metrics["premium_received"],
            "breakeven": cash_metrics["breakeven"],
            "contracts_affordable": cash_metrics["contracts_affordable"],
        },
        "cumulative_score": score,
        "rating": rating,
        "earnings_before_expiration": earnings_before_exp,
        "reasons": reasons,
        "warnings": warnings,
    }



def _handle_put_wheel_opportunity(
    symbol: str,
    *,
    account_size=None,
    max_cash_required=None,
) -> str:
    if not symbol:
        return json.dumps({
            "error": "Missing required symbol"
        })

    symbol = symbol.strip().upper()
    effective_cash_budget = _resolve_cash_secured_budget(
        account_size,
        max_cash_required,
    )

    if not re.match(r"^[A-Z0-9.\-]{1,10}$", symbol):
        return json.dumps({
            "error": "Invalid ticker symbol",
            "symbol": symbol
        })

    try:
        sym = Symbol.objects.prefetch_related("expiration_snapshots").filter(
            ticker__iexact=symbol
        ).first()
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

    put_contracts = _get_symbol_put_contracts(sym)

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
            "filters_applied": {
                "account_size": account_size,
                "max_cash_required": max_cash_required,
                "effective_max_cash_required": effective_cash_budget,
            },
            "error": "No put contracts found in option_data."
        }, default=_json_default)

    evaluated = []
    total_valid_contracts = 0
    min_cash_required_seen = None

    for contract in put_contracts:
        scored = _score_put_contract(
            contract,
            stock_price=stock_price,
            rsi=rsi,
            quality_score=quality_score,
            technical_score=technical_score,
            next_earnings_date=next_earnings_date,
            today=today,
            budget=effective_cash_budget,
        )

        if scored:
            total_valid_contracts += 1
            cash_required = scored["contract"].get("cash_required")
            if cash_required is not None:
                if min_cash_required_seen is None or cash_required < min_cash_required_seen:
                    min_cash_required_seen = cash_required
            if (
                effective_cash_budget is not None
                and cash_required is not None
                and cash_required > effective_cash_budget
            ):
                continue
            evaluated.append(scored)

    if not evaluated:
        error_message = "Put contracts were found, but none had enough valid data to evaluate."
        if total_valid_contracts > 0 and effective_cash_budget is not None:
            error_message = (
                "No put contracts fit the cash-secured budget constraint."
            )
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
            "filters_applied": {
                "account_size": account_size,
                "max_cash_required": max_cash_required,
                "effective_max_cash_required": effective_cash_budget,
            },
            "smallest_cash_required": min_cash_required_seen,
            "error": error_message,
        }, default=_json_default)

    evaluated = sorted(
        evaluated,
        key=lambda item: item["cumulative_score"],
        reverse=True
    )

    best = evaluated[0]
    top_candidates = evaluated[:5]

    call_contracts = _get_symbol_call_contracts(sym)
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
        "filters_applied": {
            "account_size": account_size,
            "max_cash_required": max_cash_required,
            "effective_max_cash_required": effective_cash_budget,
        },

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
            "cash_required": best["contract"].get("cash_required"),
            "premium_received": best["contract"].get("premium_received"),
            "breakeven": best["contract"].get("breakeven"),
            "contracts_affordable": best["contract"].get("contracts_affordable"),
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
        sym = Symbol.objects.prefetch_related("expiration_snapshots").filter(
            ticker__iexact=symbol
        ).first()
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
    result = _evaluate_covered_call_symbol(
        sym,
        shares_owned=shares_owned,
        cost_basis=cost_basis,
        assigned_price=assigned_price,
        premium_received_from_put=premium_received_from_put,
        target_delta=target_delta,
        max_dte=max_dte,
        min_roi=min_roi,
        max_delta_filter=None,
        style=style,
        covered_call_strategy=covered_call_strategy,
        target_exit_price=target_exit_price,
    )
    return json.dumps(result, default=_json_default)


def _handle_spread_opportunity(args: dict) -> str:
    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return json.dumps({"error": "Missing required symbol"})

    if not re.match(r"^[A-Z0-9.\-]{1,10}$", symbol):
        return json.dumps({"error": "Invalid ticker symbol", "symbol": symbol})

    spread_type = _normalize_spread_type(args.get("spread_type"))
    directional_view = _normalize_directional_view(args.get("directional_view"))
    risk_profile = _normalize_risk_profile(args.get("risk_profile"))
    max_dte = _to_int(args.get("max_dte"))
    min_credit = _to_float(args.get("min_credit"))
    max_debit = _to_float(args.get("max_debit"))
    max_risk = _to_float(args.get("max_risk"))
    preferred_width = _to_float(args.get("width"))

    try:
        sym = Symbol.objects.prefetch_related("expiration_snapshots").filter(
            ticker__iexact=symbol
        ).first()
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

    evaluation = _evaluate_spread_candidates(
        sym=sym,
        spread_type=spread_type,
        directional_view=directional_view,
        risk_profile=risk_profile,
        max_dte=max_dte,
        min_credit=min_credit,
        max_debit=max_debit,
        max_risk=max_risk,
        preferred_width=preferred_width,
    )
    credit_filter_settings = _credit_spread_filters(
        evaluation["profile"],
        requested_max_dte=max_dte,
    )
    debit_filter_settings = _debit_spread_filters(
        evaluation["profile"],
        requested_max_dte=max_dte,
    )

    base_payload = {
        "symbol": sym.ticker,
        "current_price": evaluation["stock_price"],
        "stock_quality_score": evaluation["quality_score"],
        "quality_score": evaluation["quality_score"],
        "technical_score": evaluation["technical_score"],
        "classification": sym.classification,
        "next_earnings_date": (
            evaluation["next_earnings_date"].isoformat()
            if evaluation["next_earnings_date"]
            else None
        ),
        "spread_type_requested": spread_type,
        "spread_types_evaluated": evaluation["candidate_types"],
        "directional_view_requested": directional_view,
        "resolved_directional_view": evaluation["resolved_view"],
        "risk_profile": risk_profile,
        "underlying_iv": evaluation["avg_iv"],
        "filters_applied": {
            "max_dte": max_dte,
            "min_credit": min_credit,
            "max_debit": max_debit,
            "max_risk": max_risk,
            "width": preferred_width,
            "credit_filters": credit_filter_settings,
            "debit_filters": debit_filter_settings,
        },
    }

    if not evaluation["all_candidates"]:
        base_payload.update({
            "top_candidates": [],
            "warnings": [],
            "error": (
                "No spread candidates passed the available option-chain data and filter constraints."
            ),
            "option_chain_summary": {
                "put_contracts": evaluation["put_contract_count"],
                "call_contracts": evaluation["call_contract_count"],
            },
        })
        return json.dumps(base_payload, default=_json_default)

    best_spread = evaluation["all_candidates"][0]
    top_candidates = evaluation["all_candidates"][:5]
    warnings = list(best_spread.get("warnings") or [])
    if evaluation["put_contract_count"] == 0:
        warnings.append("No put contracts were available in the stored chain.")
    if evaluation["call_contract_count"] == 0 and any(
        spread_name in evaluation["candidate_types"]
        for spread_name in {
            "bear_call_credit_spread",
            "bull_call_debit_spread",
            "iron_condor",
            "iron_butterfly",
        }
    ):
        warnings.append("No call contracts were available in the stored chain.")

    base_payload.update({
        "best_spread": best_spread,
        "top_candidates": top_candidates,
        "warnings": _dedupe_preserve_order(warnings),
        "summary": {
            "rating": best_spread.get("rating"),
            "score": best_spread.get("spread_score"),
            "spread_score": best_spread.get("spread_score"),
            "spread_type": best_spread.get("spread_type"),
            "expiration": best_spread.get("expiration"),
            "dte": best_spread.get("dte"),
            "estimated_probability_of_profit": best_spread.get(
                "estimated_probability_of_profit"
            ),
        },
    })
    return json.dumps(base_payload, default=_json_default)


def _handle_scan_put_opportunities(
    args: dict,
    *,
    plan_context: dict[str, Any] | None = None,
) -> str:
    runtime_plan = _resolve_runtime_plan_context(plan_context=plan_context)
    requested_limit = int(args.get("limit") or 10)
    limit = _clamp_scan_limit(requested_limit, runtime_plan)
    offset = int(args.get("offset") or 0)
    max_extra_pages = (runtime_plan.get("entitlements") or {}).get("max_extra_pages")
    if max_extra_pages is not None and offset > 0:
        return _scan_pagination_error(
            plan_context=runtime_plan,
            requested_limit=requested_limit,
            requested_offset=offset,
        )
    min_score = float(args.get("min_score") or 50)
    min_roi = _to_float(args.get("min_roi"))
    max_dte = _to_int(args.get("max_dte"))
    min_price = _to_float(args.get("min_price"))
    max_price = _to_float(args.get("max_price"))
    min_rsi = _to_float(args.get("min_rsi"))
    max_rsi = _to_float(args.get("max_rsi"))
    max_delta = _to_float(args.get("max_delta"))
    account_size = _to_float(args.get("account_size"))
    max_cash_required = _to_float(args.get("max_cash_required"))
    effective_cash_budget = _resolve_cash_secured_budget(
        account_size,
        max_cash_required,
    )

    today = date.today()

    try:
        symbols = (
            Symbol.objects.filter(score__gte=65)
            .filter(Q(option_data__isnull=False) | Q(expiration_snapshots__isnull=False))
            .prefetch_related("expiration_snapshots")
            .distinct()
        )
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

        if min_rsi is not None:
            if rsi is None or rsi < min_rsi:
                continue
        if max_rsi is not None:
            if rsi is None or rsi > max_rsi:
                continue

        put_contracts = _get_symbol_put_contracts(sym)
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
                budget=effective_cash_budget,
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
        if (
            effective_cash_budget is not None
            and c.get("cash_required") is not None
            and c["cash_required"] > effective_cash_budget
        ):
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
            "cash_required": c.get("cash_required"),
            "premium_received": c.get("premium_received"),
            "breakeven": c.get("breakeven"),
            "contracts_affordable": c.get("contracts_affordable"),
            "earnings_risk": best_scored["earnings_before_expiration"],
            
            "stock_quality_score": quality_score,
            "technical_score": technical_score,
            "rsi": rsi,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    page = results[offset : offset + limit]

    return json.dumps({
        "scan_date": today.isoformat(),
        "plan": runtime_plan.get("plan"),
        "trial_days_left": runtime_plan.get("trial_days_left"),
        "total_results_available": len(results),
        "offset": offset,
        "limit": limit,
        "requested_limit": requested_limit,
        "next_offset": offset + len(page) if (offset + len(page)) < len(results) else None,
        "results_returned": len(page),
        "filters_applied": {
            "min_score": min_score,
            "min_roi": min_roi,
            "max_dte": max_dte,
            "min_price": min_price,
            "max_price": max_price,
            "min_rsi": min_rsi,
            "max_rsi": max_rsi,
            "max_delta": max_delta,
            "account_size": account_size,
            "max_cash_required": max_cash_required,
            "effective_max_cash_required": effective_cash_budget,
            "limit_applied": _scan_limit_applied_payload(
                plan_context=runtime_plan,
                requested_limit=requested_limit,
                applied_limit=limit,
            ),
        },
        "opportunities": page,
    }, default=_json_default)


def _handle_scan_spread_opportunities(
    args: dict,
    *,
    plan_context: dict[str, Any] | None = None,
) -> str:
    spread_type = _normalize_spread_type(args.get("spread_type"))
    directional_view = _normalize_directional_view(args.get("directional_view"))
    risk_profile = _normalize_risk_profile(args.get("risk_profile"))
    runtime_plan = _resolve_runtime_plan_context(plan_context=plan_context)
    requested_limit = int(args.get("limit") or 10)
    limit = _clamp_scan_limit(requested_limit, runtime_plan)
    max_dte = _to_int(args.get("max_dte"))
    min_return_on_risk_pct = _to_float(args.get("min_return_on_risk_pct"))
    min_probability_of_profit = _to_float(args.get("min_probability_of_profit"))
    max_risk = _to_float(args.get("max_risk"))
    min_quality_score = _to_float(args.get("min_quality_score"))
    max_short_delta = _to_float(args.get("max_short_delta"))
    exclude_earnings = args.get("exclude_earnings")
    today = date.today()

    profile_overrides = {}
    if exclude_earnings is not None:
        profile_overrides["credit_exclude_earnings_before_expiration"] = bool(
            exclude_earnings
        )
    if min_return_on_risk_pct is not None:
        profile_overrides["credit_min_return_on_risk_pct"] = min_return_on_risk_pct
        profile_overrides["debit_min_reward_to_risk"] = max(
            min_return_on_risk_pct / 100,
            0,
        )
    if min_probability_of_profit is not None:
        profile_overrides["credit_min_probability_of_profit"] = (
            min_probability_of_profit
        )
    if max_short_delta is not None:
        clamped_short_delta = max(0, abs(max_short_delta))
        profile_overrides["credit_short_delta_min"] = 0.01
        profile_overrides["credit_short_delta_max"] = clamped_short_delta
        profile_overrides["debit_short_delta_min"] = 0.01
        profile_overrides["debit_short_delta_max"] = clamped_short_delta

    try:
        symbols = (
            Symbol.objects.filter(
                Q(option_data__isnull=False)
                | Q(call_data__isnull=False)
                | Q(expiration_snapshots__isnull=False)
            )
            .prefetch_related("expiration_snapshots")
            .distinct()
        )
    except Exception as e:
        return json.dumps({"error": "Database error", "details": str(e)})

    results = []

    for sym in symbols:
        evaluation = _evaluate_spread_candidates(
            sym=sym,
            spread_type=spread_type,
            directional_view=directional_view,
            risk_profile=risk_profile,
            profile_overrides=profile_overrides or None,
            max_dte=max_dte,
            min_credit=None,
            max_debit=None,
            max_risk=max_risk,
            preferred_width=None,
        )

        quality_score = _to_float(evaluation["quality_score"])
        if min_quality_score is not None and (
            quality_score is None or quality_score < min_quality_score
        ):
            continue

        matched_candidates = _filter_spread_candidates(
            evaluation,
            min_return_on_risk_pct=min_return_on_risk_pct,
            min_probability_of_profit=min_probability_of_profit,
            max_risk=max_risk,
            max_short_delta=max_short_delta,
            exclude_earnings=exclude_earnings,
        )

        if not matched_candidates:
            continue

        best = matched_candidates[0]
        results.append({
            "ticker": sym.ticker,
            "price": evaluation["stock_price"],
            "classification": sym.classification,
            "stock_quality_score": quality_score,
            "quality_score": quality_score,
            "technical_score": evaluation["technical_score"],
            "next_earnings_date": (
                evaluation["next_earnings_date"].isoformat()
                if evaluation["next_earnings_date"]
                else None
            ),
            "resolved_directional_view": evaluation["resolved_view"],
            "score": best.get("spread_score"),
            "spread_score": best.get("spread_score"),
            "rating": best.get("rating"),
            "spread_type": best.get("spread_type"),
            "expiration": best.get("expiration"),
            "dte": best.get("dte"),
            "width": best.get("width"),
            "net_credit": best.get("net_credit"),
            "net_debit": best.get("net_debit"),
            "max_profit": best.get("max_profit"),
            "max_loss": best.get("max_loss"),
            "breakeven": best.get("breakeven"),
            "breakeven_low": best.get("breakeven_low"),
            "breakeven_high": best.get("breakeven_high"),
            "return_on_risk_pct": best.get("return_on_risk_pct"),
            "reward_to_risk": best.get("reward_to_risk"),
            "estimated_probability_of_profit": best.get(
                "estimated_probability_of_profit"
            ),
            "avg_iv": best.get("avg_iv"),
            "max_short_delta": best.get("max_short_delta"),
            "short_leg_deltas": best.get("short_leg_deltas") or [],
            "earnings_before_expiration": best.get("earnings_before_expiration"),
            "warnings": best.get("warnings") or [],
            "reasons": best.get("reasons") or [],
            "strategy_fit": best.get("strategy_fit"),
            "legs": best.get("legs") or [],
        })

    results.sort(key=_candidate_sort_key, reverse=True)
    top = results[:limit]

    return json.dumps({
        "scan_date": today.isoformat(),
        "plan": runtime_plan.get("plan"),
        "trial_days_left": runtime_plan.get("trial_days_left"),
        "total_symbols_scanned": symbols.count(),
        "results_returned": len(top),
        "spread_type_requested": spread_type,
        "directional_view_requested": directional_view,
        "risk_profile_used": risk_profile,
        "filters_applied": {
            "spread_type": spread_type,
            "directional_view": directional_view,
            "risk_profile": risk_profile,
            "limit": limit,
            "requested_limit": requested_limit,
            "max_dte": max_dte,
            "min_return_on_risk_pct": min_return_on_risk_pct,
            "min_probability_of_profit": min_probability_of_profit,
            "max_risk": max_risk,
            "min_quality_score": min_quality_score,
            "max_short_delta": max_short_delta,
            "exclude_earnings": exclude_earnings,
            "limit_applied": _scan_limit_applied_payload(
                plan_context=runtime_plan,
                requested_limit=requested_limit,
                applied_limit=limit,
            ),
        },
        "opportunities": top,
    }, default=_json_default)


def _handle_scan_covered_call_opportunities(
    args: dict,
    *,
    plan_context: dict[str, Any] | None = None,
) -> str:
    runtime_plan = _resolve_runtime_plan_context(plan_context=plan_context)
    requested_limit = int(args.get("limit") or 10)
    limit = _clamp_scan_limit(requested_limit, runtime_plan)
    min_roi = _to_float(args.get("min_roi"))
    max_delta = _to_float(args.get("max_delta"))
    max_dte = _to_int(args.get("max_dte"))
    today = date.today()

    try:
        symbols = Symbol.objects.filter(score__gte=65).prefetch_related(
            "expiration_snapshots"
        )
    except Exception as e:
        return json.dumps({"error": "Database error", "details": str(e)})

    results = []

    for sym in symbols:
        payload = _evaluate_covered_call_symbol(
            sym,
            shares_owned=100,
            cost_basis=None,
            assigned_price=None,
            premium_received_from_put=None,
            target_delta=None,
            max_dte=max_dte,
            min_roi=min_roi,
            max_delta_filter=max_delta,
            style="balanced",
            covered_call_strategy="balanced_income",
            target_exit_price=None,
        )
        best_contract = payload.get("best_contract") or {}
        if payload.get("error") or not best_contract:
            continue
        contract_delta = _to_float(best_contract.get("delta"))

        results.append({
            "ticker": sym.ticker,
            "price": payload.get("current_price"),
            "classification": payload.get("classification"),
            "score": best_contract.get("covered_call_score"),
            "covered_call_score": best_contract.get("covered_call_score"),
            "rating": best_contract.get("rating"),
            "strike": best_contract.get("strike"),
            "expiration": best_contract.get("expiration"),
            "dte": best_contract.get("dte"),
            "premium_yield_pct": best_contract.get("premium_yield_pct"),
            "annualized_yield_pct": best_contract.get("annualized_yield_pct"),
            "delta": contract_delta,
            "iv": best_contract.get("iv"),
            "mid": best_contract.get("mid"),
            "bid": best_contract.get("bid"),
            "ask": best_contract.get("ask"),
            "upside_to_strike_pct": best_contract.get("upside_to_strike_pct"),
            "call_away_risk": best_contract.get("call_away_risk"),
            "stock_quality_score": payload.get("stock_quality_score"),
            "quality_score": payload.get("quality_score"),
            "technical_score": payload.get("technical_score"),
            "warnings": payload.get("warnings") or [],
            "reasons": best_contract.get("reasons") or [],
            "ex_dividend_risk": payload.get("ex_dividend_risk"),
        })

    results.sort(
        key=lambda item: (
            item.get("score") or 0,
            item.get("premium_yield_pct") or 0,
            item.get("upside_to_strike_pct") or 0,
        ),
        reverse=True,
    )
    top = results[:limit]

    return json.dumps({
        "scan_date": today.isoformat(),
        "plan": runtime_plan.get("plan"),
        "trial_days_left": runtime_plan.get("trial_days_left"),
        "total_symbols_scanned": symbols.count(),
        "results_returned": len(top),
        "filters_applied": {
            "limit": limit,
            "requested_limit": requested_limit,
            "min_roi": min_roi,
            "max_delta": max_delta,
            "max_dte": max_dte,
            "style": "balanced",
            "covered_call_strategy": "balanced_income",
            "shares_assumed": 100,
            "limit_applied": _scan_limit_applied_payload(
                plan_context=runtime_plan,
                requested_limit=requested_limit,
                applied_limit=limit,
            ),
        },
        "opportunities": top,
    }, default=_json_default)


def _handle_build_monthly_income_plan(
    args: dict,
    *,
    plan_context: dict[str, Any] | None = None,
) -> str:
    runtime_plan = _resolve_runtime_plan_context(plan_context=plan_context)
    monthly_income_target = _to_float(args.get("monthly_income_target"))
    account_size = _to_float(args.get("account_size"))
    max_cash_required = _to_float(args.get("max_cash_required"))
    effective_cash_budget = _resolve_cash_secured_budget(
        account_size,
        max_cash_required,
    )
    limit = max(1, _to_int(args.get("limit")) or 5)
    min_put_roi = _to_float(args.get("min_put_roi"))
    max_put_delta = _to_float(args.get("max_put_delta"))
    max_put_dte = _to_int(args.get("max_put_dte"))
    min_call_roi = _to_float(args.get("min_call_roi"))
    max_call_delta = _to_float(args.get("max_call_delta"))
    max_call_dte = _to_int(args.get("max_call_dte"))
    covered_call_style = _normalize_covered_call_style(args.get("covered_call_style"))
    default_covered_call_strategy = _normalize_covered_call_strategy(
        args.get("covered_call_strategy")
    )
    if default_covered_call_strategy is None:
        default_covered_call_strategy = _default_covered_call_strategy(None) or "balanced_income"

    raw_positions = args.get("positions") or []
    if raw_positions and not isinstance(raw_positions, list):
        return json.dumps({"error": "positions must be a list"})

    covered_call_positions = []
    skipped_positions = []

    for index, raw_position in enumerate(raw_positions):
        if not isinstance(raw_position, dict):
            skipped_positions.append({
                "position_index": index,
                "error": "Each position must be an object.",
            })
            continue

        symbol = str(raw_position.get("symbol") or "").strip().upper()
        shares_owned = _to_int(raw_position.get("shares_owned"))
        if not symbol:
            skipped_positions.append({
                "position_index": index,
                "error": "Position is missing symbol.",
            })
            continue
        if shares_owned is None:
            skipped_positions.append({
                "position_index": index,
                "symbol": symbol,
                "error": "Position is missing shares_owned.",
            })
            continue
        if shares_owned < 100:
            skipped_positions.append({
                "position_index": index,
                "symbol": symbol,
                "shares_owned": shares_owned,
                "error": "At least 100 shares are required to sell one standard covered call.",
            })
            continue

        position_strategy = _normalize_covered_call_strategy(
            raw_position.get("covered_call_strategy")
        ) or default_covered_call_strategy
        target_exit_price = _to_float(raw_position.get("target_exit_price"))
        position_args = {
            "symbol": symbol,
            "shares_owned": shares_owned,
            "cost_basis": _to_float(raw_position.get("cost_basis")),
            "assigned_price": _to_float(raw_position.get("assigned_price")),
            "premium_received_from_put": _to_float(
                raw_position.get("premium_received_from_put")
            ),
            "covered_call_strategy": position_strategy,
            "target_exit_price": target_exit_price,
            "style": covered_call_style,
            "max_dte": max_call_dte,
            "min_roi": min_call_roi,
        }

        payload = json.loads(_handle_covered_call_opportunity(position_args))
        if payload.get("error"):
            skipped_positions.append({
                "position_index": index,
                "symbol": symbol,
                "shares_owned": shares_owned,
                "error": payload["error"],
            })
            continue

        best_contract = payload.get("best_contract") or {}
        contract_delta = _to_float(best_contract.get("delta"))
        if max_call_delta is not None and (
            contract_delta is None or abs(contract_delta) > max_call_delta
        ):
            skipped_positions.append({
                "position_index": index,
                "symbol": symbol,
                "shares_owned": shares_owned,
                "error": (
                    "Best covered call opportunity did not satisfy max_call_delta filter "
                    f"({max_call_delta})."
                ),
            })
            continue

        covered_call_positions.append({
            "symbol": symbol,
            "shares_owned": shares_owned,
            "covered_share_lots": payload.get("covered_share_lots"),
            "cost_basis": payload.get("cost_basis"),
            "classification": payload.get("classification"),
            "stock_quality_score": payload.get("stock_quality_score"),
            "quality_score": payload.get("quality_score"),
            "technical_score": payload.get("technical_score"),
            "covered_call_strategy": payload.get("covered_call_strategy"),
            "best_contract": best_contract,
            "estimated_monthly_income": _estimate_normalized_monthly_income(
                best_contract.get("premium_income"),
                best_contract.get("dte"),
            ),
            "warnings": payload.get("warnings") or [],
            "ex_dividend_risk": payload.get("ex_dividend_risk"),
            "summary": payload.get("summary") or {},
        })

    covered_call_positions.sort(
        key=lambda item: (
            _to_float(item.get("best_contract", {}).get("covered_call_score")) or 0,
            _to_float(item.get("estimated_monthly_income")) or 0,
        ),
        reverse=True,
    )
    covered_call_positions = covered_call_positions[:limit]

    primary_put_idea = None
    allocated_put_ideas = []
    alternative_put_ideas = []
    put_allocation_summary = None
    put_plan_warning = None
    include_put_ideas = bool(not raw_positions or effective_cash_budget is not None)

    if include_put_ideas:
        put_selection_limit = max(1, min(limit, 3))
        put_scan_args = {
            "limit": (
                max(6, put_selection_limit * 4)
                if effective_cash_budget is not None
                else max(3, limit)
            )
        }
        if account_size is not None:
            put_scan_args["account_size"] = account_size
        if max_cash_required is not None:
            put_scan_args["max_cash_required"] = max_cash_required
        if min_put_roi is not None:
            put_scan_args["min_roi"] = min_put_roi
        if max_put_delta is not None:
            put_scan_args["max_delta"] = max_put_delta
        if max_put_dte is not None:
            put_scan_args["max_dte"] = max_put_dte

        put_payload = json.loads(
            _handle_scan_put_opportunities(put_scan_args, plan_context=runtime_plan)
        )
        if put_payload.get("error"):
            put_plan_warning = put_payload["error"]
        else:
            put_opportunities = put_payload.get("opportunities") or []
            if put_opportunities:
                enriched_put_ideas = [
                    {
                        **put_opportunities[0],
                        "estimated_monthly_income": _estimate_normalized_monthly_income(
                            put_opportunities[0].get("premium_received"),
                            put_opportunities[0].get("dte"),
                        ),
                    }
                ] + [
                    {
                        **item,
                        "estimated_monthly_income": _estimate_normalized_monthly_income(
                            item.get("premium_received"),
                            item.get("dte"),
                        ),
                    }
                    for item in put_opportunities[1:]
                ]
                allocated_put_ideas, alternative_put_ideas, put_allocation_summary = (
                    _select_diversified_put_allocation(
                        enriched_put_ideas,
                        total_budget=effective_cash_budget,
                        max_positions=put_selection_limit,
                    )
                )
                primary_put_idea = allocated_put_ideas[0] if allocated_put_ideas else None
            else:
                put_plan_warning = "No cash-secured put ideas matched the current filters."

    covered_call_monthly_income = round(
        sum(
            _to_float(position.get("estimated_monthly_income")) or 0
            for position in covered_call_positions
        ),
        2,
    )
    total_put_monthly_income = round(
        sum(
            _to_float(idea.get("estimated_monthly_income")) or 0
            for idea in allocated_put_ideas
        ),
        2,
    )
    primary_put_monthly_income = _to_float(primary_put_idea.get("estimated_monthly_income")) if primary_put_idea else None
    total_estimated_monthly_income = round(
        covered_call_monthly_income + total_put_monthly_income,
        2,
    )

    warnings = []
    if not raw_positions:
        warnings.append(
            "No owned positions were provided, so the plan defaults to cash-secured put / wheel ideas only."
        )
    elif skipped_positions:
        warnings.append(
            "Some provided positions could not be used for covered calls and were skipped."
        )
    if put_plan_warning:
        warnings.append(put_plan_warning)
    if put_allocation_summary and put_allocation_summary.get("diversified"):
        warnings.append(
            f"The CSP cash allocation was diversified across {put_allocation_summary['positions_selected']} tickers."
        )
    if primary_put_idea and len(alternative_put_ideas) > 0:
        warnings.append(
            "Only the allocated CSP ideas are included in the monthly income total; alternative CSP ideas are replacements, not additive."
        )
    if monthly_income_target is not None:
        warnings.append(
            "Monthly estimates are normalized from current option premium and DTE, not guaranteed recurring income."
        )

    target_met = None
    estimated_gap_to_target = None
    if monthly_income_target is not None:
        target_met = total_estimated_monthly_income >= monthly_income_target
        estimated_gap_to_target = round(
            monthly_income_target - total_estimated_monthly_income,
            2,
        )

    if covered_call_positions and primary_put_idea:
        plan_type = "mixed_income_plan"
    elif covered_call_positions:
        plan_type = "covered_calls_only"
    elif primary_put_idea:
        plan_type = "cash_secured_puts_only"
    else:
        return json.dumps(
            {
                "plan": runtime_plan.get("plan"),
                "trial_days_left": runtime_plan.get("trial_days_left"),
                "error": "No valid monthly income plan candidates found.",
                "skipped_positions": skipped_positions,
                "filters_applied": {
                    "monthly_income_target": monthly_income_target,
                    "account_size": account_size,
                    "max_cash_required": max_cash_required,
                    "effective_max_cash_required": effective_cash_budget,
                    "min_put_roi": min_put_roi,
                    "max_put_delta": max_put_delta,
                    "max_put_dte": max_put_dte,
                    "min_call_roi": min_call_roi,
                    "max_call_delta": max_call_delta,
                    "max_call_dte": max_call_dte,
                    "covered_call_style": covered_call_style,
                    "covered_call_strategy": default_covered_call_strategy,
                },
                "warnings": _dedupe_preserve_order(warnings),
            },
            default=_json_default,
        )

    return json.dumps(
        {
            "plan": runtime_plan.get("plan"),
            "trial_days_left": runtime_plan.get("trial_days_left"),
            "plan_type": plan_type,
            "monthly_income_target": monthly_income_target,
            "owned_positions_provided": bool(raw_positions),
            "covered_call_positions_evaluated": len(covered_call_positions),
            "allocated_put_positions": len(allocated_put_ideas),
            "primary_put_idea_included": primary_put_idea is not None,
            "covered_call_positions": covered_call_positions,
            "allocated_put_ideas": allocated_put_ideas,
            "primary_put_idea": primary_put_idea,
            "alternative_put_ideas": alternative_put_ideas,
            "put_allocation_summary": put_allocation_summary,
            "skipped_positions": skipped_positions,
            "summary": {
                "estimated_monthly_income_from_covered_calls": covered_call_monthly_income,
                "estimated_monthly_income_from_primary_put": primary_put_monthly_income,
                "estimated_monthly_income_from_puts": total_put_monthly_income,
                "estimated_total_monthly_income": total_estimated_monthly_income,
                "monthly_income_target": monthly_income_target,
                "target_met": target_met,
                "estimated_gap_to_target": estimated_gap_to_target,
            },
            "filters_applied": {
                "monthly_income_target": monthly_income_target,
                "account_size": account_size,
                "max_cash_required": max_cash_required,
                "effective_max_cash_required": effective_cash_budget,
                "limit": limit,
                "min_put_roi": min_put_roi,
                "max_put_delta": max_put_delta,
                "max_put_dte": max_put_dte,
                "min_call_roi": min_call_roi,
                "max_call_delta": max_call_delta,
                "max_call_dte": max_call_dte,
                "covered_call_style": covered_call_style,
                "covered_call_strategy": default_covered_call_strategy,
            },
            "warnings": _dedupe_preserve_order(warnings),
        },
        default=_json_default,
    )


def _handle_compare_spread_candidates(args: dict) -> str:
    raw_symbols = args.get("symbols") or []
    if raw_symbols and not isinstance(raw_symbols, list):
        return json.dumps({"error": "symbols must be a list when provided"})

    symbols = []
    for value in raw_symbols:
        if value is None:
            continue
        symbol = str(value).strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    single_symbol = str(args.get("symbol") or "").strip().upper()
    if single_symbol and single_symbol not in symbols:
        symbols.append(single_symbol)

    if not symbols:
        return json.dumps(
            {"error": "Provide `symbols` or `symbol` for spread comparison."}
        )

    spread_types = []
    raw_spread_types = args.get("spread_types") or []
    if raw_spread_types and not isinstance(raw_spread_types, list):
        return json.dumps({"error": "spread_types must be a list when provided"})
    for value in raw_spread_types:
        normalized = _normalize_spread_type(value)
        if normalized not in spread_types:
            spread_types.append(normalized)
    if not spread_types:
        spread_types = [_normalize_spread_type(args.get("spread_type"))]

    directional_view = _normalize_directional_view(args.get("directional_view"))
    risk_profile = _normalize_risk_profile(args.get("risk_profile"))
    max_dte = _to_int(args.get("max_dte"))
    min_credit = _to_float(args.get("min_credit"))
    max_debit = _to_float(args.get("max_debit"))
    max_risk = _to_float(args.get("max_risk"))
    preferred_width = _to_float(args.get("width"))
    min_return_on_risk_pct = _to_float(args.get("min_return_on_risk_pct"))
    min_probability_of_profit = _to_float(args.get("min_probability_of_profit"))
    min_quality_score = _to_float(args.get("min_quality_score"))
    max_short_delta = _to_float(args.get("max_short_delta"))
    exclude_earnings = args.get("exclude_earnings")

    profile_overrides = {}
    if exclude_earnings is not None:
        profile_overrides["credit_exclude_earnings_before_expiration"] = bool(
            exclude_earnings
        )
    if min_return_on_risk_pct is not None:
        profile_overrides["credit_min_return_on_risk_pct"] = min_return_on_risk_pct
        profile_overrides["debit_min_reward_to_risk"] = max(
            min_return_on_risk_pct / 100,
            0,
        )
    if min_probability_of_profit is not None:
        profile_overrides["credit_min_probability_of_profit"] = (
            min_probability_of_profit
        )
    if max_short_delta is not None:
        clamped_short_delta = max(0, abs(max_short_delta))
        profile_overrides["credit_short_delta_min"] = 0.01
        profile_overrides["credit_short_delta_max"] = clamped_short_delta
        profile_overrides["debit_short_delta_min"] = 0.01
        profile_overrides["debit_short_delta_max"] = clamped_short_delta

    ranked_candidates = []
    skipped = []

    for symbol in symbols:
        try:
            sym = Symbol.objects.prefetch_related("expiration_snapshots").filter(
                ticker__iexact=symbol
            ).first()
        except Exception as e:
            skipped.append(
                {
                    "symbol": symbol,
                    "error": f"Database error while fetching symbol data: {str(e)}",
                }
            )
            continue

        if sym is None:
            skipped.append(
                {"symbol": symbol, "error": f"No data found in database for {symbol}"}
            )
            continue

        for requested_spread_type in spread_types:
            evaluation = _evaluate_spread_candidates(
                sym=sym,
                spread_type=requested_spread_type,
                directional_view=directional_view,
                risk_profile=risk_profile,
                profile_overrides=profile_overrides or None,
                max_dte=max_dte,
                min_credit=min_credit,
                max_debit=max_debit,
                max_risk=max_risk,
                preferred_width=preferred_width,
            )

            quality_score = _to_float(evaluation["quality_score"])
            if min_quality_score is not None and (
                quality_score is None or quality_score < min_quality_score
            ):
                skipped.append(
                    {
                        "symbol": symbol,
                        "spread_type_requested": requested_spread_type,
                        "error": (
                            "Underlying quality score below min_quality_score filter "
                            f"({min_quality_score})."
                        ),
                    }
                )
                continue

            matched_candidates = _filter_spread_candidates(
                evaluation,
                min_return_on_risk_pct=min_return_on_risk_pct,
                min_probability_of_profit=min_probability_of_profit,
                max_risk=max_risk,
                max_short_delta=max_short_delta,
                exclude_earnings=exclude_earnings,
            )
            if not matched_candidates:
                skipped.append(
                    {
                        "symbol": symbol,
                        "spread_type_requested": requested_spread_type,
                        "error": (
                            "No spread candidates passed the available option-chain data "
                            "and filter constraints."
                        ),
                    }
                )
                continue

            best = matched_candidates[0]
            ranked_candidates.append(
                {
                    "symbol": symbol,
                    "spread_type_requested": requested_spread_type,
                    "price": evaluation["stock_price"],
                    "classification": sym.classification,
                    "stock_quality_score": quality_score,
                    "quality_score": quality_score,
                    "technical_score": evaluation["technical_score"],
                    "resolved_directional_view": evaluation["resolved_view"],
                    "comparison_score": best.get("spread_score"),
                    "spread_score": best.get("spread_score"),
                    "score": best.get("spread_score"),
                    "rating": best.get("rating"),
                    "warnings": best.get("warnings") or [],
                    "reasons": best.get("reasons") or [],
                    "best_spread": best,
                }
            )

    ranked_candidates.sort(
        key=lambda item: (
            item.get("comparison_score") or 0,
            item.get("stock_quality_score") or 0,
            item.get("best_spread", {}).get("return_on_risk_pct")
            or item.get("best_spread", {}).get("reward_to_risk")
            or 0,
        ),
        reverse=True,
    )

    comparison_mode = "ticker_comparison"
    if len(symbols) == 1 and len(spread_types) > 1:
        comparison_mode = "spread_type_comparison"
    elif len(symbols) > 1 and len(spread_types) > 1:
        comparison_mode = "matrix_comparison"

    return json.dumps(
        {
            "symbols_requested": symbols,
            "spread_types_requested": spread_types,
            "comparison_mode": comparison_mode,
            "candidates_compared": len(ranked_candidates),
            "winner": ranked_candidates[0] if ranked_candidates else None,
            "ranked_candidates": ranked_candidates,
            "skipped": skipped,
            "filters_applied": {
                "directional_view": directional_view,
                "risk_profile": risk_profile,
                "max_dte": max_dte,
                "min_credit": min_credit,
                "max_debit": max_debit,
                "max_risk": max_risk,
                "width": preferred_width,
                "min_return_on_risk_pct": min_return_on_risk_pct,
                "min_probability_of_profit": min_probability_of_profit,
                "min_quality_score": min_quality_score,
                "max_short_delta": max_short_delta,
                "exclude_earnings": exclude_earnings,
            },
        },
        default=_json_default,
    )


def _handle_compare_put_candidates(args: dict) -> str:
    raw_symbols = args.get("symbols") or []
    if not isinstance(raw_symbols, list) or not raw_symbols:
        return json.dumps({"error": "symbols must be a non-empty list"})

    max_delta = _to_float(args.get("max_delta"))
    min_roi = _to_float(args.get("min_roi"))
    min_quality_score = _to_float(args.get("min_quality_score"))
    account_size = _to_float(args.get("account_size"))
    max_cash_required = _to_float(args.get("max_cash_required"))
    effective_cash_budget = _resolve_cash_secured_budget(
        account_size,
        max_cash_required,
    )

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
        payload = json.loads(
            _handle_put_wheel_opportunity(
                symbol,
                account_size=account_size,
                max_cash_required=max_cash_required,
            )
        )
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
            "account_size": account_size,
            "max_cash_required": max_cash_required,
            "effective_max_cash_required": effective_cash_budget,
        },
    }, default=_json_default)


def _handle_compare_covered_call_candidates(args: dict) -> str:
    raw_symbols = args.get("symbols") or []
    if not isinstance(raw_symbols, list) or not raw_symbols:
        return json.dumps({"error": "symbols must be a non-empty list"})

    max_delta = _to_float(args.get("max_delta"))
    min_roi = _to_float(args.get("min_roi"))

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
        try:
            sym = Symbol.objects.filter(ticker__iexact=symbol).first()
        except Exception as e:
            skipped.append({
                "symbol": symbol,
                "error": f"Database error while fetching symbol data: {str(e)}",
            })
            continue

        if sym is None:
            skipped.append({
                "symbol": symbol,
                "error": f"No data found in database for {symbol}",
            })
            continue

        payload = _evaluate_covered_call_symbol(
            sym,
            shares_owned=100,
            cost_basis=None,
            assigned_price=None,
            premium_received_from_put=None,
            target_delta=None,
            max_dte=None,
            min_roi=min_roi,
            max_delta_filter=max_delta,
            style="balanced",
            covered_call_strategy="balanced_income",
            target_exit_price=None,
        )
        if payload.get("error"):
            filter_reason = None
            if max_delta is not None and min_roi is not None:
                filter_reason = (
                    "Best covered call opportunity did not satisfy max_delta and/or "
                    f"min_roi filters (max_delta={max_delta}, min_roi={min_roi})."
                )
            elif max_delta is not None:
                filter_reason = (
                    "Best covered call opportunity did not satisfy max_delta filter "
                    f"({max_delta})."
                )
            elif min_roi is not None:
                filter_reason = (
                    "Best covered call opportunity did not satisfy min_roi filter "
                    f"({min_roi})."
                )

            skipped.append({
                "symbol": symbol,
                "error": filter_reason or payload["error"],
            })
            continue

        best_contract = payload.get("best_contract") or {}
        comparison_score = _to_float(payload.get("summary", {}).get("covered_call_score"))

        ranked_candidates.append({
            "symbol": symbol,
            "price": payload.get("current_price"),
            "classification": payload.get("classification"),
            "stock_quality_score": payload.get("stock_quality_score"),
            "quality_score": payload.get("quality_score"),
            "technical_score": payload.get("technical_score"),
            "comparison_score": comparison_score,
            "covered_call_score": comparison_score,
            "score": comparison_score,
            "covered_call_rating": payload.get("summary", {}).get("rating"),
            "rating": payload.get("summary", {}).get("rating"),
            "call_away_risk": best_contract.get("call_away_risk"),
            "best_contract": best_contract,
            "warnings": payload.get("warnings") or [],
            "reasons": best_contract.get("reasons") or [],
            "earnings_risk": "Earnings occur before expiration." in (payload.get("warnings") or []),
            "ex_dividend_risk": payload.get("ex_dividend_risk"),
        })

    ranked_candidates.sort(
        key=lambda item: (
            item.get("comparison_score") or 0,
            item.get("stock_quality_score") or 0,
            item.get("best_contract", {}).get("premium_yield_pct") or 0,
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
            "style": "balanced",
            "covered_call_strategy": "balanced_income",
            "shares_assumed": 100,
        },
    }, default=_json_default)


def _canonicalize_tool_response(tool_name: str, response: str) -> str:
    """Add stable API field names while preserving legacy response aliases.

    Tool handlers predate a shared vocabulary and therefore use combinations of
    ``price``/``current_price`` and generic ``score`` keys.  The agent and any
    new API consumer should use the canonical fields added here; legacy keys
    remain during the compatibility period.
    """
    try:
        payload = json.loads(response)
    except (TypeError, ValueError):
        return response

    if not isinstance(payload, (dict, list)):
        return response

    if tool_name in {
        "get_put_wheel_opportunity",
        "scan_put_opportunities",
        "compare_put_candidates",
    }:
        score_field = "put_opportunity_score"
        score_sources = ("put_opportunity_score", "opportunity_score", "cumulative_score", "score")
    elif tool_name in {
        "get_covered_call_opportunity",
        "scan_covered_call_opportunities",
        "compare_covered_call_candidates",
    }:
        score_field = "covered_call_score"
        score_sources = ("covered_call_score", "score")
    elif tool_name in {
        "get_spread_opportunity",
        "scan_spread_opportunities",
        "compare_spread_candidates",
    }:
        score_field = "spread_score"
        score_sources = ("spread_score", "score")
    else:
        return response

    def normalize(value: Any) -> Any:
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if not isinstance(value, dict):
            return value

        normalized = {key: normalize(item) for key, item in value.items()}
        if "underlying_price" not in normalized:
            normalized["underlying_price"] = normalized.get(
                "current_price", normalized.get("price")
            )
        if normalized.get("underlying_price") is None:
            normalized.pop("underlying_price", None)

        if "stock_quality_score" not in normalized and "quality_score" in normalized:
            normalized["stock_quality_score"] = normalized["quality_score"]

        if score_field not in normalized:
            for source in score_sources:
                if source in normalized:
                    normalized[score_field] = normalized[source]
                    break

        if "rating" not in normalized:
            for source in ("opportunity_rating", "covered_call_rating"):
                if source in normalized:
                    normalized["rating"] = normalized[source]
                    break
        return normalized

    return json.dumps(normalize(payload), default=_json_default)


def handle_tool_call(
    tool_name: str,
    tool_args: dict,
    *,
    user=None,
    plan_context: dict[str, Any] | None = None,
) -> str:
    runtime_plan = _resolve_runtime_plan_context(user=user, plan_context=plan_context)
    if tool_name == "analyze_stock":
        daily_limit = (runtime_plan.get("entitlements") or {}).get("daily_analyze_stock")
        if daily_limit is not None and user is not None:
            used_today = _count_daily_analyze_stock_calls(user)
            if used_today >= daily_limit:
                return json.dumps(
                    {
                        "error": (
                            "daily_analyze_stock_limit_reached: "
                            f"{runtime_plan.get('plan')} plan allows {daily_limit} "
                            "analyze_stock call(s) per day."
                        ),
                        "error_code": "daily_analyze_stock_limit_reached",
                        "plan": runtime_plan.get("plan"),
                        "trial_days_left": runtime_plan.get("trial_days_left"),
                        "limit": daily_limit,
                        "used_today": used_today,
                        "upgrade_available": runtime_plan.get("plan") == "free",
                    }
                )
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

            return _canonicalize_tool_response(tool_name, json.dumps(report))

        except Exception as e:
            return json.dumps({"error": str(e), "symbol": symbol})

    if tool_name == "get_put_wheel_opportunity":
        result = _handle_put_wheel_opportunity(
            tool_args["symbol"], account_size=_to_float(tool_args.get("account_size")),
            max_cash_required=_to_float(tool_args.get("max_cash_required")),
        )
    elif tool_name == "get_covered_call_opportunity":
        result = _handle_covered_call_opportunity(tool_args)
    elif tool_name == "build_monthly_income_plan":
        result = _handle_build_monthly_income_plan(tool_args, plan_context=runtime_plan)
    elif tool_name == "get_spread_opportunity":
        result = _handle_spread_opportunity(tool_args)
    elif tool_name == "scan_put_opportunities":
        result = _handle_scan_put_opportunities(tool_args, plan_context=runtime_plan)
    elif tool_name == "scan_spread_opportunities":
        result = _handle_scan_spread_opportunities(tool_args, plan_context=runtime_plan)
    elif tool_name == "scan_covered_call_opportunities":
        result = _handle_scan_covered_call_opportunities(tool_args, plan_context=runtime_plan)
    elif tool_name == "compare_spread_candidates":
        result = _handle_compare_spread_candidates(tool_args)
    elif tool_name == "compare_put_candidates":
        result = _handle_compare_put_candidates(tool_args)
    elif tool_name == "compare_covered_call_candidates":
        result = _handle_compare_covered_call_candidates(tool_args)
    else:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})

    return _canonicalize_tool_response(tool_name, result)


def _augment_tool_args_from_query(
    tool_name: str,
    tool_args: dict,
    user_query: str,
    history: list[dict[str, Any]] | None = None,
) -> dict:
    merged_args = dict(tool_args or {})

    if tool_name in {
        "analyze_stock",
        "get_put_wheel_opportunity",
        "build_monthly_income_plan",
        "scan_put_opportunities",
        "compare_put_candidates",
    }:
        merged_args.update(_extract_cash_budget_from_query(user_query))

    if tool_name == "build_monthly_income_plan":
        extracted_positions = _extract_owned_positions_from_query(user_query)
        if extracted_positions and "positions" not in merged_args:
            normalized_positions = []
            for position in extracted_positions:
                normalized_position = {
                    "symbol": position["symbol"],
                    "shares_owned": position.get("shares_owned") or 100,
                }
                if position.get("cost_basis") is not None:
                    normalized_position["cost_basis"] = position["cost_basis"]
                normalized_positions.append(normalized_position)
            merged_args["positions"] = normalized_positions

    if tool_name != "scan_put_opportunities":
        return merged_args

    if _is_show_more_follow_up(user_query):
        scan_state = _extract_history_tool_state(history).get("scan_put_opportunities")
        if isinstance(scan_state, dict):
            previous_base_arguments = scan_state.get("base_arguments") or {}
            for key, value in previous_base_arguments.items():
                merged_args.setdefault(key, value)
            if scan_state.get("limit") is not None:
                merged_args.setdefault("limit", scan_state.get("limit"))
            if "offset" not in merged_args:
                if scan_state.get("next_offset") is not None:
                    merged_args["offset"] = scan_state.get("next_offset")
                elif scan_state.get("total_results_available") is not None:
                    merged_args["offset"] = scan_state.get("total_results_available")

    extracted_filters = {}
    extracted_filters.update(_extract_underlying_price_filters_from_query(user_query))
    extracted_filters.update(_extract_rsi_filters_from_query(user_query))
    merged_args.update(extracted_filters)
    return merged_args


def _validate_provider(provider: str) -> str:
    if provider not in {"anthropic", "openai", "gemini"}:
        raise ValueError(
            "AGENT_MODEL_PROVIDER must be one of 'anthropic', 'openai', or 'gemini'"
        )
    return provider


def _get_agent_provider(plan: str) -> str:
    global_provider = str(getattr(settings, "AGENT_MODEL_PROVIDER", "anthropic")).strip().lower()

    if plan == "free":
        configured_provider = str(
            getattr(settings, "AGENT_MODEL_PROVIDER_FREE", "")
        ).strip().lower()
        if configured_provider:
            return _validate_provider(configured_provider)
        return _validate_provider(global_provider)

    if plan == "pro":
        configured_provider = str(
            getattr(settings, "AGENT_MODEL_PROVIDER_PRO", "")
        ).strip().lower()
        if configured_provider:
            return _validate_provider(configured_provider)
        return _validate_provider(global_provider)

    return _validate_provider(global_provider)


def _get_agent_model(provider: str, plan: str) -> str:
    configured_model = str(getattr(settings, "AGENT_MODEL", "")).strip()
    if configured_model:
        return configured_model
    if plan == "free":
        configured_free_model = str(getattr(settings, "AGENT_MODEL_FREE", "")).strip()
        if configured_free_model:
            return configured_free_model
        if provider == "anthropic":
            return "claude-3-5-haiku-latest"
        if provider == "gemini":
            return "gemini-2.5-flash"
        return "gpt-4.1-mini"

    configured_pro_model = str(getattr(settings, "AGENT_MODEL_PRO", "")).strip()
    if configured_pro_model:
        return configured_pro_model
    if provider == "anthropic":
        return "claude-sonnet-4-5"
    if provider == "gemini":
        return "gemini-2.5-flash"
    return "gpt-4.1"


def _serialize_tool_trace_entry(tool_name: str, tool_args: dict[str, Any], iteration: int) -> dict[str, Any]:
    return {
        "name": tool_name,
        "arguments": json.loads(json.dumps(tool_args or {}, default=_json_default)),
        "iteration": iteration,
    }


def _build_agent_history(
    conversation_history: list[dict[str, Any]],
    *,
    user_query: str,
    answer: str,
    tool_state: dict[str, Any],
) -> list[dict[str, Any]]:
    meta_entry = _build_history_meta_entry(tool_state)
    history_items = []
    if meta_entry is not None:
        history_items.append(meta_entry)
    history_items.extend(conversation_history)
    history_items.extend([
        {"role": "user", "content": user_query},
        {"role": "assistant", "content": answer},
    ])
    return history_items


def run_agent(
    query: str,
    history: list[dict[str, Any]] | None = None,
    *,
    agent_run_id: int | None = None,
    user=None,
    plan_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    empty_usage_summary = calculate_usage_cost([])
    normalized_query = (query or "").strip()
    if not normalized_query:
        raise ValueError("query is required")

    if history is not None and not isinstance(history, list):
        raise ValueError("history must be a list")

    raw_history = history or []
    runtime_plan = _resolve_runtime_plan_context(user=user, plan_context=plan_context)
    conversation_history = _trim_history_for_plan(
        _history_without_meta(raw_history),
        runtime_plan,
    )
    history_tool_state = _extract_history_tool_state(raw_history)

    fast_path_response = _get_fast_path_response(normalized_query)
    if fast_path_response is not None:
        logger.info(
            "Agent fast-path response run_id=%s reason=%s",
            agent_run_id,
            "social" if _is_simple_social_query(normalized_query) else "query_too_long",
        )
        _log_agent_response(agent_run_id, normalized_query, fast_path_response)
        return {
            "answer": fast_path_response,
            "history": _build_agent_history(
                conversation_history,
                user_query=normalized_query,
                answer=fast_path_response,
                tool_state=history_tool_state,
            ),
            "used_tools": [],
            "llm_usage": [],
            "llm_usage_summary": empty_usage_summary,
        }

    prepared_query = _prepare_agent_query(normalized_query, history=raw_history)
    provider = _get_agent_provider(runtime_plan["plan"])
    model_name = _get_agent_model(provider, runtime_plan["plan"])
    max_iterations = max(1, int(getattr(settings, "AGENT_MAX_ITERATIONS", 10)))
    llm_timeout_seconds = max(
        1, int(getattr(settings, "AGENT_OPENAI_TIMEOUT_SECONDS", 45))
    )
    overall_timeout_seconds = max(
        llm_timeout_seconds,
        int(getattr(settings, "AGENT_OVERALL_TIMEOUT_SECONDS", 90)),
    )
    openai_max_retries = max(
        0, int(getattr(settings, "AGENT_OPENAI_MAX_RETRIES", 1))
    )

    if provider == "anthropic":
        api_key = getattr(settings, "ANTHROPIC_API_KEY", None) or os.environ.get(
            "ANTHROPIC_API_KEY"
        )
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Configure it for the service running agent jobs."
            )
        anthropic_max_tokens = max(
            1, int(getattr(settings, "AGENT_ANTHROPIC_MAX_TOKENS", 4096))
        )
        messages = [
            *conversation_history,
            {"role": "user", "content": prepared_query},
        ]
        client = Anthropic(
            api_key=api_key,
            timeout=llm_timeout_seconds,
        )
    else:
        api_key_setting_name = "GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"
        api_key = getattr(settings, api_key_setting_name, None) or os.environ.get(
            api_key_setting_name
        )
        if not api_key:
            raise RuntimeError(
                f"{api_key_setting_name} is not set. Configure it for the service running agent jobs."
            )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *conversation_history,
            {"role": "user", "content": prepared_query},
        ]
        openai_client_kwargs = {
            "api_key": api_key,
            "timeout": llm_timeout_seconds,
            "max_retries": openai_max_retries,
        }
        if provider == "gemini":
            openai_client_kwargs["base_url"] = getattr(
                settings,
                "GEMINI_OPENAI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta/openai/",
            )
        client = OpenAI(
            **openai_client_kwargs,
        )

    started_at = time.monotonic()
    used_tools: list[dict[str, Any]] = []
    llm_usage: list[dict[str, Any]] = []
    tool_state = history_tool_state

    logger.info(
        "Agent run started run_id=%s provider=%s model=%s query_length=%s history_items=%s max_iterations=%s llm_timeout=%ss overall_timeout=%ss",
        agent_run_id,
        provider,
        model_name,
        len(normalized_query),
        len(conversation_history),
        max_iterations,
        llm_timeout_seconds,
        overall_timeout_seconds,
    )

    # Agentic loop keeps running until the model stops calling tools.
    for iteration in range(1, max_iterations + 1):
        elapsed_before_request = time.monotonic() - started_at
        if elapsed_before_request >= overall_timeout_seconds:
            raise RuntimeError(
                f"Agent run exceeded overall timeout ({overall_timeout_seconds}s)"
            )

        logger.info(
            "Agent iteration start run_id=%s iteration=%s elapsed=%.2fs",
            agent_run_id,
            iteration,
            elapsed_before_request,
        )
        if provider == "anthropic":
            response = client.messages.create(
                model=model_name,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=ANTHROPIC_TOOLS,
                tool_choice={"type": "auto"},
                max_tokens=anthropic_max_tokens,
            )
            llm_usage.append(
                extract_anthropic_usage_metrics(
                    response,
                    model=model_name,
                    prompt_key="agent_chat",
                    iteration=iteration,
                )
            )
            _persist_llm_usage(agent_run_id, llm_usage)

            tool_uses = [
                block for block in response.content if getattr(block, "type", None) == "tool_use"
            ]
            text_blocks = [
                block.text for block in response.content if getattr(block, "type", None) == "text"
            ]

            logger.info(
                "Agent iteration complete run_id=%s iteration=%s tool_calls=%s elapsed=%.2fs",
                agent_run_id,
                iteration,
                len(tool_uses),
                time.monotonic() - started_at,
            )

            if not tool_uses:
                answer = "\n".join(block for block in text_blocks if block).strip()
                logger.info(
                    "Agent run completed run_id=%s iterations=%s total_elapsed=%.2fs",
                    agent_run_id,
                    iteration,
                    time.monotonic() - started_at,
                )
                _log_agent_response(agent_run_id, normalized_query, answer)
                return {
                    "answer": answer,
                    "history": _build_agent_history(
                        conversation_history,
                        user_query=normalized_query,
                        answer=answer,
                        tool_state=tool_state,
                    ),
                    "used_tools": used_tools,
                    "llm_usage": llm_usage,
                    "llm_usage_summary": calculate_usage_cost(llm_usage),
                }

            assistant_content = []
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif getattr(block, "type", None) == "tool_use":
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            messages.append({"role": "assistant", "content": assistant_content})

            tool_results = []
            for tool_use in tool_uses:
                tool_started_at = time.monotonic()
                logger.info(
                    "Agent tool start run_id=%s iteration=%s tool=%s",
                    agent_run_id,
                    iteration,
                    tool_use.name,
                )
                tool_input = _augment_tool_args_from_query(
                    tool_use.name,
                    tool_use.input,
                    normalized_query,
                    history=raw_history,
                )
                used_tools.append(
                    _serialize_tool_trace_entry(tool_use.name, tool_input, iteration)
                )
                _persist_used_tools(agent_run_id, used_tools)
                result = handle_tool_call(
                    tool_use.name,
                    tool_input,
                    user=user,
                    plan_context=runtime_plan,
                )
                tool_state = _update_history_tool_state(
                    tool_state,
                    tool_name=tool_use.name,
                    tool_args=tool_input,
                    tool_result=result,
                )
                logger.info(
                    "Agent tool complete run_id=%s iteration=%s tool=%s duration=%.2fs",
                    agent_run_id,
                    iteration,
                    tool_use.name,
                    time.monotonic() - tool_started_at,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result,
                })

                elapsed_after_tool = time.monotonic() - started_at
                if elapsed_after_tool >= overall_timeout_seconds:
                    raise RuntimeError(
                        f"Agent run exceeded overall timeout ({overall_timeout_seconds}s)"
                    )

            messages.append({"role": "user", "content": tool_results})
        else:
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            llm_usage.append(
                extract_openai_usage_metrics(
                    response,
                    provider=provider,
                    model=model_name,
                    prompt_key="agent_chat",
                    iteration=iteration,
                )
            )
            _persist_llm_usage(agent_run_id, llm_usage)

            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            logger.info(
                "Agent iteration complete run_id=%s iteration=%s tool_calls=%s elapsed=%.2fs",
                agent_run_id,
                iteration,
                len(tool_calls),
                time.monotonic() - started_at,
            )

            if not tool_calls:
                answer = message.content or ""
                logger.info(
                    "Agent run completed run_id=%s iterations=%s total_elapsed=%.2fs",
                    agent_run_id,
                    iteration,
                    time.monotonic() - started_at,
                )
                _log_agent_response(agent_run_id, normalized_query, answer)
                return {
                    "answer": answer,
                    "history": _build_agent_history(
                        conversation_history,
                        user_query=normalized_query,
                        answer=answer,
                        tool_state=tool_state,
                    ),
                    "used_tools": used_tools,
                    "llm_usage": llm_usage,
                    "llm_usage_summary": calculate_usage_cost(llm_usage),
                }

            messages.append(message.model_dump(exclude_none=True))

            for tool_call in tool_calls:
                tool_started_at = time.monotonic()
                logger.info(
                    "Agent tool start run_id=%s iteration=%s tool=%s",
                    agent_run_id,
                    iteration,
                    tool_call.function.name,
                )
                tool_args = _augment_tool_args_from_query(
                    tool_call.function.name,
                    json.loads(tool_call.function.arguments),
                    normalized_query,
                    history=raw_history,
                )
                used_tools.append(
                    _serialize_tool_trace_entry(
                        tool_call.function.name,
                        tool_args,
                        iteration,
                    )
                )
                _persist_used_tools(agent_run_id, used_tools)
                result = handle_tool_call(
                    tool_call.function.name,
                    tool_args,
                    user=user,
                    plan_context=runtime_plan,
                )
                tool_state = _update_history_tool_state(
                    tool_state,
                    tool_name=tool_call.function.name,
                    tool_args=tool_args,
                    tool_result=result,
                )
                logger.info(
                    "Agent tool complete run_id=%s iteration=%s tool=%s duration=%.2fs",
                    agent_run_id,
                    iteration,
                    tool_call.function.name,
                    time.monotonic() - tool_started_at,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result,
                })

                elapsed_after_tool = time.monotonic() - started_at
                if elapsed_after_tool >= overall_timeout_seconds:
                    raise RuntimeError(
                        f"Agent run exceeded overall timeout ({overall_timeout_seconds}s)"
                    )

    raise RuntimeError(f"Agent loop exceeded maximum iterations ({max_iterations})")


def serialize_agent_run(agent_run: AgentRun) -> dict[str, Any]:
    used_tools = agent_run.used_tools_json or []
    llm_usage = agent_run.llm_usage_json or []
    plan_data = serialize_plan_context(agent_run.user)
    return {
        "job_id": agent_run.id,
        "query": agent_run.query,
        "history": agent_run.history_json or [],
        "status": agent_run.status,
        "answer": agent_run.result_text,
        "error": agent_run.error_text,
        "used_tools": used_tools,
        "used_tool": used_tools[-1] if used_tools else None,
        "llm_usage": llm_usage,
        "llm_usage_summary": agent_run.llm_usage_summary_json or calculate_usage_cost(llm_usage),
        "created_at": agent_run.created_at.isoformat() if agent_run.created_at else None,
        "started_at": agent_run.started_at.isoformat() if agent_run.started_at else None,
        "finished_at": agent_run.finished_at.isoformat() if agent_run.finished_at else None,
        "plan": plan_data["plan"],
        "trial_days_left": plan_data["trial_days_left"],
    }


class AgentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        query = request.data.get("query", "").strip()
        if not query:
            return Response({"error": "query is required"}, status=400)

        # Restore conversation history from the request (client sends it back)
        history = request.data.get("history", [])
        if history is not None and not isinstance(history, list):
            return Response({"error": "history must be a list"}, status=400)

        plan_context = get_plan_context(request.user)
        daily_query_limit = (plan_context.get("entitlements") or {}).get("daily_queries")
        if daily_query_limit is not None:
            used_today = _count_daily_agent_queries(request.user)
            if used_today >= daily_query_limit:
                return Response(
                    {
                        "error": "daily_limit_reached",
                        "plan": plan_context["plan"],
                        "trial_days_left": plan_context["trial_days_left"],
                        "limit": daily_query_limit,
                        "used_today": used_today,
                        "trial_expired": plan_context["trial_expired"],
                        "upgrade_available": True,
                    },
                    status=429,
                )

        agent_run = AgentRun.objects.create(
            user=request.user,
            query=query,
            history_json=history or [],
        )
        from api.tasks import run_agent_run

        run_agent_run.delay(agent_run.id)
        return Response(
            serialize_agent_run(agent_run),
            status=status.HTTP_202_ACCEPTED,
        )

    def get(self, request, job_id: int | None = None):
        if job_id is None:
            requested_limit = _to_int(request.query_params.get("limit")) or 20
            limit = max(1, min(requested_limit, 100))
            runs = AgentRun.objects.filter(user=request.user).order_by("-created_at", "-id")[:limit]
            return Response(
                {
                    "results": [serialize_agent_run(agent_run) for agent_run in runs],
                    "count": len(runs),
                }
            )
        agent_run = AgentRun.objects.filter(pk=job_id, user=request.user).first()
        if agent_run is None:
            return Response({"error": "Agent run not found"}, status=404)
        return Response(serialize_agent_run(agent_run))
