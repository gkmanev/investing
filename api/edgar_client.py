"""
EdgarClient: fetch SEC financial statements via edgartools.

Normalises XBRL data into the same dict structure expected by
FinancialMetricsCalculator (field names match FMP's API).
"""
import re
import logging
from typing import Any, Dict, List, Optional

import pandas as pd
from edgar import set_identity, Company, MultiFinancials
from django.conf import settings

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")  # matches "2025-09-27" or "2025-09-27 (FY)"


def _clean_date(col: str) -> str:
    m = _DATE_RE.match(str(col))
    return m.group(1) if m else str(col)


def _date_cols(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if _DATE_RE.match(str(c))]


def _base_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Return only consolidated rows.

    StitchedStatement.to_dataframe() is already pre-filtered (no abstract/dimension
    columns), so this is a no-op for that case.  For single-filing Statement DataFrames
    the full filter is applied.
    """
    if "abstract" not in df.columns:
        return df
    return df[
        ~df["abstract"].fillna(False)
        & ~df["is_breakdown"].fillna(False)
        & df["dimension_member"].isna()
    ]


class EdgarClient:
    """Fetch and normalise annual 10-K financial statements from SEC EDGAR."""

    def __init__(self):
        identity = getattr(settings, "EDGAR_IDENTITY", "")
        if not identity:
            raise ValueError(
                "EDGAR_IDENTITY is missing from Django settings. "
                "Set it to 'Name email@example.com' as required by the SEC."
            )
        set_identity(identity)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
    def fetch_financial_data(self, symbol: str) -> Dict[str, Any]:
        """
        Return {"balance_sheet": [...], "income_statement": [...], "cash_flow": [...]}.

        Each list is sorted newest-first with FMP-compatible field names so that
        FinancialMetricsCalculator works unchanged.  Five 10-K filings are stitched
        via MultiFinancials giving up to 5 distinct fiscal years.
        """
        symbol = symbol.upper()
        multi = MultiFinancials.extract(
            Company(symbol).get_filings(form="10-K").head(5)
        )

        bs_stmt = multi.balance_sheet()
        is_stmt = multi.income_statement()
        cf_stmt = multi.cashflow_statement()

        missing = [
            name
            for name, s in [
                ("balance_sheet", bs_stmt),
                ("income_statement", is_stmt),
                ("cash_flow", cf_stmt),
            ]
            if s is None
        ]
        if missing:
            raise Exception(
                f"Incomplete financial data for {symbol}; missing: {', '.join(missing)}"
            )

        bs_raw = bs_stmt.to_dataframe()
        is_raw = is_stmt.to_dataframe()
        cf_raw = cf_stmt.to_dataframe()

        bs_map: Dict[str, dict] = {}
        is_map: Dict[str, dict] = {}
        cf_map: Dict[str, dict] = {}

        for col in _date_cols(bs_raw):
            d = _clean_date(col)
            bs_map[d] = self._parse_bs(_base_rows(bs_raw), col, d, symbol)

        for col in _date_cols(is_raw):
            d = _clean_date(col)
            is_map[d] = self._parse_is(_base_rows(is_raw), col, d, symbol)

        for col in _date_cols(cf_raw):
            d = _clean_date(col)
            cf_map[d] = self._parse_cf(_base_rows(cf_raw), col, d, symbol)

        # Enrich income statement with EBITDA = operating income + D&A
        for date, is_rec in is_map.items():
            dna = (cf_map.get(date) or {}).get("depreciationAndAmortization") or 0
            is_rec["ebitda"] = (is_rec.get("operatingIncome") or 0) + dna

        def _sort(d: dict) -> list:
            return sorted(d.values(), key=lambda r: r["date"], reverse=True)

        return {
            "balance_sheet": _sort(bs_map),
            "income_statement": _sort(is_map),
            "cash_flow": _sort(cf_map),
        }

    # ------------------------------------------------------------------
    # Statement parsers
    # ------------------------------------------------------------------
    @staticmethod
    def _get(base: pd.DataFrame, col: str, standard=(), xbrl=()) -> Optional[float]:
        """First non-null value matching any standard_concept or xbrl concept name."""
        for sc in standard:
            rows = base[base["standard_concept"] == sc]
            if not rows.empty and col in rows.columns:
                v = rows.iloc[0][col]
                if pd.notna(v):
                    return float(v)
        for cn in xbrl:
            rows = base[base["concept"] == cn]
            if not rows.empty and col in rows.columns:
                v = rows.iloc[0][col]
                if pd.notna(v):
                    return float(v)
        return None

    def _parse_bs(self, base: pd.DataFrame, col: str, date: str, symbol: str) -> dict:
        g = lambda std=(), xb=(): self._get(base, col, standard=std, xbrl=xb)

        cash = g(
            xb=("us-gaap_CashAndCashEquivalentsAtCarryingValue",),
            std=("CashAndMarketableSecurities",),
        ) or 0

        short_term = g(
            std=("ShortTermDebt",),
            xb=("us-gaap_CommercialPaper", "us-gaap_ShortTermBorrowings",
                "us-gaap_NotesPayableToBankCurrent"),
        ) or 0
        if short_term < 0:
            short_term = -short_term  # normalise to positive

        current_ltd = g(
            std=("CurrentPortionOfLongTermDebt",),
            xb=("us-gaap_LongTermDebtCurrent",),
        ) or 0
        long_term = g(
            std=("LongTermDebt",),
            xb=("us-gaap_LongTermDebtNoncurrent", "us-gaap_LongTermDebt"),
        ) or 0
        total_debt = short_term + current_ltd + long_term

        equity = g(
            std=("AllEquityBalance",),
            xb=(
                "us-gaap_StockholdersEquity",
                "us-gaap_StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            ),
        ) or 0

        return {
            "date": date,
            "symbol": symbol,
            "cashAndCashEquivalents": cash,
            "totalCurrentAssets": g(std=("CurrentAssetsTotal",), xb=("us-gaap_AssetsCurrent",)) or 0,
            "totalCurrentLiabilities": g(std=("CurrentLiabilitiesTotal",), xb=("us-gaap_LiabilitiesCurrent",)) or 0,
            "totalAssets": g(std=("Assets",), xb=("us-gaap_Assets",)) or 0,
            "totalDebt": total_debt,
            "totalStockholdersEquity": equity,
            "totalEquity": equity,
            "inventory": g(std=("Inventories",), xb=("us-gaap_InventoryNet",)) or 0,
            "commonStockSharesOutstanding": g(
                std=("SharesYearEnd",),
                xb=("us-gaap_CommonStockSharesOutstanding",),
            ),
        }

    def _parse_is(self, base: pd.DataFrame, col: str, date: str, symbol: str) -> dict:
        g = lambda std=(), xb=(): self._get(base, col, standard=std, xbrl=xb)

        revenue = g(
            std=("Revenue",),
            xb=(
                "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                "us-gaap_Revenues",
                "us-gaap_SalesRevenueNet",
            ),
        ) or 0
        gross_profit = g(std=("GrossProfit",), xb=("us-gaap_GrossProfit",)) or 0
        op_income = g(std=("OperatingIncomeLoss",), xb=("us-gaap_OperatingIncomeLoss",)) or 0
        net_income = g(std=("NetIncome",), xb=("us-gaap_NetIncomeLoss",)) or 0
        pretax = g(
            std=("PretaxIncomeLoss",),
            xb=("us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",),
        )
        income_tax = g(std=("IncomeTaxes",), xb=("us-gaap_IncomeTaxExpenseBenefit",))
        diluted_shares = g(
            std=("SharesFullyDilutedAverage",),
            xb=("us-gaap_WeightedAverageNumberOfDilutedSharesOutstanding",),
        )
        interest_expense = g(
            std=("InterestExpense",),
            xb=(
                "us-gaap_InterestExpenseNonoperating",
                "us-gaap_InterestExpense",
                "us-gaap_InterestExpenseDebt",
            ),
        )

        safe_rev = revenue or 1
        return {
            "date": date,
            "symbol": symbol,
            "revenue": revenue,
            "grossProfit": gross_profit,
            "grossProfitRatio": gross_profit / safe_rev,
            "operatingIncome": op_income,
            "operatingIncomeRatio": op_income / safe_rev,
            "netIncome": net_income,
            "netIncomeRatio": net_income / safe_rev,
            "ebitda": op_income,  # updated with D&A after CF is parsed
            "incomeBeforeTax": pretax,
            "incomeTaxExpense": income_tax,
            "weightedAverageShsOutDil": diluted_shares,
            "interestExpense": interest_expense,
        }

    def _parse_cf(self, base: pd.DataFrame, col: str, date: str, symbol: str) -> dict:
        g = lambda std=(), xb=(): self._get(base, col, standard=std, xbrl=xb)

        # Use XBRL concept directly for CF totals — standard_concept is reused on
        # individual WC-change rows too, so the first-match rule picks the wrong row.
        op_cf = (
            self._get(base, col, xbrl=("us-gaap_NetCashProvidedByUsedInOperatingActivities",))
            or 0
        )

        capex = (
            self._get(base, col, xbrl=(
                "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment",
                "us-gaap_PaymentsForCapitalImprovements",
            ))
            or 0
        )
        if capex > 0:
            capex = -capex  # standardise to negative (cash outflow)

        dna = g(
            std=("DepreciationExpense",),
            xb=(
                "us-gaap_DepreciationDepletionAndAmortization",
                "us-gaap_DepreciationAmortizationAndAccretionNet",
            ),
        ) or 0
        # Fallback: some companies use custom D&A concepts with no standard_concept
        # (e.g. msft_DepreciationAmortizationAndOther). Match by label keyword instead.
        if not dna:
            depr = base[base["label"].str.lower().str.contains(r"\bdepreciation\b", na=False, regex=True)]
            if not depr.empty and col in depr.columns:
                v = depr.iloc[0][col]
                if pd.notna(v):
                    dna = abs(float(v))

        # Working capital change = sum of IncreaseDecreaseIn* items.
        # Use "_IncreaseDecreaseIn\w" (underscore-anchored) to avoid matching
        # PeriodIncreaseDecreaseIncluding... concepts (e.g. net-cash-change row).
        wc_rows = base[base["concept"].str.contains(r"_IncreaseDecreaseIn\w", na=False, regex=True)]
        wc_change = (
            sum(float(v) for v in wc_rows[col].dropna())
            if col in wc_rows.columns
            else 0.0
        )

        return {
            "date": date,
            "symbol": symbol,
            "operatingCashFlow": op_cf,
            "capitalExpenditure": capex,
            "changeInWorkingCapital": wc_change,
            "depreciationAndAmortization": dna,
        }
