"""
Django management command for AI-powered Financial Due Diligence Analysis

Usage:
    python manage.py analyze_stock BSX
    python manage.py analyze_stock AAPL --save
"""

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import requests
import json
from typing import Dict, Any, Tuple
from openai import OpenAI


class FinancialDDAgent:
    """
    AI-powered Financial Due Diligence Agent
    Fetches financial data and performs comprehensive analysis using OpenAI
    """
    
    def __init__(self, base_url: str = None):
        """
        Initialize the agent
        
        Args:
            base_url: Base URL for financial data API
        """
        api_key = getattr(settings, 'OPENAI_API_KEY', None)
        if api_key:
            self.client = OpenAI(api_key=api_key)
        else:
            self.client = OpenAI()  # Falls back to environment variable
        self.base_url = base_url or getattr(settings, 'FINANCIAL_API_BASE_URL', 'http://127.0.0.1:8080')
        self.model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o-mini')
    
    def fetch_financial_data(self, symbol: str) -> Dict[str, Any]:
        """
        Fetch all financial statements for a given symbol
        
        Args:
            symbol: Stock symbol (e.g., 'BSX', 'AAPL')
            
        Returns:
            Dictionary containing balance sheet, income statement, and cash flow data
        """
        endpoint = f"{self.base_url}/api/financial-statements/"
        statements = {
            'balance_sheet': 'balance-sheet',
            'income_statement': 'income-statement',
            'cash_flow': 'cash-flow-statement'
        }
        
        financial_data = {}
        
        for key, statement_type in statements.items():
            try:
                params = {
                    'symbol': symbol.upper(),
                    'statement_type': statement_type
                }
                response = requests.get(endpoint, params=params, timeout=30)
                response.raise_for_status()
                financial_data[key] = response.json()
            except requests.RequestException as e:
                raise Exception(f"Failed to fetch {statement_type}: {str(e)}")
        
        return financial_data
    
    def analyze_with_model(self, symbol: str, financial_data: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """
        Analyze financial data using AI model
        
        Args:
            symbol: Stock symbol
            financial_data: Complete financial statements data
            
        Returns:
            Tuple of (report, rating)
        """
        schema = {
            "name": "financial_due_diligence_report",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "symbol": {"type": "string"},
                    "rating": {
                        "type": "string",
                        "enum": ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "periods": {"type": "array", "items": {"type": "string"}},
                    "key_metrics": {"$ref": "#/$defs/section"},
                    "growth": {"$ref": "#/$defs/section"},
                    "financial_health": {"$ref": "#/$defs/section"},
                    "red_flags": {"$ref": "#/$defs/section"},
                    "growth_potential": {"$ref": "#/$defs/section"},
                    "final_justification": {"type": "string"},
                },
                "required": [
                    "symbol",
                    "rating",
                    "confidence",
                    "periods",
                    "key_metrics",
                    "growth",
                    "financial_health",
                    "red_flags",
                    "growth_potential",
                    "final_justification",
                ],
                "$defs": {
                    "calculation": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string"},
                            "formula": {"type": "string"},
                            "period": {"type": "string"},
                            "inputs": {"type": "object"},
                            "result": {"type": ["number", "string"]},
                            "units": {"type": "string"},
                            "source_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": [
                            "name",
                            "formula",
                            "period",
                            "inputs",
                            "result",
                            "units",
                            "source_paths",
                        ],
                    },
                    "section": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "summary": {"type": "string"},
                            "details": {"type": "array", "items": {"type": "string"}},
                            "calculations": {
                                "type": "array",
                                "items": {"$ref": "#/$defs/calculation"},
                            },
                            "justification": {"type": "string"},
                        },
                        "required": ["summary", "details", "calculations", "justification"],
                    },
                },
            },
        }

        prompt = (
            f"You are a financial analyst performing due diligence on {symbol}.\n"
            "Return ONLY valid JSON that matches the provided schema.\n"
            "Rules:\n"
            "- Every section must include a justification tied to specific statement line-items and periods.\n"
            "- Put numeric calculations in calculations[].\n"
            "- Use source_paths to reference where inputs came from in the provided financial_data JSON.\n"
            "- If a metric cannot be computed, include a detail explaining why and omit that calculation.\n\n"
            f"Financial Data:\n{json.dumps(financial_data, indent=2)}"
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_schema", "json_schema": schema},
                max_tokens=4000,
            )

            report_text = response.choices[0].message.content
            report = json.loads(report_text)
            rating = report.get("rating", "HOLD")

            return report, rating
            
        except Exception as e:
            raise Exception(f"Analysis failed: {str(e)}")
    
    def format_output(self, symbol: str, rating: str, report: Dict[str, Any]) -> str:
        """
        Format the analysis output for display
        
        Args:
            symbol: Stock symbol
            rating: Investment rating
            report: Structured report data
            
        Returns:
            Formatted string for output
        """
        rating_emojis = {
            'STRONG BUY': '🟢🟢',
            'BUY': '🟢',
            'HOLD': '🟡',
            'SELL': '🔴',
            'STRONG SELL': '🔴🔴'
        }
        
        emoji = rating_emojis.get(rating, '⚪')
        
        sections = [
            ("Key Metrics", "key_metrics"),
            ("Growth", "growth"),
            ("Financial Health", "financial_health"),
            ("Red Flags", "red_flags"),
            ("Growth Potential", "growth_potential"),
        ]
        section_blocks = []
        for title, key in sections:
            section = report.get(key, {})
            summary = section.get("summary", "No summary provided.")
            details = section.get("details", [])
            details_block = "\n".join(f"- {detail}" for detail in details)
            if details_block:
                section_body = f"{summary}\n{details_block}"
            else:
                section_body = summary
            section_blocks.append(f"{title}\n{section_body}")
        final_justification = report.get("final_justification", "")

        output = f"""
{'='*80}
                    FINANCIAL DUE DILIGENCE REPORT
{'='*80}

Company Symbol: {symbol}
Investment Rating: {emoji} {rating} {emoji}

{'='*80}

{chr(10).join(section_blocks)}

Final Justification
{final_justification}

"""
        return output
    
    def analyze(self, symbol: str) -> Dict[str, Any]:
        """
        Perform complete financial due diligence analysis
        
        Args:
            symbol: Stock symbol to analyze
            
        Returns:
            Dictionary containing analysis results
        """
        try:
            # Fetch financial data
            financial_data = self.fetch_financial_data(symbol)
            
            # Analyze with AI model
            report, rating = self.analyze_with_model(symbol.upper(), financial_data)
            
            # Format output
            formatted_output = self.format_output(symbol.upper(), rating, report)
            
            return {
                'symbol': symbol.upper(),
                'rating': rating,
                'report': report,
                'formatted_output': formatted_output,
                'success': True
            }
            
        except Exception as e:
            return {
                'symbol': symbol.upper(),
                'error': str(e),
                'success': False
            }


class Command(BaseCommand):
    help = 'Perform AI-powered financial due diligence analysis on a stock symbol'

    def add_arguments(self, parser):
        # Positional argument
        parser.add_argument(
            'symbol',
            type=str,
            help='Stock symbol to analyze (e.g., BSX, AAPL, TSLA)'
        )
        
        # Optional arguments
        parser.add_argument(
            '--save',
            action='store_true',
            help='Save the report to the database'
        )
        
        parser.add_argument(
            '--base-url',
            type=str,
            help='Base URL for financial API (overrides settings)'
        )

    def handle(self, *args, **options):
        symbol = options['symbol'].upper()
        save_report = options['save']
        base_url = options.get('base_url')
        
        # Display header
        self.stdout.write(self.style.SUCCESS('='*80))
        self.stdout.write(self.style.SUCCESS(f'  Starting Financial Due Diligence for {symbol}'))
        self.stdout.write(self.style.SUCCESS('='*80))
        self.stdout.write('')
        
        # Initialize agent
        try:
            agent = FinancialDDAgent(base_url=base_url)
        except Exception as e:
            raise CommandError(f'Failed to initialize agent: {str(e)}')
        
        # Fetch financial data
        self.stdout.write('📊 Fetching financial statements...')
        self.stdout.write('  ✓ Fetching balance-sheet')
        self.stdout.write('  ✓ Fetching income-statement')
        self.stdout.write('  ✓ Fetching cash-flow-statement')
        
        # Perform analysis
        self.stdout.write('')
        self.stdout.write('🤖 Analyzing with OpenAI...')
        
        result = agent.analyze(symbol)
        
        if not result['success']:
            raise CommandError(f"❌ Analysis failed: {result['error']}")
        
        # Display results
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('✅ Analysis complete!'))
        self.stdout.write('')
        self.stdout.write(result['formatted_output'])
        
        # Save report if requested
        if save_report:
            try:
                from api.models import DueDiligenceReport

                DueDiligenceReport.objects.create(
                    symbol=result["symbol"],
                    rating=result["rating"],
                    confidence=result["report"].get("confidence"),
                    model_name=agent.model,
                    report=result["report"],
                )
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'❌ Failed to save report: {str(e)}'))
        
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('='*80))
        
        # Return data for potential use in tests or other commands
        # return result
