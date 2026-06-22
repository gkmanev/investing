import json
from datetime import date, timedelta
from decimal import Decimal

from rest_framework.test import APITestCase

from api import agent_views
from api.models import Symbol


class ScanPutRsiFilterTests(APITestCase):
    def test_handle_scan_put_opportunities_respects_max_rsi(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="OVERSOLD",
            price=Decimal("88.00"),
            rsi=Decimal("28.00"),
            score=84,
            classification="Quality (selective)",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            option_data={
                "puts": [
                    {
                        "strike": 80,
                        "expiration": expiration.isoformat(),
                        "bid": 2.20,
                        "ask": 2.50,
                        "delta": -0.21,
                        "iv": 32.00,
                        "volume": 280,
                        "open_interest": 1500,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="NOTOS",
            price=Decimal("92.00"),
            rsi=Decimal("44.00"),
            score=90,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 85,
                        "expiration": expiration.isoformat(),
                        "bid": 2.50,
                        "ask": 2.80,
                        "delta": -0.20,
                        "iv": 29.00,
                        "volume": 320,
                        "open_interest": 1700,
                    }
                ]
            },
        )

        payload = json.loads(agent_views._handle_scan_put_opportunities({"max_rsi": 30}))

        self.assertEqual(payload["results_returned"], 1)
        self.assertEqual(payload["filters_applied"]["max_rsi"], 30.0)
        self.assertEqual(payload["opportunities"][0]["ticker"], "OVERSOLD")

    def test_handle_scan_put_opportunities_respects_min_rsi(self) -> None:
        expiration = date.today() + timedelta(days=28)
        Symbol.objects.create(
            ticker="NOTOB",
            price=Decimal("140.00"),
            rsi=Decimal("68.00"),
            score=88,
            classification="High-quality compounder",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.BUY,
            option_data={
                "puts": [
                    {
                        "strike": 125,
                        "expiration": expiration.isoformat(),
                        "bid": 3.30,
                        "ask": 3.60,
                        "delta": -0.23,
                        "iv": 31.00,
                        "volume": 260,
                        "open_interest": 1300,
                    }
                ]
            },
        )
        Symbol.objects.create(
            ticker="OVERBOUGHT",
            price=Decimal("150.00"),
            rsi=Decimal("79.00"),
            score=86,
            classification="Quality (selective)",
            liquidity=Symbol.LIQUIDITY_GOOD,
            technical_score=Symbol.TechnicalScore.NEUTRAL,
            option_data={
                "puts": [
                    {
                        "strike": 135,
                        "expiration": expiration.isoformat(),
                        "bid": 3.80,
                        "ask": 4.10,
                        "delta": -0.22,
                        "iv": 35.00,
                        "volume": 300,
                        "open_interest": 1550,
                    }
                ]
            },
        )

        payload = json.loads(agent_views._handle_scan_put_opportunities({"min_rsi": 75}))

        self.assertEqual(payload["results_returned"], 1)
        self.assertEqual(payload["filters_applied"]["min_rsi"], 75.0)
        self.assertEqual(payload["opportunities"][0]["ticker"], "OVERBOUGHT")
