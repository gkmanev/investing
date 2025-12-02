from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

import requests
from django.core.management.base import BaseCommand, CommandError

from api.models import Investment

BASE_URL = "http://127.0.0.1:8000"
INVESTMENTS_ENDPOINT = f"{BASE_URL}/api/investments/"
OPTION_EXPIRATIONS_ENDPOINT = (
    "https://seeking-alpha.p.rapidapi.com/symbols/get-option-expirations"
)
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}


class Command(BaseCommand):
    """Fetch option suitability data for investments."""

    help = "Fetches local investments and updates their options suitability."

    def add_arguments(self, parser) -> None:  # pragma: no cover - argparse wiring
        parser.add_argument(
            "--screener_name",
            help="Name of the screener whose investments should be processed.",
        )
        parser.add_argument(
            "--skip-priced",
            action="store_true",
            help="Skip investments that already have a stored price.",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        screener_name: str = options["screener_name"]
        investments_payload = self._fetch_json(
            INVESTMENTS_ENDPOINT, params={"screener_type": screener_name}
        )
        tickers = self._extract_tickers(investments_payload)
        if not tickers:
            raise CommandError(
                "Investments endpoint did not return any entries with ticker information."
            )

        if options.get("skip_priced"):
            priced_tickers = {
                ticker.upper()
                for ticker in Investment.objects.filter(
                    ticker__in=tickers, price__isnull=False
                ).values_list("ticker", flat=True)
            }
            tickers = [ticker for ticker in tickers if ticker.upper() not in priced_tickers]
            if not tickers:
                raise CommandError(
                    "No tickers remain to update after skipping priced investments."
                )

        updated_tickers: list[str] = []
        for ticker in tickers:
            try:
                expirations = self._fetch_option_expirations(ticker)
            except CommandError as exc:
                self.stderr.write(
                    f"Failed to fetch option expirations for {ticker}: {exc}. "
                    "Continuing without options data."
                )
                continue

            options_suitability = self._calculate_options_suitability(expirations)
            investment, created = Investment.objects.get_or_create(
                ticker=ticker, defaults={"category": "stock"}
            )

            investment.options_suitability = options_suitability
            if created:
                investment.save()
            else:
                investment.save(update_fields=["options_suitability", "updated_at"])

            action = "Created" if created else "Updated"
            self.stdout.write(
                f"{action} investment {ticker} with options suitability={options_suitability}"
            )
            updated_tickers.append(ticker)

        if not updated_tickers:
            raise CommandError(
                "No investments were updated with options suitability data."
            )

        return ", ".join(updated_tickers)

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

    def _extract_tickers(self, payload: Any) -> list[str]:
        entries: Iterable[Any]
        if isinstance(payload, list):
            entries = payload
        elif isinstance(payload, dict):
            for key in ("results", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    entries = value
                    break
            else:
                raise CommandError(
                    "Investments payload did not contain a list of results under 'results' or 'data'."
                )
        else:
            raise CommandError("Investments payload had an unexpected structure.")

        tickers: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ticker = entry.get("ticker")
            if ticker:
                tickers.append(str(ticker))

        return tickers

    def _fetch_option_expirations(self, ticker: str) -> list[str]:
        payload = self._fetch_json(
            OPTION_EXPIRATIONS_ENDPOINT,
            params={"symbol": ticker},
            headers=API_HEADERS,
        )
        return self._extract_option_dates(payload)

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

    def _calculate_options_suitability(self, dates: list[str]) -> int:
        if not dates:
            return -1

        today = date.today()
        if today.month == 12:
            target_month = 1
            target_year = today.year + 1
        else:
            target_month = today.month + 1
            target_year = today.year

        expirations_next_month = 0
        for value in dates:
            try:
                expiration = datetime.strptime(str(value), "%m/%d/%Y").date()
            except ValueError:
                continue

            if expiration.year == target_year and expiration.month == target_month:
                expirations_next_month += 1

        return 1 if expirations_next_month >= 3 else 0
