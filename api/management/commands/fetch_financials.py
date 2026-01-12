from __future__ import annotations

from typing import Any

import requests
from django.core.management.base import BaseCommand, CommandError

from api.models import FinancialStatement

API_URL = "https://seeking-alpha.p.rapidapi.com/symbols/get-financials"
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
    "Content-Type": "application/json",
}


class Command(BaseCommand):
    """Fetch financial statement data for a symbol and store the payload."""

    help = "Fetch financial statement data from Seeking Alpha via RapidAPI."

    def add_arguments(self, parser) -> None:  # pragma: no cover - argparse wiring
        parser.add_argument(
            "--symbol",
            default="anet",
            help="Ticker symbol to fetch (default: anet).",
        )
        parser.add_argument(
            "--target-currency",
            dest="target_currency",
            default="USD",
            help="Target currency (default: USD).",
        )
        parser.add_argument(
            "--period-type",
            dest="period_type",
            default="annual",
            help="Period type (default: annual).",
        )
        parser.add_argument(
            "--statement-type",
            dest="statement_type",
            default="income-statement",
            help="Statement type (default: income-statement).",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        rapidapi_calls = 0
        symbol = str(options["symbol"]).strip()
        if not symbol:
            raise CommandError("Symbol cannot be empty.")

        target_currency = str(options["target_currency"]).strip() or "USD"
        period_type = str(options["period_type"]).strip() or "annual"
        statement_type = str(options["statement_type"]).strip() or "income-statement"

        payload = self._fetch_payload(
            symbol=symbol,
            target_currency=target_currency,
            period_type=period_type,
            statement_type=statement_type,
        )
        rapidapi_calls += 1

        statement, created = FinancialStatement.objects.update_or_create(
            symbol=symbol.upper(),
            target_currency=target_currency.upper(),
            period_type=period_type.lower(),
            statement_type=statement_type,
            defaults={"payload": payload},
        )

        action = "Created" if created else "Updated"
        message = (
            f"{action} financial statement for {statement.symbol} "
            f"({statement.statement_type}, {statement.period_type}, "
            f"{statement.target_currency})."
        )
        self.stdout.write(message)
        self.stdout.write(f"RapidAPI calls: {rapidapi_calls}")
        return message

    def _fetch_payload(
        self,
        *,
        symbol: str,
        target_currency: str,
        period_type: str,
        statement_type: str,
    ) -> Any:
        params = {
            "symbol": symbol,
            "target_currency": target_currency,
            "period_type": period_type,
            "statement_type": statement_type,
        }

        try:
            response = requests.get(
                API_URL, headers=API_HEADERS, params=params, timeout=30
            )
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise CommandError(f"Failed to call Seeking Alpha API: {exc}") from exc

        if response.status_code != 200:
            raise CommandError(
                "Received unexpected status code "
                f"{response.status_code}: {response.text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CommandError("Received invalid JSON from Seeking Alpha API") from exc

        if not isinstance(payload, list):
            raise CommandError("Unexpected payload structure: expected a list response.")

        return payload
