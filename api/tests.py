from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Investment


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
