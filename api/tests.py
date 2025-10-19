from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from unittest.mock import MagicMock, patch

from .models import Investment, ScreenerFilter, ScreenerType


class InvestmentAPITestCase(APITestCase):
    def setUp(self) -> None:
        self.list_url = reverse("investment-list")
        self.detail_url_name = "investment-detail"

    def create_investment(self, **overrides):
        defaults = {
            "name": "Index Fund",
            "ticker": "IDX",
            "category": "Fund",
            "risk_level": "medium",
            "description": "Diversified index fund.",
        }
        defaults.update(overrides)
        return Investment.objects.create(**defaults)

    def test_can_create_investment(self) -> None:
        payload = {
            "name": "Growth Fund",
            "ticker": "GRW",
            "category": "Fund",
            "risk_level": "medium",
            "description": "Long-term growth fund.",
        }

        response = self.client.post(self.list_url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Investment.objects.count(), 1)
        self.assertEqual(Investment.objects.get().ticker, "GRW")

    def test_list_returns_created_items(self) -> None:
        self.create_investment(name="Bond ETF", ticker="BND", risk_level="low")

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["ticker"], "BND")

    def test_can_update_investment(self) -> None:
        investment = self.create_investment()
        url = reverse(self.detail_url_name, args=[investment.id])

        response = self.client.patch(url, {"risk_level": "high"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        investment.refresh_from_db()
        self.assertEqual(investment.risk_level, "high")

    def test_cannot_create_invalid_investment(self) -> None:
        response = self.client.post(
            self.list_url,
            {
                "name": "   ",
                "ticker": "",
                "category": "Fund",
                "risk_level": "medium",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)
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
        self.assertEqual(second.filters.count(), 1)
        growth_filter = second.filters.get()
        self.assertEqual(
            growth_filter.label,
            "field=revenue_growth, operator=>, value=0.2",
        )
        self.assertEqual(
            growth_filter.payload,
            {"field": "revenue_growth", "operator": ">", "value": 0.2},
        )
        self.assertNotIn("industry_id", growth_filter.payload)
        self.assertNotIn("industryId", growth_filter.payload)
        self.assertNotIn("industryId", growth_filter.label)

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
    def test_command_prints_ticker_names(self, mock_post: MagicMock) -> None:
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
        )

        _, kwargs = mock_post.call_args
        payload = kwargs["json"]
        self.assertIn("filter", payload)
        self.assertIn("asset_primary_sector", payload["filter"])
        self.assertEqual(payload["filter"]["asset_primary_sector"].get("eq"), "Energy")
        self.assertIn("marketcap_display", payload["filter"])
        self.assertEqual(payload["filter"]["marketcap_display"].get("gte"), 5_000_000_000)

    def test_command_rejects_invalid_market_cap_argument(self) -> None:
        with self.assertRaisesMessage(CommandError, "Market cap value must be a number optionally followed by K, M, B, or T."):
            call_command("fetch_screener_results", self.screener.name, "--market-cap", "ten-billion")

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
