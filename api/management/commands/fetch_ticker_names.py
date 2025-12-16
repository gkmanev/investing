from __future__ import annotations

from typing import Any, Iterable

import requests
from django.core.management.base import BaseCommand, CommandError

INVESTMENTS_URL = "http://127.0.0.1:8000/api/investments/"
DEFAULT_SCREENER_TYPE = "Stocks by Quant"


class Command(BaseCommand):
    """Fetch ticker names from the local investments endpoint."""

    help = (
        "Fetch ticker names from the investments API using the options suitability "
        "and screener filters."
    )

    def add_arguments(self, parser) -> None:  # pragma: no cover - argparse wiring
        parser.add_argument(
            "--investments-url",
            default=INVESTMENTS_URL,
            dest="investments_url",
            help="Investments endpoint to query.",
        )
        parser.add_argument(
            "--options-suitability",
            type=int,
            default=1,
            dest="options_suitability",
            help="Options suitability filter value (default: 1).",
        )
        parser.add_argument(
            "--screener-type",
            default=DEFAULT_SCREENER_TYPE,
            dest="screener_type",
            help="Screener type filter value (default: 'Stocks by Quant').",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        investments_url: str = options["investments_url"]
        options_suitability: int = options["options_suitability"]
        screener_type: str = options["screener_type"]

        try:
            response = requests.get(
                investments_url,
                params={
                    "options_suitability": options_suitability,
                    "screener_type": screener_type,
                },
                timeout=30,
            )
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise CommandError(f"Failed to call '{investments_url}': {exc}") from exc

        if response.status_code != 200:
            raise CommandError(
                f"Received unexpected status code {response.status_code} from "
                f"'{investments_url}': {response.text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CommandError(
                f"Response from '{investments_url}' did not contain valid JSON."
            ) from exc

        tickers = self._extract_tickers(payload)
        if not tickers:
            raise CommandError("No ticker names were found in the response payload.")

        formatted = "\n".join(tickers)
        return formatted

    def _extract_tickers(self, payload: Any) -> list[str]:
        entries: Iterable[Any]
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            for key in ("results", "data"):
                nested_value = payload.get(key)
                if isinstance(nested_value, list):
                    entries = nested_value
                    break
            else:
                entries = [payload]
        else:
            return []

        tickers: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ticker_value = entry.get("ticker")
            if isinstance(ticker_value, str):
                tickers.append(ticker_value)
        return tickers
