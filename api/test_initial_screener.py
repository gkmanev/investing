from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from django.test import TestCase

from api.management.commands import initial_screener as initial_screener_command
from api.models import Symbol


class InitialScreenerTechnicalsTestCase(TestCase):
    @patch("api.management.commands.initial_screener.requests.post")
    def test_fetch_next_earnings_date_uses_tradingview_scan_endpoint(
        self, mock_post: MagicMock
    ) -> None:
        earliest = date.today() + timedelta(days=7)
        later = date.today() + timedelta(days=14)
        past = date.today() - timedelta(days=2)
        earliest_ts = int(datetime.combine(earliest, datetime.min.time()).timestamp())
        later_ts = int(datetime.combine(later, datetime.min.time()).timestamp())
        past_ts = int(datetime.combine(past, datetime.min.time()).timestamp())

        response = MagicMock(status_code=200)
        response.json.return_value = {
            "data": [
                {"s": "NASDAQ:AAPL", "d": [later_ts]},
                {"s": "NASDAQ:AAPL", "d": [earliest_ts]},
                {"s": "NASDAQ:AAPL", "d": [past_ts]},
            ]
        }
        mock_post.return_value = response

        result, debug = initial_screener_command._fetch_next_earnings_date(
            "AAPL", "NASDAQ"
        )

        self.assertEqual(result, earliest)
        self.assertEqual(debug["source"], "tradingview")
        self.assertEqual(debug["status"], "ok")
        self.assertEqual(debug["requested_symbol"], "NASDAQ:AAPL")
        self.assertEqual(debug["returned_symbol"], "NASDAQ:AAPL")
        mock_post.assert_called_once_with(
            initial_screener_command.TV_SCANNER_URL,
            json={
                "symbols": {"tickers": ["NASDAQ:AAPL"], "query": {"types": []}},
                "columns": ["earnings_release_next_date"],
            },
            headers=initial_screener_command.TV_SCAN_HEADERS,
            timeout=20,
        )

    @patch("api.management.commands.initial_screener.requests.post")
    def test_fetch_next_earnings_date_returns_none_on_tradingview_error(
        self, mock_post: MagicMock
    ) -> None:
        mock_post.side_effect = requests.RequestException("boom")

        result, debug = initial_screener_command._fetch_next_earnings_date(
            "AAPL", "NASDAQ"
        )

        self.assertIsNone(result)
        self.assertEqual(debug["status"], "request_exception:RequestException")
        self.assertEqual(debug["requested_symbol"], "NASDAQ:AAPL")

    @patch("api.management.commands.initial_screener.requests.post")
    def test_fetch_next_earnings_date_uses_exchange_aliases(
        self, mock_post: MagicMock
    ) -> None:
        next_date = date.today() + timedelta(days=10)
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "data": [{"s": "NYSE:AAPL", "d": [next_date.isoformat()]}]
        }
        mock_post.return_value = response

        result, debug = initial_screener_command._fetch_next_earnings_date(
            "AAPL", "NYQ"
        )

        self.assertEqual(result, next_date)
        self.assertEqual(debug["requested_symbol"], "NYSE:AAPL")

    @patch("api.management.commands.initial_screener.requests.post")
    def test_fetch_next_earnings_date_returns_none_when_tradingview_empty(
        self, mock_post: MagicMock
    ) -> None:
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": []}
        mock_post.return_value = response

        result, debug = initial_screener_command._fetch_next_earnings_date(
            "AAPL", "NASDAQ"
        )

        self.assertIsNone(result)
        self.assertEqual(debug["status"], "empty_result")

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
                "source": "tradingview",
                "status": "ok",
                "requested_symbol": "NASDAQ:AAPL",
                "returned_symbol": "NASDAQ:AAPL",
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
                "source": "tradingview",
                "status": "empty_result",
                "requested_symbol": "NASDAQ:MSFT",
                "returned_symbol": None,
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
