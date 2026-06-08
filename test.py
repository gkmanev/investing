# test_tv_voi_lens_only.py

from __future__ import annotations

import json
from typing import Any

import requests


class TradingViewVOILens:
    BASE_URL = (
        "https://options-charting.tradingview.com/v1/"
        "voi-lens/{symbol}?label-product=product_page_volume_tab"
    )

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
    }

    def fetch(self, exchange: str, ticker: str) -> dict[str, Any]:
        encoded_symbol = f"{exchange}%3A{ticker}"

        url = self.BASE_URL.format(symbol=encoded_symbol)

        response = requests.get(
            url,
            headers=self.HEADERS,
            timeout=30,
        )

        print("HTTP status:", response.status_code)
        print("Content-Type:", response.headers.get("content-type"))
        print("Cache-Control:", response.headers.get("cache-control"))

        if response.status_code != 200:
            print("\nResponse text:")
            print(response.text[:3000])

        response.raise_for_status()
        return response.json()

    @staticmethod
    def print_top_level_summary(data: dict[str, Any]) -> None:
        print("\n=== TOP LEVEL SUMMARY ===")
        print("Top-level keys:", list(data.keys()))

        items = data.get("items", [])
        print("Number of expiration groups:", len(items))

        if items:
            print("\nFirst item keys:", list(items[0].keys()))
            print("First item sample:")
            print(json.dumps(items[0], indent=2)[:4000])

    @staticmethod
    def print_expiration_dates(data: dict[str, Any]) -> None:
        expirations = sorted(
            item.get("exp")
            for item in data.get("items", [])
            if item.get("exp") is not None
        )

        print("\n=== EXPIRATION DATES ===")
        print(expirations)

    @staticmethod
    def print_expiration_table(data: dict[str, Any]) -> None:
        print("\n=== EXPIRATION TABLE ===")
        print(
            "expiration | strike_count | min_strike | max_strike | "
            "call_volume | put_volume | total_volume"
        )

        items = sorted(
            data.get("items", []),
            key=lambda item: item.get("exp", 0),
        )

        for item in items:
            exp = item.get("exp")
            strikes = item.get("strikes", [])

            strike_values = [
                row.get("s")
                for row in strikes
                if row.get("s") is not None
            ]

            call_volume = sum(
                int(row.get("c", {}).get("v") or 0)
                for row in strikes
            )

            put_volume = sum(
                int(row.get("p", {}).get("v") or 0)
                for row in strikes
            )

            min_strike = min(strike_values) if strike_values else None
            max_strike = max(strike_values) if strike_values else None

            print(
                exp,
                "|",
                len(strikes),
                "|",
                min_strike,
                "|",
                max_strike,
                "|",
                call_volume,
                "|",
                put_volume,
                "|",
                call_volume + put_volume,
            )

    @staticmethod
    def print_strikes_for_expiration(
        data: dict[str, Any],
        expiration: int,
        limit: int | None = None,
    ) -> None:
        print(f"\n=== STRIKES FOR EXPIRATION {expiration} ===")
        print("strike | call_volume | put_volume")

        item = next(
            (
                item
                for item in data.get("items", [])
                if item.get("exp") == expiration
            ),
            None,
        )

        if item is None:
            print("Expiration not found.")
            return

        strikes = sorted(
            item.get("strikes", []),
            key=lambda row: row.get("s", 0),
        )

        if limit is not None:
            strikes = strikes[:limit]

        for row in strikes:
            strike = row.get("s")
            call_volume = row.get("c", {}).get("v")
            put_volume = row.get("p", {}).get("v")

            print(
                strike,
                "|",
                call_volume if call_volume is not None else 0,
                "|",
                put_volume if put_volume is not None else 0,
            )

    @staticmethod
    def inspect_call_put_keys(data: dict[str, Any]) -> None:
        call_keys: set[str] = set()
        put_keys: set[str] = set()

        examples_with_extra_keys = []

        for item in data.get("items", []):
            exp = item.get("exp")

            for row in item.get("strikes", []):
                strike = row.get("s")
                call_data = row.get("c", {})
                put_data = row.get("p", {})

                call_keys.update(call_data.keys())
                put_keys.update(put_data.keys())

                if len(call_data.keys()) > 1 or len(put_data.keys()) > 1:
                    examples_with_extra_keys.append(
                        {
                            "exp": exp,
                            "strike": strike,
                            "call": call_data,
                            "put": put_data,
                        }
                    )

        print("\n=== CALL / PUT INNER KEYS ===")
        print("Call keys found:", sorted(call_keys))
        print("Put keys found:", sorted(put_keys))

        if examples_with_extra_keys:
            print("\nExamples with extra keys:")
            print(json.dumps(examples_with_extra_keys[:20], indent=2))
        else:
            print("No extra keys found inside c/p objects.")

    @staticmethod
    def save_json(data: dict[str, Any], filename: str = "voi_lens_response.json") -> None:
        with open(filename, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)

        print(f"\nSaved full response to: {filename}")


def main() -> None:
    exchange = "NYSE"
    ticker = "CCJ"

    preferred_expiration = 20260612

    client = TradingViewVOILens()

    print(f"Fetching VOI lens for {exchange}:{ticker}")

    data = client.fetch(exchange, ticker)

    client.print_top_level_summary(data)
    client.print_expiration_dates(data)
    client.print_expiration_table(data)
    client.inspect_call_put_keys(data)

    client.print_strikes_for_expiration(
        data,
        expiration=preferred_expiration,
        limit=None,
    )

    client.save_json(data)


if __name__ == "__main__":
    main()