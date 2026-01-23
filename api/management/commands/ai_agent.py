"""
Django management command for AI-powered Put-Selling Due Diligence

Usage:
    python manage.py analyze_stock GLW
    python manage.py analyze_stock AAPL --save
"""

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import requests
import json
from typing import Dict, Any
from openai import OpenAI


class FinancialDDAgent:
    """
    AI agent evaluating whether a company is suitable for
    selling cash-secured puts (wheel strategy).
    """

    def __init__(self, base_url: str = None):
        api_key = getattr(settings, "OPENAI_API_KEY", None)
        self.client = OpenAI(api_key=api_key) if api_key else OpenAI()

        self.base_url = base_url or getattr(
            settings, "FINANCIAL_API_BASE_URL", settings.LOCAL_API_BASE_URL
        )
        self.model = getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    def fetch_financial_data(self, symbol: str) -> Dict[str, Any]:
        endpoint = f"{self.base_url}/api/financial-statements/"
        statements = {
            "balance_sheet": "balance-sheet",
            "income_statement": "income-statement",
            "cash_flow": "cash-flow-statement",
        }

        financial_data: Dict[str, Any] = {}

        for key, statement_type in statements.items():
            params = {"symbol": symbol.upper(), "statement_type": statement_type}
            response = requests.get(endpoint, params=params, timeout=30)
            response.raise_for_status()
            financial_data[key] = response.json()

        if not financial_data or not all(financial_data.values()):
            raise Exception("Incomplete financial data returned from API")

        return financial_data

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
SELLING CASH-SECURED PUTS where ASSIGNMENT IS ACCEPTABLE and the position may
transition into COVERED CALLS (wheel strategy).

This is NOT a buy/sell stock rating task.

Evaluation priority (strict):
1. Cash Flow Statement
2. Balance Sheet
3. Income Statement

Key principles:
- Cash generation > earnings
- Cyclicality acceptable if cash flow recovers
- Moderate leverage acceptable
- Focus on survivability and assignment safety

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
    help = "Evaluate a stock for put-selling (wheel strategy) suitability"

    def add_arguments(self, parser):
        parser.add_argument("symbol", type=str)
        parser.add_argument("--save", action="store_true")
        parser.add_argument("--base-url", type=str)

    def handle(self, *args, **options):
        symbol = options["symbol"].upper()
        save_report = options["save"]
        base_url = options.get("base_url")

        self.stdout.write(self.style.SUCCESS("=" * 80))
        self.stdout.write(self.style.SUCCESS(f" Evaluating {symbol} for put-selling "))
        self.stdout.write(self.style.SUCCESS("=" * 80))

        try:
            agent = FinancialDDAgent(base_url=base_url)
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
                    financial_data=financial_data,
                )

                self.stdout.write(self.style.SUCCESS("✔ Report saved to database"))

        except Exception as e:
            raise CommandError(str(e))

        self.stdout.write(self.style.SUCCESS("=" * 80))
