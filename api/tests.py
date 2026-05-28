from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
import json
import urllib.error
from typing import Any

import numpy as np
import pandas as pd
import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from unittest.mock import MagicMock, call, patch

from api import agent_views
from api.custom_filters import CUSTOM_FILTER_PAYLOAD, CUSTOM_FILTER_PAYLOAD_V2
from api.management.commands import ai_agents_potential as ai_agents_potential_command
from api.management.commands import initial_screener as initial_screener_command
from api.management.commands.fetch_profile_data import (
    API_HEADERS,
    OPTION_EXPIRATIONS_ENDPOINT,
    Command,
)
from api.management.commands.trading_view_scrape import (
    Command as TradingViewCommand,
    FinancialModelingPrepClient,
    RequestRateLimiter,
    TradingViewOptions,
)

from .models import CboeSecurity, DueDiligenceReport, Investment, ScreenerFilter, ScreenerType, Symbol
from .serializers import SymbolSerializer


class InitialScreenerHelpersTestCase(APITestCase):
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

    @patch("api.management.commands.initial_screener.talib.RSI")
    @patch("api.management.commands.initial_screener.yf.download")
    def test_fetch_price_and_rsi_uses_fmp_for_price_and_yfinance_for_rsi(
        self,
        mock_download: MagicMock,
        mock_rsi: MagicMock,
    ) -> None:
        mock_download.return_value = pd.DataFrame({"Close": [100.0, 101.0, 102.0]})
        mock_rsi.return_value = np.array([np.nan, 44.12, 55.67])
        price_client = MagicMock()
        price_client.get_underlying_price.return_value = "123.45"

        price, rsi = initial_screener_command._fetch_price_and_rsi(
            "AAPL",
            price_client=price_client,
        )

        self.assertEqual(price, Decimal("123.45"))
        self.assertEqual(rsi, Decimal("55.67"))
        price_client.get_underlying_price.assert_called_once_with("AAPL")
        mock_download.assert_called_once()

    @patch("api.management.commands.initial_screener.talib.RSI")
    @patch("api.management.commands.initial_screener.yf.download")
    def test_fetch_price_and_rsi_does_not_fallback_to_yfinance_price(
        self,
        mock_download: MagicMock,
        mock_rsi: MagicMock,
    ) -> None:
        mock_download.return_value = pd.DataFrame({"Close": [90.0, 91.0, 92.0]})
        mock_rsi.return_value = np.array([np.nan, 40.01, 41.25])
        price_client = MagicMock()
        price_client.get_underlying_price.side_effect = ValueError("bad quote")

        price, rsi = initial_screener_command._fetch_price_and_rsi(
            "AAPL",
            price_client=price_client,
        )

        self.assertIsNone(price)
        self.assertEqual(rsi, Decimal("41.25"))


class InvestmentAPITestCase(APITestCase):
    def setUp(self) -> None:
        self.list_url = reverse("investment-list")
        self.detail_url_name = "investment-detail"

    def create_investment(self, **overrides):
        defaults = {
            "ticker": "IDX",
            "category": "Fund",
            "description": "Diversified index fund.",
            "price": 10.5,
            "volume": 1000,
            "market_cap": 5_000_000,
        }
        defaults.update(overrides)
        return Investment.objects.create(**defaults)

    def test_can_create_investment(self) -> None:
        payload = {
            "ticker": "GRW",
            "category": "Fund",
            "description": "Long-term growth fund.",
            "price": 12.34,
            "volume": 2500,
            "market_cap": 12_000_000,
        }

        response = self.client.post(self.list_url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Investment.objects.count(), 1)
        self.assertEqual(Investment.objects.get().ticker, "GRW")

    def test_list_returns_created_items(self) -> None:
        self.create_investment(ticker="BND", category="ETF")

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["ticker"], "BND")

    def test_list_can_filter_by_category_and_ticker(self) -> None:
        self.create_investment(ticker="BND", category="ETF")
        self.create_investment(ticker="GRW", category="Fund")

        response = self.client.get(
            self.list_url,
            {"category": "fund", "ticker": "rw"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["ticker"], "GRW")

    def test_list_can_filter_by_screener_type_and_options_suitability(self) -> None:
        self.create_investment(
            ticker="BND", category="ETF", screener_type="Growth", options_suitability=1
        )
        self.create_investment(
            ticker="GRW", category="Fund", screener_type="Value", options_suitability=0
        )
        self.create_investment(
            ticker="MOM", category="ETF", screener_type="Growth", options_suitability=0
        )

        response = self.client.get(
            self.list_url,
            {"screener_type": "growth", "options_suitability": "0"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["ticker"], "MOM")

    def test_list_can_filter_by_options_suitability_only(self) -> None:
        self.create_investment(ticker="OPT1", options_suitability=1)
        self.create_investment(ticker="OPT0", options_suitability=0)

        response = self.client.get(self.list_url, {"options_suitability": "1"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["ticker"], "OPT1")

    def test_list_can_filter_by_screener_type_with_spaces(self) -> None:
        screener = "Strong Buy Stocks With Short Squeeze Potential"
        self.create_investment(
            ticker="SBS", category="Stock", screener_type=screener, options_suitability=1
        )
        self.create_investment(
            ticker="OTHER",
            category="Stock",
            screener_type="Other Screener",
            options_suitability=1,
        )

        response = self.client.get(
            self.list_url,
            {"screener_type": screener, "options_suitability": "1"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["ticker"], "SBS")

    def test_list_custom_screener_filter_returns_count(self) -> None:
        custom_screener = "Custom screener filter"
        self.create_investment(ticker="CSTM1", screener_type=custom_screener)
        self.create_investment(ticker="CSTM2", screener_type=custom_screener)
        self.create_investment(ticker="OTHER", screener_type="Another Screener")

        response = self.client.get(self.list_url, {"screener_type": custom_screener})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertEqual(
            {item["ticker"] for item in response.data["results"]}, {"CSTM1", "CSTM2"}
        )

    def test_legacy_screenter_type_query_param_is_supported(self) -> None:
        self.create_investment(ticker="BND", category="ETF", screener_type="Growth")
        self.create_investment(ticker="GRW", category="Fund", screener_type="Value")

        response = self.client.get(self.list_url, {"screenter_type": "value"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["ticker"], "GRW")

    def test_list_can_filter_by_numeric_ranges(self) -> None:
        self.create_investment(ticker="LOW", price=5, market_cap=1_000_000, volume=10)
        self.create_investment(ticker="MID", price=15, market_cap=5_000_000, volume=1_000)
        self.create_investment(ticker="HIGH", price=25, market_cap=50_000_000, volume=10_000)

        response = self.client.get(
            self.list_url,
            {
                "min_price": "10",
                "max_price": "20",
                "min_market_cap": "2000000",
                "max_market_cap": "6000000",
                "min_volume": "999",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["ticker"], "MID")

    def test_list_rejects_invalid_numeric_filters(self) -> None:
        response = self.client.get(self.list_url, {"min_price": "abc"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("min_price", response.data)

    def test_list_rejects_invalid_options_suitability_filter(self) -> None:
        response = self.client.get(
            self.list_url, {"options_suitability": "not-an-integer"}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("options_suitability", response.data)

    def test_list_can_filter_by_cboe_membership(self) -> None:
        self.create_investment(ticker="AAA", category="ETF")
        self.create_investment(ticker="BBB", category="ETF")
        CboeSecurity.objects.create(symbol="AAA")

        response = self.client.get(self.list_url, {"cboe": "true"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["ticker"] for item in response.data], ["AAA"])

        response = self.client.get(self.list_url, {"cboe": "false"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["ticker"] for item in response.data], ["BBB"])

    def test_can_update_investment(self) -> None:
        investment = self.create_investment()
        url = reverse(self.detail_url_name, args=[investment.id])

        response = self.client.patch(url, {"price": "15.42"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        investment.refresh_from_db()
        self.assertEqual(str(investment.price), "15.4200")

    def test_cannot_create_invalid_investment(self) -> None:
        response = self.client.post(
            self.list_url,
            {
                "ticker": "",
                "category": "Fund",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ticker", response.data)


class ScreenerTypeAPITestCase(APITestCase):
    def setUp(self) -> None:
        self.list_url = reverse("screenertype-list")

    def test_can_create_screener_type(self) -> None:
        payload = {"name": "Top Gainers", "description": "Daily top performing stocks."}

        response = self.client.post(self.list_url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ScreenerType.objects.count(), 1)
        self.assertEqual(ScreenerType.objects.get().name, "Top Gainers")

    def test_list_includes_filters(self) -> None:
        screener_type = ScreenerType.objects.create(
            name="Value Stocks", description="Stocks filtered by valuation metrics."
        )
        ScreenerFilter.objects.create(
            screener_type=screener_type,
            label="Market Cap >= 500M",
            payload={"field": "market_cap", "operator": ">=", "value": 500_000_000},
            display_order=2,
        )
        ScreenerFilter.objects.create(
            screener_type=screener_type,
            label="P/E < 15",
            payload={"field": "pe_ratio", "operator": "<", "value": 15},
            display_order=1,
        )

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        filters = response.data[0]["filters"]
        self.assertEqual(len(filters), 2)
        # Filters should be ordered by ``display_order``
        self.assertEqual(filters[0]["label"], "P/E < 15")
        self.assertEqual(filters[1]["label"], "Market Cap >= 500M")


class ScreenerFilterAPITestCase(APITestCase):
    def setUp(self) -> None:
        self.screener_type = ScreenerType.objects.create(name="Momentum", description="")
        self.list_url = reverse("screenerfilter-list")

    def test_can_create_filter(self) -> None:
        payload = {
            "screener_type": self.screener_type.id,
            "label": "Relative Strength > 70",
            "payload": {"field": "relative_strength", "operator": ">", "value": 70},
            "display_order": 5,
        }

        response = self.client.post(self.list_url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ScreenerFilter.objects.count(), 1)
        filter_obj = ScreenerFilter.objects.get()
        self.assertEqual(filter_obj.screener_type, self.screener_type)
        self.assertEqual(filter_obj.label, "Relative Strength > 70")
        self.assertEqual(filter_obj.display_order, 5)

    def test_cannot_create_filter_with_blank_label(self) -> None:
        payload = {"screener_type": self.screener_type.id, "label": "   "}

        response = self.client.post(self.list_url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("label", response.data)


class FetchScreenersCommandTests(APITestCase):
    def _assert_custom_filter(self, name: str, payload: dict) -> None:
        custom_screener = ScreenerType.objects.get(name=name)
        custom_filters = list(custom_screener.filters.order_by("display_order"))
        self.assertEqual(len(custom_filters), 1)
        self.assertEqual(custom_filters[0].label, name)
        self.assertEqual(custom_filters[0].payload, payload)
        self.assertEqual(custom_filters[0].display_order, 1)

    @patch("api.management.commands.fetch_screeners.requests.get")
    def test_fetch_and_persist_screeners(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    {
                        "attributes": {
                            "name": "Value Stocks",
                            "description": "Stocks filtered by valuation metrics.",
                            "filters": [
                                {
                                    "field": "pe_ratio",
                                    "operator": "<",
                                    "value": 15,
                                },
                                {
                                    "field": "market_cap",
                                    "operator": ">=",
                                    "value": 500_000_000,
                                },
                            ],
                        }
                    },
                    {
                        "attributes": {
                            "name": "Growth Picks",
                            "shortDescription": "High growth companies.",
                            "filters": [
                                {
                                    "industryId": 999,
                                },
                                {
                                    "field": "revenue_growth",
                                    "operator": ">",
                                    "value": 0.2,
                                    "industryId": 999,
                                },
                            ],
                        }
                    },
                ]
            },
            text="{}",
        )

        call_command("fetch_screeners")

        self.assertEqual(ScreenerType.objects.count(), 4)
        screener = ScreenerType.objects.get(name="Value Stocks")
        self.assertEqual(screener.description, "Stocks filtered by valuation metrics.")

        filters = list(screener.filters.order_by("display_order"))
        self.assertEqual(len(filters), 2)
        self.assertEqual(filters[0].label, "field=pe_ratio, operator=<, value=15")
        self.assertEqual(
            filters[0].payload,
            {"field": "pe_ratio", "operator": "<", "value": 15},
        )
        self.assertEqual(filters[0].display_order, 1)

        self.assertEqual(filters[1].label, "field=market_cap, operator=>=, value=500000000")
        self.assertEqual(
            filters[1].payload,
            {"field": "market_cap", "operator": ">=", "value": 500_000_000},
        )
        self.assertEqual(filters[1].display_order, 2)

        second = ScreenerType.objects.get(name="Growth Picks")
        self.assertEqual(second.description, "High growth companies.")
        filters = list(second.filters.order_by("display_order"))
        self.assertEqual(len(filters), 2)

        industry_filter = filters[0]
        self.assertEqual(industry_filter.label, "industry_id=999")
        self.assertEqual(industry_filter.payload, {"industry_id": 999})
        self.assertEqual(industry_filter.display_order, 1)

        growth_filter = filters[1]
        self.assertEqual(
            growth_filter.label,
            "field=revenue_growth, industry_id=999, operator=>, value=0.2",
        )
        self.assertEqual(
            growth_filter.payload,
            {
                "field": "revenue_growth",
                "operator": ">",
                "value": 0.2,
                "industry_id": 999,
            },
        )
        self.assertIn("industry_id", growth_filter.payload)

        self.assertEqual(filters[1].display_order, 2)

        self._assert_custom_filter("Custom screener filter", CUSTOM_FILTER_PAYLOAD)
        self._assert_custom_filter("Custom screener filterV2", CUSTOM_FILTER_PAYLOAD_V2)

    @patch("api.management.commands.fetch_screeners.requests.get")
    def test_command_removes_missing_filters(self, mock_get: MagicMock) -> None:
        screener = ScreenerType.objects.create(name="Momentum", description="")
        ScreenerFilter.objects.create(
            screener_type=screener,
            label="field=old, operator=>, value=1",
            payload={"field": "old", "operator": ">", "value": 1},
            display_order=1,
        )

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    {
                        "attributes": {
                            "name": "Momentum",
                            "description": "Updated description.",
                            "filters": ["Volume Surge"],
                        }
                    }
                ]
            },
            text="{}",
        )

        call_command("fetch_screeners")

        screener.refresh_from_db()
        self.assertEqual(screener.description, "Updated description.")
        filters = list(screener.filters.order_by("display_order"))
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].label, "Volume Surge")
        self.assertEqual(filters[0].payload, "Volume Surge")
        self.assertEqual(filters[0].display_order, 1)

        self._assert_custom_filter("Custom screener filter", CUSTOM_FILTER_PAYLOAD)
        self._assert_custom_filter("Custom screener filterV2", CUSTOM_FILTER_PAYLOAD_V2)

    @patch("api.management.commands.fetch_screeners.requests.get")
    def test_command_trims_quant_rating_values(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    {
                        "attributes": {
                            "name": "Stocks by Quant",
                            "description": "Quant focused screener.",
                            "filters": [
                                {
                                    "quant_rating": [
                                        "strong_buy",
                                        "buy",
                                        "hold",
                                        "sell",
                                    ],
                                    "field": "sample",
                                }
                            ],
                        }
                    }
                ]
            },
            text="{}",
        )

        call_command("fetch_screeners")

        screener = ScreenerType.objects.get(name="Stocks by Quant")
        self.assertEqual(screener.description, "Quant focused screener.")

        filters = list(screener.filters.order_by("display_order"))
        self.assertEqual(len(filters), 1)
        self.assertEqual(
            filters[0].payload,
            {"field": "sample", "quant_rating": ["strong_buy", "buy"]},
        )
        self.assertEqual(
            filters[0].label,
            'field=sample, quant_rating=["strong_buy", "buy"]',
        )

        self._assert_custom_filter("Custom screener filter", CUSTOM_FILTER_PAYLOAD)
        self._assert_custom_filter("Custom screener filterV2", CUSTOM_FILTER_PAYLOAD_V2)

    @patch("api.management.commands.fetch_screeners.requests.get")
    def test_command_removes_industry_id_from_quant_screener(
        self, mock_get: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    {
                        "attributes": {
                            "name": "Stocks by Quant",
                            "description": "Quant screener with industry filter.",
                            "filters": [
                                {
                                    "industry_id": {"in": [123, 456], "exclude": False},
                                    "quant_rating": {"in": ["strong_buy", "buy"]},
                                    "field": "sample",
                                }
                            ],
                        }
                    }
                ]
            },
            text="{}",
        )

        call_command("fetch_screeners")

        screener = ScreenerType.objects.get(name="Stocks by Quant")
        filters = list(screener.filters.order_by("display_order"))
        self.assertEqual(len(filters), 1)
        self.assertEqual(
            filters[0].payload,
            {"field": "sample", "quant_rating": {"in": ["strong_buy", "buy"]}},
        )
        self.assertNotIn("industry_id", filters[0].label)
        self.assertNotIn("industry_id", json.dumps(filters[0].payload))


class FetchTickerNamesCommandTests(APITestCase):
    @patch("api.management.commands.fetch_ticker_names.requests.get")
    def test_command_returns_tickers_from_endpoint(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"ticker": "AAA"},
                {"ticker": "BBB"},
            ],
            text="{}",
        )

        buffer = StringIO()
        result = call_command("fetch_ticker_names", stdout=buffer)

        mock_get.assert_called_once_with(
            "http://127.0.0.1:8000/api/investments/",
            params={"options_suitability": 1, "screener_type": "Stocks by Quant"},
            timeout=30,
        )
        self.assertEqual(result, "AAA\nBBB")
        self.assertEqual(buffer.getvalue(), "AAA\nBBB\n")

    @patch("api.management.commands.fetch_ticker_names.requests.get")
    def test_command_errors_when_no_tickers_found(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [], text="[]")

        with self.assertRaisesMessage(
            CommandError, "No ticker names were found in the response payload."
        ):
            call_command("fetch_ticker_names")


class FetchScreenerResultsCommandTests(APITestCase):
    def setUp(self) -> None:
        self.screener = ScreenerType.objects.create(
            name="Value Stocks", description="Stocks filtered by valuation metrics."
        )
        ScreenerFilter.objects.create(
            screener_type=self.screener,
            label="Market Cap >= 500M",
            payload={"field": "market_cap", "operator": ">=", "value": 500_000_000},
            display_order=1,
        )

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_creates_investments_from_tickers(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    {"attributes": {"p": {"names": ["Apple Inc."]}}},
                    {"attributes": {"p": {"name": "Microsoft Corporation"}}},
                    {"attributes": {"name": "Tesla, Inc."}},
                ]
            },
            text="{}",
        )

        buffer = StringIO()
        result = call_command(
            "fetch_screener_results", screener_name=self.screener.name, stdout=buffer
        )

        expected_output = "Apple Inc.\nMicrosoft Corporation\nTesla, Inc."
        self.assertEqual(result, expected_output)
        self.assertEqual(buffer.getvalue(), expected_output + "\n")

        tickers = Investment.objects.order_by("ticker").values_list("ticker", flat=True)
        self.assertEqual(list(tickers), ["Apple Inc.", "Microsoft Corporation", "Tesla, Inc."])
        self.assertTrue(
            Investment.objects.filter(ticker="Apple Inc.", category="stock").exists()
        )
        self.assertEqual(
            Investment.objects.filter(screener_type=self.screener.name).count(), 3
        )

    @patch("api.management.commands.fetch_screener_results.Command._fetch_weekly_option_tickers")
    @patch("api.management.commands.fetch_screener_results.Command._fetch_price_and_rsi")
    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_populates_price_and_rsi_for_all_tickers(
        self,
        mock_post: MagicMock,
        mock_price_and_rsi: MagicMock,
        mock_weekly_tickers: MagicMock,
    ) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    {"attributes": {"name": "WEEKLY"}},
                    {"attributes": {"name": "DAILY"}},
                ]
            },
            text="{}",
        )
        mock_weekly_tickers.return_value = {"WEEKLY"}
        mock_price_and_rsi.side_effect = [
            (Decimal("123.45"), Decimal("55.67")),
            (Decimal("67.89"), Decimal("44.32")),
        ]

        call_command("fetch_screener_results", screener_name=self.screener.name)

        weekly_investment = Investment.objects.get(ticker="WEEKLY")
        self.assertTrue(weekly_investment.weekly_options)
        self.assertEqual(weekly_investment.price, Decimal("123.45"))
        self.assertEqual(weekly_investment.rsi, Decimal("55.67"))

        daily_investment = Investment.objects.get(ticker="DAILY")
        self.assertFalse(daily_investment.weekly_options)
        self.assertEqual(daily_investment.price, Decimal("67.89"))
        self.assertEqual(daily_investment.rsi, Decimal("44.32"))
        mock_price_and_rsi.assert_has_calls([call("WEEKLY"), call("DAILY")])

    @patch("api.management.commands.fetch_screener_results.Command._fetch_weekly_option_tickers")
    @patch("api.management.commands.fetch_screener_results.Command._fetch_price_and_rsi")
    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_populates_price_when_weekly_options_unknown(
        self,
        mock_post: MagicMock,
        mock_price_and_rsi: MagicMock,
        mock_weekly_tickers: MagicMock,
    ) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "UNKNOWN"}}]},
            text="{}",
        )
        mock_weekly_tickers.return_value = None
        mock_price_and_rsi.return_value = (Decimal("222.22"), Decimal("33.11"))

        call_command("fetch_screener_results", screener_name=self.screener.name)

        investment = Investment.objects.get(ticker="UNKNOWN")
        self.assertIsNone(investment.weekly_options)
        self.assertEqual(investment.price, Decimal("222.22"))
        self.assertEqual(investment.rsi, Decimal("33.11"))
        mock_price_and_rsi.assert_called_once_with("UNKNOWN")

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_prints_count_for_custom_screener(
        self, mock_post: MagicMock
    ) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    {"attributes": {"name": "Alpha Corp"}},
                    {"attributes": {"name": "Beta LLC"}},
                ]
            },
            text="{}",
        )

        buffer = StringIO()
        result = call_command(
            "fetch_screener_results",
            screener_name="Custom screener filter",
            stdout=buffer,
        )

        expected_output = "Alpha Corp\nBeta LLC"
        self.assertEqual(result, expected_output)
        self.assertEqual(
            buffer.getvalue(),
            "Returned tickers: 2\n" + expected_output + "\n",
        )

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_fetches_multiple_pages(self, mock_post: MagicMock) -> None:
        def build_response(names: list[str]) -> MagicMock:
            payload = {
                "data": [
                    {"attributes": {"name": company_name}} for company_name in names
                ]
            }
            response = MagicMock(status_code=200, text="{}")
            response.json.return_value = payload
            return response

        mock_post.side_effect = [
            build_response(["Alpha Corp"]),
            build_response(["Beta LLC"]),
            build_response([]),
        ]

        buffer = StringIO()
        result = call_command(
            "fetch_screener_results",
            screener_name=self.screener.name,
            per_page=1,
            stdout=buffer,
        )

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(
            [call.kwargs["params"]["page"] for call in mock_post.call_args_list],
            ["1", "2", "3"],
        )

        expected_output = "Alpha Corp\nBeta LLC"
        self.assertEqual(result, expected_output)
        self.assertEqual(buffer.getvalue(), expected_output + "\n")

        tickers = Investment.objects.order_by("ticker").values_list("ticker", flat=True)
        self.assertEqual(list(tickers), ["Alpha Corp", "Beta LLC"])

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_replaces_existing_screener_entries(self, mock_post: MagicMock) -> None:
        Investment.objects.create(
            ticker="Legacy", category="stock", screener_type=self.screener.name
        )
        Investment.objects.create(
            ticker="Keep", category="stock", screener_type="Another Screener"
        )

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Fresh"}}]},
            text="{}",
        )

        call_command("fetch_screener_results", screener_name=self.screener.name)

        self.assertFalse(
            Investment.objects.filter(
                ticker="Legacy", screener_type=self.screener.name
            ).exists()
        )
        self.assertTrue(
            Investment.objects.filter(
                ticker="Keep", screener_type="Another Screener"
            ).exists()
        )
        self.assertTrue(
            Investment.objects.filter(
                ticker="Fresh", screener_type=self.screener.name
            ).exists()
        )

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_updates_existing_investments(self, mock_post: MagicMock) -> None:
        investment = Investment.objects.create(
            ticker="Apple Inc.",
            category="legacy",
            description="",
        )

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "data": [
                    {"attributes": {"p": {"names": ["Apple Inc."]}}},
                ]
            },
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=self.screener.name,
            asset_type="fund",
        )

        investment.refresh_from_db()
        self.assertEqual(investment.category, "fund")
        self.assertEqual(investment.screener_type, self.screener.name)

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_applies_market_cap_argument(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Example"}}]},
            text="{}",
        )

        buffer = StringIO()
        call_command(
            "fetch_screener_results",
            screener_name=self.screener.name,
            market_cap="10B",
            stdout=buffer,
        )

        _, kwargs = mock_post.call_args
        self.assertIn("json", kwargs)
        payload = kwargs["json"]
        self.assertEqual(payload.get("field"), "market_cap")
        self.assertEqual(payload.get("operator"), ">=")
        self.assertEqual(payload.get("value"), 500_000_000)
        self.assertIn("marketcap_display", payload)
        self.assertEqual(payload["marketcap_display"].get("gte"), 10_000_000_000)

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_applies_price_arguments(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Sample"}}]},
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=self.screener.name,
            min_price="10",
            max_price="25.5",
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertIn("close", payload)
        self.assertEqual(payload["close"].get("gte"), 10.0)
        self.assertEqual(payload["close"].get("lte"), 25.5)

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_does_not_merge_custom_filter_for_standard_screeners(
        self, mock_post: MagicMock
    ) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Sample"}}]},
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=self.screener.name,
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]

        self.assertEqual(
            payload,
            {"field": "market_cap", "operator": ">=", "value": 500_000_000},
        )
        self.assertNotIn("value_category", payload)

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_includes_custom_filter_with_overrides_for_custom_screener(
        self, mock_post: MagicMock
    ) -> None:
        custom_screener = ScreenerType.objects.create(
            name="Custom screener filter", description="Custom filter payload only."
        )
        ScreenerFilter.objects.create(
            screener_type=custom_screener,
            label="Base filters",
            payload={"close": {"lte": 50}},
            display_order=1,
        )

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Sample"}}]},
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=custom_screener.name,
            market_cap="7B",
            min_price="15",
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]

        self.assertEqual(payload.get("exchange"), CUSTOM_FILTER_PAYLOAD["exchange"])
        self.assertEqual(payload.get("altman_z_score"), CUSTOM_FILTER_PAYLOAD["altman_z_score"])
        self.assertIn("close", payload)
        self.assertEqual(payload["close"].get("lte"), 50)
        self.assertEqual(payload["close"].get("gte"), 15.0)
        self.assertEqual(payload["marketcap_display"].get("gte"), 7_000_000_000)

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_includes_custom_filter_v2(self, mock_post: MagicMock) -> None:
        custom_screener = ScreenerType.objects.create(
            name="Custom screener filterV2",
            description="Custom filter payload V2 only.",
        )
        ScreenerFilter.objects.create(
            screener_type=custom_screener,
            label="Base filters",
            payload={"close": {"lte": 25}},
            display_order=1,
        )

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Sample"}}]},
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=custom_screener.name,
            market_cap="6B",
            min_price="10",
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]

        self.assertEqual(payload.get("exchange"), CUSTOM_FILTER_PAYLOAD_V2["exchange"])
        self.assertIn("marketcap_display", payload)
        self.assertEqual(payload["marketcap_display"].get("gte"), 6_000_000_000)
        self.assertIn("quant_rating", payload)
        self.assertNotIn("altman_z_score", payload)
        self.assertEqual(payload["close"].get("lte"), 25)
        self.assertEqual(payload["close"].get("gte"), 10.0)

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_removes_industry_id_for_quant_screener(
        self, mock_post: MagicMock
    ) -> None:
        quant_screener = ScreenerType.objects.create(
            name="Stocks by Quant", description="Quant focused filters."
        )
        ScreenerFilter.objects.create(
            screener_type=quant_screener,
            label="Quant filters",
            payload={
                "quant_rating": {"in": ["strong_buy", "buy"]},
                "industry_id": {"in": [1, 2], "exclude": False},
                "close": {"gte": 30.0, "lte": 160.0},
            },
            display_order=1,
        )

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Sample"}}]},
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=quant_screener.name,
            market_cap="5B",
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertNotIn("industry_id", payload)
        self.assertIn("quant_rating", payload)
        self.assertEqual(payload.get("close"), {"gte": 30.0, "lte": 160.0})

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_updates_nested_filter_section(self, mock_post: MagicMock) -> None:
        nested_screener = ScreenerType.objects.create(
            name="Energy Focus", description="Composite filter payload."
        )
        ScreenerFilter.objects.create(
            screener_type=nested_screener,
            label="Energy Sector",
            payload={
                "filter": {
                    "asset_primary_sector": {"eq": "Energy"},
                    "marketcap_display": {"gte": 750_000_000},
                    "close": {"lte": 50.0},
                }
            },
            display_order=1,
        )

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Sample"}}]},
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=nested_screener.name,
            market_cap="5B",
            min_price="12",
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertIn("filter", payload)
        self.assertIn("asset_primary_sector", payload["filter"])
        self.assertEqual(payload["filter"]["asset_primary_sector"].get("eq"), "Energy")
        self.assertIn("marketcap_display", payload["filter"])
        self.assertEqual(payload["filter"]["marketcap_display"].get("gte"), 5_000_000_000)
        self.assertIn("close", payload["filter"])
        self.assertEqual(payload["filter"]["close"].get("lte"), 50.0)
        self.assertEqual(payload["filter"]["close"].get("gte"), 12.0)

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_limits_quant_rating_value(self, mock_post: MagicMock) -> None:
        quant_screener = ScreenerType.objects.create(
            name="Quant Focus", description="Quant driven filters."
        )
        ScreenerFilter.objects.create(
            screener_type=quant_screener,
            label="Quant Rating",
            payload={"filter": {"quant_rating": ["strong_buy", "buy"]}},
            display_order=1,
        )

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Sample"}}]},
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=quant_screener.name,
            quant_rating="strong_buy",
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]

        self.assertIn("filter", payload)
        self.assertEqual(
            payload["filter"].get("quant_rating"), {"in": ["strong_buy"]}
        )

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_overrides_missing_quant_rating_value(
        self, mock_post: MagicMock
    ) -> None:
        quant_screener = ScreenerType.objects.create(
            name="Quant Focus", description="Quant driven filters."
        )
        ScreenerFilter.objects.create(
            screener_type=quant_screener,
            label="Quant Rating",
            payload={"filter": {"quant_rating": ["buy"]}},
            display_order=1,
        )

        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Sample"}}]},
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=quant_screener.name,
            quant_rating="strong_buy",
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]

        self.assertIn("filter", payload)
        self.assertEqual(
            payload["filter"].get("quant_rating"), {"in": ["strong_buy"]}
        )

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_overrides_quant_rating_from_custom_filter(
        self, mock_post: MagicMock
    ) -> None:
        custom_screener = ScreenerType.objects.create(
            name="Custom screener filter", description="Custom filter payload only."
        )
        ScreenerFilter.objects.create(
            screener_type=custom_screener,
            label="Base filters",
            payload={},
            display_order=1,
        )
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Sample"}}]},
            text="{}",
        )

        call_command(
            "fetch_screener_results",
            screener_name=custom_screener.name,
            quant_rating="strong_buy",
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]

        self.assertIn("quant_rating", payload)
        self.assertEqual(payload["quant_rating"], {"in": ["strong_buy"]})

    def test_command_rejects_invalid_market_cap_argument(self) -> None:
        with self.assertRaisesMessage(CommandError, "Market cap value must be a number optionally followed by K, M, B, or T."):
            call_command(
                "fetch_screener_results",
                screener_name=self.screener.name,
                market_cap="ten-billion",
            )

    def test_command_rejects_invalid_price_arguments(self) -> None:
        with self.assertRaisesMessage(CommandError, "Price filters must be numeric values."):
            call_command(
                "fetch_screener_results",
                screener_name=self.screener.name,
                min_price="ten",
            )

        with self.assertRaisesMessage(CommandError, "Minimum price cannot be greater than maximum price."):
            call_command(
                "fetch_screener_results",
                screener_name=self.screener.name,
                min_price="50",
                max_price="10",
            )

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_errors_when_no_names_present(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"p": {}}}]},
            text="{}",
        )

        with self.assertRaisesMessage(
            CommandError, "Seeking Alpha API response did not include any ticker names."
        ):
            call_command(
                "fetch_screener_results", screener_name=self.screener.name
            )


class FetchProfileDataCommandTests(APITestCase):

    def setUp(self) -> None:
        self.screener_name = "Growth"
        self.investments = [
            Investment.objects.create(ticker="AAA", category="stock"),
            Investment.objects.create(ticker="BBB", category="stock"),
            Investment.objects.create(ticker="CCC", category="stock"),
        ]

    def _build_next_month_dates(self, days: list[int]) -> list[str]:
        today = date.today()
        if today.month == 12:
            month = 1
            year = today.year + 1
        else:
            month = today.month + 1
            year = today.year

        return [f"{month:02d}/{day:02d}/{year}" for day in days]

    def _parse_date(self, value: str) -> date:
        return datetime.strptime(value, "%m/%d/%Y").date()

    def _expected_option_expiration(self, dates: list[str]) -> date | None:
        today = date.today()
        window_start = today + timedelta(days=20)
        window_end = today + timedelta(days=40)
        parsed = [self._parse_date(value) for value in dates]
        expirations_in_window = [
            expiration
            for expiration in parsed
            if window_start <= expiration <= window_end
        ]
        return max(expirations_in_window) if expirations_in_window else None

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_sets_option_exp_with_three_expirations(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_expirations.return_value = {
            "dates": self._build_next_month_dates([5, 12, 19]),
            "ticker_id": "AAA",
        }
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"ticker": "AAA", "weekly_options": True}],
            text="{}",
        )

        call_command("fetch_profile_data", screener_name=self.screener_name)
        investment = Investment.objects.get(ticker="AAA")
        expected_expiration = self._expected_option_expiration(
            mock_expirations.return_value["dates"]
        )
        self.assertEqual(investment.option_exp, expected_expiration)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_sets_option_exp_with_fewer_than_three_expirations(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_expirations.return_value = {
            "dates": self._build_next_month_dates([5, 12]),
            "ticker_id": "AAA",
        }
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"ticker": "AAA", "weekly_options": True}],
            text="{}",
        )

        call_command("fetch_profile_data", screener_name=self.screener_name)
        investment = Investment.objects.get(ticker="AAA")
        expected_expiration = self._expected_option_expiration(
            mock_expirations.return_value["dates"]
        )
        self.assertEqual(investment.option_exp, expected_expiration)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_clears_option_exp_with_no_expirations(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_expirations.return_value = {"dates": [], "ticker_id": "AAA"}
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"ticker": "AAA", "weekly_options": True}],
            text="{}",
        )

        call_command("fetch_profile_data", screener_name=self.screener_name)
        investment = Investment.objects.get(ticker="AAA")
        self.assertIsNone(investment.option_exp)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_creates_missing_investments(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"ticker": "NEW1", "weekly_options": True},
                {"ticker": "NEW2", "weekly_options": True},
            ],
            text="{}",
        )
        mock_expirations.return_value = {"dates": [], "ticker_id": "NEW"}

        buffer = StringIO()
        call_command("fetch_profile_data", screener_name=self.screener_name, stdout=buffer)

        for ticker in ("NEW1", "NEW2"):
            investment = Investment.objects.get(ticker=ticker)
            self.assertEqual(investment.category, "stock")
            self.assertIsNone(investment.price)
            self.assertIsNone(investment.market_cap)

        output = buffer.getvalue()
        self.assertIn("Created investment NEW1", output)
        self.assertIn("Created investment NEW2", output)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_sets_investment_id_to_ticker_id_on_create(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"ticker": "NEW1", "weekly_options": True},
            ],
            text="{}",
        )
        mock_expirations.return_value = {"dates": [], "ticker_id": "1105"}

        call_command("fetch_profile_data", screener_name=self.screener_name)

        investment = Investment.objects.get(ticker="NEW1")
        self.assertEqual(investment.id, 1105)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_updates_investment_id_when_missing(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"ticker": "AAA", "weekly_options": True}],
            text="{}",
        )
        mock_expirations.return_value = {"dates": [], "ticker_id": "9999"}

        call_command("fetch_profile_data", screener_name=self.screener_name)

        investment = Investment.objects.get(ticker="AAA")
        self.assertEqual(investment.id, 9999)

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_errors_on_unsuccessful_response(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=500, text="error")

        with self.assertRaises(CommandError):
            call_command("fetch_profile_data", screener_name=self.screener_name)

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_can_skip_investments_with_price(self, mock_get: MagicMock) -> None:
        Investment.objects.filter(ticker="AAA").update(price=Decimal("5.00"))
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"ticker": "AAA", "weekly_options": True},
                {"ticker": "BBB", "weekly_options": True},
            ],
            text="{}",
        )

        with patch(
            "api.management.commands.fetch_profile_data.Command._fetch_option_expirations",
            return_value={"dates": [], "ticker_id": "BBB"},
        ) as mock_expirations:
            buffer = StringIO()
            call_command(
                "fetch_profile_data",
                "--skip-priced",
                screener_name=self.screener_name,
                stdout=buffer,
            )

        self.assertEqual(mock_expirations.call_args_list, [call("BBB")])
        output = buffer.getvalue()
        self.assertNotIn("AAA", output)
        self.assertIn("BBB", output)

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_can_skip_investments_with_price_no_remaining(
        self, mock_get: MagicMock
    ) -> None:
        Investment.objects.filter(ticker="AAA").update(price=Decimal("5.00"))
        Investment.objects.filter(ticker="BBB").update(price=Decimal("7.00"))
        Investment.objects.filter(ticker="CCC").update(price=Decimal("9.00"))

        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"ticker": "AAA", "weekly_options": True},
                {"ticker": "BBB", "weekly_options": True},
                {"ticker": "CCC", "weekly_options": True},
            ],
            text="{}",
        )

        with self.assertRaisesMessage(
            CommandError, "No tickers remain to update after skipping priced investments."
        ):
            call_command(
                "fetch_profile_data", "--skip-priced", screener_name=self.screener_name
            )

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_fetches_only_requested_screener(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"ticker": "AAA", "weekly_options": True}],
            text="{}",
        )
        mock_expirations.return_value = {"dates": [], "ticker_id": "AAA"}

        call_command("fetch_profile_data", screener_name=self.screener_name)

        mock_get.assert_called_once()
        self.assertEqual(
            mock_get.call_args.kwargs.get("params"),
            {"screener_type": self.screener_name},
        )

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_fetch_option_expirations_uses_expected_headers_and_params(
        self, mock_get: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": {"attributes": {"dates": [], "ticker_id": 1}}},
            text="{}",
        )

        command = Command()
        command._fetch_option_expirations("XYZ")

        mock_get.assert_called_once()
        request = mock_get.call_args
        self.assertEqual(request.args[0], OPTION_EXPIRATIONS_ENDPOINT)
        self.assertEqual(request.kwargs.get("params"), {"symbol": "XYZ"})
        self.assertEqual(request.kwargs.get("headers"), API_HEADERS)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_skips_option_fetch_for_non_weekly(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"ticker": "AAA", "weekly_options": True},
                {"ticker": "BBB", "weekly_options": False},
            ],
            text="{}",
        )
        mock_expirations.return_value = {
            "dates": self._build_next_month_dates([5, 12, 19]),
            "ticker_id": "AAA",
        }

        call_command("fetch_profile_data", screener_name=self.screener_name)

        mock_expirations.assert_called_once_with("AAA")
        investment = Investment.objects.get(ticker="BBB")
        self.assertIsNone(investment.option_exp)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_fetches_options_for_string_weekly_flag(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"ticker": "AAA", "weekly_options": "true"}],
            text="{}",
        )
        mock_expirations.return_value = {
            "dates": self._build_next_month_dates([5]),
            "ticker_id": "AAA",
        }

        call_command("fetch_profile_data", screener_name=self.screener_name)

        mock_expirations.assert_called_once_with("AAA")
        investment = Investment.objects.get(ticker="AAA")
        self.assertIsNotNone(investment.option_exp)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_fetches_options_when_weekly_missing(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"ticker": "AAA"}],
            text="{}",
        )
        mock_expirations.return_value = {
            "dates": self._build_next_month_dates([5]),
            "ticker_id": "AAA",
        }

        call_command("fetch_profile_data", screener_name=self.screener_name)

        mock_expirations.assert_called_once_with("AAA")
        investment = Investment.objects.get(ticker="AAA")
        self.assertIsNotNone(investment.option_exp)


class PutCheckerCommandTests(APITestCase):
    """Tests for the put_checker management command."""

    _CMD = "api.management.commands.put_checker.Command"

    def setUp(self) -> None:
        self.symbol = Symbol.objects.create(
            ticker="AAPL",
            score=80,
            price=Decimal("100.00"),
            rsi=Decimal("45.00"),
        )

    def _option_exp_in_window(self) -> date:
        """Return a date 25 days from today (within the 15-30 day window)."""
        return date.today() + timedelta(days=25)

    def _put_option(
        self, strike: float = 95.0, bid: float = 3.0, ask: float = 4.0
    ) -> dict:
        return {
            "option_type": "put",
            "strike_price": strike,
            "bid": bid,
            "ask": ask,
            "last_price": (bid + ask) / 2,
            "volume": 200,
            "open_interest": 1000,
            "contract_symbol": "AAPL250101P00095000",
            "implied_volatility": None,
        }

    # Early-exit paths

    def test_no_qualifying_symbols_returns_early(self) -> None:
        """Command exits silently when no Symbol has score >= 75."""
        Symbol.objects.all().delete()
        buffer = StringIO()
        result = call_command("put_checker", stdout=buffer)
        self.assertEqual(result, "")
        self.assertEqual(buffer.getvalue(), "")

    def test_rsi_filter_excludes_symbol_with_high_rsi(self) -> None:
        """--rsi flag skips symbols whose RSI is outside 30-70."""
        self.symbol.rsi = Decimal("85.00")
        self.symbol.save()
        buffer = StringIO()
        result = call_command("put_checker", rsi=True, stdout=buffer)
        self.assertEqual(result, "")

    # price + RSI refresh

    @patch("api.management.commands.put_checker.Command._fetch_risk_free_rate")
    @patch("api.management.commands.put_checker.Command._ensure_option_expiration")
    @patch("api.management.commands.put_checker.Command._fetch_next_earnings_date")
    @patch("api.management.commands.put_checker.Command._fetch_price_and_rsi")
    def test_price_and_rsi_fetched_and_saved_before_option_processing(
        self,
        mock_pnr: MagicMock,
        mock_earnings: MagicMock,
        mock_ensure_exp: MagicMock,
        mock_rfr: MagicMock,
    ) -> None:
        """Fresh price and RSI are persisted on Symbol before the option chain is checked."""
        mock_pnr.return_value = (Decimal("110.00"), Decimal("55.00"))
        mock_earnings.return_value = None
        mock_ensure_exp.return_value = None  # forces skipped_missing_exp -> CommandError
        mock_rfr.return_value = Decimal("0.0450")

        with self.assertRaises(CommandError):
            call_command("put_checker")

        mock_pnr.assert_called_once_with("AAPL")
        self.symbol.refresh_from_db()
        self.assertEqual(self.symbol.price, Decimal("110.00"))
        self.assertEqual(self.symbol.rsi, Decimal("55.00"))

    @patch("api.management.commands.put_checker.Command._fetch_risk_free_rate")
    @patch("api.management.commands.put_checker.Command._ensure_option_expiration")
    @patch("api.management.commands.put_checker.Command._fetch_next_earnings_date")
    @patch("api.management.commands.put_checker.Command._fetch_price_and_rsi")
    def test_unchanged_price_and_rsi_not_saved(
        self,
        mock_pnr: MagicMock,
        mock_earnings: MagicMock,
        mock_ensure_exp: MagicMock,
        mock_rfr: MagicMock,
    ) -> None:
        """Symbol is not re-saved when the fetched values match what is already stored."""
        mock_pnr.return_value = (Decimal("100.00"), Decimal("45.00"))
        mock_earnings.return_value = None
        mock_ensure_exp.return_value = None
        mock_rfr.return_value = Decimal("0.0450")

        original_updated_at = self.symbol.updated_at

        with self.assertRaises(CommandError):
            call_command("put_checker")

        self.symbol.refresh_from_db()
        self.assertEqual(self.symbol.updated_at, original_updated_at)

    # Skip conditions

    @patch("api.management.commands.put_checker.Command._fetch_risk_free_rate")
    @patch("api.management.commands.put_checker.Command._ensure_option_expiration")
    @patch("api.management.commands.put_checker.Command._fetch_next_earnings_date")
    @patch("api.management.commands.put_checker.Command._fetch_price_and_rsi")
    def test_skips_symbol_with_no_price(
        self,
        mock_pnr: MagicMock,
        mock_earnings: MagicMock,
        mock_ensure_exp: MagicMock,
        mock_rfr: MagicMock,
    ) -> None:
        """Symbol without a price (and whose fetch also returns None) is skipped."""
        self.symbol.price = None
        self.symbol.save()
        mock_pnr.return_value = (None, None)
        mock_earnings.return_value = None
        mock_ensure_exp.return_value = self._option_exp_in_window()
        mock_rfr.return_value = Decimal("0.0450")

        with self.assertRaises(CommandError):
            call_command("put_checker")

        self.symbol.refresh_from_db()
        self.assertIsNone(self.symbol.price)

    # Happy path

    @patch("api.management.commands.put_checker.Command._fetch_risk_free_rate")
    @patch("api.management.commands.put_checker.Command._fetch_options_yfinance")
    @patch("api.management.commands.put_checker.Command._ensure_option_expiration")
    @patch("api.management.commands.put_checker.Command._fetch_next_earnings_date")
    @patch("api.management.commands.put_checker.Command._fetch_price_and_rsi")
    def test_happy_path_updates_strike_price_and_prints_summary(
        self,
        mock_pnr: MagicMock,
        mock_earnings: MagicMock,
        mock_ensure_exp: MagicMock,
        mock_fetch_opts: MagicMock,
        mock_rfr: MagicMock,
    ) -> None:
        """When a valid put option is found the symbol strike_price is saved and a
        summary is printed.

        Calibration: spot=100, strike=95, bid=3.0, ask=4.0, T=25/365 yr, rfr=4.5%.
        Black-Scholes gives IV~50%, delta~-0.32 (in range), ROI~3.68% (above 2%).
        """
        mock_pnr.return_value = (Decimal("100.00"), Decimal("45.00"))
        mock_earnings.return_value = None
        mock_ensure_exp.return_value = self._option_exp_in_window()
        mock_fetch_opts.return_value = [self._put_option(strike=95.0, bid=3.0, ask=4.0)]
        mock_rfr.return_value = Decimal("0.0450")

        buffer = StringIO()
        call_command("put_checker", stdout=buffer)

        self.symbol.refresh_from_db()
        self.assertIsNotNone(self.symbol.option_data)
        self.assertEqual(self.symbol.option_data.get("strike_price"), 95.0)
        output = buffer.getvalue()
        self.assertIn("AAPL", output)
        self.assertIn("ROI", output)

    # No ROI candidates

    @patch("api.management.commands.put_checker.Command._fetch_risk_free_rate")
    @patch("api.management.commands.put_checker.Command._fetch_options_yfinance")
    @patch("api.management.commands.put_checker.Command._ensure_option_expiration")
    @patch("api.management.commands.put_checker.Command._fetch_next_earnings_date")
    @patch("api.management.commands.put_checker.Command._fetch_price_and_rsi")
    def test_no_roi_candidates_raises_command_error(
        self,
        mock_pnr: MagicMock,
        mock_earnings: MagicMock,
        mock_ensure_exp: MagicMock,
        mock_fetch_opts: MagicMock,
        mock_rfr: MagicMock,
    ) -> None:
        """CommandError is raised when no put option meets the ROI threshold."""
        mock_pnr.return_value = (Decimal("100.00"), Decimal("45.00"))
        mock_earnings.return_value = None
        mock_ensure_exp.return_value = self._option_exp_in_window()
        # bid=0.01, ask=0.02 -> ROI=0.016% (well below the 2% threshold)
        mock_fetch_opts.return_value = [self._put_option(strike=95.0, bid=0.01, ask=0.02)]
        mock_rfr.return_value = Decimal("0.0450")

        with self.assertRaises(CommandError):
            call_command("put_checker")

    # seeking_alpha_ticker_id persistence

    @patch("api.management.commands.put_checker.Command._fetch_risk_free_rate")
    @patch("api.management.commands.put_checker.Command._fetch_option_expirations")
    @patch("api.management.commands.put_checker.Command._fetch_option_expirations_yfinance")
    @patch("api.management.commands.put_checker.Command._fetch_next_earnings_date")
    @patch("api.management.commands.put_checker.Command._fetch_price_and_rsi")
    def test_seeking_alpha_ticker_id_saved_from_api(
        self,
        mock_pnr: MagicMock,
        mock_earnings: MagicMock,
        mock_exps_yf: MagicMock,
        mock_exps_api: MagicMock,
        mock_rfr: MagicMock,
    ) -> None:
        """When the Seeking Alpha expiration API returns a ticker_id it is saved on Symbol."""
        exp_date = self._option_exp_in_window()
        mock_pnr.return_value = (Decimal("100.00"), Decimal("45.00"))
        mock_earnings.return_value = None
        mock_exps_yf.return_value = []  # force rapidapi path
        mock_exps_api.return_value = {
            "ticker_id": 99999,
            "dates": [exp_date.strftime("%m/%d/%Y")],
        }
        mock_rfr.return_value = Decimal("0.0450")

        # The command resolves the expiration then fails fetching the option chain
        # (no further network mocks); ticker_id should already be saved before that.
        try:
            call_command("put_checker")
        except (CommandError, Exception):
            pass

        self.symbol.refresh_from_db()
        self.assertEqual(self.symbol.seeking_alpha_ticker_id, 99999)


class SymbolAPITestCase(APITestCase):
    def setUp(self) -> None:
        self.list_url = reverse("symbol-list")

    def test_list_can_filter_by_technical_score(self) -> None:
        Symbol.objects.create(
            ticker="STRONG",
            technical_score=Symbol.TechnicalScore.STRONG_BUY,
        )
        Symbol.objects.create(
            ticker="BUY",
            technical_score=Symbol.TechnicalScore.BUY,
        )
        Symbol.objects.create(
            ticker="EMPTY",
            technical_score=None,
        )

        response = self.client.get(
            self.list_url,
            {"technical_score": "strong buy"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["ticker"] for item in response.data], ["STRONG"])

    def test_list_can_filter_by_multiple_technical_scores(self) -> None:
        Symbol.objects.create(
            ticker="STRONG",
            technical_score=Symbol.TechnicalScore.STRONG_BUY,
        )
        Symbol.objects.create(
            ticker="BUY",
            technical_score=Symbol.TechnicalScore.BUY,
        )
        Symbol.objects.create(
            ticker="NEUTRAL",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
        )
        Symbol.objects.create(
            ticker="SELL",
            technical_score=Symbol.TechnicalScore.SELL,
        )

        response = self.client.get(
            self.list_url,
            {"technical_score_in": "Neutral,Buy,Strong Buy"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["ticker"] for item in response.data],
            ["BUY", "NEUTRAL", "STRONG"],
        )

    def test_list_can_filter_by_option_volume_and_iv(self) -> None:
        Symbol.objects.create(
            ticker="LOW",
            option_volume=100,
            option_iv=Decimal("12.3400"),
        )
        Symbol.objects.create(
            ticker="MATCH",
            option_volume=500,
            option_iv=Decimal("25.5000"),
        )
        Symbol.objects.create(
            ticker="HIGH",
            option_volume=1000,
            option_iv=Decimal("44.0000"),
        )

        response = self.client.get(
            self.list_url,
            {
                "min_option_volume": "400",
                "max_option_volume": "900",
                "min_option_iv": "20",
                "max_option_iv": "30",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["ticker"] for item in response.data], ["MATCH"])


@override_settings(FINANCIAL_MODELING_API_KEY="test-fmp-key")
class TradingViewScrapeCommandTests(APITestCase):
    def setUp(self) -> None:
        self.symbol = Symbol.objects.create(
            ticker="AAPL",
            exchange="NASDAQ",
            score=80,
            price=Decimal("100.00"),
        )
        self.command = TradingViewCommand()
        self.price_client = MagicMock()
        self.price_client.get_underlying_price.return_value = "100.00"

    def _expiration_int(self, days: int = 30) -> int:
        return int((date.today() + timedelta(days=days)).strftime("%Y%m%d"))

    def _option_row(
        self,
        *,
        strike: str,
        bid: str,
        ask: str,
        delta: str,
        option_symbol: str,
        volume: str = "150",
        iv: str = "24.1250",
    ) -> dict[str, object]:
        return {
            "option_symbol": option_symbol,
            "option_type": "put",
            "strike_price": strike,
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "delta": delta,
            "volume": volume,
            "iv": iv,
        }

    def test_get_monthly_rsi_uses_tradingview_symbol_endpoint(self) -> None:
        session = MagicMock()
        session.request.return_value = MagicMock(
            json=MagicMock(return_value={"RSI|1M": 57.32}),
            status_code=200,
        )

        rsi = TradingViewOptions(session=session).get_monthly_rsi("NASDAQ", "AAPL")

        self.assertEqual(rsi, 57.32)
        session.request.assert_called_once_with(
            "GET",
            "https://scanner.tradingview.com/symbol",
            params={
                "symbol": "NASDAQ:AAPL",
                "fields": "RSI|1M",
                "no_404": "true",
                "label-product": "popup-technicals",
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://www.tradingview.com/symbols/NASDAQ-AAPL/technicals/",
            },
            timeout=30,
        )

    @patch("api.management.commands.trading_view_scrape.certifi.where", return_value="dummy.pem")
    @patch("api.management.commands.trading_view_scrape.time.sleep")
    @patch("api.management.commands.trading_view_scrape.urllib.request.urlopen")
    def test_fmp_client_retries_after_429(
        self,
        mock_urlopen: MagicMock,
        mock_sleep: MagicMock,
        _mock_certifi_where: MagicMock,
    ) -> None:
        too_many_requests = urllib.error.HTTPError(
            "https://financialmodelingprep.com/stable/quote-short?symbol=AAPL&apikey=test-fmp-key",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            None,
        )
        success_response = MagicMock()
        success_response.read.return_value = b'[{"symbol":"AAPL","price":100.0}]'
        success_context = MagicMock()
        success_context.__enter__.return_value = success_response
        success_context.__exit__.return_value = None
        mock_urlopen.side_effect = [too_many_requests, success_context]

        price = FinancialModelingPrepClient(api_key="test-fmp-key").get_underlying_price("AAPL")

        self.assertEqual(price, 100.0)
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(2)

    @patch("api.management.commands.trading_view_scrape.certifi.where", return_value="dummy.pem")
    @patch("api.management.commands.trading_view_scrape.urllib.request.urlopen")
    def test_fmp_client_uses_exact_symbol_match(
        self,
        mock_urlopen: MagicMock,
        _mock_certifi_where: MagicMock,
    ) -> None:
        response = MagicMock()
        response.read.return_value = (
            b'[{"symbol":"AAP","price":12.34},{"symbol":"AAPL","price":100.0}]'
        )
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = None
        mock_urlopen.return_value = context

        price = FinancialModelingPrepClient(api_key="test-fmp-key").get_underlying_price("AAPL")

        self.assertEqual(price, 100.0)

    @patch("api.management.commands.trading_view_scrape.certifi.where", return_value="dummy.pem")
    @patch("api.management.commands.trading_view_scrape.urllib.request.urlopen")
    def test_fmp_client_rejects_missing_exact_symbol_match(
        self,
        mock_urlopen: MagicMock,
        _mock_certifi_where: MagicMock,
    ) -> None:
        response = MagicMock()
        response.read.return_value = b'[{"symbol":"AAP","price":12.34}]'
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = None
        mock_urlopen.return_value = context

        with self.assertRaisesMessage(
            ValueError,
            "No exact quote match returned for AAPL",
        ):
            FinancialModelingPrepClient(api_key="test-fmp-key").get_underlying_price("AAPL")

    @patch("api.management.commands.trading_view_scrape.time.sleep")
    @patch("api.management.commands.trading_view_scrape.time.monotonic")
    def test_request_rate_limiter_backoff_blocks_subsequent_requests(
        self, mock_monotonic: MagicMock, mock_sleep: MagicMock
    ) -> None:
        mock_monotonic.side_effect = [10.0, 11.0, 12.0]
        rate_limiter = RequestRateLimiter(2.0)

        rate_limiter.backoff(2.0)
        rate_limiter.wait()

        mock_sleep.assert_called_once_with(1.0)

    def test_handle_rejects_non_positive_worker_count(self) -> None:
        with self.assertRaisesMessage(CommandError, "--workers must be at least 1."):
            call_command("trading_view_scrape", "--workers", "0")

    def test_handle_rejects_non_positive_max_rps(self) -> None:
        with self.assertRaisesMessage(CommandError, "--max-rps must be greater than 0."):
            call_command("trading_view_scrape", "--max-rps", "0")

    @patch("api.management.commands.trading_view_scrape.Command._process_symbol")
    def test_handle_processes_symbols_with_worker_pool(
        self, mock_process_symbol: MagicMock
    ) -> None:
        Symbol.objects.create(ticker="MSFT", exchange="NASDAQ", score=81)
        mock_process_symbol.return_value = (True, False)
        stdout = StringIO()
        stderr = StringIO()

        call_command("trading_view_scrape", "--workers", "1", stdout=stdout, stderr=stderr)

        self.assertEqual(mock_process_symbol.call_count, 2)
        self.assertEqual(
            sorted(call.kwargs["symbol"].ticker for call in mock_process_symbol.call_args_list),
            ["AAPL", "MSFT"],
        )
        for process_call in mock_process_symbol.call_args_list:
            self.assertIn("reporter", process_call.kwargs)
            self.assertTrue(process_call.kwargs["fetch_rsi"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(
            "Processed 2 symbols | updated 2 | cleared 0 | errors 0",
            stdout.getvalue(),
        )

    def test_process_symbol_updates_rsi_from_tradingview(self) -> None:
        expiration = self._expiration_int()
        client = MagicMock()
        client.get_monthly_rsi.return_value = "55.75"
        client.get_expirations.return_value = [expiration]
        client.get_chain.return_value = [
            self._option_row(
                strike="95",
                bid="3.40",
                ask="3.60",
                delta="-0.34",
                option_symbol="AAPL_MAIN",
            )
        ]

        changed, was_cleared = self.command._process_symbol(
            client=client,
            price_client=self.price_client,
            symbol=self.symbol,
            exchange="NASDAQ",
            min_dte=25,
            max_dte=40,
            delta_min=Decimal("-0.37"),
            delta_max=Decimal("-0.24"),
            roi_threshold=Decimal("2"),
            fetch_rsi=True,
        )

        self.assertTrue(changed)
        self.assertFalse(was_cleared)

        self.symbol.refresh_from_db()
        self.assertEqual(self.symbol.rsi, Decimal("55.75"))
        self.assertEqual(self.symbol.option_volume, 150)
        self.assertEqual(self.symbol.option_iv, Decimal("24.1250"))
        client.get_monthly_rsi.assert_called_once_with(exchange="NASDAQ", ticker="AAPL")

    def test_process_symbol_reports_429_with_operator_guidance(self) -> None:
        client = MagicMock()
        self.price_client.get_underlying_price.side_effect = urllib.error.HTTPError(
            "https://financialmodelingprep.com/stable/quote-short?symbol=AAPL&apikey=test-fmp-key",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            None,
        )

        with self.assertRaisesMessage(
            CommandError,
            "Financial Modeling Prep request failed for AAPL: rate limited by Financial Modeling Prep (HTTP 429). Check your API quota or retry later.",
        ):
            self.command._process_symbol(
                client=client,
                price_client=self.price_client,
                symbol=self.symbol,
                exchange="NASDAQ",
                min_dte=25,
                max_dte=40,
                delta_min=Decimal("-0.37"),
                delta_max=Decimal("-0.24"),
                roi_threshold=Decimal("2"),
                fetch_rsi=True,
            )

    def test_process_symbol_keeps_smaller_abs_delta_alternatives(self) -> None:
        expiration = self._expiration_int()
        client = MagicMock()
        client.get_monthly_rsi.return_value = "55.75"
        client.get_expirations.return_value = [expiration]
        client.get_chain.return_value = [
            self._option_row(
                strike="95",
                bid="3.40",
                ask="3.60",
                delta="-0.34",
                option_symbol="AAPL_MAIN",
            ),
            self._option_row(
                strike="94",
                bid="2.80",
                ask="3.00",
                delta="-0.31",
                option_symbol="AAPL_ALT_1",
            ),
            self._option_row(
                strike="93",
                bid="2.20",
                ask="2.40",
                delta="-0.28",
                option_symbol="AAPL_ALT_2",
            ),
            self._option_row(
                strike="92",
                bid="3.00",
                ask="3.20",
                delta="-0.36",
                option_symbol="AAPL_RISKIER",
            ),
        ]

        changed, was_cleared = self.command._process_symbol(
            client=client,
            price_client=self.price_client,
            symbol=self.symbol,
            exchange="NASDAQ",
            min_dte=25,
            max_dte=40,
            delta_min=Decimal("-0.37"),
            delta_max=Decimal("-0.24"),
            roi_threshold=Decimal("2"),
            fetch_rsi=True,
        )

        self.assertTrue(changed)
        self.assertFalse(was_cleared)

        self.symbol.refresh_from_db()
        self.assertEqual(self.symbol.option_data["option_symbol"], "AAPL_MAIN")
        self.assertEqual(self.symbol.option_data["strike_price"], 95.0)
        self.assertEqual(self.symbol.option_volume, 150)
        self.assertEqual(self.symbol.option_iv, Decimal("24.1250"))
        self.assertEqual(
            [item["option_symbol"] for item in self.symbol.option_data["alternatives"]],
            ["AAPL_ALT_1", "AAPL_ALT_2"],
        )
        self.assertEqual(
            [item["delta"] for item in self.symbol.option_data["alternatives"]],
            [-0.31, -0.28],
        )

    def test_process_symbol_clears_option_volume_and_iv_without_roi_candidate(self) -> None:
        expiration = self._expiration_int()
        self.symbol.option_volume = 750
        self.symbol.option_iv = Decimal("33.3300")
        self.symbol.option_data = {"option_symbol": "OLD"}
        self.symbol.roi = Decimal("3.10")
        self.symbol.save(
            update_fields=["option_volume", "option_iv", "option_data", "roi", "updated_at"]
        )

        client = MagicMock()
        client.get_monthly_rsi.return_value = "55.75"
        client.get_expirations.return_value = [expiration]
        client.get_chain.return_value = [
            self._option_row(
                strike="95",
                bid="0.10",
                ask="0.20",
                delta="-0.34",
                option_symbol="AAPL_LOW_ROI",
            )
        ]

        changed, was_cleared = self.command._process_symbol(
            client=client,
            price_client=self.price_client,
            symbol=self.symbol,
            exchange="NASDAQ",
            min_dte=25,
            max_dte=40,
            delta_min=Decimal("-0.37"),
            delta_max=Decimal("-0.24"),
            roi_threshold=Decimal("2"),
            fetch_rsi=True,
        )

        self.assertTrue(changed)
        self.assertTrue(was_cleared)

        self.symbol.refresh_from_db()
        self.assertIsNone(self.symbol.option_volume)
        self.assertIsNone(self.symbol.option_iv)
        self.assertIsNone(self.symbol.option_data)
        self.assertIsNone(self.symbol.roi)

    def test_process_symbol_can_skip_rsi_fetch_and_preserve_existing_value(self) -> None:
        expiration = self._expiration_int()
        self.symbol.rsi = Decimal("41.25")
        self.symbol.save(update_fields=["rsi", "updated_at"])

        client = MagicMock()
        client.get_expirations.return_value = [expiration]
        client.get_chain.return_value = [
            self._option_row(
                strike="95",
                bid="3.40",
                ask="3.60",
                delta="-0.34",
                option_symbol="AAPL_MAIN",
            )
        ]

        changed, was_cleared = self.command._process_symbol(
            client=client,
            price_client=self.price_client,
            symbol=self.symbol,
            exchange="NASDAQ",
            min_dte=25,
            max_dte=40,
            delta_min=Decimal("-0.37"),
            delta_max=Decimal("-0.24"),
            roi_threshold=Decimal("2"),
            fetch_rsi=False,
        )

        self.assertTrue(changed)
        self.assertFalse(was_cleared)

        self.symbol.refresh_from_db()
        self.assertEqual(self.symbol.rsi, Decimal("41.25"))
        client.get_monthly_rsi.assert_not_called()

    def test_process_symbol_rejects_snapshot_when_put_strike_exceeds_price(self) -> None:
        expiration = self._expiration_int()
        original_updated_at = self.symbol.updated_at
        self.price_client.get_underlying_price.return_value = "26.9050"

        client = MagicMock()
        client.get_monthly_rsi.return_value = "60.02"
        client.get_expirations.return_value = [expiration]
        client.get_chain.return_value = [
            self._option_row(
                strike="245",
                bid="5.00",
                ask="5.80",
                delta="-0.29",
                option_symbol="AAPL_BAD",
                volume="20",
                iv="35.9300",
            )
        ]

        with self.assertRaisesMessage(
            CommandError,
            "Inconsistent market snapshot for AAPL: underlying price 26.9050 is not above selected put strike 245.",
        ):
            self.command._process_symbol(
                client=client,
                price_client=self.price_client,
                symbol=self.symbol,
                exchange="NASDAQ",
                min_dte=25,
                max_dte=40,
                delta_min=Decimal("-0.37"),
                delta_max=Decimal("-0.24"),
                roi_threshold=Decimal("2"),
                fetch_rsi=True,
            )

        self.symbol.refresh_from_db()
        self.assertEqual(self.symbol.price, Decimal("100.00"))
        self.assertEqual(self.symbol.updated_at, original_updated_at)

    @patch("api.management.commands.trading_view_scrape.yf")
    def test_process_symbol_backfills_missing_volume_from_yfinance(
        self, mock_yf: MagicMock
    ) -> None:
        expiration = self._expiration_int()
        expiration_date = datetime.strptime(str(expiration), "%Y%m%d").date()
        client = MagicMock()
        client.get_monthly_rsi.return_value = "55.75"
        client.get_expirations.return_value = [expiration]
        client.get_chain.return_value = [
            self._option_row(
                strike="95",
                bid="3.40",
                ask="3.60",
                delta="-0.34",
                option_symbol="AAPL_MAIN",
                volume=None,
            )
        ]

        mock_yf.Ticker.return_value.option_chain.return_value = MagicMock(
            puts=MagicMock(to_dict=MagicMock(return_value=[{"strike": 95, "volume": 321}])),
            calls=MagicMock(to_dict=MagicMock(return_value=[])),
        )

        changed, was_cleared = self.command._process_symbol(
            client=client,
            price_client=self.price_client,
            symbol=self.symbol,
            exchange="NASDAQ",
            min_dte=25,
            max_dte=40,
            delta_min=Decimal("-0.37"),
            delta_max=Decimal("-0.24"),
            roi_threshold=Decimal("2"),
            fetch_rsi=True,
        )

        self.assertTrue(changed)
        self.assertFalse(was_cleared)

        self.symbol.refresh_from_db()
        self.assertEqual(self.symbol.option_volume, 321)
        self.assertEqual(self.symbol.option_data["volume"], 321)
        mock_yf.Ticker.assert_called_once_with("AAPL")
        mock_yf.Ticker.return_value.option_chain.assert_called_once_with(
            expiration_date.isoformat()
        )

    @patch("api.management.commands.trading_view_scrape.yf")
    def test_process_symbol_skips_yfinance_when_tradingview_volume_exists(
        self, mock_yf: MagicMock
    ) -> None:
        expiration = self._expiration_int()
        client = MagicMock()
        client.get_monthly_rsi.return_value = "55.75"
        client.get_expirations.return_value = [expiration]
        client.get_chain.return_value = [
            self._option_row(
                strike="95",
                bid="3.40",
                ask="3.60",
                delta="-0.34",
                option_symbol="AAPL_MAIN",
                volume="150",
            )
        ]

        changed, was_cleared = self.command._process_symbol(
            client=client,
            price_client=self.price_client,
            symbol=self.symbol,
            exchange="NASDAQ",
            min_dte=25,
            max_dte=40,
            delta_min=Decimal("-0.37"),
            delta_max=Decimal("-0.24"),
            roi_threshold=Decimal("2"),
            fetch_rsi=True,
        )

        self.assertTrue(changed)
        self.assertFalse(was_cleared)
        mock_yf.Ticker.assert_not_called()

    def test_symbol_serializer_returns_raw_option_data_only(self) -> None:
        self.symbol.option_volume = 321
        self.symbol.option_iv = Decimal("18.7500")
        self.symbol.option_data = {
            "option_symbol": "AAPL_MAIN",
            "strike_price": 95.0,
            "bid": 3.4,
            "ask": 3.6,
            "volume": 321,
            "iv": 18.75,
            "mid": 3.5,
            "alternatives": [
                {
                    "option_symbol": "AAPL_ALT_1",
                    "strike_price": 94.0,
                    "bid": 2.8,
                    "ask": 3.0,
                    "volume": 210,
                    "iv": 20.1,
                    "mid": 2.9,
                }
            ],
        }
        self.symbol.save(update_fields=["option_volume", "option_iv", "option_data", "updated_at"])

        data = SymbolSerializer(self.symbol).data

        self.assertNotIn("strike_price", data)
        self.assertNotIn("bid", data)
        self.assertNotIn("ask", data)
        self.assertNotIn("mid", data)
        self.assertNotIn("alternatives", data)
        self.assertEqual(data["option_volume"], 321)
        self.assertEqual(data["option_iv"], "18.7500")
        self.assertEqual(data["option_data"]["strike_price"], 95.0)
        self.assertEqual(data["option_data"]["bid"], 3.4)
        self.assertEqual(data["option_data"]["ask"], 3.6)
        self.assertEqual(data["option_data"]["volume"], 321)
        self.assertEqual(data["option_data"]["iv"], 18.75)
        self.assertEqual(data["option_data"]["mid"], 3.5)
        self.assertEqual(len(data["option_data"]["alternatives"]), 1)
        self.assertEqual(data["option_data"]["alternatives"][0]["strike_price"], 94.0)
        self.assertEqual(data["option_data"]["alternatives"][0]["bid"], 2.8)
        self.assertEqual(data["option_data"]["alternatives"][0]["ask"], 3.0)
        self.assertEqual(data["option_data"]["alternatives"][0]["volume"], 210)
        self.assertEqual(data["option_data"]["alternatives"][0]["iv"], 20.1)
        self.assertEqual(data["option_data"]["alternatives"][0]["mid"], 2.9)


class AIAgentsPotentialCommandTestCase(APITestCase):
    def test_extract_json_payload_accepts_code_fenced_json(self) -> None:
        payload = ai_agents_potential_command.extract_json_payload(
            '```json\n{"summary":"Compact view","bull_points":["A"],"risk_points":[],"score_alignment":"supports"}\n```'
        )

        self.assertEqual(payload["summary"], "Compact view")
        self.assertEqual(payload["score_alignment"], "supports")

    @patch("api.management.commands.ai_agents_potential.OpenAI")
    @patch("api.management.commands.ai_agents_potential.generate_text")
    def test_command_outputs_frontend_json_and_saves_report(
        self,
        mock_generate_text: MagicMock,
        mock_openai: MagicMock,
    ) -> None:
        mock_openai.return_value = MagicMock()

        def fake_generate_text(*, prompt: str, **kwargs: Any) -> str:
            if "BUSINESS POTENTIAL" in prompt:
                return json.dumps(
                    {
                        "summary": "High switching costs and sticky customer relationships support the moat.",
                        "bull_points": ["Pricing power intact", "Recurring enterprise demand"],
                        "risk_points": ["Execution matters"],
                        "score_alignment": "supports",
                    }
                )
            if "SECTOR AND INDUSTRY GROWTH POTENTIAL" in prompt:
                return json.dumps(
                    {
                        "summary": "End-market demand remains healthy with multi-year infrastructure spending.",
                        "bull_points": ["Data center capex expanding", "Industry CAGR remains attractive"],
                        "risk_points": ["Macro slowdown can compress budgets"],
                        "score_alignment": "supports",
                    }
                )
            if "DISRUPTION RISK FROM AI AND AI AGENTS" in prompt:
                return json.dumps(
                    {
                        "summary": "AI agents could compress seat demand and speed competition, though scale still matters.",
                        "bull_points": ["Embedded workflows raise switching costs"],
                        "risk_points": ["AI agents can replace seat-based workflows"],
                        "score_alignment": "nuances",
                    }
                )

            return json.dumps(
                {
                    "executive_summary": "Compelling setup driven by durable demand and resilient positioning, but AI agents add a real disruption watchpoint. Main question is whether differentiation survives lower software barriers.",
                    "overall_stance": "bullish",
                    "score_alignment": "supports",
                    "confidence": 0.82,
                    "insights": [
                        {
                            "key": "business_moat",
                            "summary": "Sticky customer relationships and pricing power reinforce the moat.",
                            "tone": "bull",
                            "dot_color": "green",
                            "tag": "Competitive moat",
                        },
                        {
                            "key": "revenue_growth",
                            "summary": "Infrastructure and enterprise spend support a healthy growth runway.",
                            "tone": "bull",
                            "dot_color": "blue",
                            "tag": "Growth driver",
                        },
                        {
                            "key": "ai_disruption_risk",
                            "summary": "AI agents may pressure seat growth and invite faster entrants despite incumbency advantages.",
                            "tone": "risk",
                            "dot_color": "amber",
                            "tag": "Disruption watch",
                        },
                        {
                            "key": "key_risks",
                            "summary": "Execution risk and faster AI competition remain the main watchpoints.",
                            "tone": "risk",
                            "dot_color": "red",
                            "tag": "Monitor closely",
                        },
                    ],
                }
            )

        mock_generate_text.side_effect = fake_generate_text

        stdout = StringIO()
        stderr = StringIO()

        call_command(
            "ai_agents_potential",
            "NVDA",
            "--score",
            "81",
            "--format",
            "json",
            "--save-to-db",
            stdout=stdout,
            stderr=stderr,
        )

        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["ticker"], "NVDA")
        self.assertEqual(payload["fundamental_score"], 81)
        self.assertEqual(payload["overall_stance"], "bullish")
        self.assertEqual(payload["score_alignment"], "supports")
        self.assertEqual(len(payload["insights"]), 4)
        self.assertEqual(payload["insights"][0]["key"], "business_moat")
        self.assertEqual(payload["insights"][2]["key"], "ai_disruption_risk")
        self.assertEqual(payload["agents"][0]["key"], "business_potential")

        report = DueDiligenceReport.objects.get(symbol="NVDA")
        self.assertEqual(report.rating, "BUY")
        self.assertEqual(report.report["report_kind"], "frontend_analysis_panel")
        self.assertIn("Saved summarized report", stderr.getvalue())

    def test_format_usage_summary_estimates_cost_from_usage(self) -> None:
        summary = ai_agents_potential_command.format_usage_summary(
            [
                {
                    "model": "gpt-4o-mini",
                    "input_tokens": 1000,
                    "cached_input_tokens": 200,
                    "output_tokens": 300,
                    "web_search_calls": 3,
                    "search_tool_type": "web_search_preview",
                },
                {
                    "model": "gpt-4o-mini",
                    "input_tokens": 500,
                    "cached_input_tokens": 0,
                    "output_tokens": 100,
                    "web_search_calls": 0,
                    "search_tool_type": "web_search_preview",
                },
            ]
        )

        self.assertIsNotNone(summary)
        self.assertIn("1300 input", summary)
        self.assertIn("200 cached input", summary)
        self.assertIn("400 output", summary)
        self.assertIn("3 web search call(s)", summary)
        self.assertIn("Estimated cost: $0.0755", summary)

    def test_format_usage_summary_uses_standard_web_search_pricing(self) -> None:
        summary = ai_agents_potential_command.format_usage_summary(
            [
                {
                    "model": "gpt-4o-mini",
                    "input_tokens": 1000,
                    "cached_input_tokens": 0,
                    "output_tokens": 300,
                    "web_search_calls": 3,
                    "search_tool_type": "web_search",
                }
            ]
        )

        self.assertIsNotNone(summary)
        self.assertIn("1000 input", summary)
        self.assertIn("300 output", summary)
        self.assertIn("3 web search call(s)", summary)
        self.assertIn("24000 billed search input", summary)
        self.assertIn("Estimated cost: $0.0339", summary)


class PutWheelAgentViewTests(APITestCase):
    def test_handle_put_wheel_opportunity_returns_explicit_stock_and_opportunity_scores(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=31)
        Symbol.objects.create(
            ticker="ADI",
            price=Decimal("400.00"),
            rsi=Decimal("50.00"),
            score=85,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 375,
                        "expiration": expiration.isoformat(),
                        "bid": 10.00,
                        "ask": 10.50,
                        "delta": -0.30,
                        "iv": 40.45,
                        "volume": 150,
                        "open_interest": 800,
                    },
                    {
                        "strike": 375,
                        "expiration": expiration.isoformat(),
                        "bid": 10.90,
                        "ask": 11.10,
                        "delta": -0.30,
                        "iv": 40.45,
                        "volume": 150,
                        "open_interest": 800,
                    },
                ]
            },
        )

        payload = json.loads(agent_views._handle_put_wheel_opportunity("ADI"))

        self.assertEqual(payload["quality_score"], 85)
        self.assertEqual(payload["stock_quality_score"], 85)
        self.assertEqual(payload["technical_score"], Symbol.TechnicalScore.BUY)
        self.assertEqual(payload["summary"]["quality_score"], 85)
        self.assertEqual(payload["summary"]["stock_quality_score"], 85)
        self.assertEqual(payload["summary"]["technical_score"], Symbol.TechnicalScore.BUY)
        self.assertEqual(payload["summary"]["opportunity_rating"], "Good opportunity")
        self.assertEqual(payload["summary"]["opportunity_score"], payload["summary"]["score"])
        self.assertEqual(payload["summary"]["best_roi"], 2.93)
        self.assertEqual(payload["best_put_opportunity"]["contract"]["roi"], 2.93)
