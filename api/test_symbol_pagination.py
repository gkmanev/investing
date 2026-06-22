from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from api.models import Symbol


class SymbolPaginationTests(APITestCase):
    def test_symbols_default_page_size_is_25(self) -> None:
        for index in range(30):
            Symbol.objects.create(ticker=f"TICK{index:02d}")

        response = self.client.get(reverse("symbol-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 30)
        self.assertEqual(response.data["page"], 1)
        self.assertEqual(response.data["page_size"], 25)
        self.assertEqual(len(response.data["results"]), 25)
        self.assertTrue(response.data["has_more"])
