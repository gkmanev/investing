"""
Django management command for AI-powered Financial Due Diligence Analysis

Usage:
    python manage.py analyze_stock BSX
    python manage.py analyze_stock AAPL --save
    python manage.py analyze_stock TSLA --save --output-dir reports/
"""

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import requests
import json
from typing import Dict, Any, Tuple
from pathlib import Path
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
        self.base_url = base_url or getattr(settings, 'FINANCIAL_API_BASE_URL', 'http://127.0.0.1:8000')
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
    
    def analyze_with_model(self, symbol: str, financial_data: Dict[str, Any]) -> Tuple[str, str]:
        """
        Analyze financial data using AI model
        
        Args:
            symbol: Stock symbol
            financial_data: Complete financial statements data
            
        Returns:
            Tuple of (analysis_text, rating)
        """
        prompt = f"""You are a financial analyst performing due diligence on {symbol}. Analyze the following financial statements and provide:

1. **Key Metrics Analysis**: Calculate and interpret important ratios:
   - Liquidity: Current Ratio, Quick Ratio
   - Profitability: Gross Margin, Operating Margin, Net Margin, ROE, ROA
   - Leverage: Debt-to-Equity, Interest Coverage
   - Efficiency: Asset Turnover, Inventory Turnover

2. **Growth Analysis**: Evaluate trends over the periods provided:
   - Revenue growth (YoY and multi-year CAGR)
   - Earnings growth
   - Cash flow trends
   - Consistency and sustainability of growth

3. **Financial Health Assessment**:
   - Liquidity position and working capital
   - Debt levels and capital structure
   - Cash generation and free cash flow
   - Operational efficiency

4. **Red Flags & Risks**: Identify concerning patterns:
   - Deteriorating margins
   - Rising debt levels
   - Negative cash flows
   - Revenue quality issues
   - Any accounting concerns

5. **Growth Potential**: Based on the financial data:
   - Cash available for expansion
   - Profitability trends suggesting competitive advantage
   - Operational leverage opportunities

6. **Final Rating**: Assign ONE of these ratings with justification:
   - STRONG BUY: Exceptional fundamentals and strong growth
   - BUY: Good fundamentals with growth potential
   - HOLD: Stable but limited upside
   - SELL: Weakening fundamentals
   - STRONG SELL: Serious financial concerns

Financial Data:
{json.dumps(financial_data, indent=2)}

Provide your analysis in clear sections with specific numbers and calculations. You must end with ### JUSTIFICTION ###."""

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "user", "content": prompt}
                ],
                max_tokens=4000
            )
            
            analysis_text = response.choices[0].message.content
            
            # Extract rating
            rating_keywords = ['STRONG BUY', 'STRONG SELL', 'BUY', 'SELL', 'HOLD']
            rating = 'HOLD'  # default
            for keyword in rating_keywords:
                if keyword in analysis_text.upper():
                    rating = keyword
                    break
            
            return analysis_text, rating
            
        except Exception as e:
            raise Exception(f"Analysis failed: {str(e)}")
    
    def format_output(self, symbol: str, rating: str, analysis: str) -> str:
        """
        Format the analysis output for display
        
        Args:
            symbol: Stock symbol
            rating: Investment rating
            analysis: Analysis text
            
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
        
        output = f"""
{'='*80}
                    FINANCIAL DUE DILIGENCE REPORT
{'='*80}

Company Symbol: {symbol}
Investment Rating: {emoji} {rating} {emoji}

{'='*80}

{analysis}

{'='*80}

⚠️  DISCLAIMER: This analysis is generated by AI for informational purposes only.
    It should not be considered financial advice. Always conduct your own research
    and consult with a qualified financial advisor before making investment decisions.

{'='*80}
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
            analysis_text, rating = self.analyze_with_model(symbol.upper(), financial_data)
            
            # Format output
            formatted_output = self.format_output(symbol.upper(), rating, analysis_text)
            
            return {
                'symbol': symbol.upper(),
                'rating': rating,
                'analysis': analysis_text,
                'financial_data': financial_data,
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
            help='Save the report to a file'
        )
        
        parser.add_argument(
            '--output-dir',
            type=str,
            default='financial_reports',
            help='Directory to save reports (default: financial_reports/)'
        )
        
        parser.add_argument(
            '--base-url',
            type=str,
            help='Base URL for financial API (overrides settings)'
        )

    def handle(self, *args, **options):
        symbol = options['symbol'].upper()
        save_report = options['save']
        output_dir = options['output_dir']
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
                # Create output directory if it doesn't exist
                output_path = Path(output_dir)
                output_path.mkdir(parents=True, exist_ok=True)
                
                # Generate filename
                filename = output_path / f"{symbol}_DD_Report.txt"
                
                # Write report
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(result['formatted_output'])
                
                self.stdout.write('')
                self.stdout.write(self.style.SUCCESS(f'✅ Report saved to {filename}'))
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'❌ Failed to save report: {str(e)}'))
        
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('='*80))
        
        # Return data for potential use in tests or other commands
        # return result