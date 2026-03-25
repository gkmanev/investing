"""
Django management command for AI-powered Put-Selling Due Diligence (V2).

Usage:
    python manage.py ai_agentV2 GLW
    python manage.py ai_agentV2 AAPL MSFT --save
"""

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import csv
import http.client
import json
from pathlib import Path
from typing import Dict, Any
from openai import OpenAI


class FinancialDDAgentV2:
    """
    AI agent evaluating whether a company is suitable for
    selling cash-secured puts (wheel strategy).
    """

    def __init__(self):
        api_key = getattr(settings, "OPENAI_API_KEY", None)
        self.client = OpenAI(api_key=api_key) if api_key else OpenAI()

        self.model = getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")
        self.rapidapi_host = getattr(
            settings, "RAPIDAPI_HOST", "financial-modeling-prep.p.rapidapi.com"
        )
        self.rapidapi_key = getattr(
            settings, "RAPIDAPI_KEY", "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f"
        )

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    def _fetch_json(self, path: str) -> Any:
        headers = {
            "x-rapidapi-key": self.rapidapi_key,
            "x-rapidapi-host": self.rapidapi_host,
            "Content-Type": "application/json",
        }

        conn = http.client.HTTPSConnection(self.rapidapi_host, timeout=30)
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            body = response.read()
        finally:
            conn.close()

        if response.status >= 400:
            raise Exception(
                f"RapidAPI request failed ({response.status}): {response.reason}"
            )

        if not body:
            return None

        return json.loads(body.decode("utf-8"))

    def fetch_financial_data(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()
        api_key_param = self.rapidapi_key
        statements = {
            "balance_sheet": (
                f"/v3/balance-sheet-statement/{symbol}?apikey={api_key_param}"
            ),
            "income_statement": (
                f"/v3/income-statement/{symbol}?apikey={api_key_param}"
            ),
            "cash_flow": f"/v3/cash-flow-statement/{symbol}?apikey={api_key_param}",
        }

        financial_data: Dict[str, Any] = {}

        for key, path in statements.items():
            statement_data = self._fetch_json(path)
            financial_data[key] = statement_data

        if not financial_data or not all(financial_data.values()):
            raise Exception("Incomplete financial data returned from API")

        self._write_financial_csv(financial_data, Path("finV2.csv"))

        return financial_data

    @staticmethod
    def _format_head(data: Any) -> Any:
        if isinstance(data, list):
            return data[:1]
        if isinstance(data, dict):
            items = list(data.items())[:10]
            return {key: value for key, value in items}
        return data

    @staticmethod
    def _write_financial_csv(financial_data: Dict[str, Any], path: Path) -> None:
        rows: list[dict[str, Any]] = []
        for statement_type, data in financial_data.items():
            if isinstance(data, list):
                for entry in data:
                    if isinstance(entry, dict):
                        row = dict(entry)
                        row["statement_type"] = statement_type
                        rows.append(row)
            elif isinstance(data, dict):
                row = dict(data)
                row["statement_type"] = statement_type
                rows.append(row)

        if not rows:
            return

        fieldnames: list[str] = []
        seen = set()
        for row in rows:
            for key in row.keys():
                if key in seen:
                    continue
                seen.add(key)
                fieldnames.append(key)

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    # ------------------------------------------------------------------
    # AI analysis
    # ------------------------------------------------------------------
    def analyze_with_model(
        self, symbol: str, financial_data: Dict[str, Any]
    ) -> Dict[str, Any]:

        schema = {
            "name": "great_company_put_selling_evaluation",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "ticker": {"type": "string"},
                    "great_company_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "classification": {
                        "type": "string",
                        "enum": ["Wheel-Ready", "Watchlist", "Avoid for Puts"],
                    },
                    "summary": {"type": "string"},
                    "cash_flow": {
                        "type": "object",
                        "properties": {
                            "fcf_status": {"type": "string", "enum": ["positive", "mixed", "negative"]},
                            "fcf_trend": {
                                "type": "string",
                                "enum": ["stable", "volatile_but_resilient", "deteriorating"],
                            },
                            "earnings_quality": {
                                "type": "string",
                                "enum": ["strong", "acceptable", "weak"],
                            },
                        },
                        "required": ["fcf_status", "fcf_trend", "earnings_quality"],
                    },
                    "balance_sheet": {
                        "type": "object",
                        "properties": {
                            "leverage_level": {"type": "string", "enum": ["low", "moderate", "elevated"]},
                            "liquidity": {"type": "string", "enum": ["strong", "adequate", "weak"]},
                            "dilution_risk": {"type": "string", "enum": ["low", "moderate", "high"]},
                        },
                        "required": ["leverage_level", "liquidity", "dilution_risk"],
                    },
                    "profitability": {
                        "type": "object",
                        "properties": {
                            "earnings_consistency": {
                                "type": "string",
                                "enum": ["stable", "cyclical_positive", "unstable"],
                            },
                            "margin_profile": {
                                "type": "string",
                                "enum": ["stable", "cyclical", "compressed"],
                            },
                        },
                        "required": ["earnings_consistency", "margin_profile"],
                    },
                    "risk_flags": {"type": "array", "items": {"type": "string"}},
                    "put_selling_guidance": {
                        "type": "object",
                        "properties": {
                            "assignment_tolerance": {
                                "type": "string",
                                "enum": ["high", "moderate", "low"],
                            },
                            "recommended_delta_range": {
                                "type": "array",
                                "items": {"type": "number"},
                                "minItems": 2,
                                "maxItems": 2,
                            },
                            "earnings_caution": {"type": "boolean"},
                        },
                        "required": [
                            "assignment_tolerance",
                            "recommended_delta_range",
                            "earnings_caution",
                        ],
                    },
                },
                "required": [
                    "ticker",
                    "great_company_score",
                    "classification",
                    "summary",
                    "cash_flow",
                    "balance_sheet",
                    "profitability",
                    "risk_flags",
                    "put_selling_guidance",
                ],
            },
        }

        prompt = f"""
You are a financial analysis agent evaluating whether a company is suitable for
long term investment.

This is NOT a buy/sell stock rating task.

Evaluation priority (strict):
1. Cash Flow Statement
2. Balance Sheet
3. Income Statement

Key principles:
- Cash generation > earnings
- Cyclicality acceptable if cash flow recovers
- Moderate leverage acceptable
- Focus on survivability and long term holding the stock

Scoring weights:
- Cash Flow 40%
- Balance Sheet 35%
- Profitability 25%

Return ONLY valid JSON matching the schema.
No forecasts. No price targets. No hype.

Financial Data:
{json.dumps(financial_data, indent=2)}
"""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_schema", "json_schema": schema},
            temperature=0.1,
            max_tokens=1200,
        )

        report = json.loads(response.choices[0].message.content)
        report["ticker"] = symbol.upper()
        return report


# ======================================================================
# Django command
# ======================================================================
class Command(BaseCommand):
    help = "Evaluate a stock for put-selling (wheel strategy) suitability (V2)"

    def add_arguments(self, parser):
        parser.add_argument("symbols", nargs="+", type=str)
        parser.add_argument("--save", action="store_true")

    def handle(self, *args, **options):
        symbols = [s.upper() for s in options["symbols"]]
        save_report = options["save"]

        agent = FinancialDDAgentV2()
        failures = []

        for symbol in symbols:
            self.stdout.write(self.style.SUCCESS("=" * 80))
            self.stdout.write(self.style.SUCCESS(f" Evaluating {symbol} for put-selling "))
            self.stdout.write(self.style.SUCCESS("=" * 80))

            try:
                financial_data = agent.fetch_financial_data(symbol)
                report = agent.analyze_with_model(symbol, financial_data)

                classification = report["classification"]
                score = report["great_company_score"]

                rating_map = {
                    "Wheel-Ready": "BUY",
                    "Watchlist": "HOLD",
                    "Avoid for Puts": "SELL",
                }

                rating = rating_map.get(classification, "HOLD")
                confidence = round(score / 100, 2)

                self.stdout.write("")
                self.stdout.write(self.style.SUCCESS("Result"))
                self.stdout.write(f"Classification : {classification}")
                self.stdout.write(f"Score          : {score}")
                self.stdout.write(f"Rating (DB)    : {rating}")
                self.stdout.write("")

                if save_report:
                    from api.models import DueDiligenceReport

                    DueDiligenceReport.objects.create(
                        symbol=symbol,
                        rating=rating,
                        confidence=confidence,
                        model_name=agent.model,
                        report=report,
                    )

                    self.stdout.write(self.style.SUCCESS("Report saved to database"))

            except Exception as e:
                failures.append(f"{symbol}: {e}")

            self.stdout.write(self.style.SUCCESS("=" * 80))

        if failures:
            raise CommandError("One or more symbols failed:\n" + "\n".join(failures))
