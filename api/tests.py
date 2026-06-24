from datetime import date, datetime, timedelta
from decimal import Decimal
from io import StringIO
import json
import urllib.error
from typing import Any

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

    def test_fetch_price_and_rsi_uses_fmp_for_price_and_rsi(self) -> None:
        price_client = MagicMock()
        price_client.get_underlying_price.return_value = "123.45"
        price_client.get_rsi.return_value = "55.67"

        price, rsi = initial_screener_command._fetch_price_and_rsi(
            "AAPL",
            price_client=price_client,
        )

        self.assertEqual(price, Decimal("123.45"))
        self.assertEqual(rsi, Decimal("55.67"))
        price_client.get_underlying_price.assert_called_once_with("AAPL")
        price_client.get_rsi.assert_called_once_with("AAPL")

    def test_fetch_price_and_rsi_returns_rsi_even_if_fmp_price_fails(self) -> None:
        price_client = MagicMock()
        price_client.get_underlying_price.side_effect = ValueError("bad quote")
        price_client.get_rsi.return_value = "41.25"

        price, rsi = initial_screener_command._fetch_price_and_rsi(
            "AAPL",
            price_client=price_client,
        )

        self.assertIsNone(price)
        self.assertEqual(rsi, Decimal("41.25"))
        price_client.get_rsi.assert_called_once_with("AAPL")


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
        self.assertEqual([item["ticker"] for item in response.data["results"]], ["STRONG"])
        self.assertEqual(response.data["count"], 1)
        self.assertFalse(response.data["has_more"])

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
            [item["ticker"] for item in response.data["results"]],
            ["BUY", "NEUTRAL", "STRONG"],
        )
        self.assertEqual(response.data["count"], 3)
        self.assertFalse(response.data["has_more"])

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
        self.assertEqual([item["ticker"] for item in response.data["results"]], ["MATCH"])
        self.assertEqual(response.data["count"], 1)
        self.assertFalse(response.data["has_more"])

    def test_list_is_paginated(self) -> None:
        for index in range(30):
            Symbol.objects.create(ticker=f"TICK{index:02d}")

        response = self.client.get(self.list_url, {"page": 2, "page_size": 10})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 30)
        self.assertEqual(response.data["page"], 2)
        self.assertEqual(response.data["page_size"], 10)
        self.assertTrue(response.data["has_more"])
        self.assertIsNotNone(response.data["next"])
        self.assertIsNotNone(response.data["previous"])
        self.assertEqual(len(response.data["results"]), 10)
        self.assertEqual(response.data["results"][0]["ticker"], "TICK10")
        self.assertEqual(response.data["results"][-1]["ticker"], "TICK19")


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
        self.price_client.get_price_and_volume.return_value = ("100.00", 500000)

    def _expiration_int(self, days: int = 30) -> int:
        return int((date.today() + timedelta(days=days)).strftime("%Y%m%d"))

    def _option_row(
        self,
        *,
        strike: str,
        bid: object,
        ask: object,
        delta: str,
        option_symbol: str,
        volume: object = "150",
        iv: object = "24.1250",
        option_type: str = "put",
        open_interest: object = None,
    ) -> dict[str, object]:
        return {
            "option_symbol": option_symbol,
            "option_type": option_type,
            "strike_price": strike,
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "delta": delta,
            "volume": volume,
            "open_interest": open_interest,
            "iv": iv,
        }

    def test_parse_expiration_volume_data_sums_and_indexes_selected_expiration(self) -> None:
        volume_data = TradingViewOptions.parse_expiration_volume_data(
            {
                "items": [
                    {
                        "exp": 20260717,
                        "strikes": [
                            {"s": 95, "c": {"v": 120}, "p": {"v": 80}},
                            {"s": 100, "c": {"v": "15"}, "p": {"v": "25"}},
                            {"s": 105, "c": {}, "p": {"v": None}},
                        ],
                    }
                ]
            },
            20260717,
        )

        self.assertEqual(volume_data["total_volume"], 240)
        self.assertEqual(
            volume_data["strike_volumes"]["95"],
            {"call_volume": 120, "put_volume": 80, "total_volume": 200},
        )
        self.assertEqual(
            volume_data["strike_volumes"]["100"],
            {"call_volume": 15, "put_volume": 25, "total_volume": 40},
        )

    def test_parse_expiration_volume_returns_none_when_expiration_missing(self) -> None:
        volume = TradingViewOptions.parse_expiration_volume(
            {
                "items": [
                    {
                        "exp": 20260717,
                        "strikes": [{"s": 95, "c": {"v": 120}, "p": {"v": 80}}],
                    }
                ]
            },
            20260821,
        )

        self.assertIsNone(volume)

    def test_apply_contract_volumes_maps_lens_data_onto_chain_rows(self) -> None:
        chain = [
            self._option_row(
                strike="95",
                bid="1.00",
                ask="1.20",
                delta="-0.30",
                option_symbol="AAPL_PUT_95",
                option_type="put",
            ),
            self._option_row(
                strike="95",
                bid="0.80",
                ask="1.00",
                delta="0.25",
                option_symbol="AAPL_CALL_95",
                option_type="call",
            ),
        ]

        TradingViewOptions.apply_contract_volumes(
            chain,
            {
                "95": {
                    "call_volume": 44,
                    "put_volume": 66,
                    "total_volume": 110,
                }
            },
        )

        self.assertEqual(chain[0]["contract_volume"], 66)
        self.assertEqual(chain[0]["put_volume"], 66)
        self.assertEqual(chain[0]["call_volume"], 44)
        self.assertEqual(chain[0]["total_volume_at_strike"], 110)
        self.assertEqual(chain[1]["contract_volume"], 44)

    @patch("api.management.commands.trading_view_scrape.certifi.where", return_value="dummy.pem")
    @patch("api.management.commands.trading_view_scrape.urllib.request.urlopen")
    def test_fmp_client_get_rsi_returns_latest_timestamp_value(
        self,
        mock_urlopen: MagicMock,
        _mock_certifi_where: MagicMock,
    ) -> None:
        response = MagicMock()
        response.read.return_value = (
            b'[{"date":"2025-06-03 10:00:00","rsi":62.10},'
            b'{"date":"2025-06-02 10:00:00","rsi":57.32}]'
        )
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = None
        mock_urlopen.return_value = context

        rsi = FinancialModelingPrepClient(api_key="test-fmp-key").get_rsi("AAPL")

        self.assertEqual(rsi, 62.10)
        called_url = mock_urlopen.call_args[0][0]
        self.assertIn("technical-indicators/rsi", called_url)
        self.assertIn("symbol=AAPL", called_url)
        self.assertIn("periodLength=14", called_url)
        self.assertIn("timeframe=1hour", called_url)

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

    @override_settings(FINANCIAL_MODELING_API_KEY="")
    @patch("api.management.commands.trading_view_scrape.Command._process_symbol")
    def test_handle_uses_stored_prices_when_fmp_key_missing(
        self, mock_process_symbol: MagicMock
    ) -> None:
        mock_process_symbol.return_value = (True, False)
        stdout = StringIO()
        stderr = StringIO()

        call_command("trading_view_scrape", "--workers", "1", stdout=stdout, stderr=stderr)

        self.assertEqual(mock_process_symbol.call_count, 1)
        self.assertIsNone(mock_process_symbol.call_args.kwargs["price_client"])
        self.assertIn(
            "FINANCIAL_MODELING_API_KEY is not configured; using stored Symbol.price values.",
            stderr.getvalue(),
        )
        self.assertIn(
            "Processed 1 symbols | updated 1 | cleared 0 | errors 0",
            stdout.getvalue(),
        )

    def test_process_symbol_updates_rsi_from_fmp(self) -> None:
        expiration = self._expiration_int()
        self.price_client.get_rsi.return_value = "55.75"
        client = MagicMock()
        client.get_expirations.return_value = [expiration]
        client.get_expiration_volume_data.return_value = {
            "total_volume": 1240,
            "strike_volumes": {"95": {"call_volume": 300, "put_volume": 940, "total_volume": 1240}},
        }
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
        self.assertEqual(self.symbol.option_volume, 1240)
        self.assertEqual(self.symbol.option_iv, Decimal("24.1250"))
        self.assertEqual(self.symbol.option_data["contract_volume"], 940)
        self.assertEqual(self.symbol.option_data["put_volume"], 940)
        self.assertEqual(self.symbol.option_data["call_volume"], 300)
        self.assertEqual(self.symbol.option_data["total_volume_at_strike"], 1240)
        self.price_client.get_rsi.assert_called_once_with("AAPL")

    def test_process_symbol_reports_429_with_operator_guidance(self) -> None:
        client = MagicMock()
        self.price_client.get_price_and_volume.side_effect = urllib.error.HTTPError(
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
        self.price_client.get_rsi.return_value = "55.75"
        client.get_expirations.return_value = [expiration]
        client.get_expiration_volume_data.return_value = {
            "total_volume": 980,
            "strike_volumes": {
                "95": {"call_volume": 180, "put_volume": 500, "total_volume": 680},
                "94": {"call_volume": 90, "put_volume": 140, "total_volume": 230},
                "93": {"call_volume": 15, "put_volume": 55, "total_volume": 70},
            },
        }
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
        self.assertEqual(self.symbol.option_volume, 980)
        self.assertEqual(self.symbol.option_iv, Decimal("24.1250"))
        self.assertEqual(self.symbol.option_data["contract_volume"], 500)
        self.assertEqual(
            [item["option_symbol"] for item in self.symbol.option_data["alternatives"]],
            ["AAPL_ALT_1", "AAPL_ALT_2"],
        )
        self.assertEqual(
            [item["delta"] for item in self.symbol.option_data["alternatives"]],
            [-0.31, -0.28],
        )
        self.assertEqual(
            [item["contract_volume"] for item in self.symbol.option_data["alternatives"]],
            [140, 55],
        )

    def test_process_symbol_refreshes_option_volume_and_clears_iv_without_roi_candidate(self) -> None:
        expiration = self._expiration_int()
        self.symbol.option_volume = 750
        self.symbol.option_iv = Decimal("33.3300")
        self.symbol.option_data = {"option_symbol": "OLD"}
        self.symbol.roi = Decimal("3.10")
        self.symbol.save(
            update_fields=["option_volume", "option_iv", "option_data", "roi", "updated_at"]
        )

        client = MagicMock()
        self.price_client.get_rsi.return_value = "55.75"
        client.get_expirations.return_value = [expiration]
        client.get_expiration_volume_data.return_value = {
            "total_volume": 620,
            "strike_volumes": {"95": {"call_volume": 20, "put_volume": 140, "total_volume": 160}},
        }
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
        self.assertEqual(self.symbol.option_volume, 620)
        self.assertIsNone(self.symbol.option_iv)
        self.assertIsNone(self.symbol.option_data)
        self.assertIsNone(self.symbol.roi)

    def test_process_symbol_reports_saved_calls_when_no_put_candidate(self) -> None:
        expiration = self._expiration_int()
        client = MagicMock()
        self.price_client.get_rsi.return_value = "55.75"
        client.get_expirations.return_value = [expiration]
        client.get_expiration_volume_data.return_value = {
            "total_volume": 840,
            "strike_volumes": {
                "95": {"call_volume": 90, "put_volume": 130, "total_volume": 220},
                "110": {"call_volume": 250, "put_volume": 40, "total_volume": 290},
                "115": {"call_volume": 330, "put_volume": 0, "total_volume": 330},
            },
        }
        client.get_chain.return_value = [
            self._option_row(
                strike="95",
                bid="0.10",
                ask="0.20",
                delta="-0.34",
                option_symbol="AAPL_LOW_ROI",
            ),
            self._option_row(
                strike="110",
                bid="1.10",
                ask="1.30",
                delta="0.42",
                option_symbol="AAPL_CALL_1",
                option_type="call",
            ),
            self._option_row(
                strike="115",
                bid="0.70",
                ask="0.90",
                delta="0.28",
                option_symbol="AAPL_CALL_2",
                option_type="call",
            ),
        ]
        reporter = MagicMock()

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
            reporter=reporter,
        )

        self.assertTrue(changed)
        self.assertFalse(was_cleared)
        reporter.assert_called_once_with(
            f"AAPL: no puts in delta range for "
            f"{datetime.strptime(str(expiration), '%Y%m%d').date()}; "
            "price/expiration updated, option data cleared; saved 2 calls."
        )
        self.symbol.refresh_from_db()
        self.assertIsNone(self.symbol.option_data)
        self.assertEqual(self.symbol.option_volume, 840)
        self.assertEqual(len(self.symbol.call_data), 2)
        self.assertEqual(
            [item["contract_volume"] for item in self.symbol.call_data],
            [250, 330],
        )
        self.assertEqual(
            [item["total_volume_at_strike"] for item in self.symbol.call_data],
            [290, 330],
        )

    def test_process_symbol_reports_no_qualifying_calls_when_no_put_candidate(self) -> None:
        expiration = self._expiration_int()
        client = MagicMock()
        self.price_client.get_rsi.return_value = "55.75"
        client.get_expirations.return_value = [expiration]
        client.get_expiration_volume_data.return_value = {
            "total_volume": 410,
            "strike_volumes": {
                "95": {"call_volume": 50, "put_volume": 140, "total_volume": 190},
                "130": {"call_volume": 60, "put_volume": 10, "total_volume": 70},
            },
        }
        client.get_chain.return_value = [
            self._option_row(
                strike="95",
                bid="0.10",
                ask="0.20",
                delta="-0.34",
                option_symbol="AAPL_LOW_ROI",
            ),
            self._option_row(
                strike="130",
                bid="0.10",
                ask="0.20",
                delta="0.05",
                option_symbol="AAPL_CALL_LOW_DELTA",
                option_type="call",
            ),
        ]
        reporter = MagicMock()

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
            reporter=reporter,
        )

        self.assertTrue(changed)
        self.assertFalse(was_cleared)
        reporter.assert_called_once_with(
            f"AAPL: no puts in delta range for "
            f"{datetime.strptime(str(expiration), '%Y%m%d').date()}; "
            "price/expiration updated, option data cleared; no qualifying calls."
        )
        self.symbol.refresh_from_db()
        self.assertIsNone(self.symbol.option_data)
        self.assertIsNone(self.symbol.call_data)
        self.assertEqual(self.symbol.option_volume, 410)

    def test_collect_call_contracts_allows_missing_liquidity_and_sorts_by_strike(
        self,
    ) -> None:
        contracts = self.command._collect_call_contracts(
            chain=[
                self._option_row(
                    strike="105",
                    bid="0",
                    ask="3.00",
                    delta="0.16",
                    option_symbol="AAPL_ZERO_BID",
                    option_type="call",
                ),
                self._option_row(
                    strike="95",
                    bid="1.00",
                    ask="1.20",
                    delta="0.28",
                    option_symbol="AAPL_NO_LIQUIDITY",
                    option_type="call",
                    volume=None,
                    open_interest=None,
                ),
                self._option_row(
                    strike="90",
                    bid="1.00",
                    ask="2.00",
                    delta="0.30",
                    option_symbol="AAPL_WIDE_SPREAD",
                    option_type="call",
                    open_interest="500",
                ),
                self._option_row(
                    strike="92",
                    bid="1.00",
                    ask="1.20",
                    delta="0.22",
                    option_symbol="AAPL_OI_ONLY",
                    option_type="call",
                    volume=None,
                    open_interest="500",
                ),
                self._option_row(
                    strike="87",
                    bid="1.50",
                    ask="1.80",
                    delta="0.42",
                    option_symbol="AAPL_STRIKE_87",
                    option_type="call",
                ),
                self._option_row(
                    strike="89",
                    bid="1.10",
                    ask="1.30",
                    delta="0.35",
                    option_symbol="AAPL_STRIKE_89",
                    option_type="call",
                ),
            ]
        )

        self.assertEqual(
            [contract["option_symbol"] for contract in contracts],
            [
                "AAPL_STRIKE_87",
                "AAPL_STRIKE_89",
                "AAPL_WIDE_SPREAD",
                "AAPL_OI_ONLY",
                "AAPL_NO_LIQUIDITY",
            ],
        )
        self.assertEqual(
            [contract["strike_price"] for contract in contracts],
            [
                Decimal("87"),
                Decimal("89"),
                Decimal("90"),
                Decimal("92"),
                Decimal("95"),
            ],
        )
        self.assertEqual(contracts[-2]["open_interest"], 500)
        self.assertIsNone(contracts[-2]["volume"])
        self.assertIsNone(contracts[-1]["open_interest"])
        self.assertIsNone(contracts[-1]["volume"])

    def test_process_symbol_can_skip_rsi_fetch_and_preserve_existing_value(self) -> None:
        expiration = self._expiration_int()
        self.symbol.rsi = Decimal("41.25")
        self.symbol.save(update_fields=["rsi", "updated_at"])

        client = MagicMock()
        client.get_expirations.return_value = [expiration]
        client.get_expiration_volume_data.return_value = {
            "total_volume": 560,
            "strike_volumes": {"95": {"call_volume": 110, "put_volume": 210, "total_volume": 320}},
        }
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
        self.price_client.get_rsi.assert_not_called()

    def test_process_symbol_uses_stored_price_when_price_client_missing(self) -> None:
        expiration = self._expiration_int()
        client = MagicMock()
        client.get_expirations.return_value = [expiration]
        client.get_expiration_volume_data.return_value = {
            "total_volume": 730,
            "strike_volumes": {"95": {"call_volume": 120, "put_volume": 260, "total_volume": 380}},
        }
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
            price_client=None,
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
        self.assertEqual(self.symbol.price, Decimal("100.00"))
        self.assertIsNone(self.symbol.rsi)

    def test_process_symbol_rejects_snapshot_when_put_strike_exceeds_price(self) -> None:
        expiration = self._expiration_int()
        original_updated_at = self.symbol.updated_at
        self.price_client.get_price_and_volume.return_value = ("26.9050", 500000)

        client = MagicMock()
        self.price_client.get_rsi.return_value = "60.02"
        client.get_expirations.return_value = [expiration]
        client.get_expiration_volume_data.return_value = {
            "total_volume": 205,
            "strike_volumes": {"245": {"call_volume": 5, "put_volume": 25, "total_volume": 30}},
        }
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

    @patch("api.management.commands.trading_view_scrape.certifi.where", return_value="dummy.pem")
    @patch("api.management.commands.trading_view_scrape.urllib.request.urlopen")
    def test_fmp_client_get_price_and_volume_returns_both_fields(
        self,
        mock_urlopen: MagicMock,
        _mock_certifi_where: MagicMock,
    ) -> None:
        response = MagicMock()
        response.read.return_value = b'[{"symbol":"AAPL","price":100.0,"volume":500000}]'
        context = MagicMock()
        context.__enter__.return_value = response
        context.__exit__.return_value = None
        mock_urlopen.return_value = context

        price, volume = FinancialModelingPrepClient(api_key="test-fmp-key").get_price_and_volume("AAPL")

        self.assertEqual(price, 100.0)
        self.assertEqual(volume, 500000)

    def test_process_symbol_uses_tradingview_expiration_volume(self) -> None:
        expiration = self._expiration_int()
        self.price_client.get_price_and_volume.return_value = ("100.00", 321)
        self.price_client.get_rsi.return_value = "55.75"
        client = MagicMock()
        client.get_expirations.return_value = [expiration]
        client.get_expiration_volume_data.return_value = {
            "total_volume": 654,
            "strike_volumes": {"95": {"call_volume": 200, "put_volume": 454, "total_volume": 654}},
        }
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
        self.assertEqual(self.symbol.option_volume, 654)
        self.assertEqual(self.symbol.option_data["contract_volume"], 454)
        self.price_client.get_price_and_volume.assert_called_once_with("AAPL")

    def test_symbol_serializer_returns_raw_option_and_call_data(self) -> None:
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
        self.symbol.call_data = [
            {
                "option_symbol": "AAPL_CALL_1",
                "strike_price": 110.0,
                "bid": 1.1,
                "ask": 1.3,
                "volume": 450,
                "iv": 17.25,
                "mid": 1.2,
                "delta": 0.42,
            }
        ]
        self.symbol.save(
            update_fields=[
                "option_volume",
                "option_iv",
                "option_data",
                "call_data",
                "updated_at",
            ]
        )

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
        self.assertEqual(len(data["call_data"]), 1)
        self.assertEqual(data["call_data"][0]["strike_price"], 110.0)
        self.assertEqual(data["call_data"][0]["bid"], 1.1)
        self.assertEqual(data["call_data"][0]["ask"], 1.3)
        self.assertEqual(data["call_data"][0]["volume"], 450)
        self.assertEqual(data["call_data"][0]["iv"], 17.25)
        self.assertEqual(data["call_data"][0]["mid"], 1.2)
        self.assertEqual(data["call_data"][0]["delta"], 0.42)


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

    def test_handle_put_wheel_opportunity_ignores_zero_bid_zero_ask_and_low_volume(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=31)
        Symbol.objects.create(
            ticker="AVGO",
            price=Decimal("250.00"),
            rsi=Decimal("48.00"),
            score=88,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 235,
                        "expiration": expiration.isoformat(),
                        "bid": 0,
                        "ask": 3.40,
                        "delta": -0.24,
                        "iv": 29.0,
                        "volume": 180,
                        "open_interest": 1200,
                    },
                    {
                        "strike": 230,
                        "expiration": expiration.isoformat(),
                        "bid": 3.10,
                        "ask": 0,
                        "delta": -0.22,
                        "iv": 27.5,
                        "volume": 160,
                        "open_interest": 1000,
                    },
                    {
                        "strike": 225,
                        "expiration": expiration.isoformat(),
                        "bid": 2.60,
                        "ask": 2.90,
                        "delta": -0.20,
                        "iv": 26.0,
                        "volume": 40,
                        "open_interest": 900,
                    },
                    {
                        "strike": 220,
                        "expiration": expiration.isoformat(),
                        "bid": 2.00,
                        "ask": 2.20,
                        "delta": -0.19,
                        "iv": 25.0,
                        "volume": 140,
                        "open_interest": 850,
                    },
                ]
            },
        )

        payload = json.loads(agent_views._handle_put_wheel_opportunity("AVGO"))

        self.assertEqual(payload["best_put_opportunity"]["contract"]["strike"], 220.0)
        self.assertEqual(payload["best_put_opportunity"]["contract"]["volume"], 140)

    def test_handle_compare_put_candidates_ranks_symbols_and_filters_errors(
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
                        "bid": 10.90,
                        "ask": 11.10,
                        "delta": -0.30,
                        "iv": 40.45,
                        "volume": 150,
                        "open_interest": 800,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="XYZ",
            price=Decimal("100.00"),
            rsi=Decimal("68.00"),
            score=70,
            classification="Quality (selective)",
            liquidity=Symbol.LIQUIDITY_WEAK,
            technical_score=Symbol.TechnicalScore.SELL,
            option_data={
                "puts": [
                    {
                        "strike": 98,
                        "expiration": expiration.isoformat(),
                        "bid": 1.00,
                        "ask": 1.80,
                        "delta": -0.38,
                        "iv": 55.00,
                        "volume": 1,
                        "open_interest": 20,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_compare_put_candidates(
                {"symbols": ["ADI", "XYZ", "MISSING"]}
            )
        )

        self.assertEqual(payload["symbols_compared"], 2)
        self.assertEqual(payload["winner"]["symbol"], "ADI")
        self.assertEqual(
            [item["symbol"] for item in payload["ranked_candidates"]],
            ["ADI", "XYZ"],
        )
        self.assertGreater(
            payload["ranked_candidates"][0]["comparison_score"],
            payload["ranked_candidates"][1]["comparison_score"],
        )
        self.assertEqual(payload["ranked_candidates"][0]["opportunity_rating"], "Good opportunity")
        self.assertTrue(
            any(item["symbol"] == "MISSING" for item in payload["skipped"])
        )

    def test_handle_put_wheel_opportunity_applies_cash_budget(self) -> None:
        expiration = date.today() + timedelta(days=31)
        Symbol.objects.create(
            ticker="BUD",
            price=Decimal("115.00"),
            rsi=Decimal("48.00"),
            score=88,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 110,
                        "expiration": expiration.isoformat(),
                        "bid": 5.80,
                        "ask": 6.20,
                        "delta": -0.28,
                        "iv": 38.00,
                        "volume": 220,
                        "open_interest": 950,
                    },
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 2.30,
                        "ask": 2.50,
                        "delta": -0.22,
                        "iv": 34.00,
                        "volume": 240,
                        "open_interest": 1200,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_put_wheel_opportunity(
                "BUD",
                account_size=10_000,
            )
        )

        self.assertEqual(payload["best_put_opportunity"]["contract"]["strike"], 95)
        self.assertEqual(payload["best_put_opportunity"]["contract"]["cash_required"], 9500)
        self.assertEqual(payload["best_put_opportunity"]["contract"]["premium_received"], 240.0)
        self.assertEqual(payload["best_put_opportunity"]["contract"]["breakeven"], 92.6)
        self.assertEqual(payload["best_put_opportunity"]["contract"]["contracts_affordable"], 1)
        self.assertEqual(payload["summary"]["cash_required"], 9500)
        self.assertEqual(payload["summary"]["contracts_affordable"], 1)
        self.assertEqual(payload["filters_applied"]["effective_max_cash_required"], 10000)
        self.assertEqual(len(payload["top_put_candidates"]), 1)

    def test_handle_compare_put_candidates_respects_cash_budget(self) -> None:
        expiration = date.today() + timedelta(days=31)
        Symbol.objects.create(
            ticker="FIT",
            price=Decimal("102.00"),
            rsi=Decimal("49.00"),
            score=84,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 2.00,
                        "ask": 2.20,
                        "delta": -0.24,
                        "iv": 31.00,
                        "volume": 180,
                        "open_interest": 900,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="RICH",
            price=Decimal("140.00"),
            rsi=Decimal("46.00"),
            score=90,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 120,
                        "expiration": expiration.isoformat(),
                        "bid": 4.40,
                        "ask": 4.80,
                        "delta": -0.25,
                        "iv": 35.00,
                        "volume": 210,
                        "open_interest": 1100,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_compare_put_candidates(
                {
                    "symbols": ["FIT", "RICH"],
                    "account_size": 10_000,
                }
            )
        )

        self.assertEqual(payload["symbols_compared"], 1)
        self.assertEqual(payload["winner"]["symbol"], "FIT")
        self.assertEqual(payload["winner"]["best_contract"]["cash_required"], 9500)
        self.assertEqual(payload["filters_applied"]["effective_max_cash_required"], 10000)
        self.assertTrue(
            any(
                item["symbol"] == "RICH"
                and item["error"] == "No put contracts fit the cash-secured budget constraint."
                for item in payload["skipped"]
            )
        )

    def test_handle_scan_put_opportunities_respects_cash_budget(self) -> None:
        expiration = date.today() + timedelta(days=31)
        Symbol.objects.create(
            ticker="SMALL",
            price=Decimal("104.00"),
            rsi=Decimal("51.00"),
            score=86,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 2.10,
                        "ask": 2.30,
                        "delta": -0.23,
                        "iv": 29.00,
                        "volume": 240,
                        "open_interest": 1400,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="LARGE",
            price=Decimal("130.00"),
            rsi=Decimal("47.00"),
            score=92,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.STRONG_BUY,
            option_data={
                "puts": [
                    {
                        "strike": 115,
                        "expiration": expiration.isoformat(),
                        "bid": 5.00,
                        "ask": 5.40,
                        "delta": -0.24,
                        "iv": 36.00,
                        "volume": 280,
                        "open_interest": 1600,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_scan_put_opportunities(
                {"account_size": 10_000}
            )
        )

        self.assertEqual(payload["results_returned"], 1)
        self.assertEqual(payload["opportunities"][0]["ticker"], "SMALL")
        self.assertEqual(payload["opportunities"][0]["cash_required"], 9500)
        self.assertEqual(payload["opportunities"][0]["premium_received"], 220.0)
        self.assertEqual(payload["opportunities"][0]["breakeven"], 92.8)
        self.assertEqual(payload["opportunities"][0]["contracts_affordable"], 1)
        self.assertEqual(payload["filters_applied"]["effective_max_cash_required"], 10000)

    def test_handle_scan_put_opportunities_respects_max_price(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="UNDER",
            price=Decimal("125.00"),
            rsi=Decimal("49.00"),
            score=88,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 110,
                        "expiration": expiration.isoformat(),
                        "bid": 3.60,
                        "ask": 4.00,
                        "delta": -0.24,
                        "iv": 34.00,
                        "volume": 260,
                        "open_interest": 1200,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="OVER",
            price=Decimal("220.00"),
            rsi=Decimal("45.00"),
            score=96,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.STRONG_BUY,
            option_data={
                "puts": [
                    {
                        "strike": 200,
                        "expiration": expiration.isoformat(),
                        "bid": 7.00,
                        "ask": 7.60,
                        "delta": -0.24,
                        "iv": 31.00,
                        "volume": 340,
                        "open_interest": 1800,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_scan_put_opportunities(
                {"max_price": 150}
            )
        )

        self.assertEqual(payload["results_returned"], 1)
        self.assertEqual(payload["filters_applied"]["max_price"], 150.0)
        self.assertEqual(payload["opportunities"][0]["ticker"], "UNDER")

    def test_handle_compare_covered_call_candidates_ranks_symbols_and_filters_errors(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="AAPL",
            price=Decimal("195.00"),
            score=84,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 210,
                        "expiration": expiration.isoformat(),
                        "bid": 4.00,
                        "ask": 4.20,
                        "delta": 0.28,
                        "iv": 24.0,
                        "volume": 800,
                        "open_interest": 3000,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="AMZN",
            price=Decimal("100.00"),
            score=80,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 110,
                        "expiration": expiration.isoformat(),
                        "bid": 1.50,
                        "ask": 1.70,
                        "delta": 0.28,
                        "iv": 21.0,
                        "volume": 300,
                        "open_interest": 1700,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="NFLX",
            price=Decimal("500.00"),
            score=80,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 520,
                        "expiration": expiration.isoformat(),
                        "bid": 6.00,
                        "ask": 6.20,
                        "delta": 0.34,
                        "iv": 27.0,
                        "volume": 650,
                        "open_interest": 2800,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_compare_covered_call_candidates(
                {"symbols": ["AAPL", "AMZN", "NFLX", "MISSING"], "max_delta": 0.30, "min_roi": 1.0}
            )
        )

        self.assertEqual(payload["symbols_compared"], 2)
        self.assertEqual(payload["winner"]["symbol"], "AAPL")
        self.assertEqual(
            [item["symbol"] for item in payload["ranked_candidates"]],
            ["AAPL", "AMZN"],
        )
        self.assertGreater(
            payload["ranked_candidates"][0]["comparison_score"],
            payload["ranked_candidates"][1]["comparison_score"],
        )
        self.assertEqual(payload["ranked_candidates"][0]["covered_call_rating"], "Excellent")
        self.assertEqual(payload["ranked_candidates"][0]["call_away_risk"], "Low")
        self.assertTrue(
            any(
                item["symbol"] == "NFLX" and "max_delta" in item["error"]
                for item in payload["skipped"]
            )
        )
        self.assertTrue(
            any(item["symbol"] == "MISSING" for item in payload["skipped"])
        )

    def test_handle_compare_covered_call_candidates_respects_min_roi(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="CRM",
            price=Decimal("280.00"),
            score=82,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 295,
                        "expiration": expiration.isoformat(),
                        "bid": 1.20,
                        "ask": 1.30,
                        "delta": 0.27,
                        "iv": 23.0,
                        "volume": 320,
                        "open_interest": 1400,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="ORCL",
            price=Decimal("90.00"),
            score=78,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 1.30,
                        "ask": 1.50,
                        "delta": 0.29,
                        "iv": 24.0,
                        "volume": 450,
                        "open_interest": 1800,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_compare_covered_call_candidates(
                {"symbols": ["CRM", "ORCL"], "min_roi": 1.5}
            )
        )

        self.assertEqual(payload["symbols_compared"], 1)
        self.assertEqual(payload["winner"]["symbol"], "ORCL")
        self.assertEqual(
            [item["symbol"] for item in payload["ranked_candidates"]],
            ["ORCL"],
        )
        self.assertTrue(
            any(
                item["symbol"] == "CRM" and "min_roi" in item["error"]
                for item in payload["skipped"]
            )
        )

    def test_handle_build_monthly_income_plan_combines_covered_calls_and_puts(self) -> None:
        call_expiration = date.today() + timedelta(days=28)
        put_expiration = date.today() + timedelta(days=31)
        Symbol.objects.create(
            ticker="AAPL",
            price=Decimal("195.00"),
            score=84,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 210,
                        "expiration": call_expiration.isoformat(),
                        "bid": 4.00,
                        "ask": 4.20,
                        "delta": 0.28,
                        "iv": 24.0,
                        "volume": 800,
                        "open_interest": 3000,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="LARGE",
            price=Decimal("104.00"),
            rsi=Decimal("51.00"),
            score=90,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 95,
                        "expiration": put_expiration.isoformat(),
                        "bid": 2.10,
                        "ask": 2.30,
                        "delta": -0.23,
                        "iv": 29.00,
                        "volume": 240,
                        "open_interest": 1400,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="MID",
            price=Decimal("72.00"),
            rsi=Decimal("49.00"),
            score=88,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 60,
                        "expiration": put_expiration.isoformat(),
                        "bid": 1.50,
                        "ask": 1.70,
                        "delta": -0.24,
                        "iv": 30.00,
                        "volume": 260,
                        "open_interest": 1500,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="SMALL",
            price=Decimal("41.00"),
            rsi=Decimal("47.00"),
            score=86,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 30,
                        "expiration": put_expiration.isoformat(),
                        "bid": 0.90,
                        "ask": 1.10,
                        "delta": -0.20,
                        "iv": 27.00,
                        "volume": 220,
                        "open_interest": 1300,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_build_monthly_income_plan(
                {
                    "positions": [{"symbol": "AAPL", "shares_owned": 200, "cost_basis": 180}],
                    "account_size": 10_000,
                    "monthly_income_target": 600,
                }
            )
        )

        self.assertEqual(payload["plan_type"], "mixed_income_plan")
        self.assertEqual(payload["covered_call_positions_evaluated"], 1)
        self.assertEqual(payload["covered_call_positions"][0]["symbol"], "AAPL")
        self.assertEqual(payload["covered_call_positions"][0]["best_contract"]["strike"], 210.0)
        self.assertEqual(payload["allocated_put_positions"], 2)
        self.assertCountEqual(
            [item["ticker"] for item in payload["allocated_put_ideas"]],
            ["MID", "SMALL"],
        )
        self.assertEqual(
            payload["primary_put_idea"]["ticker"],
            payload["allocated_put_ideas"][0]["ticker"],
        )
        self.assertEqual(payload["put_allocation_summary"]["total_cash_required"], 9000.0)
        self.assertEqual(payload["put_allocation_summary"]["remaining_cash"], 1000.0)
        self.assertEqual(
            [item["ticker"] for item in payload["alternative_put_ideas"]],
            ["LARGE"],
        )
        self.assertEqual(payload["summary"]["monthly_income_target"], 600.0)
        self.assertTrue(payload["summary"]["target_met"])
        self.assertGreater(payload["summary"]["estimated_total_monthly_income"], 0)
        self.assertGreater(
            payload["summary"]["estimated_monthly_income_from_puts"],
            payload["summary"]["estimated_monthly_income_from_primary_put"],
        )

    def test_handle_build_monthly_income_plan_positions_only_returns_covered_calls(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="MSFT",
            price=Decimal("420.00"),
            score=88,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 440,
                        "expiration": expiration.isoformat(),
                        "bid": 5.20,
                        "ask": 5.40,
                        "delta": 0.29,
                        "iv": 21.0,
                        "volume": 700,
                        "open_interest": 3200,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_build_monthly_income_plan(
                {
                    "positions": [{"symbol": "MSFT", "shares_owned": 100}],
                }
            )
        )

        self.assertEqual(payload["plan_type"], "covered_calls_only")
        self.assertEqual(payload["covered_call_positions"][0]["symbol"], "MSFT")
        self.assertIsNone(payload["primary_put_idea"])
        self.assertGreater(
            payload["summary"]["estimated_monthly_income_from_covered_calls"],
            0,
        )

    def test_handle_build_monthly_income_plan_without_positions_defaults_to_puts(self) -> None:
        expiration = date.today() + timedelta(days=31)
        Symbol.objects.create(
            ticker="FIT",
            price=Decimal("102.00"),
            rsi=Decimal("49.00"),
            score=84,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 2.00,
                        "ask": 2.20,
                        "delta": -0.24,
                        "iv": 31.00,
                        "volume": 180,
                        "open_interest": 900,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="RICH",
            price=Decimal("140.00"),
            rsi=Decimal("46.00"),
            score=90,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 120,
                        "expiration": expiration.isoformat(),
                        "bid": 4.40,
                        "ask": 4.80,
                        "delta": -0.25,
                        "iv": 35.00,
                        "volume": 210,
                        "open_interest": 1100,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_build_monthly_income_plan(
                {"account_size": 10_000, "monthly_income_target": 250}
            )
        )

        self.assertEqual(payload["plan_type"], "cash_secured_puts_only")
        self.assertEqual(payload["primary_put_idea"]["ticker"], "FIT")
        self.assertEqual(payload["primary_put_idea"]["cash_required"], 9500)
        self.assertEqual(payload["allocated_put_positions"], 1)
        self.assertEqual(len(payload["allocated_put_ideas"]), 1)
        self.assertEqual(payload["summary"]["target_met"], False)
        self.assertIn(
            "No owned positions were provided, so the plan defaults to cash-secured put / wheel ideas only.",
            payload["warnings"],
        )

    def test_handle_covered_call_opportunity_returns_ranked_call_candidates(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="AAPL",
            price=Decimal("195.00"),
            score=84,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            next_earnings_date=date.today() + timedelta(days=50),
            call_data={
                "calls": [
                    {
                        "strike": 205,
                        "expiration": expiration.isoformat(),
                        "bid": 5.00,
                        "ask": 5.20,
                        "delta": 0.39,
                        "iv": 25.5,
                        "volume": 1200,
                        "open_interest": 5000,
                    },
                    {
                        "strike": 210,
                        "expiration": expiration.isoformat(),
                        "bid": 4.00,
                        "ask": 4.20,
                        "delta": 0.28,
                        "iv": 24.0,
                        "volume": 800,
                        "open_interest": 3000,
                    },
                    {
                        "strike": 220,
                        "expiration": expiration.isoformat(),
                        "bid": 1.50,
                        "ask": 1.60,
                        "delta": 0.16,
                        "iv": 22.0,
                        "volume": 200,
                        "open_interest": 900,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {
                    "symbol": "AAPL",
                    "shares_owned": 200,
                    "cost_basis": 180,
                    "style": "balanced",
                }
            )
        )

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["stock_quality_score"], 84)
        self.assertEqual(payload["technical_score"], Symbol.TechnicalScore.NEUTRAL)
        self.assertEqual(payload["covered_share_lots"], 2)
        self.assertEqual(payload["best_contract"]["strike"], 210.0)
        self.assertEqual(payload["best_contract"]["rating"], "Excellent")
        self.assertEqual(payload["best_contract"]["covered_call_score"], payload["summary"]["covered_call_score"])
        self.assertEqual(payload["best_contract"]["premium_income"], 820.0)
        self.assertEqual(payload["best_contract"]["call_away_risk"], "Low")
        self.assertEqual(len(payload["top_candidates"]), 1)
        self.assertEqual(payload["filters_applied"]["style_delta_min"], 0.25)
        self.assertEqual(payload["filters_applied"]["style_delta_max"], 0.35)
        self.assertEqual(payload["filters_applied"]["style_min_dte"], 21)
        self.assertEqual(payload["filters_applied"]["style_max_dte"], 45)
        self.assertFalse(payload["ex_dividend_risk"]["data_available"])
        self.assertIn("Ex-dividend data is unavailable.", payload["warnings"])

    def test_handle_covered_call_opportunity_ignores_invalid_tradeable_contracts(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="META",
            price=Decimal("500.00"),
            score=86,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            next_earnings_date=date.today() + timedelta(days=60),
            call_data={
                "calls": [
                    {
                        "strike": 520,
                        "expiration": expiration.isoformat(),
                        "bid": 6.10,
                        "ask": 0,
                        "delta": 0.29,
                        "iv": 23.0,
                        "volume": 900,
                        "open_interest": 2500,
                    },
                    {
                        "strike": 525,
                        "expiration": expiration.isoformat(),
                        "bid": 4.90,
                        "ask": 5.20,
                        "delta": 0.30,
                        "iv": 22.0,
                        "volume": 45,
                        "open_interest": 2100,
                    },
                    {
                        "strike": 530,
                        "expiration": expiration.isoformat(),
                        "bid": 4.20,
                        "ask": 4.50,
                        "delta": 0.28,
                        "iv": 21.5,
                        "volume": 300,
                        "open_interest": 2400,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {
                    "symbol": "META",
                    "shares_owned": 100,
                    "style": "balanced",
                }
            )
        )

        self.assertEqual(payload["best_contract"]["strike"], 530.0)
        self.assertEqual(payload["best_contract"]["volume"], 300)

    def test_handle_covered_call_opportunity_style_filters_change_selected_contract(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="MSFT",
            price=Decimal("420.00"),
            score=88,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 430,
                        "expiration": expiration.isoformat(),
                        "bid": 8.00,
                        "ask": 8.20,
                        "delta": 0.48,
                        "iv": 23.0,
                        "volume": 950,
                        "open_interest": 4100,
                    },
                    {
                        "strike": 440,
                        "expiration": expiration.isoformat(),
                        "bid": 5.20,
                        "ask": 5.40,
                        "delta": 0.31,
                        "iv": 21.0,
                        "volume": 700,
                        "open_interest": 3200,
                    },
                    {
                        "strike": 455,
                        "expiration": expiration.isoformat(),
                        "bid": 2.20,
                        "ask": 2.35,
                        "delta": 0.20,
                        "iv": 19.0,
                        "volume": 250,
                        "open_interest": 1500,
                    },
                ]
            },
        )

        balanced_payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {"symbol": "MSFT", "shares_owned": 100, "style": "balanced"}
            )
        )
        income_payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {"symbol": "MSFT", "shares_owned": 100, "style": "income"}
            )
        )

        self.assertEqual(balanced_payload["best_contract"]["strike"], 440.0)
        self.assertEqual(len(balanced_payload["top_candidates"]), 1)
        self.assertEqual(income_payload["best_contract"]["strike"], 430.0)
        self.assertEqual(len(income_payload["top_candidates"]), 1)

    def test_handle_covered_call_opportunity_defaults_to_balanced_income_strategy(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="CRM",
            price=Decimal("280.00"),
            score=82,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 292.5,
                        "expiration": expiration.isoformat(),
                        "bid": 4.80,
                        "ask": 5.00,
                        "delta": 0.31,
                        "iv": 24.0,
                        "volume": 600,
                        "open_interest": 2400,
                    },
                    {
                        "strike": 300,
                        "expiration": expiration.isoformat(),
                        "bid": 2.20,
                        "ask": 2.35,
                        "delta": 0.18,
                        "iv": 21.0,
                        "volume": 300,
                        "open_interest": 1700,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {"symbol": "CRM", "shares_owned": 100}
            )
        )

        self.assertEqual(payload["covered_call_strategy"], "balanced_income")
        self.assertEqual(payload["filters_applied"]["filter_source"], "strategy")
        self.assertEqual(payload["best_contract"]["strike"], 292.5)

    def test_handle_covered_call_opportunity_strategy_filters_change_selected_contract(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="NVDA",
            price=Decimal("500.00"),
            score=90,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 515,
                        "expiration": expiration.isoformat(),
                        "bid": 11.00,
                        "ask": 11.40,
                        "delta": 0.41,
                        "iv": 29.0,
                        "volume": 1300,
                        "open_interest": 6200,
                    },
                    {
                        "strike": 530,
                        "expiration": expiration.isoformat(),
                        "bid": 7.00,
                        "ask": 7.30,
                        "delta": 0.30,
                        "iv": 26.0,
                        "volume": 950,
                        "open_interest": 4800,
                    },
                    {
                        "strike": 550,
                        "expiration": expiration.isoformat(),
                        "bid": 3.20,
                        "ask": 3.35,
                        "delta": 0.18,
                        "iv": 24.0,
                        "volume": 500,
                        "open_interest": 2600,
                    },
                ]
            },
        )

        keep_payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {
                    "symbol": "NVDA",
                    "shares_owned": 100,
                    "covered_call_strategy": "keep_shares_conservative",
                }
            )
        )
        premium_payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {
                    "symbol": "NVDA",
                    "shares_owned": 100,
                    "covered_call_strategy": "high_premium_ok_called",
                }
            )
        )

        self.assertEqual(keep_payload["best_contract"]["strike"], 550.0)
        self.assertEqual(
            keep_payload["filters_applied"]["covered_call_strategy"],
            "keep_shares_conservative",
        )
        self.assertIn(
            "Premium will be lower because you are prioritizing share retention.",
            keep_payload["warnings"],
        )
        self.assertEqual(premium_payload["best_contract"]["strike"], 515.0)
        self.assertEqual(
            premium_payload["filters_applied"]["covered_call_strategy"],
            "high_premium_ok_called",
        )
        self.assertIn(
            "This gives more premium but materially increases the chance your shares are called away.",
            premium_payload["warnings"],
        )

    def test_handle_covered_call_opportunity_exit_strategy_returns_effective_exit_metrics(self) -> None:
        expiration = date.today() + timedelta(days=21)
        Symbol.objects.create(
            ticker="AMZN",
            price=Decimal("100.00"),
            score=80,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 105,
                        "expiration": expiration.isoformat(),
                        "bid": 2.90,
                        "ask": 3.10,
                        "delta": 0.42,
                        "iv": 22.0,
                        "volume": 400,
                        "open_interest": 2000,
                    },
                    {
                        "strike": 110,
                        "expiration": expiration.isoformat(),
                        "bid": 1.50,
                        "ask": 1.70,
                        "delta": 0.28,
                        "iv": 21.0,
                        "volume": 300,
                        "open_interest": 1700,
                    },
                    {
                        "strike": 115,
                        "expiration": expiration.isoformat(),
                        "bid": 0.80,
                        "ask": 0.92,
                        "delta": 0.18,
                        "iv": 20.0,
                        "volume": 250,
                        "open_interest": 1400,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {
                    "symbol": "AMZN",
                    "shares_owned": 100,
                    "cost_basis": 96,
                    "covered_call_strategy": "exit_at_target_price",
                    "target_exit_price": 109,
                }
            )
        )

        self.assertEqual(payload["best_contract"]["strike"], 110.0)
        self.assertEqual(payload["best_contract"]["effective_exit_price"], 111.6)
        self.assertEqual(payload["best_contract"]["gain_if_called_from_cost_basis"], 1560.0)
        self.assertEqual(payload["summary"]["effective_exit_price"], 111.6)

    def test_handle_covered_call_opportunity_wheel_continuation_uses_adjusted_basis(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="ORCL",
            price=Decimal("90.00"),
            score=78,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 93,
                        "expiration": expiration.isoformat(),
                        "bid": 0.90,
                        "ask": 1.10,
                        "delta": 0.28,
                        "iv": 26.0,
                        "volume": 500,
                        "open_interest": 2100,
                    },
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 0.40,
                        "ask": 0.60,
                        "delta": 0.24,
                        "iv": 24.0,
                        "volume": 450,
                        "open_interest": 1800,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {
                    "symbol": "ORCL",
                    "shares_owned": 100,
                    "covered_call_strategy": "wheel_continuation",
                    "assigned_price": 100,
                    "premium_received_from_put": 5,
                }
            )
        )

        self.assertEqual(payload["best_contract"]["strike"], 95.0)
        self.assertEqual(payload["best_contract"]["wheel_cost_basis_before_call"], 95.0)
        self.assertEqual(payload["best_contract"]["adjusted_cost_basis_after_call"], 94.5)

    def test_handle_covered_call_opportunity_exit_strategy_requires_target_exit_price(self) -> None:
        payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {
                    "symbol": "AAPL",
                    "shares_owned": 100,
                    "covered_call_strategy": "exit_at_target_price",
                }
            )
        )

        self.assertIn("error", payload)
        self.assertEqual(
            payload["error"],
            "target_exit_price is required when covered_call_strategy is exit_at_target_price.",
        )

    def test_handle_covered_call_opportunity_requires_100_shares(self) -> None:
        payload = json.loads(
            agent_views._handle_covered_call_opportunity(
                {"symbol": "AAPL", "shares_owned": 75}
            )
        )

        self.assertIn("error", payload)
        self.assertEqual(
            payload["error"],
            "At least 100 shares are required to sell one standard covered call.",
        )

    def test_handle_scan_covered_call_opportunities_ranks_and_filters_delta(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="AAPL",
            price=Decimal("195.00"),
            score=84,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 210,
                        "expiration": expiration.isoformat(),
                        "bid": 4.00,
                        "ask": 4.20,
                        "delta": 0.28,
                        "iv": 24.0,
                        "volume": 800,
                        "open_interest": 3000,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="MSFT",
            price=Decimal("420.00"),
            score=88,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 440,
                        "expiration": expiration.isoformat(),
                        "bid": 5.20,
                        "ask": 5.40,
                        "delta": 0.29,
                        "iv": 21.0,
                        "volume": 700,
                        "open_interest": 3200,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="NFLX",
            price=Decimal("500.00"),
            score=80,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 520,
                        "expiration": expiration.isoformat(),
                        "bid": 6.00,
                        "ask": 6.20,
                        "delta": 0.34,
                        "iv": 27.0,
                        "volume": 650,
                        "open_interest": 2800,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_scan_covered_call_opportunities(
                {"limit": 2, "min_roi": 1.0, "max_delta": 0.30, "max_dte": 35}
            )
        )

        self.assertEqual(payload["total_symbols_scanned"], 3)
        self.assertEqual(payload["results_returned"], 2)
        self.assertEqual(
            [item["ticker"] for item in payload["opportunities"]],
            ["AAPL", "MSFT"],
        )
        self.assertEqual(payload["filters_applied"]["covered_call_strategy"], "balanced_income")
        self.assertEqual(payload["filters_applied"]["shares_assumed"], 100)
        self.assertEqual(payload["opportunities"][0]["covered_call_score"], payload["opportunities"][0]["score"])
        self.assertEqual(payload["opportunities"][0]["premium_yield_pct"], 2.1)
        self.assertIn(
            "Ex-dividend data is unavailable.",
            payload["opportunities"][0]["warnings"],
        )

    def test_handle_scan_covered_call_opportunities_respects_roi_and_dte_filters(
        self,
    ) -> None:
        near_expiration = date.today() + timedelta(days=24)
        longer_expiration = date.today() + timedelta(days=40)
        Symbol.objects.create(
            ticker="AMZN",
            price=Decimal("100.00"),
            score=80,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 110,
                        "expiration": near_expiration.isoformat(),
                        "bid": 1.50,
                        "ask": 1.70,
                        "delta": 0.28,
                        "iv": 21.0,
                        "volume": 300,
                        "open_interest": 1700,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="CRM",
            price=Decimal("280.00"),
            score=82,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 295,
                        "expiration": near_expiration.isoformat(),
                        "bid": 1.20,
                        "ask": 1.30,
                        "delta": 0.27,
                        "iv": 23.0,
                        "volume": 320,
                        "open_interest": 1400,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="ORCL",
            price=Decimal("90.00"),
            score=78,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            call_data={
                "calls": [
                    {
                        "strike": 95,
                        "expiration": longer_expiration.isoformat(),
                        "bid": 1.00,
                        "ask": 1.20,
                        "delta": 0.29,
                        "iv": 24.0,
                        "volume": 450,
                        "open_interest": 1800,
                    }
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_scan_covered_call_opportunities(
                {"limit": 5, "min_roi": 1.0, "max_dte": 30}
            )
        )

        self.assertEqual(payload["total_symbols_scanned"], 3)
        self.assertEqual(payload["results_returned"], 1)
        self.assertEqual(
            [item["ticker"] for item in payload["opportunities"]],
            ["AMZN"],
        )
        self.assertEqual(payload["opportunities"][0]["premium_yield_pct"], 1.6)

    def test_handle_spread_opportunity_returns_bull_put_credit_metrics(self) -> None:
        expiration = date.today() + timedelta(days=39)
        Symbol.objects.create(
            ticker="AAPL",
            price=Decimal("195.25"),
            score=84,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=60),
            option_data={
                "puts": [
                    {
                        "strike": 180,
                        "expiration": expiration.isoformat(),
                        "bid": 1.15,
                        "ask": 1.25,
                        "mid": 1.20,
                        "delta": -0.20,
                        "iv": 29.1,
                        "volume": 740,
                        "open_interest": 5100,
                    },
                    {
                        "strike": 185,
                        "expiration": expiration.isoformat(),
                        "bid": 2.10,
                        "ask": 2.25,
                        "mid": 2.18,
                        "delta": -0.28,
                        "iv": 28.4,
                        "volume": 850,
                        "open_interest": 6200,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_spread_opportunity(
                {
                    "symbol": "AAPL",
                    "spread_type": "bull_put_credit_spread",
                    "risk_profile": "balanced",
                    "width": 5,
                }
            )
        )

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["stock_quality_score"], 84)
        self.assertEqual(payload["technical_score"], Symbol.TechnicalScore.BUY)
        self.assertEqual(payload["best_spread"]["spread_type"], "bull_put_credit_spread")
        self.assertEqual(payload["best_spread"]["net_credit"], 0.98)
        self.assertEqual(payload["best_spread"]["max_profit"], 98.0)
        self.assertEqual(payload["best_spread"]["max_loss"], 402.0)
        self.assertEqual(payload["best_spread"]["breakeven"], 184.02)
        self.assertEqual(payload["best_spread"]["return_on_risk_pct"], 24.38)
        self.assertEqual(payload["best_spread"]["dte"], 39)
        self.assertEqual(len(payload["best_spread"]["legs"]), 2)
        self.assertEqual(payload["summary"]["spread_type"], "bull_put_credit_spread")

    def test_handle_spread_opportunity_ignores_low_volume_legs(self) -> None:
        expiration = date.today() + timedelta(days=35)
        Symbol.objects.create(
            ticker="CRM",
            price=Decimal("250.00"),
            score=82,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=80),
            option_data={
                "puts": [
                    {
                        "strike": 230,
                        "expiration": expiration.isoformat(),
                        "bid": 1.10,
                        "ask": 1.20,
                        "mid": 1.15,
                        "delta": -0.18,
                        "iv": 26.0,
                        "volume": 400,
                        "open_interest": 3000,
                    },
                    {
                        "strike": 235,
                        "expiration": expiration.isoformat(),
                        "bid": 2.10,
                        "ask": 2.25,
                        "mid": 2.18,
                        "delta": -0.26,
                        "iv": 27.0,
                        "volume": 40,
                        "open_interest": 2800,
                    },
                    {
                        "strike": 240,
                        "expiration": expiration.isoformat(),
                        "bid": 3.20,
                        "ask": 3.35,
                        "mid": 3.28,
                        "delta": -0.30,
                        "iv": 28.0,
                        "volume": 320,
                        "open_interest": 3400,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_spread_opportunity(
                {
                    "symbol": "CRM",
                    "spread_type": "bull_put_credit_spread",
                    "risk_profile": "balanced",
                    "width": 10,
                }
            )
        )

        self.assertEqual(payload["best_spread"]["legs"][0]["strike"], 240.0)
        self.assertEqual(payload["best_spread"]["legs"][1]["strike"], 230.0)

    def test_handle_spread_opportunity_auto_prefers_bull_call_debit_when_iv_is_low(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="MSFT",
            price=Decimal("100.00"),
            score=82,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=70),
            option_data={
                "puts": [
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 0.45,
                        "ask": 0.55,
                        "mid": 0.50,
                        "delta": -0.15,
                        "iv": 18.0,
                        "volume": 220,
                        "open_interest": 1600,
                    },
                    {
                        "strike": 100,
                        "expiration": expiration.isoformat(),
                        "bid": 1.15,
                        "ask": 1.25,
                        "mid": 1.20,
                        "delta": -0.24,
                        "iv": 18.5,
                        "volume": 260,
                        "open_interest": 1800,
                    },
                ]
            },
            call_data={
                "calls": [
                    {
                        "strike": 100,
                        "expiration": expiration.isoformat(),
                        "bid": 4.35,
                        "ask": 4.45,
                        "mid": 4.40,
                        "delta": 0.55,
                        "iv": 17.5,
                        "volume": 480,
                        "open_interest": 3200,
                    },
                    {
                        "strike": 110,
                        "expiration": expiration.isoformat(),
                        "bid": 0.95,
                        "ask": 1.05,
                        "mid": 1.00,
                        "delta": 0.28,
                        "iv": 17.8,
                        "volume": 420,
                        "open_interest": 2900,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_spread_opportunity(
                {
                    "symbol": "MSFT",
                    "spread_type": "auto",
                    "directional_view": "bullish",
                    "risk_profile": "balanced",
                    "width": 5,
                }
            )
        )

        self.assertEqual(payload["resolved_directional_view"], "bullish")
        self.assertEqual(
            payload["best_spread"]["spread_type"],
            "bull_call_debit_spread",
        )
        self.assertEqual(payload["best_spread"]["net_debit"], 3.4)
        self.assertEqual(payload["best_spread"]["breakeven"], 103.4)
        self.assertEqual(payload["best_spread"]["reward_to_risk"], 1.94)
        self.assertEqual(
            payload["filters_applied"]["debit_filters"]["min_reward_to_risk"],
            1.5,
        )
        self.assertEqual(payload["summary"]["spread_type"], "bull_call_debit_spread")

    def test_handle_spread_opportunity_returns_iron_condor_structure(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="TSLA",
            price=Decimal("100.00"),
            score=76,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            next_earnings_date=date.today() + timedelta(days=90),
            option_data={
                "puts": [
                    {
                        "strike": 90,
                        "expiration": expiration.isoformat(),
                        "bid": 0.45,
                        "ask": 0.55,
                        "mid": 0.50,
                        "delta": -0.09,
                        "iv": 27.5,
                        "volume": 310,
                        "open_interest": 1500,
                    },
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 0.95,
                        "ask": 1.05,
                        "mid": 1.00,
                        "delta": -0.18,
                        "iv": 28.0,
                        "volume": 420,
                        "open_interest": 2200,
                    },
                ]
            },
            call_data={
                "calls": [
                    {
                        "strike": 105,
                        "expiration": expiration.isoformat(),
                        "bid": 1.05,
                        "ask": 1.15,
                        "mid": 1.10,
                        "delta": 0.17,
                        "iv": 27.9,
                        "volume": 390,
                        "open_interest": 2100,
                    },
                    {
                        "strike": 110,
                        "expiration": expiration.isoformat(),
                        "bid": 0.50,
                        "ask": 0.60,
                        "mid": 0.55,
                        "delta": 0.09,
                        "iv": 27.4,
                        "volume": 305,
                        "open_interest": 1600,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_spread_opportunity(
                {
                    "symbol": "TSLA",
                    "spread_type": "iron_condor",
                    "directional_view": "neutral",
                    "risk_profile": "balanced",
                    "width": 5,
                }
            )
        )

        self.assertEqual(payload["best_spread"]["spread_type"], "iron_condor")
        self.assertEqual(payload["best_spread"]["net_credit"], 1.05)
        self.assertEqual(payload["best_spread"]["max_profit"], 105.0)
        self.assertEqual(payload["best_spread"]["max_loss"], 395.0)
        self.assertEqual(payload["best_spread"]["breakeven_low"], 93.95)
        self.assertEqual(payload["best_spread"]["breakeven_high"], 106.05)
        self.assertEqual(payload["best_spread"]["strategy_fit"], "Neutral income trade")
        self.assertEqual(len(payload["best_spread"]["legs"]), 4)

    def test_handle_spread_opportunity_credit_filters_respect_earnings_policy(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="DHI",
            price=Decimal("150.00"),
            score=78,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=10),
            option_data={
                "puts": [
                    {
                        "strike": 135,
                        "expiration": expiration.isoformat(),
                        "bid": 0.65,
                        "ask": 0.75,
                        "mid": 0.70,
                        "delta": -0.16,
                        "iv": 29.0,
                        "volume": 180,
                        "open_interest": 600,
                    },
                    {
                        "strike": 140,
                        "expiration": expiration.isoformat(),
                        "bid": 1.65,
                        "ask": 1.75,
                        "mid": 1.70,
                        "delta": -0.31,
                        "iv": 29.5,
                        "volume": 220,
                        "open_interest": 850,
                    },
                ]
            },
        )

        conservative_payload = json.loads(
            agent_views._handle_spread_opportunity(
                {
                    "symbol": "DHI",
                    "spread_type": "bull_put_credit_spread",
                    "risk_profile": "conservative",
                    "width": 5,
                }
            )
        )
        aggressive_payload = json.loads(
            agent_views._handle_spread_opportunity(
                {
                    "symbol": "DHI",
                    "spread_type": "bull_put_credit_spread",
                    "risk_profile": "aggressive",
                    "width": 5,
                }
            )
        )

        self.assertIn("error", conservative_payload)
        self.assertEqual(
            conservative_payload["filters_applied"]["credit_filters"][
                "exclude_earnings_before_expiration"
            ],
            True,
        )
        self.assertNotIn("error", aggressive_payload)
        self.assertEqual(
            aggressive_payload["filters_applied"]["credit_filters"]["allow_earnings"],
            True,
        )
        self.assertEqual(
            aggressive_payload["best_spread"]["spread_type"],
            "bull_put_credit_spread",
        )

    def test_handle_scan_spread_opportunities_ranks_credit_spreads_with_filters(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=35)
        Symbol.objects.create(
            ticker="AAPL",
            price=Decimal("200.00"),
            score=88,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.STRONG_BUY,
            next_earnings_date=date.today() + timedelta(days=70),
            option_data={
                "puts": [
                    {
                        "strike": 180,
                        "expiration": expiration.isoformat(),
                        "bid": 1.05,
                        "ask": 1.15,
                        "mid": 1.10,
                        "delta": -0.14,
                        "iv": 25.0,
                        "volume": 520,
                        "open_interest": 2400,
                    },
                    {
                        "strike": 185,
                        "expiration": expiration.isoformat(),
                        "bid": 2.00,
                        "ask": 2.10,
                        "mid": 2.05,
                        "delta": -0.22,
                        "iv": 25.8,
                        "volume": 640,
                        "open_interest": 3100,
                    },
                ]
            },
        )
        Symbol.objects.create(
            ticker="AMZN",
            price=Decimal("140.00"),
            score=82,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=80),
            option_data={
                "puts": [
                    {
                        "strike": 125,
                        "expiration": expiration.isoformat(),
                        "bid": 0.70,
                        "ask": 0.80,
                        "mid": 0.75,
                        "delta": -0.11,
                        "iv": 24.0,
                        "volume": 410,
                        "open_interest": 1800,
                    },
                    {
                        "strike": 130,
                        "expiration": expiration.isoformat(),
                        "bid": 1.80,
                        "ask": 1.90,
                        "mid": 1.85,
                        "delta": -0.19,
                        "iv": 24.5,
                        "volume": 480,
                        "open_interest": 2100,
                    },
                ]
            },
        )
        Symbol.objects.create(
            ticker="DHI",
            price=Decimal("150.00"),
            score=84,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=10),
            option_data={
                "puts": [
                    {
                        "strike": 135,
                        "expiration": expiration.isoformat(),
                        "bid": 0.65,
                        "ask": 0.75,
                        "mid": 0.70,
                        "delta": -0.16,
                        "iv": 29.0,
                        "volume": 180,
                        "open_interest": 600,
                    },
                    {
                        "strike": 140,
                        "expiration": expiration.isoformat(),
                        "bid": 1.65,
                        "ask": 1.75,
                        "mid": 1.70,
                        "delta": -0.23,
                        "iv": 29.5,
                        "volume": 220,
                        "open_interest": 850,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_scan_spread_opportunities(
                {
                    "spread_type": "bull_put_credit_spread",
                    "directional_view": "bullish",
                    "limit": 2,
                    "max_dte": 45,
                    "min_return_on_risk_pct": 20,
                    "min_probability_of_profit": 70,
                    "max_risk": 500,
                    "min_quality_score": 80,
                    "max_short_delta": 0.25,
                    "exclude_earnings": True,
                }
            )
        )

        self.assertEqual(payload["total_symbols_scanned"], 3)
        self.assertEqual(payload["results_returned"], 2)
        self.assertEqual(
            [item["ticker"] for item in payload["opportunities"]],
            ["AMZN", "AAPL"],
        )
        self.assertEqual(
            payload["filters_applied"]["spread_type"],
            "bull_put_credit_spread",
        )
        self.assertEqual(payload["filters_applied"]["exclude_earnings"], True)
        self.assertEqual(
            payload["opportunities"][0]["spread_type"],
            "bull_put_credit_spread",
        )
        self.assertEqual(payload["opportunities"][0]["return_on_risk_pct"], 28.21)
        self.assertEqual(payload["opportunities"][0]["max_short_delta"], 0.19)
        self.assertTrue(payload["opportunities"][0]["short_leg_deltas"])

    def test_handle_scan_spread_opportunities_supports_debit_spreads_via_auto(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="MSFT",
            price=Decimal("100.00"),
            score=82,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=70),
            option_data={
                "puts": [
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 0.45,
                        "ask": 0.55,
                        "mid": 0.50,
                        "delta": -0.15,
                        "iv": 18.0,
                        "volume": 220,
                        "open_interest": 1600,
                    },
                    {
                        "strike": 100,
                        "expiration": expiration.isoformat(),
                        "bid": 1.15,
                        "ask": 1.25,
                        "mid": 1.20,
                        "delta": -0.24,
                        "iv": 18.5,
                        "volume": 260,
                        "open_interest": 1800,
                    },
                ]
            },
            call_data={
                "calls": [
                    {
                        "strike": 100,
                        "expiration": expiration.isoformat(),
                        "bid": 4.35,
                        "ask": 4.45,
                        "mid": 4.40,
                        "delta": 0.55,
                        "iv": 17.5,
                        "volume": 480,
                        "open_interest": 3200,
                    },
                    {
                        "strike": 110,
                        "expiration": expiration.isoformat(),
                        "bid": 0.95,
                        "ask": 1.05,
                        "mid": 1.00,
                        "delta": 0.28,
                        "iv": 17.8,
                        "volume": 420,
                        "open_interest": 2900,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_scan_spread_opportunities(
                {
                    "spread_type": "auto",
                    "directional_view": "bullish",
                    "max_dte": 35,
                    "min_return_on_risk_pct": 150,
                    "max_risk": 400,
                    "max_short_delta": 0.30,
                }
            )
        )

        self.assertEqual(payload["total_symbols_scanned"], 1)
        self.assertEqual(payload["results_returned"], 1)
        self.assertEqual(payload["opportunities"][0]["ticker"], "MSFT")
        self.assertEqual(
            payload["opportunities"][0]["spread_type"],
            "bull_call_debit_spread",
        )
        self.assertEqual(payload["opportunities"][0]["net_debit"], 3.4)
        self.assertEqual(payload["opportunities"][0]["reward_to_risk"], 1.94)
        self.assertEqual(payload["opportunities"][0]["return_on_risk_pct"], 194.0)
        self.assertEqual(payload["opportunities"][0]["max_short_delta"], 0.28)

    def test_handle_spread_opportunity_balanced_credit_defaults_to_45_dte(
        self,
    ) -> None:
        near_expiration = date.today() + timedelta(days=40)
        far_expiration = date.today() + timedelta(days=50)
        Symbol.objects.create(
            ticker="QCOM",
            price=Decimal("160.00"),
            score=83,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=80),
            option_data={
                "puts": [
                    {
                        "strike": 145,
                        "expiration": near_expiration.isoformat(),
                        "bid": 0.55,
                        "ask": 0.65,
                        "mid": 0.60,
                        "delta": -0.14,
                        "iv": 26.0,
                        "volume": 400,
                        "open_interest": 1800,
                    },
                    {
                        "strike": 150,
                        "expiration": near_expiration.isoformat(),
                        "bid": 1.70,
                        "ask": 1.80,
                        "mid": 1.75,
                        "delta": -0.24,
                        "iv": 26.5,
                        "volume": 520,
                        "open_interest": 2400,
                    },
                    {
                        "strike": 145,
                        "expiration": far_expiration.isoformat(),
                        "bid": 0.75,
                        "ask": 0.85,
                        "mid": 0.80,
                        "delta": -0.15,
                        "iv": 26.2,
                        "volume": 420,
                        "open_interest": 1900,
                    },
                    {
                        "strike": 150,
                        "expiration": far_expiration.isoformat(),
                        "bid": 2.05,
                        "ask": 2.15,
                        "mid": 2.10,
                        "delta": -0.25,
                        "iv": 26.8,
                        "volume": 560,
                        "open_interest": 2500,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_spread_opportunity(
                {
                    "symbol": "QCOM",
                    "spread_type": "bull_put_credit_spread",
                    "risk_profile": "balanced",
                    "width": 5,
                }
            )
        )

        self.assertEqual(payload["filters_applied"]["credit_filters"]["max_dte"], 45)
        self.assertEqual(
            payload["best_spread"]["expiration"], near_expiration.isoformat()
        )
        self.assertTrue(all(item["dte"] <= 45 for item in payload["top_candidates"]))

    def test_handle_scan_spread_opportunities_respects_risk_profile(self) -> None:
        expiration = date.today() + timedelta(days=50)
        Symbol.objects.create(
            ticker="MSFG",
            price=Decimal("100.00"),
            score=82,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=90),
            call_data={
                "calls": [
                    {
                        "strike": 100,
                        "expiration": expiration.isoformat(),
                        "bid": 4.35,
                        "ask": 4.45,
                        "mid": 4.40,
                        "delta": 0.55,
                        "iv": 17.5,
                        "volume": 480,
                        "open_interest": 3200,
                    },
                    {
                        "strike": 110,
                        "expiration": expiration.isoformat(),
                        "bid": 0.95,
                        "ask": 1.05,
                        "mid": 1.00,
                        "delta": 0.28,
                        "iv": 17.8,
                        "volume": 420,
                        "open_interest": 2900,
                    },
                ]
            },
        )

        conservative_payload = json.loads(
            agent_views._handle_scan_spread_opportunities(
                {
                    "spread_type": "bull_call_debit_spread",
                    "directional_view": "bullish",
                    "risk_profile": "conservative",
                    "limit": 5,
                }
            )
        )
        aggressive_payload = json.loads(
            agent_views._handle_scan_spread_opportunities(
                {
                    "spread_type": "bull_call_debit_spread",
                    "directional_view": "bullish",
                    "risk_profile": "aggressive",
                    "limit": 5,
                }
            )
        )

        self.assertEqual(conservative_payload["risk_profile_used"], "conservative")
        self.assertEqual(conservative_payload["results_returned"], 0)
        self.assertEqual(aggressive_payload["risk_profile_used"], "aggressive")
        self.assertEqual(aggressive_payload["results_returned"], 1)
        self.assertEqual(aggressive_payload["opportunities"][0]["ticker"], "MSFG")

    def test_handle_scan_spread_opportunities_normalizes_fractional_iv(self) -> None:
        expiration = date.today() + timedelta(days=30)
        Symbol.objects.create(
            ticker="ARM",
            price=Decimal("200.00"),
            score=86,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.STRONG_BUY,
            next_earnings_date=date.today() + timedelta(days=90),
            option_iv=Decimal("1.0500"),
            option_data={
                "puts": [
                    {
                        "strike": 180,
                        "expiration": expiration.isoformat(),
                        "bid": 0.95,
                        "ask": 1.05,
                        "mid": 1.00,
                        "delta": -0.14,
                        "iv": 1.05,
                        "volume": 420,
                        "open_interest": 1900,
                    },
                    {
                        "strike": 185,
                        "expiration": expiration.isoformat(),
                        "bid": 2.00,
                        "ask": 2.10,
                        "mid": 2.05,
                        "delta": -0.24,
                        "iv": 1.05,
                        "volume": 560,
                        "open_interest": 2400,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_scan_spread_opportunities(
                {
                    "spread_type": "bull_put_credit_spread",
                    "directional_view": "bullish",
                    "risk_profile": "conservative",
                    "limit": 5,
                }
            )
        )

        self.assertEqual(payload["results_returned"], 1)
        self.assertEqual(payload["opportunities"][0]["ticker"], "ARM")
        self.assertEqual(payload["opportunities"][0]["avg_iv"], 105.0)
        self.assertEqual(payload["opportunities"][0]["legs"][0]["iv"], 105.0)
        self.assertNotIn(
            "IV is low for a premium-selling spread.",
            payload["opportunities"][0]["warnings"],
        )

    def test_handle_compare_spread_candidates_ranks_symbols(self) -> None:
        expiration = date.today() + timedelta(days=35)
        Symbol.objects.create(
            ticker="AAPL",
            price=Decimal("200.00"),
            score=88,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.STRONG_BUY,
            next_earnings_date=date.today() + timedelta(days=70),
            option_data={
                "puts": [
                    {
                        "strike": 180,
                        "expiration": expiration.isoformat(),
                        "bid": 1.05,
                        "ask": 1.15,
                        "mid": 1.10,
                        "delta": -0.14,
                        "iv": 25.0,
                        "volume": 520,
                        "open_interest": 2400,
                    },
                    {
                        "strike": 185,
                        "expiration": expiration.isoformat(),
                        "bid": 2.00,
                        "ask": 2.10,
                        "mid": 2.05,
                        "delta": -0.22,
                        "iv": 25.8,
                        "volume": 640,
                        "open_interest": 3100,
                    },
                ]
            },
        )
        Symbol.objects.create(
            ticker="AMZN",
            price=Decimal("140.00"),
            score=82,
            classification="Quality (selective)",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=80),
            option_data={
                "puts": [
                    {
                        "strike": 125,
                        "expiration": expiration.isoformat(),
                        "bid": 0.70,
                        "ask": 0.80,
                        "mid": 0.75,
                        "delta": -0.18,
                        "iv": 24.0,
                        "volume": 410,
                        "open_interest": 1800,
                    },
                    {
                        "strike": 130,
                        "expiration": expiration.isoformat(),
                        "bid": 1.85,
                        "ask": 1.95,
                        "mid": 1.90,
                        "delta": -0.27,
                        "iv": 24.5,
                        "volume": 480,
                        "open_interest": 2100,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_compare_spread_candidates(
                {
                    "symbols": ["AAPL", "AMZN", "MISSING"],
                    "spread_type": "bull_put_credit_spread",
                    "directional_view": "bullish",
                    "risk_profile": "balanced",
                    "width": 5,
                }
            )
        )

        self.assertEqual(payload["comparison_mode"], "ticker_comparison")
        self.assertEqual(payload["candidates_compared"], 2)
        self.assertEqual(payload["winner"]["symbol"], "AMZN")
        self.assertEqual(
            [item["symbol"] for item in payload["ranked_candidates"]],
            ["AMZN", "AAPL"],
        )
        self.assertEqual(
            payload["ranked_candidates"][0]["spread_type_requested"],
            "bull_put_credit_spread",
        )
        self.assertTrue(any(item["symbol"] == "MISSING" for item in payload["skipped"]))

    def test_handle_compare_spread_candidates_supports_spread_type_comparison(
        self,
    ) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="MSFT",
            price=Decimal("100.00"),
            score=82,
            classification="High-quality compounder",
            technical_score=Symbol.TechnicalScore.BUY,
            next_earnings_date=date.today() + timedelta(days=70),
            option_data={
                "puts": [
                    {
                        "strike": 90,
                        "expiration": expiration.isoformat(),
                        "bid": 0.40,
                        "ask": 0.50,
                        "mid": 0.45,
                        "delta": -0.16,
                        "iv": 18.0,
                        "volume": 220,
                        "open_interest": 1600,
                    },
                    {
                        "strike": 95,
                        "expiration": expiration.isoformat(),
                        "bid": 1.35,
                        "ask": 1.45,
                        "mid": 1.40,
                        "delta": -0.26,
                        "iv": 18.5,
                        "volume": 260,
                        "open_interest": 1800,
                    },
                ]
            },
            call_data={
                "calls": [
                    {
                        "strike": 100,
                        "expiration": expiration.isoformat(),
                        "bid": 4.35,
                        "ask": 4.45,
                        "mid": 4.40,
                        "delta": 0.55,
                        "iv": 17.5,
                        "volume": 480,
                        "open_interest": 3200,
                    },
                    {
                        "strike": 110,
                        "expiration": expiration.isoformat(),
                        "bid": 0.95,
                        "ask": 1.05,
                        "mid": 1.00,
                        "delta": 0.28,
                        "iv": 17.8,
                        "volume": 420,
                        "open_interest": 2900,
                    },
                ]
            },
        )

        payload = json.loads(
            agent_views._handle_compare_spread_candidates(
                {
                    "symbol": "MSFT",
                    "spread_types": [
                        "bull_put_credit_spread",
                        "bull_call_debit_spread",
                    ],
                    "directional_view": "bullish",
                    "risk_profile": "balanced",
                }
            )
        )

        self.assertEqual(payload["comparison_mode"], "spread_type_comparison")
        self.assertEqual(payload["candidates_compared"], 2)
        self.assertEqual(payload["winner"]["symbol"], "MSFT")
        self.assertEqual(
            payload["winner"]["spread_type_requested"], "bull_call_debit_spread"
        )
        self.assertEqual(
            [item["spread_type_requested"] for item in payload["ranked_candidates"]],
            ["bull_call_debit_spread", "bull_put_credit_spread"],
        )
