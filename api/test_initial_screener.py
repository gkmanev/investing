from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from api.management.commands import initial_screener as initial_screener_command
from api.models import Symbol


class InitialScreenerTechnicalsTestCase(TestCase):
    @patch("api.management.commands.initial_screener.requests.post")
    def test_fetch_tv_technicals_maps_recommendation_scores(self, mock_post: MagicMock) -> None:
        response = MagicMock()
        response.json.return_value = {
            "data": [
                {"d": ["AAPL", 0.62]},
                {"d": ["MSFT", 0.20]},
                {"d": ["NVDA", 0.00]},
                {"d": ["TSLA", -0.20]},
                {"d": ["INTC", -0.70]},
            ]
        }
        mock_post.return_value = response

        result = initial_screener_command.fetch_tv_technicals(
            ["AAPL", "MSFT", "NVDA", "TSLA", "INTC"]
        )

        self.assertEqual(result["AAPL"], Symbol.TechnicalScore.STRONG_BUY)
        self.assertEqual(result["MSFT"], Symbol.TechnicalScore.BUY)
        self.assertEqual(result["NVDA"], Symbol.TechnicalScore.NEUTRAL)
        self.assertEqual(result["TSLA"], Symbol.TechnicalScore.SELL)
        self.assertEqual(result["INTC"], Symbol.TechnicalScore.STRONG_SELL)
        mock_post.assert_called_once()

    @patch("api.management.commands.initial_screener._fetch_next_earnings_date")
    @patch("api.management.commands.initial_screener._fetch_dcf")
    @patch("api.management.commands.initial_screener._fetch_price_and_rsi")
    def test_process_symbol_updates_technical_score(
        self,
        mock_fetch_price_and_rsi: MagicMock,
        mock_fetch_dcf: MagicMock,
        mock_fetch_next_earnings_date: MagicMock,
    ) -> None:
        symbol = Symbol.objects.create(
            ticker="AAPL",
            score=90,
            technical_score=Symbol.TechnicalScore.SELL,
        )
        mock_fetch_price_and_rsi.return_value = (Decimal("100.50"), Decimal("55.00"))
        mock_fetch_dcf.return_value = Decimal("123.45")
        mock_fetch_next_earnings_date.return_value = (
            date(2026, 4, 30),
            {
                "source": "yahoo",
                "quote_status": "ok",
                "fmp_status": "not_needed",
                "candidate_dates": ["2026-04-30"],
            },
        )

        result = initial_screener_command._process_symbol(
            symbol,
            fmp_api_key="demo-key",
            force=False,
            technical_rating=Symbol.TechnicalScore.STRONG_BUY,
        )

        symbol.refresh_from_db()
        self.assertEqual(symbol.technical_score, Symbol.TechnicalScore.STRONG_BUY)
        self.assertEqual(symbol.price, Decimal("100.50"))
        self.assertEqual(symbol.rsi, Decimal("55.00"))
        self.assertEqual(symbol.dcf, Decimal("123.45"))
        self.assertEqual(symbol.next_earnings_date, date(2026, 4, 30))
        self.assertEqual(result["technical_rating"], Symbol.TechnicalScore.STRONG_BUY)

    @patch("api.management.commands.initial_screener._fetch_next_earnings_date")
    @patch("api.management.commands.initial_screener._fetch_dcf")
    @patch("api.management.commands.initial_screener._fetch_price_and_rsi")
    def test_process_symbol_keeps_existing_technical_score_when_missing_from_batch(
        self,
        mock_fetch_price_and_rsi: MagicMock,
        mock_fetch_dcf: MagicMock,
        mock_fetch_next_earnings_date: MagicMock,
    ) -> None:
        symbol = Symbol.objects.create(
            ticker="MSFT",
            score=90,
            technical_score=Symbol.TechnicalScore.BUY,
        )
        mock_fetch_price_and_rsi.return_value = (None, None)
        mock_fetch_dcf.return_value = None
        mock_fetch_next_earnings_date.return_value = (
            None,
            {
                "source": "none",
                "quote_status": "empty_result",
                "fmp_status": "not_needed",
                "candidate_dates": [],
            },
        )

        initial_screener_command._process_symbol(
            symbol,
            fmp_api_key="",
            force=False,
            technical_rating=initial_screener_command.TECHNICAL_SCORE_MISSING,
        )

        symbol.refresh_from_db()
        self.assertEqual(symbol.technical_score, Symbol.TechnicalScore.BUY)
