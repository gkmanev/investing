from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable

import requests
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from api.models import Investment

API_URL = "https://seeking-alpha.p.rapidapi.com/symbols/get-option-expirations"
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}


class Command(BaseCommand):
    """Fetch option expirations for Stocks by Quant investments."""

    help = (
        "Query Stocks by Quant tickers with options suitability set to 1 and "
        "print the closest option expiration date within the next 30 days."
    )

    def handle(self, *args, **options) -> str | None:  # pragma: no cover - CLI entry
        tickers = list(
            Investment.objects.filter(
                screener_type="Stocks by Quant", options_suitability=1
            ).values_list("ticker", flat=True)
        )

        if not tickers:
            self.stdout.write("No tickers found for Stocks by Quant with options suitability 1.")
            return None

        today = timezone.now().date()
        upper_bound = today + timedelta(days=30)

        for ticker in tickers:
            try:
                dates = self._fetch_expiration_dates(ticker)
            except CommandError as exc:
                self.stderr.write(f"{ticker}: {exc}")
                continue

            closest_date = self._select_closest_date(dates, today, upper_bound)
            if closest_date is None:
                self.stdout.write(
                    f"{ticker}: No option expiration date within the next 30 days."
                )
            else:
                self.stdout.write(f"{ticker}: {closest_date.isoformat()}")

        return None

    def _fetch_expiration_dates(self, ticker: str) -> list[str]:
        try:
            response = requests.get(
                API_URL, headers=API_HEADERS, params={"symbol": ticker}, timeout=30
            )
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise CommandError(f"Failed to call Seeking Alpha API: {exc}") from exc

        if response.status_code != 200:
            raise CommandError(
                f"Received unexpected status code {response.status_code}: {response.text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CommandError("Received invalid JSON from Seeking Alpha API") from exc

        try:
            return payload["data"]["attributes"]["dates"]
        except (TypeError, KeyError) as exc:
            raise CommandError("Missing expected data in Seeking Alpha response") from exc

    def _select_closest_date(
        self, dates: Iterable[str], today: date, upper_bound: date
    ) -> date | None:
        """Return the soonest expiration date between today and the upper bound."""

        valid_dates: list[date] = []
        for date_string in dates:
            try:
                parsed_date = datetime.strptime(date_string, "%m/%d/%Y").date()
            except ValueError:  # pragma: no cover - malformed upstream data
                continue

            if today <= parsed_date <= upper_bound:
                valid_dates.append(parsed_date)

        if not valid_dates:
            return None

        return min(valid_dates)
