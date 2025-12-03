from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

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
        "print the two closest option expiration dates nearest to the 31-day "
        "cutoff."
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
        upper_bound = today + timedelta(days=31)

        for ticker in tickers:
            try:
                payload = self._fetch_expiration_data(ticker)
            except CommandError as exc:
                self.stderr.write(f"{ticker}: {exc}")
                continue

            closest_dates = self._select_closest_dates(
                payload["dates"], today, upper_bound
            )
            if not closest_dates:
                self.stdout.write(
                    f"{ticker} (ticker_id {payload['ticker_id']}): No option "
                    "expiration date within the next 31 days."
                )
            else:
                formatted_dates = ", ".join(date.isoformat() for date in closest_dates)
                furthest_date = max(closest_dates)
                self.stdout.write(
                    f"{ticker} (ticker_id {payload['ticker_id']}): {formatted_dates}; "
                    f"furthest: {furthest_date.isoformat()}"
                )

        return None

    def _fetch_expiration_data(self, ticker: str) -> dict:
        payload = self._fetch_json(
            API_URL, params={"symbol": ticker}, headers=API_HEADERS
        )

        ticker_id = self._extract_ticker_id(payload)
        dates = self._extract_option_dates(payload)

        if ticker_id is None:
            raise CommandError("Missing expected data in Seeking Alpha response")

        return {"ticker_id": ticker_id, "dates": dates}

    def _fetch_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = requests.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise CommandError(f"Failed to call '{url}': {exc}") from exc

        if response.status_code != 200:
            raise CommandError(
                f"Received unexpected status code {response.status_code} from '{url}': {response.text}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise CommandError(f"Response from '{url}' did not contain valid JSON.") from exc

    def _extract_option_dates(self, payload: Any) -> list[str]:
        data_section = payload
        if isinstance(payload, dict) and "data" in payload:
            data_section = payload.get("data")

        if isinstance(data_section, dict):
            attributes = data_section.get("attributes")
            source = attributes if isinstance(attributes, dict) else data_section
            dates = source.get("dates")
            if isinstance(dates, list):
                return [str(value) for value in dates if value is not None]
        elif isinstance(data_section, list):
            for entry in data_section:
                if not isinstance(entry, dict):
                    continue
                attributes = entry.get("attributes")
                source = attributes if isinstance(attributes, dict) else entry
                dates = source.get("dates")
                if isinstance(dates, list):
                    return [str(value) for value in dates if value is not None]

        return []

    def _extract_ticker_id(self, payload: Any) -> str | None:
        data_section = payload
        if isinstance(payload, dict) and "data" in payload:
            data_section = payload.get("data")

        if isinstance(data_section, dict):
            attributes = data_section.get("attributes")
            source = attributes if isinstance(attributes, dict) else data_section
            ticker_id = source.get("ticker_id")
            if ticker_id is not None:
                return str(ticker_id)
        elif isinstance(data_section, list):
            for entry in data_section:
                if not isinstance(entry, dict):
                    continue
                attributes = entry.get("attributes")
                source = attributes if isinstance(attributes, dict) else entry
                ticker_id = source.get("ticker_id")
                if ticker_id is not None:
                    return str(ticker_id)

        return None

    def _select_closest_dates(
        self, dates: Iterable[str], today: date, upper_bound: date
    ) -> list[date]:
        """
        Return up to two expiration dates that are closest to the 31-day cutoff.

        Dates are filtered to the window [today, upper_bound] and then ordered by
        proximity to the upper bound so the nearest eligible expirations are
        returned first.
        """

        valid_dates: list[date] = []
        for date_string in dates:
            try:
                parsed_date = datetime.strptime(date_string, "%m/%d/%Y").date()
            except ValueError:  # pragma: no cover - malformed upstream data
                continue

            if today <= parsed_date <= upper_bound:
                valid_dates.append(parsed_date)
        return sorted(valid_dates, key=lambda candidate: (upper_bound - candidate).days)[
            :2
        ]
