import json
import os
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List

from django.conf import settings
from openai import OpenAI, OpenAIError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from api.helper import FinancialMetricsCalculator


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
            while True:
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

        except OpenAIError as e:
            return Response({"error": f"AI service error: {str(e)}"}, status=502)
