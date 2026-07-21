from __future__ import annotations

from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from api.models import Symbol


def _annual_statements() -> dict[str, list[dict[str, object]]]:
    statements = {"balance_sheet": [], "income_statement": [], "cash_flow": []}
    for offset, year in enumerate(range(2025, 2020, -1)):
        date = f"{year}-12-31"
        statements["balance_sheet"].append(
            {
                "date": date,
                "symbol": "TEST",
                "totalDebt": 1_000 - offset * 50,
                "cashAndCashEquivalents": 300,
                "totalCurrentAssets": 800,
                "totalCurrentLiabilities": 400,
                "totalStockholdersEquity": 2_000,
                "weightedAverageShsOutDil": 100,
            }
        )
        statements["income_statement"].append(
            {
                "date": date,
                "symbol": "TEST",
                "revenue": 2_000 - offset * 100,
                "netIncome": 300 - offset * 10,
                "grossProfit": 1_000,
                "operatingIncome": 400,
                "ebitda": 450,
                "interestExpense": 20,
                "weightedAverageShsOutDil": 100,
            }
        )
        statements["cash_flow"].append(
            {
                "date": date,
                "symbol": "TEST",
                "operatingCashFlow": 500 - offset * 20,
                "capitalExpenditure": -100,
                "changeInWorkingCapital": -20,
            }
        )
    return statements


@override_settings(FINANCIAL_MODELING_API_KEY="test-key")
class RefreshFinancialQualityScoresTests(TestCase):
    def setUp(self) -> None:
        self.symbol = Symbol.objects.create(ticker="TEST", score=1)
        self.payloads = _annual_statements()

    def _session(self) -> Mock:
        session = Mock()

        def get(_url, *, params, timeout):
            endpoint = _url.rsplit("/", 1)[-1]
            source = {
                "balance-sheet-statement": "balance_sheet",
                "income-statement": "income_statement",
                "cash-flow-statement": "cash_flow",
            }[endpoint]
            response = Mock()
            response.json.return_value = self.payloads[source]
            return response

        session.get.side_effect = get
        return session

    @patch("api.management.commands.refresh_financial_quality_scores.requests.Session")
    def test_refreshes_score_and_classification(self, session_factory: Mock) -> None:
        session_factory.return_value = self._session()

        call_command(
            "refresh_financial_quality_scores",
            "--symbols",
            "TEST",
            "--delay",
            "0",
        )

        self.symbol.refresh_from_db()
        self.assertGreater(self.symbol.score, 1)
        self.assertTrue(self.symbol.classification)

    @patch("api.management.commands.refresh_financial_quality_scores.requests.Session")
    def test_dry_run_does_not_persist(self, session_factory: Mock) -> None:
        session_factory.return_value = self._session()

        call_command(
            "refresh_financial_quality_scores",
            "--symbols",
            "TEST",
            "--delay",
            "0",
            "--dry-run",
        )

        self.symbol.refresh_from_db()
        self.assertEqual(self.symbol.score, 1)
        self.assertIsNone(self.symbol.classification)
