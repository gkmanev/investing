from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from unittest.mock import MagicMock, patch

from api.management.commands.fetch_profile_data import API_HEADERS, PROFILE_ENDPOINT

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

        self.assertEqual(ScreenerType.objects.count(), 2)
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
        filters = list(screener.filters.all())
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0].label, "Volume Surge")
        self.assertEqual(filters[0].payload, "Volume Surge")

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

        filter_obj = screener.filters.get()
        self.assertEqual(
            filter_obj.payload,
            {"field": "sample", "quant_rating": ["strong_buy", "buy"]},
        )
        self.assertEqual(
            filter_obj.label,
            'field=sample, quant_rating=["strong_buy", "buy"]',
        )


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
        result = call_command("fetch_screener_results", self.screener.name, stdout=buffer)

        expected_output = "Apple Inc.\nMicrosoft Corporation\nTesla, Inc."
        self.assertEqual(result, expected_output)
        self.assertEqual(buffer.getvalue(), expected_output + "\n")

        tickers = Investment.objects.order_by("ticker").values_list("ticker", flat=True)
        self.assertEqual(list(tickers), ["Apple Inc.", "Microsoft Corporation", "Tesla, Inc."])
        self.assertTrue(
            Investment.objects.filter(ticker="Apple Inc.", category="stock").exists()
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
            self.screener.name,
            "--per-page",
            "1",
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
            self.screener.name,
            "--type",
            "fund",
        )

        investment.refresh_from_db()
        self.assertEqual(investment.category, "fund")

    @patch("api.management.commands.fetch_screener_results.requests.post")
    def test_command_applies_market_cap_argument(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"data": [{"attributes": {"name": "Example"}}]},
            text="{}",
        )

        buffer = StringIO()
        call_command("fetch_screener_results", self.screener.name, "--market-cap", "10B", stdout=buffer)

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
            self.screener.name,
            "--min-price",
            "10",
            "--max-price",
            "25.5",
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertIn("close", payload)
        self.assertEqual(payload["close"].get("gte"), 10.0)
        self.assertEqual(payload["close"].get("lte"), 25.5)

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
            nested_screener.name,
            "--market-cap",
            "5B",
            "--min-price",
            "12",
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

    def test_command_rejects_invalid_market_cap_argument(self) -> None:
        with self.assertRaisesMessage(CommandError, "Market cap value must be a number optionally followed by K, M, B, or T."):
            call_command("fetch_screener_results", self.screener.name, "--market-cap", "ten-billion")

    def test_command_rejects_invalid_price_arguments(self) -> None:
        with self.assertRaisesMessage(CommandError, "Price filters must be numeric values."):
            call_command(
                "fetch_screener_results",
                self.screener.name,
                "--min-price",
                "ten",
            )

        with self.assertRaisesMessage(CommandError, "Minimum price cannot be greater than maximum price."):
            call_command(
                "fetch_screener_results",
                self.screener.name,
                "--min-price",
                "50",
                "--max-price",
                "10",
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
            call_command("fetch_screener_results", self.screener.name)


class FetchProfileDataCommandTests(APITestCase):
    def setUp(self) -> None:
        self.investments = [
            Investment.objects.create(ticker="AAA", category="stock"),
            Investment.objects.create(ticker="BBB", category="stock"),
            Investment.objects.create(ticker="CCC", category="stock"),
            Investment.objects.create(ticker="DDD", category="stock"),
        ]

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_updates_all_investments_in_chunks(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = [
            MagicMock(
                status_code=200,
                json=lambda: [
                    {"ticker": "AAA"},
                    {"ticker": "BBB"},
                    {"ticker": "CCC"},
                    {"ticker": "DDD"},
                ],
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "AAA",
                            "type": "profile",
                            "attributes": {
                                "lastDaily": {"last": "10.5"},
                                "marketCap": "1000",
                            },
                        },
                        {
                            "id": "BBB",
                            "type": "profile",
                            "attributes": {
                                "lastDaily": {"last": "12.25"},
                                "marketCap": "2000",
                            },
                        },
                    ]
                },
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "CCC",
                            "type": "profile",
                            "attributes": {
                                "lastDaily": {"last": "8.75"},
                                "marketCap": "3000",
                            },
                        },
                        {
                            "id": "DDD",
                            "type": "profile",
                            "attributes": {
                                "lastDaily": {"last": "9.00"},
                                "marketCap": "4000",
                            },
                        },
                    ]
                },
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "CCC",
                            "attributes": {
                                "lastDaily": {"last": "3"},
                                "marketCap": "4",
                            },
                        }
                    ]
                },
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "CCC",
                            "attributes": {
                                "lastDaily": {"last": "3"},
                                "marketCap": "4",
                            },
                        }
                    ]
                },
                text="{}",
            ),
        ]

        with patch("api.management.commands.fetch_profile_data.PROFILE_CHUNK_SIZE", 2):
            call_command("fetch_profile_data")

        for ticker, price, market_cap in (
            ("AAA", "10.5000", "1000.00"),
            ("BBB", "12.2500", "2000.00"),
            ("CCC", "8.7500", "3000.00"),
            ("DDD", "9.0000", "4000.00"),
        ):
            investment = Investment.objects.get(ticker=ticker)
            self.assertEqual(str(investment.price), price)
            self.assertEqual(str(investment.market_cap), market_cap)

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_errors_on_unsuccessful_response(self, mock_get: MagicMock) -> None:
        mock_get.return_value = MagicMock(status_code=500, text="error")

        with self.assertRaises(CommandError):
            call_command("fetch_profile_data")

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_profile_request_includes_tickers_query(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = [
            MagicMock(
                status_code=200,
                json=lambda: [
                    {"ticker": "AAA"},
                    {"ticker": "BBB"},
                    {"ticker": "CCC"},
                ],
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "AAA",
                            "attributes": {
                                "lastDaily": {"last": "1"},
                                "marketCap": "2",
                            },
                        },
                        {
                            "id": "BBB",
                            "attributes": {
                                "lastDaily": {"last": "2"},
                                "marketCap": "3",
                            },
                        },
                    ]
                },
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "CCC",
                            "attributes": {
                                "lastDaily": {"last": "3"},
                                "marketCap": "4",
                            },
                        }
                    ]
                },
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "CCC",
                            "attributes": {
                                "lastDaily": {"last": "3"},
                                "marketCap": "4",
                            },
                        }
                    ]
                },
                text="{}",
            ),
        ]

        with patch("api.management.commands.fetch_profile_data.PROFILE_CHUNK_SIZE", 2):
            call_command("fetch_profile_data")

        self.assertGreaterEqual(len(mock_get.call_args_list), 3)
        first_profile_call = mock_get.call_args_list[1]
        second_profile_call = mock_get.call_args_list[2]
        self.assertEqual(
            first_profile_call.args[0],
            f"{PROFILE_ENDPOINT}?symbols=AAA%2CBBB",
        )
        self.assertEqual(
            second_profile_call.args[0],
            f"{PROFILE_ENDPOINT}?symbols=CCC",
        )
        self.assertIsNone(first_profile_call.kwargs.get("params"))
        self.assertEqual(first_profile_call.kwargs.get("headers"), API_HEADERS)

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_logs_profile_requests(self, mock_get: MagicMock) -> None:
        responses = [
            MagicMock(
                status_code=200,
                json=lambda: [
                    {"ticker": "AAA"},
                    {"ticker": "BBB"},
                    {"ticker": "CCC"},
                ],
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "AAA",
                            "attributes": {
                                "lastDaily": {"last": "1"},
                                "marketCap": "2",
                            },
                        },
                        {
                            "id": "BBB",
                            "attributes": {
                                "lastDaily": {"last": "2"},
                                "marketCap": "3",
                            },
                        },
                    ]
                },
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "CCC",
                            "attributes": {
                                "lastDaily": {"last": "3"},
                                "marketCap": "4",
                            },
                        }
                    ]
                },
                text="{}",
            ),
        ]
        fallback_response = MagicMock(
            status_code=200,
            json=lambda: {"data": []},
            text="{}",
        )

        def responder(*_: object, **__: object):
            if responses:
                return responses.pop(0)
            return fallback_response

        mock_get.side_effect = responder

        buffer = StringIO()
        with patch("api.management.commands.fetch_profile_data.PROFILE_CHUNK_SIZE", 2):
            call_command("fetch_profile_data", stdout=buffer)

        output = buffer.getvalue()
        self.assertIn("Requesting profile data for AAA, BBB", output)
        self.assertIn("Requesting profile data for CCC", output)
        self.assertIn(PROFILE_ENDPOINT, output)

    @patch("api.management.commands.fetch_profile_data.requests.get")
    def test_command_retries_missing_profiles_with_smaller_chunks(
        self, mock_get: MagicMock
    ) -> None:
        Investment.objects.get_or_create(ticker="AAA", defaults={"category": "stock"})
        Investment.objects.get_or_create(ticker="BBB", defaults={"category": "stock"})

        mock_get.side_effect = [
            MagicMock(
                status_code=200,
                json=lambda: [
                    {"ticker": "AAA"},
                    {"ticker": "BBB"},
                ],
                text="{}",
            ),
            MagicMock(status_code=200, json=lambda: {"data": []}, text="{}"),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "AAA",
                            "attributes": {
                                "lastDaily": {"last": "1.5"},
                                "marketCap": "2.5",
                            },
                        }
                    ]
                },
                text="{}",
            ),
            MagicMock(
                status_code=200,
                json=lambda: {
                    "data": [
                        {
                            "id": "BBB",
                            "attributes": {
                                "lastDaily": {"last": "3.5"},
                                "marketCap": "4.5",
                            },
                        }
                    ]
                },
                text="{}",
            ),
        ]

        buffer = StringIO()
        with patch("api.management.commands.fetch_profile_data.PROFILE_CHUNK_SIZE", 2):
            call_command("fetch_profile_data", stdout=buffer)

        prices = Investment.objects.in_bulk(field_name="ticker")
        self.assertEqual(prices["AAA"].price, Decimal("1.5"))
        self.assertEqual(prices["BBB"].price, Decimal("3.5"))

        urls = [call.args[0] for call in mock_get.call_args_list[1:]]
        self.assertEqual(
            urls[0],
            f"{PROFILE_ENDPOINT}?symbols=AAA%2CBBB",
        )
        self.assertEqual(urls[1], f"{PROFILE_ENDPOINT}?symbols=AAA")
        self.assertEqual(urls[2], f"{PROFILE_ENDPOINT}?symbols=BBB")
        output = buffer.getvalue()
        self.assertIn("Requesting profile data for AAA, BBB", output)
        self.assertIn("Requesting profile data for AAA", output)
        self.assertIn("Requesting profile data for BBB", output)
