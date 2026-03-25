"""
Financial Data Processor for LONG-TERM INVESTING Screening

This script processes raw financial statement data and pre-calculates metrics
needed for a deterministic long-term quality screen (no LLM required).

REFACTORED VERSION - Fixes:
- FCF trend now checks directional improvement/deterioration
- Interest coverage properly handles zero interest expense cases
- Share dilution uses more appropriate thresholds
- Cash conversion considers recent performance
- Added recency weighting and momentum factors to scoring

Usage example (optional main below, commented):
    python process_financial_data.py input_file.txt output_file.json
"""

import json
import sys
from typing import Dict, List, Any, Optional
from statistics import mean, stdev
from pathlib import Path


class FinancialMetricsCalculator:
    """Calculate financial metrics from raw statement data."""

    def __init__(self, financial_data: Dict[str, List[Dict[str, Any]]]):
        """
        Initialize with financial data.

        Args:
            financial_data: Dict with keys 'balance_sheet', 'income_statement', 'cash_flow'
        """
        self.balance_sheets = sorted(
            financial_data.get("balance_sheet", []),
            key=lambda x: x["date"],
            reverse=True,
        )[:5]  # Most recent 5 years

        self.income_statements = sorted(
            financial_data.get("income_statement", []),
            key=lambda x: x["date"],
            reverse=True,
        )[:5]

        self.cash_flows = sorted(
            financial_data.get("cash_flow", []),
            key=lambda x: x["date"],
            reverse=True,
        )[:5]

        if not all([self.balance_sheets, self.income_statements, self.cash_flows]):
            raise ValueError("Missing required financial statement data")

        self.symbol = self.balance_sheets[0].get("symbol", "UNKNOWN")
        self.analysis_period = f"{self.balance_sheets[-1]['date']} to {self.balance_sheets[0]['date']}"

    def safe_divide(self, numerator: float, denominator: float, default: float = 0.0) -> float:
        """Safely divide two numbers, returning default if denominator is zero."""
        try:
            if denominator == 0 or denominator is None:
                return default
            return numerator / denominator
        except (TypeError, ZeroDivisionError):
            return default

    def clamp(self, x: float, lo: float, hi: float) -> float:
        try:
            return max(lo, min(hi, x))
        except Exception:
            return lo

    # ------------------------------------------------------------------
    # CASH FLOW
    # ------------------------------------------------------------------
    def calculate_cash_flow_metrics(self) -> Dict[str, Any]:
        """Calculate cash flow related metrics."""
        metrics: Dict[str, Any] = {}

        # Calculate FCF for each year
        fcf_values: List[Dict[str, Any]] = []
        for cf in self.cash_flows:
            operating_cf = cf.get("operatingCashFlow", 0) or 0
            capex = cf.get("capitalExpenditure", 0) or 0
            fcf = operating_cf + capex  # capex is typically negative
            fcf_values.append(
                {
                    "year": cf["date"][:4],
                    "fcf": fcf,
                    "operating_cf": operating_cf,
                    "capex": capex,
                }
            )

        # FCF Status
        positive_years = sum(1 for f in fcf_values if f["fcf"] > 0)
        if positive_years == 5:
            fcf_status = "positive"
        elif positive_years >= 3:
            fcf_status = "mixed"
        else:
            fcf_status = "negative"

        # FCF 5-year average
        fcf_avg = mean([f["fcf"] for f in fcf_values])

        # FCF Volatility
        if len(fcf_values) > 1:
            fcf_std = stdev([f["fcf"] for f in fcf_values])
            coefficient_of_variation = abs(self.safe_divide(fcf_std, fcf_avg))
            if coefficient_of_variation < 0.2:
                fcf_volatility = "low"
            elif coefficient_of_variation < 0.5:
                fcf_volatility = "moderate"
            else:
                fcf_volatility = "high"
        else:
            fcf_volatility = "low"

        # FCF Trend - REFACTORED to check directional movement
        fcf_trend = self._calculate_fcf_trend(fcf_values, positive_years, fcf_volatility)

        # Cash Conversion Quality (CFO / Net Income) - REFACTORED to weight recent years
        cash_conversion_ratios: List[float] = []
        for i, cf in enumerate(self.cash_flows):
            operating_cf = cf.get("operatingCashFlow", 0) or 0
            net_income = self.income_statements[i].get("netIncome", 1) or 1
            ratio = self.safe_divide(operating_cf, net_income, 0)
            cash_conversion_ratios.append(ratio)

        avg_cash_conversion = mean(cash_conversion_ratios) if cash_conversion_ratios else 0
        
        # Weight recent performance more heavily
        recent_conversion = mean(cash_conversion_ratios[:2]) if len(cash_conversion_ratios) >= 2 else avg_cash_conversion

        # Use recent if it shows improvement
        if recent_conversion >= 1.2 and avg_cash_conversion < 1.0:
            cash_conversion = "good"  # Improving
        elif avg_cash_conversion >= 1.0:
            cash_conversion = "excellent"
        elif avg_cash_conversion >= 0.7:
            cash_conversion = "good"
        else:
            cash_conversion = "poor"

        # CapEx Intensity (|CapEx| / revenue)
        capex_intensity_values: List[float] = []
        for i, cf in enumerate(self.cash_flows):
            capex = abs(cf.get("capitalExpenditure", 0) or 0)
            revenue = self.income_statements[i].get("revenue", 1) or 1
            intensity = self.safe_divide(capex, revenue)
            capex_intensity_values.append(intensity)

        avg_capex_intensity = mean(capex_intensity_values) if capex_intensity_values else 0

        # Working Capital Trend (simple heuristic)
        working_capital_changes = [cf.get("changeInWorkingCapital", 0) or 0 for cf in self.cash_flows]
        negative_count = sum(1 for wc in working_capital_changes if wc < 0)
        if negative_count >= 4:
            working_capital_trend = "stable"
        elif negative_count >= 2:
            working_capital_trend = "improving"
        else:
            working_capital_trend = "deteriorating"

        metrics["fcf_status"] = fcf_status
        metrics["fcf_trend"] = fcf_trend
        metrics["fcf_5yr_avg"] = fcf_avg
        metrics["fcf_volatility"] = fcf_volatility
        metrics["cash_conversion"] = cash_conversion
        metrics["capex_intensity"] = avg_capex_intensity
        metrics["working_capital_trend"] = working_capital_trend
        metrics["fcf_by_year"] = fcf_values
        metrics["cash_conversion_ratios"] = cash_conversion_ratios

        return metrics

    def _calculate_fcf_trend(self, fcf_values: List[Dict[str, Any]], positive_years: int, fcf_volatility: str) -> str:
        """
        Calculate FCF trend considering directional movement.
        
        REFACTORED: Now checks actual direction, not just positive year count.
        """
        fcfs = [f["fcf"] for f in fcf_values]
        
        # Check actual trend direction if we have enough data
        if len(fcfs) >= 3:
            recent_avg = mean(fcfs[:2])
            older_avg = mean(fcfs[2:])
            
            # Calculate change magnitude (handle negative averages)
            if older_avg != 0:
                change_ratio = recent_avg / older_avg if older_avg > 0 else -(recent_avg / abs(older_avg))
            else:
                change_ratio = 1.0
            
            # Significant improvement: recent avg much better than older avg
            if recent_avg > 0 and older_avg < 0:
                # Turned positive - definite improvement
                return "improving"
            elif recent_avg > older_avg * 2 and positive_years >= 2:
                # Doubled or more with recent positive years
                return "improving"
            elif recent_avg > 0 and older_avg > 0 and change_ratio > 1.5:
                # Strong positive growth
                return "improving"
            
            # Significant deterioration
            elif recent_avg < 0 and older_avg > 0:
                # Turned negative - definite deterioration
                return "deteriorating"
            elif recent_avg < older_avg * 0.5 and older_avg > 0:
                # Halved or worse
                return "deteriorating"
        
        # Fall back to original stability logic
        if positive_years >= 4 and fcf_volatility in ["low", "moderate"]:
            return "stable"
        elif positive_years >= 3:
            return "volatile_but_resilient"
        else:
            return "deteriorating"

    def calculate_fcf_margin_metrics(self) -> Dict[str, Any]:
        """FCF margin level + trend (FCF / revenue)."""
        fcf_margins: List[float] = []
        by_year: List[Dict[str, Any]] = []

        for i, cf in enumerate(self.cash_flows):
            rev = self.income_statements[i].get("revenue", 0) or 0
            fcf = (cf.get("operatingCashFlow", 0) or 0) + (cf.get("capitalExpenditure", 0) or 0)
            m = self.safe_divide(fcf, rev, 0.0)
            fcf_margins.append(m)
            by_year.append({"year": cf["date"][:4], "fcf_margin": m})

        avg = mean(fcf_margins) if fcf_margins else 0.0

        def trend(values: List[float]) -> str:
            if len(values) < 3:
                return "stable"
            recent_avg = mean(values[:2])
            older_avg = mean(values[2:])
            chg = self.safe_divide((recent_avg - older_avg), abs(older_avg) if older_avg != 0 else 1.0, 0.0)
            if chg > 0.10:
                return "improving"
            if chg < -0.10:
                return "declining"
            return "stable"

        return {
            "fcf_margin_avg": avg,
            "fcf_margin_trend": trend(fcf_margins),
            "fcf_margin_by_year": by_year,
        }

    # ------------------------------------------------------------------
    # BALANCE SHEET
    # ------------------------------------------------------------------
    def calculate_balance_sheet_metrics(self) -> Dict[str, Any]:
        """Calculate balance sheet related metrics."""
        metrics: Dict[str, Any] = {}
        latest_bs = self.balance_sheets[0]
        oldest_bs = self.balance_sheets[-1]

        # Net Debt
        total_debt = latest_bs.get("totalDebt", 0) or 0
        cash = latest_bs.get("cashAndCashEquivalents", 0) or 0
        net_debt = total_debt - cash

        # Debt to EBITDA
        latest_income = self.income_statements[0]
        ebitda = latest_income.get("ebitda", 0) or latest_income.get("operatingIncome", 0) or 1
        debt_to_ebitda = self.safe_divide(total_debt, ebitda)

        if debt_to_ebitda < 1.5:
            leverage_level = "low"
        elif debt_to_ebitda < 3.0:
            leverage_level = "moderate"
        else:
            leverage_level = "elevated"

        # Current Ratio
        current_assets = latest_bs.get("totalCurrentAssets", 0) or 0
        current_liabilities = latest_bs.get("totalCurrentLiabilities", 1) or 1
        current_ratio = self.safe_divide(current_assets, current_liabilities, 1.0)

        if current_ratio >= 2.0:
            liquidity = "strong"
        elif current_ratio >= 1.2:
            liquidity = "adequate"
        else:
            liquidity = "weak"

        # Share Dilution (using weightedAverageShsOutDil where possible)
        latest_shares = (
            latest_bs.get("weightedAverageShsOutDil")
            or latest_income.get("weightedAverageShsOutDil", 1)
            or 1
        )
        oldest_shares = (
            oldest_bs.get("weightedAverageShsOutDil")
            or self.income_statements[-1].get("weightedAverageShsOutDil", 1)
            or 1
        )

        share_dilution = self.safe_divide((latest_shares - oldest_shares), oldest_shares)
        
        # REFACTORED: More appropriate thresholds for growth companies
        # Using annualized rate
        years = len(self.balance_sheets) - 1 if len(self.balance_sheets) > 1 else 1
        annual_dilution = abs(share_dilution) / years if years > 0 else abs(share_dilution)
        
        if annual_dilution < 0.02:  # <2% per year
            dilution_risk = "low"
        elif annual_dilution < 0.04:  # <4% per year
            dilution_risk = "moderate"
        else:  # >=4% per year
            dilution_risk = "high"

        # Debt Trend
        oldest_debt = oldest_bs.get("totalDebt", 0) or 0
        debt_change = self.safe_divide((total_debt - oldest_debt), max(abs(oldest_debt), 1))

        if debt_change < -0.05:
            debt_trend = "decreasing"
        elif debt_change < 0.05:
            debt_trend = "stable"
        else:
            debt_trend = "increasing"

        metrics["net_debt"] = net_debt
        metrics["debt_to_ebitda"] = debt_to_ebitda
        metrics["leverage_level"] = leverage_level
        metrics["current_ratio"] = current_ratio
        metrics["liquidity"] = liquidity
        metrics["share_dilution"] = share_dilution
        metrics["dilution_risk"] = dilution_risk
        metrics["debt_trend"] = debt_trend

        return metrics

    # ------------------------------------------------------------------
    # PROFITABILITY
    # ------------------------------------------------------------------
    def calculate_profitability_metrics(self) -> Dict[str, Any]:
        """Calculate profitability related metrics."""
        metrics: Dict[str, Any] = {}

        gross_margins: List[float] = []
        operating_margins: List[float] = []
        net_margins: List[float] = []
        revenues: List[float] = []

        for income in self.income_statements:
            revenue = income.get("revenue", 1) or 1
            revenues.append(revenue)

            gross_margin = income.get("grossProfitRatio")
            if gross_margin is None:
                gross_profit = income.get("grossProfit", 0) or 0
                gross_margin = self.safe_divide(gross_profit, revenue)
            gross_margins.append(gross_margin)

            operating_margin = income.get("operatingIncomeRatio")
            if operating_margin is None:
                operating_income = income.get("operatingIncome", 0) or 0
                operating_margin = self.safe_divide(operating_income, revenue)
            operating_margins.append(operating_margin)

            net_margin = income.get("netIncomeRatio")
            if net_margin is None:
                net_income = income.get("netIncome", 0) or 0
                net_margin = self.safe_divide(net_income, revenue)
            net_margins.append(net_margin)

        gross_margin_avg = mean(gross_margins) if gross_margins else 0
        operating_margin_avg = mean(operating_margins) if operating_margins else 0
        net_margin_avg = mean(net_margins) if net_margins else 0

        def trend_analysis(values: List[float]) -> str:
            if len(values) < 3:
                return "stable"
            recent_avg = mean(values[:2])
            older_avg = mean(values[2:])
            change = self.safe_divide(
                (recent_avg - older_avg),
                abs(older_avg) if older_avg != 0 else 1,
            )
            if change > 0.05:
                return "improving"
            elif change < -0.05:
                return "declining"
            else:
                return "stable"

        gross_margin_trend = trend_analysis(gross_margins)
        operating_margin_trend = trend_analysis(operating_margins)

        # Revenue Growth CAGR
        if len(revenues) >= 2:
            oldest_revenue = revenues[-1]
            latest_revenue = revenues[0]
            years = len(revenues) - 1
            if oldest_revenue > 0:
                cagr = pow(latest_revenue / oldest_revenue, 1 / years) - 1
            else:
                cagr = 0
        else:
            cagr = 0

        # Earnings Consistency
        positive_earnings = sum(
            1 for income in self.income_statements if (income.get("netIncome", 0) or 0) > 0
        )

        if positive_earnings == 5:
            earnings_consistency = "stable"
        elif positive_earnings >= 3:
            earnings_consistency = "cyclical_positive"
        else:
            earnings_consistency = "unstable"

        metrics["gross_margin_avg"] = gross_margin_avg
        metrics["gross_margin_trend"] = gross_margin_trend
        metrics["operating_margin_avg"] = operating_margin_avg
        metrics["operating_margin_trend"] = operating_margin_trend
        metrics["net_margin_avg"] = net_margin_avg
        metrics["earnings_consistency"] = earnings_consistency
        metrics["revenue_growth_cagr"] = cagr

        return metrics

    # ------------------------------------------------------------------
    # EXTRA LONG-TERM METRICS
    # ------------------------------------------------------------------
    def calculate_interest_coverage_metrics(self) -> Dict[str, Any]:
        """
        Interest coverage proxy: Operating Income / |Interest Expense|
        
        REFACTORED: Properly handles zero interest expense (debt-free companies).
        """
        coverages: List[float] = []
        by_year: List[Dict[str, Any]] = []
        has_interest_expense = False

        for inc in self.income_statements:
            op = inc.get("operatingIncome", None)
            ie = inc.get("interestExpense", None)
            year = (inc.get("date") or "")[:4]
            
            # Check if company has any meaningful interest expense
            if ie is not None and abs(ie) > 0:
                has_interest_expense = True
                ie_abs = abs(ie)
                cov = self.safe_divide(op, ie_abs, 0.0)
                coverages.append(cov)
                by_year.append({"year": year, "interest_coverage": cov})

        # REFACTORED: If no interest expense at all, this is excellent (debt-free or minimal debt)
        if not has_interest_expense:
            return {
                "interest_coverage_latest": None,
                "interest_coverage_avg": None,
                "interest_coverage_level": "excellent",
                "interest_coverage_by_year": by_year,
                "note": "No interest expense - minimal or no debt"
            }

        latest = coverages[0] if coverages else None
        avg = mean(coverages) if coverages else None

        def bucket(x: Optional[float]) -> str:
            if x is None:
                return "unknown"
            if x >= 8:
                return "strong"
            if x >= 3:
                return "adequate"
            return "weak"

        return {
            "interest_coverage_latest": latest,
            "interest_coverage_avg": avg,
            "interest_coverage_level": bucket(latest),
            "interest_coverage_by_year": by_year,
        }

    def calculate_capital_efficiency_metrics(self) -> Dict[str, Any]:
        """
        ROIC approximation (statement-only):
          NOPAT = operatingIncome * (1 - tax_rate)
          invested_capital ≈ totalDebt + totalStockholdersEquity - cashAndCashEquivalents
          ROIC = NOPAT / invested_capital

        tax_rate estimated from incomeTaxExpense / incomeBeforeTax (clamped),
        defaulting to 21% if missing.
        """
        roic_vals: List[float] = []
        by_year: List[Dict[str, Any]] = []

        n = min(len(self.income_statements), len(self.balance_sheets))
        for i in range(n):
            inc = self.income_statements[i]
            bs = self.balance_sheets[i]

            op_inc = inc.get("operatingIncome", 0) or 0
            pretax = inc.get("incomeBeforeTax", None)
            tax_exp = inc.get("incomeTaxExpense", None)

            if pretax is None or pretax == 0 or tax_exp is None:
                tax_rate = 0.21
            else:
                tax_rate = self.safe_divide(tax_exp, pretax, 0.21)
                tax_rate = self.clamp(tax_rate, 0.0, 0.35)

            nopat = op_inc * (1.0 - tax_rate)

            total_debt = bs.get("totalDebt", 0) or 0
            equity = (
                bs.get("totalStockholdersEquity", None)
                if bs.get("totalStockholdersEquity", None) is not None
                else bs.get("totalEquity", 0) or 0
            )
            cash = bs.get("cashAndCashEquivalents", 0) or 0
            invested_capital = (total_debt + equity) - cash

            roic = self.safe_divide(nopat, invested_capital, 0.0) if invested_capital > 0 else 0.0
            roic_vals.append(roic)
            by_year.append({"year": (inc.get("date") or "")[:4], "roic": roic})

        avg = mean(roic_vals) if roic_vals else 0.0

        def trend(values: List[float]) -> str:
            if len(values) < 3:
                return "stable"
            recent_avg = mean(values[:2])
            older_avg = mean(values[2:])
            chg = self.safe_divide((recent_avg - older_avg), abs(older_avg) if older_avg != 0 else 1.0, 0.0)
            if chg > 0.10:
                return "improving"
            if chg < -0.10:
                return "declining"
            return "stable"

        def bucket(x: float) -> str:
            if x >= 0.15:
                return "excellent"
            if x >= 0.10:
                return "good"
            if x >= 0.06:
                return "fair"
            return "weak"

        return {
            "roic_avg": avg,
            "roic_trend": trend(roic_vals),
            "roic_level": bucket(avg),
            "roic_by_year": by_year,
        }

    def calculate_per_share_growth_metrics(self) -> Dict[str, Any]:
        """Per-share growth (dilution-aware): revenue/share, FCF/share, NI/share CAGR."""
        by_year: List[Dict[str, Any]] = []
        rev_ps: List[float] = []
        fcf_ps: List[float] = []
        ni_ps: List[float] = []

        n = min(len(self.income_statements), len(self.balance_sheets), len(self.cash_flows))
        for i in range(n):
            inc = self.income_statements[i]
            bs = self.balance_sheets[i]
            cf = self.cash_flows[i]

            shares = (
                bs.get("weightedAverageShsOutDil")
                or inc.get("weightedAverageShsOutDil")
                or bs.get("commonStockSharesOutstanding")
                or 0
            )
            if not shares or float(shares) == 0:
                continue

            rev = inc.get("revenue", 0) or 0
            ni = inc.get("netIncome", 0) or 0
            fcf = (cf.get("operatingCashFlow", 0) or 0) + (cf.get("capitalExpenditure", 0) or 0)

            rps = self.safe_divide(rev, shares, 0.0)
            nps = self.safe_divide(ni, shares, 0.0)
            fps = self.safe_divide(fcf, shares, 0.0)

            rev_ps.append(rps)
            ni_ps.append(nps)
            fcf_ps.append(fps)
            by_year.append(
                {"year": (inc.get("date") or "")[:4], "rev_ps": rps, "ni_ps": nps, "fcf_ps": fps}
            )

        def cagr(values: List[float]) -> float:
            if len(values) < 2:
                return 0.0
            latest = values[0]
            oldest = values[-1]
            years = len(values) - 1
            if years <= 0:
                return 0.0
            if oldest > 0 and latest > 0:
                return pow(latest / oldest, 1 / years) - 1
            if oldest == 0:
                return 0.0
            return self.safe_divide((latest - oldest), abs(oldest), 0.0) / years

        return {
            "rev_per_share_cagr": cagr(rev_ps),
            "fcf_per_share_cagr": cagr(fcf_ps),
            "net_income_per_share_cagr": cagr(ni_ps),
            "per_share_by_year": by_year,
        }

    # ------------------------------------------------------------------
    # KEY METRICS
    # ------------------------------------------------------------------
    def calculate_key_metrics(self) -> Dict[str, Any]:
        """Extract latest key metrics."""
        metrics: Dict[str, Any] = {}

        latest_income = self.income_statements[0]
        latest_bs = self.balance_sheets[0]
        latest_cf = self.cash_flows[0]

        metrics["latest_revenue"] = latest_income.get("revenue", 0) or 0
        metrics["latest_net_income"] = latest_income.get("netIncome", 0) or 0
        metrics["latest_fcf"] = (latest_cf.get("operatingCashFlow", 0) or 0) + (latest_cf.get("capitalExpenditure", 0) or 0)
        metrics["latest_total_debt"] = latest_bs.get("totalDebt", 0) or 0
        metrics["latest_cash"] = latest_bs.get("cashAndCashEquivalents", 0) or 0

        # Inventory trend (basic)
        if len(self.balance_sheets) >= 2:
            latest_inventory = latest_bs.get("inventory", 0) or 0
            oldest_inventory = self.balance_sheets[-1].get("inventory", 1) or 1
            inventory_change = self.safe_divide((latest_inventory - oldest_inventory), oldest_inventory)

            latest_revenue = latest_income.get("revenue", 0) or 0
            oldest_revenue = self.income_statements[-1].get("revenue", 1) or 1
            revenue_change = self.safe_divide((latest_revenue - oldest_revenue), oldest_revenue)

            if inventory_change < revenue_change * 1.2:
                inventory_trend = "healthy"
            elif inventory_change < revenue_change * 1.5:
                inventory_trend = "building"
            else:
                inventory_trend = "excessive"
        else:
            inventory_trend = "healthy"

        metrics["inventory_trend"] = inventory_trend
        return metrics

    # ------------------------------------------------------------------
    # FLAGS / SIGNALS (LONG-TERM)
    # ------------------------------------------------------------------
    def identify_long_term_risk_flags(
        self,
        cash_flow_metrics: Dict[str, Any],
        fcf_margin_metrics: Dict[str, Any],
        balance_sheet_metrics: Dict[str, Any],
        profitability_metrics: Dict[str, Any],
        capital_efficiency_metrics: Dict[str, Any],
        per_share_metrics: Dict[str, Any],
        interest_cov_metrics: Dict[str, Any],
    ) -> List[str]:
        flags: List[str] = []

        # Cash generation
        if cash_flow_metrics["fcf_status"] == "negative":
            negative_years = sum(1 for f in cash_flow_metrics["fcf_by_year"] if f["fcf"] <= 0)
            flags.append(f"Free cash flow negative in {negative_years} of 5 years")

        if fcf_margin_metrics["fcf_margin_avg"] < 0.03 and cash_flow_metrics["fcf_status"] != "positive":
            flags.append(f"Weak FCF margin - avg: {fcf_margin_metrics['fcf_margin_avg']:.1%}")

        if cash_flow_metrics["cash_conversion"] == "poor":
            flags.append("Poor cash conversion - operating cash flow below 70% of net income")

        # Balance sheet
        if balance_sheet_metrics["leverage_level"] == "elevated":
            flags.append(f"Elevated leverage - Debt/EBITDA: {balance_sheet_metrics['debt_to_ebitda']:.2f}x")

        # REFACTORED: Only flag weak coverage if company actually has interest expense
        cov_level = interest_cov_metrics.get("interest_coverage_level")
        if cov_level == "weak" and interest_cov_metrics.get("interest_coverage_latest") is not None:
            flags.append(f"Weak interest coverage - latest: {interest_cov_metrics['interest_coverage_latest']:.1f}x")

        if balance_sheet_metrics["dilution_risk"] == "high":
            flags.append(f"High dilution - {balance_sheet_metrics['share_dilution']:.1%} over period")

        # Profitability erosion
        if profitability_metrics["operating_margin_trend"] == "declining":
            flags.append(f"Declining operating margins - avg: {profitability_metrics['operating_margin_avg']:.1%}")

        if profitability_metrics["earnings_consistency"] == "unstable":
            flags.append("Inconsistent earnings - multiple loss years")

        if profitability_metrics["revenue_growth_cagr"] < 0:
            flags.append(f"Negative revenue growth - CAGR: {profitability_metrics['revenue_growth_cagr']:.1%}")

        # Capital efficiency
        if capital_efficiency_metrics.get("roic_level") == "weak":
            flags.append(f"Weak ROIC - avg: {capital_efficiency_metrics.get('roic_avg', 0.0):.1%}")

        # Per-share compounding
        if per_share_metrics.get("fcf_per_share_cagr", 0.0) < 0:
            flags.append(f"FCF/share shrinking - CAGR: {per_share_metrics['fcf_per_share_cagr']:.1%}")

        return flags

    def identify_long_term_positive_signals(
        self,
        cash_flow_metrics: Dict[str, Any],
        fcf_margin_metrics: Dict[str, Any],
        balance_sheet_metrics: Dict[str, Any],
        profitability_metrics: Dict[str, Any],
        capital_efficiency_metrics: Dict[str, Any],
        per_share_metrics: Dict[str, Any],
        interest_cov_metrics: Dict[str, Any],
    ) -> List[str]:
        signals: List[str] = []

        if cash_flow_metrics["fcf_status"] == "positive":
            signals.append(f"Consistent positive FCF - 5-year avg: ${cash_flow_metrics['fcf_5yr_avg']:,.0f}")

        if fcf_margin_metrics["fcf_margin_avg"] >= 0.10:
            signals.append(f"Strong FCF margin - avg: {fcf_margin_metrics['fcf_margin_avg']:.1%}")

        if cash_flow_metrics["cash_conversion"] in ["excellent", "good"]:
            signals.append(f"Good cash conversion - {cash_flow_metrics['cash_conversion']}")

        if balance_sheet_metrics["leverage_level"] == "low":
            signals.append(f"Low leverage - Debt/EBITDA: {balance_sheet_metrics['debt_to_ebitda']:.2f}x")

        if capital_efficiency_metrics.get("roic_level") in ["excellent", "good"]:
            signals.append(f"Attractive ROIC - avg: {capital_efficiency_metrics.get('roic_avg', 0.0):.1%}")

        if per_share_metrics.get("fcf_per_share_cagr", 0.0) >= 0.05:
            signals.append(f"FCF/share compounding - CAGR: {per_share_metrics['fcf_per_share_cagr']:.1%}")

        if profitability_metrics["earnings_consistency"] == "stable":
            signals.append("Consistent profitability - positive earnings all 5 years")

        # REFACTORED: Only signal strong coverage if there's actual interest expense
        cov_level = interest_cov_metrics.get("interest_coverage_level")
        if cov_level == "strong":
            latest_cov = interest_cov_metrics.get("interest_coverage_latest")
            if latest_cov is not None:
                signals.append(f"Strong interest coverage - latest: {latest_cov:.1f}x")
        elif cov_level == "excellent" and interest_cov_metrics.get("note"):
            # Debt-free companies get special recognition
            signals.append("Minimal or no debt - excellent balance sheet")

        return signals

    # ------------------------------------------------------------------
    # SCORING + CLASSIFICATION (LONG-TERM)
    # ------------------------------------------------------------------
    def _calculate_long_term_score(
        self,
        cash_flow_metrics: Dict[str, Any],
        fcf_margin_metrics: Dict[str, Any],
        balance_sheet_metrics: Dict[str, Any],
        profitability_metrics: Dict[str, Any],
        capital_efficiency_metrics: Dict[str, Any],
        per_share_metrics: Dict[str, Any],
        interest_cov_metrics: Dict[str, Any],
    ) -> int:
        """
        Long-Term Quality Score (0-100). Deterministic + tunable.
        
        REFACTORED: Added recency weighting and momentum bonuses.
        """
        score = 0

        # Cash generation (35 points base + up to 10 bonus)
        if cash_flow_metrics["fcf_status"] == "positive":
            score += 15
        elif cash_flow_metrics["fcf_status"] == "mixed":
            score += 8

        fcfm = fcf_margin_metrics["fcf_margin_avg"]
        if fcfm >= 0.15:
            score += 10
        elif fcfm >= 0.10:
            score += 8
        elif fcfm >= 0.05:
            score += 5
        elif fcfm >= 0.02:
            score += 2

        if cash_flow_metrics["cash_conversion"] == "excellent":
            score += 10
        elif cash_flow_metrics["cash_conversion"] == "good":
            score += 6

        if cash_flow_metrics["fcf_volatility"] == "low":
            score += 5
        elif cash_flow_metrics["fcf_volatility"] == "moderate":
            score += 2

        # NEW: Momentum bonus for improving FCF trend
        if fcf_margin_metrics["fcf_margin_trend"] == "improving":
            score += 5
        if cash_flow_metrics["fcf_trend"] == "improving":
            score += 3

        # NEW: Recovery bonus - if last 2 years are strong after weak years
        recent_fcf = [f["fcf"] for f in cash_flow_metrics["fcf_by_year"][:2]]
        if len(recent_fcf) == 2 and all(f > 0 for f in recent_fcf):
            recent_avg = mean(recent_fcf)
            older_fcf = [f["fcf"] for f in cash_flow_metrics["fcf_by_year"][2:]]
            if older_fcf:
                older_positive = [f for f in older_fcf if f > 0]
                if older_positive:
                    older_avg = mean(older_positive)
                    if recent_avg > older_avg * 2:  # Strong recovery/acceleration
                        score += 5

        # Profitability durability (25 points)
        opm = profitability_metrics["operating_margin_avg"]
        if opm >= 0.20:
            score += 10
        elif opm >= 0.12:
            score += 8
        elif opm >= 0.07:
            score += 5
        elif opm >= 0.04:
            score += 2

        if profitability_metrics["operating_margin_trend"] == "improving":
            score += 5
        elif profitability_metrics["operating_margin_trend"] == "stable":
            score += 2

        gm = profitability_metrics["gross_margin_avg"]
        if gm >= 0.40:
            score += 5
        elif gm >= 0.25:
            score += 3
        elif gm >= 0.15:
            score += 1

        if profitability_metrics["earnings_consistency"] == "stable":
            score += 5
        elif profitability_metrics["earnings_consistency"] == "cyclical_positive":
            score += 2

        # Balance sheet resilience (20 points)
        if balance_sheet_metrics["leverage_level"] == "low":
            score += 10
        elif balance_sheet_metrics["leverage_level"] == "moderate":
            score += 7
        else:
            score += 2

        cov_level = interest_cov_metrics.get("interest_coverage_level", "unknown")
        # REFACTORED: Properly reward debt-free companies
        if cov_level == "excellent":  # No debt
            score += 5
        elif cov_level == "strong":
            score += 5
        elif cov_level == "adequate":
            score += 3
        elif cov_level == "weak":
            score += 0
        else:
            score += 2  # neutral for missing interestExpense

        cr = balance_sheet_metrics["current_ratio"]
        if cr >= 1.5:
            score += 5
        elif cr >= 1.1:
            score += 3
        elif cr >= 0.9:
            score += 1

        # Capital allocation / quality (20 points + up to 5 bonus)
        roic = capital_efficiency_metrics.get("roic_avg", 0.0)
        if roic >= 0.18:
            score += 15
        elif roic >= 0.12:
            score += 12
        elif roic >= 0.08:
            score += 8
        elif roic >= 0.05:
            score += 4

        fcfps = per_share_metrics.get("fcf_per_share_cagr", 0.0)
        if fcfps >= 0.10:
            score += 3
        elif fcfps >= 0.05:
            score += 2
        elif fcfps > 0:
            score += 1

        dil = abs(balance_sheet_metrics["share_dilution"])
        if dil < 0.02:
            score += 2
        elif dil < 0.05:
            score += 1

        # NEW: Exceptional per-share growth bonus
        if fcfps >= 0.50:  # 50%+ CAGR
            score += 5

        return min(int(round(score)), 100)

    def classify_company(
        self,
        score: int,
        risk_flags: List[str],
        cash_flow_metrics: Dict[str, Any],
        balance_sheet_metrics: Dict[str, Any],
    ) -> str:
        """Long-term investing classification."""
        negative_fcf_years = sum(1 for f in cash_flow_metrics["fcf_by_year"] if f["fcf"] <= 0)
        if negative_fcf_years >= 3:
            return "Higher risk"
        if balance_sheet_metrics["debt_to_ebitda"] > 5.0 and cash_flow_metrics["fcf_status"] != "positive":
            return "Higher risk"
        
        # REFACTORED: Use annualized dilution rate
        years = 5  # Assuming 5-year period
        annual_dilution = abs(balance_sheet_metrics["share_dilution"]) / years
        if annual_dilution > 0.05:  # >5% per year
            return "Higher risk"

        if score >= 80:
            return "High-quality compounder"
        if score >= 65:
            return "Quality (selective)"
        return "Higher risk"

    def generate_summary(
        self,
        classification: str,
        score: int,
        risk_flags: List[str],
        positive_signals: List[str],
    ) -> str:
        """Generate 2-3 sentence summary."""
        summary = f"{classification} with score of {score}/100. "
        if positive_signals:
            summary += positive_signals[0] + ". "
        else:
            summary += "Limited positive signals identified. "

        if risk_flags:
            summary += risk_flags[0] + "."
        else:
            summary += "No major concerns identified."

        return summary

    def process(self) -> Dict[str, Any]:
        """Process all financial data and return complete long-term screen report."""
        cash_flow_metrics = self.calculate_cash_flow_metrics()
        fcf_margin_metrics = self.calculate_fcf_margin_metrics()
        balance_sheet_metrics = self.calculate_balance_sheet_metrics()
        profitability_metrics = self.calculate_profitability_metrics()
        interest_cov_metrics = self.calculate_interest_coverage_metrics()
        capital_efficiency_metrics = self.calculate_capital_efficiency_metrics()
        per_share_metrics = self.calculate_per_share_growth_metrics()
        key_metrics = self.calculate_key_metrics()

        risk_flags = self.identify_long_term_risk_flags(
            cash_flow_metrics,
            fcf_margin_metrics,
            balance_sheet_metrics,
            profitability_metrics,
            capital_efficiency_metrics,
            per_share_metrics,
            interest_cov_metrics,
        )
        positive_signals = self.identify_long_term_positive_signals(
            cash_flow_metrics,
            fcf_margin_metrics,
            balance_sheet_metrics,
            profitability_metrics,
            capital_efficiency_metrics,
            per_share_metrics,
            interest_cov_metrics,
        )

        score = self._calculate_long_term_score(
            cash_flow_metrics,
            fcf_margin_metrics,
            balance_sheet_metrics,
            profitability_metrics,
            capital_efficiency_metrics,
            per_share_metrics,
            interest_cov_metrics,
        )

        classification = self.classify_company(score, risk_flags, cash_flow_metrics, balance_sheet_metrics)
        summary = self.generate_summary(classification, score, risk_flags, positive_signals)

        report: Dict[str, Any] = {
            "ticker": self.symbol,
            "analysis_period": self.analysis_period,

            # New canonical field for long-term investing:
            "long_term_quality_score": score,

            # Back-compat alias (safe to remove once your app migrates):
            "great_company_score": score,

            "classification": classification,
            "summary": summary,

            "cash_flow": {
                "fcf_status": cash_flow_metrics["fcf_status"],
                "fcf_trend": cash_flow_metrics["fcf_trend"],
                "fcf_5yr_avg": cash_flow_metrics["fcf_5yr_avg"],
                "fcf_volatility": cash_flow_metrics["fcf_volatility"],
                "cash_conversion": cash_flow_metrics["cash_conversion"],
                "capex_intensity": cash_flow_metrics["capex_intensity"],
                "working_capital_trend": cash_flow_metrics["working_capital_trend"],
            },

            "fcf_margin": {
                "fcf_margin_avg": fcf_margin_metrics["fcf_margin_avg"],
                "fcf_margin_trend": fcf_margin_metrics["fcf_margin_trend"],
            },

            "balance_sheet": {
                "net_debt": balance_sheet_metrics["net_debt"],
                "debt_to_ebitda": balance_sheet_metrics["debt_to_ebitda"],
                "leverage_level": balance_sheet_metrics["leverage_level"],
                "current_ratio": balance_sheet_metrics["current_ratio"],
                "liquidity": balance_sheet_metrics["liquidity"],
                "share_dilution": balance_sheet_metrics["share_dilution"],
                "dilution_risk": balance_sheet_metrics["dilution_risk"],
                "debt_trend": balance_sheet_metrics["debt_trend"],
            },

            "interest_coverage": {
                "interest_coverage_latest": interest_cov_metrics.get("interest_coverage_latest"),
                "interest_coverage_avg": interest_cov_metrics.get("interest_coverage_avg"),
                "interest_coverage_level": interest_cov_metrics.get("interest_coverage_level"),
            },

            "profitability": {
                "gross_margin_avg": profitability_metrics["gross_margin_avg"],
                "gross_margin_trend": profitability_metrics["gross_margin_trend"],
                "operating_margin_avg": profitability_metrics["operating_margin_avg"],
                "operating_margin_trend": profitability_metrics["operating_margin_trend"],
                "net_margin_avg": profitability_metrics["net_margin_avg"],
                "earnings_consistency": profitability_metrics["earnings_consistency"],
                "revenue_growth_cagr": profitability_metrics["revenue_growth_cagr"],
            },

            "capital_efficiency": {
                "roic_avg": capital_efficiency_metrics.get("roic_avg", 0.0),
                "roic_trend": capital_efficiency_metrics.get("roic_trend", "stable"),
                "roic_level": capital_efficiency_metrics.get("roic_level", "fair"),
            },

            "per_share": {
                "rev_per_share_cagr": per_share_metrics.get("rev_per_share_cagr", 0.0),
                "fcf_per_share_cagr": per_share_metrics.get("fcf_per_share_cagr", 0.0),
                "net_income_per_share_cagr": per_share_metrics.get("net_income_per_share_cagr", 0.0),
            },

            "key_metrics": key_metrics,
            "risk_flags": risk_flags,
            "positive_signals": positive_signals,

            # Keep this key for legacy callers; long-term screener doesn't use it.
            "put_selling_guidance": {},
        }

        return report


# Optional CLI (kept commented like your original)
# def main():
#     if len(sys.argv) < 2:
#         print("Usage: python process_financial_data.py input_file.txt [output_file.json]")
#         sys.exit(1)
#
#     input_file = Path(sys.argv[1])
#     output_file = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("processed_analysis.json")
#
#     print(f"Reading financial data from {input_file}...")
#     with open(input_file, "r") as f:
#         content = f.read()
#         financial_data = eval(content)  # NOTE: consider json instead of eval for safety
#
#     print(f"Processing financial data for {financial_data.get('balance_sheet', [{}])[0].get('symbol', 'UNKNOWN')}...")
#     calculator = FinancialMetricsCalculator(financial_data)
#     report = calculator.process()
#
#     print(f"Saving processed analysis to {output_file}...")
#     with open(output_file, "w") as f:
#         json.dump(report, f, indent=2)
#
#     print("\n✓ Analysis complete!")
#     print(f"  Ticker: {report['ticker']}")
#     print(f"  Score: {report['long_term_quality_score']}/100")
#     print(f"  Classification: {report['classification']}")
#     print(f"  Output saved to: {output_file}")
#
#
# if __name__ == "__main__":
#     main()
