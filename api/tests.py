from datetime import date, datetime
from decimal import Decimal
from io import StringIO
import json

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from unittest.mock import MagicMock, call, patch

from api.custom_filters import CUSTOM_FILTER_PAYLOAD
from api.management.commands.fetch_profile_data import (
    API_HEADERS,
    OPTION_EXPIRATIONS_ENDPOINT,
    PROFILE_ENDPOINT,
    Command,
)

from .models import Investment, ScreenerFilter, ScreenerType


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

        self.assertEqual(ScreenerType.objects.count(), 3)
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

        custom_screener = ScreenerType.objects.get(name="Custom screener filter")
        custom_filters = list(custom_screener.filters.order_by("display_order"))
        self.assertEqual(len(custom_filters), 1)
        self.assertEqual(custom_filters[0].label, "Custom screener filter")
        self.assertEqual(custom_filters[0].payload, CUSTOM_FILTER_PAYLOAD)
        self.assertEqual(custom_filters[0].display_order, 1)

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

        custom_screener = ScreenerType.objects.get(name="Custom screener filter")
        custom_filters = list(custom_screener.filters.order_by("display_order"))
        self.assertEqual(len(custom_filters), 1)
        self.assertEqual(custom_filters[0].label, "Custom screener filter")
        self.assertEqual(custom_filters[0].payload, CUSTOM_FILTER_PAYLOAD)
        self.assertEqual(custom_filters[0].display_order, 1)

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
        custom_screener = ScreenerType.objects.get(name="Custom screener filter")
        custom_filters = list(custom_screener.filters.order_by("display_order"))
        self.assertEqual(len(custom_filters), 1)
        self.assertEqual(custom_filters[0].label, "Custom screener filter")
        self.assertEqual(custom_filters[0].payload, CUSTOM_FILTER_PAYLOAD)

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
        self.assertNotIn("exchange", payload)

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

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_sets_options_suitability_true_with_three_expirations(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_expirations.return_value = {
            "dates": self._build_next_month_dates([5, 12, 19]),
            "ticker_id": "AAA",
        }
        mock_get.side_effect = [
            MagicMock(
                status_code=200, json=lambda: [{"ticker": "AAA"}], text="{}"
            ),
            MagicMock(
                status_code=200,
                json=lambda: {"data": {"attributes": {"last": 123.45}}},
                text="{}",
            ),
        ]

        buffer = StringIO()
        call_command("fetch_profile_data", screener_name=self.screener_name, stdout=buffer)
        investment = Investment.objects.get(ticker="AAA")
        self.assertEqual(investment.options_suitability, 1)
        self.assertEqual(investment.price, Decimal("123.45"))
        expected_expiration = max(
            self._parse_date(value)
            for value in mock_expirations.return_value["dates"]
        )
        self.assertEqual(investment.option_exp, expected_expiration)
        expected_url = requests.Request(
            "GET",
            PROFILE_ENDPOINT,
            params={"symbols": "AAA"},
        ).prepare().url
        self.assertIn(f"Fetching profile data from {expected_url}", buffer.getvalue())

    @patch(
        "api.management.commands.fetch_profile_data.Command._calculate_options_suitability",
        return_value=1,
    )
    @patch("api.management.commands.fetch_profile_data.Command._select_closest_dates")
    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_prefers_closest_expirations_for_option_exp(
        self,
        mock_get: MagicMock,
        mock_expirations: MagicMock,
        mock_select_closest: MagicMock,
        _mock_options_suitability: MagicMock,
    ) -> None:
        mock_get.side_effect = [
            MagicMock(
                status_code=200, json=lambda: [{"ticker": "AAA"}], text="{}"
            ),
            MagicMock(
                status_code=200,
                json=lambda: {"data": {"attributes": {"last": 41.45}}},
                text="{}",
            ),
        ]
        close_dates = [date(2026, 1, 2), date(2025, 12, 26)]
        mock_select_closest.return_value = close_dates
        mock_expirations.return_value = {
            "dates": ["01/02/2026", "12/26/2025", "01/21/2028"],
            "ticker_id": "1105",
        }

        buffer = StringIO()
        call_command(
            "fetch_profile_data", screener_name=self.screener_name, stdout=buffer
        )

        investment = Investment.objects.get(ticker="AAA")
        self.assertEqual(investment.option_exp, max(close_dates))
        mock_select_closest.assert_called_once()
        self.assertIn("furthest: 2026-01-02", buffer.getvalue())

    @patch("api.management.commands.fetch_profile_data.Command._fetch_last_price", return_value=None)
    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_reports_when_price_missing(
        self, mock_get: MagicMock, mock_expirations: MagicMock, mock_last_price: MagicMock
    ) -> None:
        mock_expirations.return_value = {
            "dates": self._build_next_month_dates([5, 12, 19, 26]),
            "ticker_id": "AAA",
        }
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: [{"ticker": "AAA"}], text="{}"
        )

        buffer = StringIO()
        call_command("fetch_profile_data", screener_name=self.screener_name, stdout=buffer)

        investment = Investment.objects.get(ticker="AAA")
        self.assertEqual(investment.options_suitability, 1)
        self.assertIsNone(investment.price)
        expected_expiration = max(
            self._parse_date(value)
            for value in mock_expirations.return_value["dates"]
        )
        self.assertEqual(investment.option_exp, expected_expiration)
        self.assertIn(
            "AAA: suitability met but profile returned no price; leaving price unchanged.",
            buffer.getvalue(),
        )

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_updates_price_from_profile_when_suitability_is_true(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        Investment.objects.filter(ticker="AAA").update(price=Decimal("5.00"))
        mock_expirations.return_value = {
            "dates": self._build_next_month_dates([5, 12, 19, 26]),
            "ticker_id": "AAA",
        }
        mock_get.side_effect = [
            MagicMock(
                status_code=200, json=lambda: [{"ticker": "AAA"}], text="{}"
            ),
            MagicMock(
                status_code=200,
                json=lambda: {"data": {"attributes": {"last": 42.15}}},
                text="{}",
            ),
        ]

        call_command("fetch_profile_data", screener_name=self.screener_name)

        investment = Investment.objects.get(ticker="AAA")
        self.assertEqual(investment.options_suitability, 1)
        self.assertEqual(investment.price, Decimal("42.15"))
        expected_expiration = max(
            self._parse_date(value)
            for value in mock_expirations.return_value["dates"]
        )
        self.assertEqual(investment.option_exp, expected_expiration)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_sets_options_suitability_false_with_fewer_than_three_expirations(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_expirations.return_value = {
            "dates": self._build_next_month_dates([5, 12]),
            "ticker_id": "AAA",
        }
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: [{"ticker": "AAA"}], text="{}"
        )

        call_command("fetch_profile_data", screener_name=self.screener_name)
        investment = Investment.objects.get(ticker="AAA")
        self.assertEqual(investment.options_suitability, 0)
        self.assertIsNone(investment.option_exp)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_sets_options_suitability_unknown_with_no_expirations(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_expirations.return_value = {"dates": [], "ticker_id": "AAA"}
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: [{"ticker": "AAA"}], text="{}"
        )

        call_command("fetch_profile_data", screener_name=self.screener_name)
        investment = Investment.objects.get(ticker="AAA")
        self.assertEqual(investment.options_suitability, -1)
        self.assertIsNone(investment.option_exp)

    @patch("api.management.commands.fetch_profile_data.Command._fetch_option_expirations")
    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_creates_missing_investments(
        self, mock_get: MagicMock, mock_expirations: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"ticker": "NEW1"},
                {"ticker": "NEW2"},
            ],
            text="{}",
        )
        mock_expirations.return_value = {"dates": [], "ticker_id": "NEW"}

        buffer = StringIO()
        call_command("fetch_profile_data", screener_name=self.screener_name, stdout=buffer)

        for ticker in ("NEW1", "NEW2"):
            investment = Investment.objects.get(ticker=ticker)
            self.assertEqual(investment.category, "stock")
            self.assertEqual(investment.options_suitability, -1)
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
                {"ticker": "NEW1"},
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
            status_code=200, json=lambda: [{"ticker": "AAA"}], text="{}"
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
                {"ticker": "AAA"},
                {"ticker": "BBB"},
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
                {"ticker": "AAA"},
                {"ticker": "BBB"},
                {"ticker": "CCC"},
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
            status_code=200, json=lambda: [{"ticker": "AAA"}], text="{}"
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

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_fetch_last_price_uses_expected_headers_and_params(
        self, mock_get: MagicMock
    ) -> None:
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": {"attributes": {"last": "10.50"}}},
            text="{}",
        )

        command = Command()
        price = command._fetch_last_price("XYZ")

        mock_get.assert_called_once()
        request = mock_get.call_args
        self.assertEqual(request.args[0], PROFILE_ENDPOINT)
        self.assertEqual(request.kwargs.get("params"), {"symbols": "XYZ"})
        self.assertEqual(request.kwargs.get("headers"), API_HEADERS)
        self.assertEqual(price, Decimal("10.50"))

    def test_extract_last_price_accepts_nested_price_block(self) -> None:
        command = Command()

        price = command._extract_last_price(
            {"data": {"attributes": {"price": {"last": "77.01"}}}}
        )

        self.assertEqual(price, Decimal("77.01"))

    def test_extract_last_price_accepts_last_daily_block(self) -> None:
        command = Command()

        price = command._extract_last_price(
            {"data": [{"attributes": {"lastDaily": {"last": "41.45"}}}]}
        )

        self.assertEqual(price, Decimal("41.45"))

