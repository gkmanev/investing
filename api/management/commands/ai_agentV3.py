"""
Django management command for AI-powered Put-Selling Due Diligence (V3).

Refactored version with:
- Enhanced schema and analysis framework
- Better error handling and logging
- Separation of concerns
- Type hints and documentation
- Configurable retry logic
- CSV export capability

Usage:
    python manage.py ai_agentV3 GLW
    python manage.py ai_agentV3 AAPL MSFT --save
    python manage.py ai_agentV3 NVDA --export results.json
"""

import csv
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from openai import OpenAI
from api.helper import FinancialMetricsCalculator


logger = logging.getLogger(__name__)


# ======================================================================
# Configuration
# ======================================================================
@dataclass
class AgentConfig:
    """Configuration for the financial analysis agent."""

    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    fmp_api_key: str = ""
    timeout: int = 30
    max_retries: int = 3
    temperature: float = 0.1
    max_tokens: int = 2000

    @classmethod
    def from_settings(cls) -> "AgentConfig":
        """Create configuration from Django settings."""
        return cls(
            openai_api_key=getattr(settings, "OPENAI_API_KEY", ""),
            openai_model=getattr(settings, "OPENAI_MODEL", "gpt-4o-mini"),
            fmp_api_key=getattr(settings, "FINANCIAL_MODELING_API_KEY", ""),
        )


# ======================================================================
# Schema and Prompt
# ======================================================================
# ENHANCED_SCHEMA = {
#     "name": "long_term_investment_screening",
#     "schema": {
#         "type": "object",
#         "additionalProperties": False,
#         "properties": {
#             "ticker": {"type": "string"},
#             "analysis_period": {"type": "string"},

#             # New canonical score (keep great_company_score too during migration)
#             "long_term_quality_score": {"type": "integer", "minimum": 0, "maximum": 100},
#             "great_company_score": {"type": "integer", "minimum": 0, "maximum": 100},

#             "classification": {
#                 "type": "string",
#                 "enum": ["High-quality compounder", "Quality (selective)", "Higher risk"],
#             },

#             "summary": {"type": "string"},

#             "cash_flow": {
#                 "type": "object",
#                 "properties": {
#                     "fcf_status": {"type": "string", "enum": ["positive", "mixed", "negative"]},
#                     "fcf_trend": {"type": "string", "enum": ["stable", "volatile_but_resilient", "deteriorating"]},
#                     "fcf_5yr_avg": {"type": "number"},
#                     "fcf_volatility": {"type": "string", "enum": ["low", "moderate", "high"]},
#                     "cash_conversion": {"type": "string", "enum": ["excellent", "good", "poor"]},
#                     "capex_intensity": {"type": "number"},
#                     "working_capital_trend": {"type": "string", "enum": ["stable", "improving", "deteriorating"]},
#                 },
#                 "required": [
#                     "fcf_status",
#                     "fcf_trend",
#                     "fcf_5yr_avg",
#                     "fcf_volatility",
#                     "cash_conversion",
#                     "capex_intensity",
#                     "working_capital_trend",
#                 ],
#             },

#             # NEW: FCF margin block
#             "fcf_margin": {
#                 "type": "object",
#                 "properties": {
#                     "fcf_margin_avg": {"type": "number"},
#                     "fcf_margin_trend": {"type": "string", "enum": ["improving", "stable", "declining"]},
#                 },
#                 "required": ["fcf_margin_avg", "fcf_margin_trend"],
#             },

#             "balance_sheet": {
#                 "type": "object",
#                 "properties": {
#                     "net_debt": {"type": "number"},
#                     "debt_to_ebitda": {"type": "number"},
#                     "leverage_level": {"type": "string", "enum": ["low", "moderate", "elevated"]},
#                     "current_ratio": {"type": "number"},
#                     "liquidity": {"type": "string", "enum": ["strong", "adequate", "weak"]},
#                     "share_dilution": {"type": "number"},
#                     "dilution_risk": {"type": "string", "enum": ["low", "moderate", "high"]},
#                     "debt_trend": {"type": "string", "enum": ["decreasing", "stable", "increasing"]},
#                 },
#                 "required": [
#                     "net_debt",
#                     "debt_to_ebitda",
#                     "leverage_level",
#                     "current_ratio",
#                     "liquidity",
#                     "share_dilution",
#                     "dilution_risk",
#                     "debt_trend",
#                 ],
#             },

#             # NEW: interest coverage block (can be unknown / null values)
#             "interest_coverage": {
#                 "type": "object",
#                 "properties": {
#                     "interest_coverage_latest": {"type": ["number", "null"]},
#                     "interest_coverage_avg": {"type": ["number", "null"]},
#                     "interest_coverage_level": {"type": "string", "enum": ["strong", "adequate", "weak", "unknown"]},
#                 },
#                 "required": ["interest_coverage_latest", "interest_coverage_avg", "interest_coverage_level"],
#             },

#             "profitability": {
#                 "type": "object",
#                 "properties": {
#                     "gross_margin_avg": {"type": "number"},
#                     "gross_margin_trend": {"type": "string", "enum": ["improving", "stable", "declining"]},
#                     "operating_margin_avg": {"type": "number"},
#                     "operating_margin_trend": {"type": "string", "enum": ["improving", "stable", "declining"]},
#                     "net_margin_avg": {"type": "number"},
#                     "earnings_consistency": {"type": "string", "enum": ["stable", "cyclical_positive", "unstable"]},
#                     "revenue_growth_cagr": {"type": "number"},
#                 },
#                 "required": [
#                     "gross_margin_avg",
#                     "gross_margin_trend",
#                     "operating_margin_avg",
#                     "operating_margin_trend",
#                     "net_margin_avg",
#                     "earnings_consistency",
#                     "revenue_growth_cagr",
#                 ],
#             },

#             # NEW: capital efficiency (ROIC)
#             "capital_efficiency": {
#                 "type": "object",
#                 "properties": {
#                     "roic_avg": {"type": "number"},
#                     "roic_trend": {"type": "string", "enum": ["improving", "stable", "declining"]},
#                     "roic_level": {"type": "string", "enum": ["excellent", "good", "fair", "weak"]},
#                 },
#                 "required": ["roic_avg", "roic_trend", "roic_level"],
#             },

#             # NEW: per-share growth block
#             "per_share": {
#                 "type": "object",
#                 "properties": {
#                     "rev_per_share_cagr": {"type": "number"},
#                     "fcf_per_share_cagr": {"type": "number"},
#                     "net_income_per_share_cagr": {"type": "number"},
#                 },
#                 "required": ["rev_per_share_cagr", "fcf_per_share_cagr", "net_income_per_share_cagr"],
#             },

#             "key_metrics": {
#                 "type": "object",
#                 "properties": {
#                     "latest_revenue": {"type": "number"},
#                     "latest_net_income": {"type": "number"},
#                     "latest_fcf": {"type": "number"},
#                     "latest_total_debt": {"type": "number"},
#                     "latest_cash": {"type": "number"},
#                     "inventory_trend": {"type": "string", "enum": ["healthy", "building", "excessive"]},
#                 },
#                 "required": [
#                     "latest_revenue",
#                     "latest_net_income",
#                     "latest_fcf",
#                     "latest_total_debt",
#                     "latest_cash",
#                     "inventory_trend",
#                 ],
#             },

#             "risk_flags": {"type": "array", "items": {"type": "string"}},
#             "positive_signals": {"type": "array", "items": {"type": "string"}},

#             # Keep for backward compatibility if some caller expects it,
#             # but DO NOT require it.
#             "put_selling_guidance": {"type": "object"},
#         },
#         "required": [
#             "ticker",
#             "analysis_period",
#             "long_term_quality_score",
#             "great_company_score",
#             "classification",
#             "summary",
#             "cash_flow",
#             "fcf_margin",
#             "balance_sheet",
#             "interest_coverage",
#             "profitability",
#             "capital_efficiency",
#             "per_share",
#             "key_metrics",
#             "risk_flags",
#             "positive_signals",
#         ],
#     },
# }




# # ======================================================================
# # CSV Handler
# # ======================================================================
# class FinancialDataCSV:
#     """Handler for reading/writing financial data CSV files."""

#     @staticmethod
#     def write_csv(financial_data: Dict[str, Any], filepath: Path) -> None:
#         """Write financial data to CSV file."""
#         rows: List[Dict[str, Any]] = []

#         for statement_type, data in financial_data.items():
#             if isinstance(data, list):
#                 for entry in data:
#                     if isinstance(entry, dict):
#                         row = dict(entry)
#                         row["statement_type"] = statement_type
#                         rows.append(row)
#             elif isinstance(data, dict):
#                 row = dict(data)
#                 row["statement_type"] = statement_type
#                 rows.append(row)

#         if not rows:
#             logger.warning("No data to write to CSV")
#             return

#         # Collect all unique fieldnames
#         fieldnames: List[str] = []
#         seen = set()
#         for row in rows:
#             for key in row.keys():
#                 if key not in seen:
#                     seen.add(key)
#                     fieldnames.append(key)

#         # Write CSV
#         with filepath.open("w", newline="", encoding="utf-8") as f:
#             writer = csv.DictWriter(f, fieldnames=fieldnames)
#             writer.writeheader()
#             writer.writerows(rows)

#         logger.info(f"Wrote {len(rows)} rows to {filepath}")

#     @staticmethod
#     def read_csv(filepath: Path) -> Dict[str, List[Dict[str, Any]]]:
#         """Read financial data from CSV file."""
#         if not filepath.exists():
#             raise FileNotFoundError(f"CSV file not found: {filepath}")

#         financial_data: Dict[str, List[Dict[str, Any]]] = {
#             "balance_sheet": [],
#             "income_statement": [],
#             "cash_flow": [],
#         }

#         with filepath.open("r", encoding="utf-8") as f:
#             reader = csv.DictReader(f)
#             for row in reader:
#                 statement_type = row.get("statement_type")
#                 if statement_type in financial_data:
#                     financial_data[statement_type].append(row)

#         return financial_data


# ======================================================================
# Analysis Agent
# ======================================================================

class SimplifiedAnalysisAgent:
    """
    Simplified agent that uses pre-processed metrics.
    
    Instead of sending raw financial statements to the LLM and asking it to
    calculate everything, we send already-calculated metrics and ask it only
    for strategic analysis and recommendations.
    """
    
    def __init__(self, openai_client, openai_model: str):
        self.client = openai_client
        self.model = openai_model
        self.fmp_api_key = getattr(settings, "FINANCIAL_MODELING_API_KEY", "")
        if not self.fmp_api_key:
            raise ValueError("FINANCIAL_MODELING_API_KEY is missing in Django settings")
        self.fmp_base_url = "https://financialmodelingprep.com/stable"

    def _fetch_json(self, url: str) -> Any:
        max_attempts = 4
        wait = 15  # seconds for first 429 retry

        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(url, timeout=30) as response:
                    body = response.read()
                return json.loads(body.decode("utf-8")) if body else None

            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = e.headers.get("Retry-After")
                    sleep_secs = int(retry_after) if retry_after else wait
                    logger.warning(
                        f"429 rate-limited, waiting {sleep_secs}s "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                    if attempt == max_attempts:
                        raise Exception(
                            f"FMP request failed (429): Too Many Requests "
                            f"after {max_attempts} attempts"
                        )
                    time.sleep(sleep_secs)
                    wait *= 2  # exponential back-off
                else:
                    raise Exception(f"FMP request failed ({e.code}): {e.reason}")

    def fetch_financial_data(self, symbol: str) -> Dict[str, Any]:
        symbol = symbol.upper()
        base = self.fmp_base_url
        key = self.fmp_api_key
        statements = {
            "balance_sheet": (
                f"{base}/balance-sheet-statement?symbol={symbol}&apikey={key}"
            ),
            "income_statement": (
                f"{base}/income-statement?symbol={symbol}&apikey={key}"
            ),
            "cash_flow": f"{base}/cash-flow-statement?symbol={symbol}&apikey={key}",
        }

        financial_data: Dict[str, Any] = {}
        missing_statements: List[str] = []

        for key, path in statements.items():
            statement_data = self._fetch_json(path)
            financial_data[key] = statement_data
            if not statement_data:
                missing_statements.append(key)
            time.sleep(1.5)  # avoid flooding RapidAPI

        if not financial_data or missing_statements:
            missing = ", ".join(missing_statements) if missing_statements else "all statements"
            raise Exception(
                f"Incomplete financial data returned from API for {symbol}; "
                f"missing: {missing}"
            )

        return financial_data
    
    
    def analyze_with_processed_data(self, processed_metrics: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze company using pre-processed metrics.
        
        This is much faster and more reliable than having the LLM calculate metrics.
        The LLM focuses on what it's good at: synthesis and strategic thinking.
        """
        
        # Create a concise prompt with pre-calculated metrics
        prompt = f"""
You are a senior equity analyst providing strategic insights ONLY.
IMPORTANT: All calculations have been done.
⚠️ CRITICAL RULES:
1. DO NOT recalculate any metrics - all calculations are final
2. DO NOT question the numbers - focus on interpretation
3. DO NOT provide generic advice - be specific to this company
4. Your response must be 150-200 words maximum

═══════════════════════════════════════════════════════════════
PRE-CALCULATED ANALYSIS
═══════════════════════════════════════════════════════════════

Ticker: {processed_metrics['ticker']}
Period: {processed_metrics['analysis_period']}
Score: {processed_metrics['great_company_score']}/100
Classification: {processed_metrics['classification']}

CASH FLOW METRICS:
- FCF Status: {processed_metrics['cash_flow']['fcf_status']}
- FCF Trend: {processed_metrics['cash_flow']['fcf_trend']}
- 5-Year Avg FCF: ${processed_metrics['cash_flow']['fcf_5yr_avg']:,.0f}
- FCF Volatility: {processed_metrics['cash_flow']['fcf_volatility']}
- Cash Conversion: {processed_metrics['cash_flow']['cash_conversion']}
- CapEx Intensity: {processed_metrics['cash_flow']['capex_intensity']:.1%}
- Working Capital: {processed_metrics['cash_flow']['working_capital_trend']}

BALANCE SHEET METRICS:
- Net Debt: ${processed_metrics['balance_sheet']['net_debt']:,.0f}
- Debt/EBITDA: {processed_metrics['balance_sheet']['debt_to_ebitda']:.2f}x
- Leverage Level: {processed_metrics['balance_sheet']['leverage_level']}
- Current Ratio: {processed_metrics['balance_sheet']['current_ratio']:.2f}
- Liquidity: {processed_metrics['balance_sheet']['liquidity']}
- Share Dilution: {processed_metrics['balance_sheet']['share_dilution']:.1%}
- Dilution Risk: {processed_metrics['balance_sheet']['dilution_risk']}
- Debt Trend: {processed_metrics['balance_sheet']['debt_trend']}

PROFITABILITY METRICS:
- Gross Margin (avg): {processed_metrics['profitability']['gross_margin_avg']:.1%}
- Gross Margin Trend: {processed_metrics['profitability']['gross_margin_trend']}
- Operating Margin (avg): {processed_metrics['profitability']['operating_margin_avg']:.1%}
- Operating Margin Trend: {processed_metrics['profitability']['operating_margin_trend']}
- Net Margin (avg): {processed_metrics['profitability']['net_margin_avg']:.1%}
- Earnings Consistency: {processed_metrics['profitability']['earnings_consistency']}
- Revenue CAGR: {processed_metrics['profitability']['revenue_growth_cagr']:.1%}

KEY METRICS:
- Latest Revenue: ${processed_metrics['key_metrics']['latest_revenue']:,.0f}
- Latest Net Income: ${processed_metrics['key_metrics']['latest_net_income']:,.0f}
- Latest FCF: ${processed_metrics['key_metrics']['latest_fcf']:,.0f}
- Latest Debt: ${processed_metrics['key_metrics']['latest_total_debt']:,.0f}
- Latest Cash: ${processed_metrics['key_metrics']['latest_cash']:,.0f}
- Inventory Trend: {processed_metrics['key_metrics']['inventory_trend']}

IDENTIFIED RISKS:
{chr(10).join('- ' + flag for flag in processed_metrics['risk_flags'])}

POSITIVE SIGNALS:
{chr(10).join('- ' + signal for signal in processed_metrics['positive_signals'])}


═══════════════════════════════════════════════════════════════
YOUR ANALYSIS TASKS
═══════════════════════════════════════════════════════════════

1. **Sanity Check**: Does the {processed_metrics['classification']} classification seem appropriate given the metrics? (1 sentence)

2. **Industry position**: Describe the industry and relative position of the {processed_metrics['ticker']}  to its peers (2-3 sentences)

2. **Competitive Position**: Evaluate the {processed_metrics['ticker']} moat strength, and competitive dynamics. (2-3 sentences)

3. **Hidden Risks**: Identify 2 material risks NOT visible in the financials (regulatory, technological disruption, customer concentration, geopolitical, etc.)

4. **Final Verdict**: Assign a 0-100 confidence score for long-term ownership and justify in one sentence.


Response format:
## Sanity Check
[your assessment]

## Competitive Position
[your assessment]

## Hidden Risks
• [risk 1]
• [risk 2]

## Verdict
Score: [0-100] - [one sentence justification]

"""
        
        try:
            # Call the LLM with the processed metrics
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=500,             
            )
            
            llm_analysis = response.choices[0].message.content
            
            # Validate response has required sections
            required_sections = ["## Sanity Check", "## Competitive Position", "## Hidden Risks", "## Verdict"]
            if not all(section in llm_analysis for section in required_sections):
                logger.warning(f"LLM response missing required sections for {processed_metrics['ticker']}")
                llm_analysis = "Analysis incomplete - using quantitative metrics only."
        
        except Exception as e:
            logger.error(f"LLM analysis failed: {e}")
            llm_analysis = f"LLM analysis unavailable - using quantitative score of {processed_metrics['great_company_score']}/100"
        
        full_report = processed_metrics.copy()
        full_report['llm_strategic_analysis'] = llm_analysis
        
        return full_report

# ======================================================================
# Django Command
# ======================================================================
class Command(BaseCommand):
    help = "Evaluate stocks for put-selling (wheel strategy) suitability - Enhanced V3"

    def add_arguments(self, parser):
        parser.add_argument(
            "symbols",
            nargs="*",
            type=str,
            help="Stock ticker symbols to analyze (omit when using --all)",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_symbols",
            help="Process all Symbol objects from the database in batches of 20",
        )
        parser.add_argument(
            "--save",
            action="store_true",
            help="Save reports to database",
        )
        parser.add_argument(
            "--export",
            type=str,
            metavar="FILEPATH",
            help="Export results to JSON file",
        )
        parser.add_argument(
            "--from-csv",
            type=str,
            metavar="FILEPATH",
            help="Read financial data from CSV instead of API",
        )
        parser.add_argument(
            "--csv-dir",
            type=str,
            default="financial_csv",
            help="Directory to write per-ticker financial CSV files",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable verbose logging",
        )

    def handle(self, *args, **options):
        # Setup logging
        if options["verbose"]:
            logging.basicConfig(level=logging.INFO)

        if options["all_symbols"]:
            from api.models import Symbol as SymbolModel
            symbols = list(
                SymbolModel.objects.filter(score__isnull=True).values_list("ticker", flat=True)
            )
            self.stdout.write(f"Loaded {len(symbols)} unprocessed symbols from database")
        else:
            symbols = [s.upper() for s in options["symbols"]]
            if symbols and symbols[0] == "SYMBOLS":
                self.stdout.write(
                    self.style.WARNING(
                        "Ignoring literal placeholder 'symbols'. "
                        "Usage is: python manage.py ai_agentV3 YOU"
                    )
                )
                symbols = symbols[1:]
            if not symbols:
                raise CommandError("Provide at least one ticker symbol, or use --all")

        save_to_db = options["save"]
        export_path = options.get("export")
        csv_path = options.get("from_csv")
        csv_dir = Path(options.get("csv_dir") or "financial_csv")

        csv_dir.mkdir(parents=True, exist_ok=True)

        # Initialize agent
        try:
            config = AgentConfig.from_settings()
            # openai_client = OpenAI(api_key=config.openai_api_key)  # LLM disabled
            # agent = SimplifiedAnalysisAgent(openai_client, config.openai_model)  # LLM disabled
            agent = SimplifiedAnalysisAgent(None, config.openai_model)
        except Exception as e:
            raise CommandError(f"Failed to initialize agent: {e}")

        reports = []
        failures = []

        BATCH_SIZE = 20

        # Process symbols in batches
        for batch_start in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[batch_start : batch_start + BATCH_SIZE]
            self.stdout.write(
                f"\nBatch {batch_start // BATCH_SIZE + 1} "
                f"({batch_start + 1}-{batch_start + len(batch)} of {len(symbols)}): "
                f"{', '.join(batch)}"
            )

            for symbol in batch:
                # Skip if already scored
                from api.models import Symbol as SymbolModel
                existing = SymbolModel.objects.filter(ticker=symbol, score__isnull=False).first()
                if existing:
                    self.stdout.write(
                        self.style.WARNING(
                            f"⏭  {symbol} already scored ({existing.score}), skipping\n"
                        )
                    )
                    continue

                self.stdout.write(
                    self.style.SUCCESS(f"\n{'=' * 80}\n Analyzing {symbol}\n{'=' * 80}")
                )

                try:
                    raw_data = agent.fetch_financial_data(symbol)
                    #FinancialDataCSV.write_csv(raw_data, csv_dir / f"{symbol}.csv")

                    calculator = FinancialMetricsCalculator(raw_data)
                    financial_data = calculator.process()

                    # Run analysis
                    # report = agent.analyze_with_processed_data(financial_data)  # LLM disabled
                    report = financial_data
                    reports.append(report)

                    self._display_analysis_report(report)

                    # Write score back to Symbol row (always, if ticker exists)
                    from api.models import Symbol as SymbolModel
                    updated = SymbolModel.objects.filter(ticker=symbol).update(
                        score=report["great_company_score"],
                        classification=report["classification"],
                    )
                    if updated:
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"✓ Score {report['great_company_score']} saved to Symbol\n"
                            )
                        )

                    # Save to database if requested
                    if save_to_db:
                        self._save_to_database(report, config.openai_model)
                        self.stdout.write(
                            self.style.SUCCESS("✓ Report saved to database\n")
                        )

                except Exception as e:
                    error_msg = f"{symbol}: {str(e)}"
                    failures.append(error_msg)
                    self.stdout.write(self.style.ERROR(f"✗ Analysis failed: {e}\n"))
                    logger.exception(f"Failed to analyze {symbol}")

                time.sleep(1.5)  # delay between symbols

            # Pause between batches (except after the last one)
            if batch_start + BATCH_SIZE < len(symbols):
                self.stdout.write("Sleeping 10s before next batch...")
                time.sleep(10)

        # Summary
        self.stdout.write(f"\n{'=' * 80}")
        self.stdout.write(f"SUMMARY: {len(reports)} successful, {len(failures)} failed")
        self.stdout.write(f"{'=' * 80}\n")

        if failures:
            raise CommandError(
                "Some symbols failed:\n" + "\n".join(f"  • {f}" for f in failures)
            )

    def _save_to_database(self, report: Dict[str, Any], model_name: str) -> None:
        """Save report to database."""
        from api.models import DueDiligenceReport

        # Map classification to rating
        rating_map = {
            "High-quality compounder": "BUY",
            "Quality (selective)": "HOLD",
            "Higher risk": "SELL",
        }

        rating = rating_map.get(report["classification"], "HOLD")
        confidence = Decimal(report.get("long_term_quality_score", report["great_company_score"])) / Decimal(100)


        DueDiligenceReport.objects.create(
            symbol=report["ticker"],
            rating=rating,
            confidence=confidence,
            model_name=model_name,
            report=report,
        )

    def _display_analysis_report(self, report: Dict[str, Any]) -> None:
        """Display concise analysis report."""
        
        # Compact Header
        self.stdout.write("\n" + "=" * 80)
        self.stdout.write(self.style.SUCCESS(
            f"\n{report['ticker']} | Score: {report['great_company_score']}/100 | "
            f"{report['classification']}"
        ))
        self.stdout.write("\n" + "=" * 80 + "\n")
        
        # Key Metrics in One Line
        km = report['key_metrics']
        self.stdout.write(
            f"Revenue: ${km['latest_revenue']/1e9:.2f}B | "
            f"FCF: ${km['latest_fcf']/1e9:.2f}B | "
            f"Debt/EBITDA: {report['balance_sheet']['debt_to_ebitda']:.2f}x | "
            f"ROIC: {report['capital_efficiency']['roic_avg']:.1%}\n"
        )
        
        # Risk Summary
        self.stdout.write(self.style.ERROR(
            f"\n⚠️  {len(report['risk_flags'])} Risk Flags"
        ))
        self.stdout.write(self.style.SUCCESS(
            f" | ✅ {len(report['positive_signals'])} Positive Signals\n"
        ))
        
        # LLM Analysis (main focus)
        if 'llm_strategic_analysis' in report:
            self.stdout.write("\n" + "-" * 80 + "\n")
            self.stdout.write(self.style.WARNING("AI STRATEGIC ANALYSIS\n"))
            self.stdout.write("-" * 80 + "\n")
            
            # Decode and format
            analysis = report['llm_strategic_analysis']
            analysis = analysis.replace('\\n', '\n')
            analysis = analysis.replace('\\u2022', '•')
            
            self.stdout.write(analysis + "\n")
        
        self.stdout.write("=" * 80 + "\n")

