from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable
import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from api.management.commands.rapidapi_counter import log_rapidapi_fetch
from api.models import Investment

BASE_URL = settings.LOCAL_API_BASE_URL
INVESTMENTS_ENDPOINT = f"{BASE_URL}/api/investments/"
OPTION_EXPIRATIONS_ENDPOINT = (
    "https://seeking-alpha.p.rapidapi.com/symbols/get-option-expirations"
)
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}
TARGET_OPTION_WEEKS = 5


class Command(BaseCommand):
    """Fetch option expiration data for investments."""

    help = "Fetches local investments and updates their option expiration dates."

    def add_arguments(self, parser) -> None:  # pragma: no cover
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
        investments = self._extract_investments(investments_payload)
        if not investments:
            raise CommandError(
                "Investments endpoint did not return any entries with ticker information."
            )

        if options.get("skip_priced"):
            priced_tickers = {
                ticker.upper()
                for ticker in Investment.objects.filter(
                    ticker__in=[entry["ticker"] for entry in investments],
                    price__isnull=False,
                ).values_list("ticker", flat=True)
            }
            investments = [
                entry
                for entry in investments
                if entry["ticker"].upper() not in priced_tickers
            ]
            if not investments:
                raise CommandError(
                    "No tickers remain to update after skipping priced investments."
                )

        updated_tickers: list[str] = []
        today = timezone.now().date()
        upper_bound = today + timedelta(days=31)
        for entry in investments:
            ticker = entry["ticker"]
            weekly_options = entry.get("weekly_options")
            expiration_data = None
            ticker_id_value = None
            chosen_option_exp = None

            if weekly_options is True:
                try:
                    expiration_data = self._fetch_option_expirations(ticker)
                except CommandError as exc:
                    self.stderr.write(
                        f"Failed to fetch option expirations for {ticker}: {exc}. "
                        "Continuing without options data."
                    )
                    continue
            else:
                expiration_data = None

            if expiration_data is not None:
                # This is just informational printing (your existing 31-day window)
                closest_dates = self._select_closest_dates(
                    expiration_data["dates"], today, upper_bound
                )
                furthest_option_date = (
                    max(closest_dates)
                    if closest_dates
                    else self._select_furthest_date(expiration_data["dates"])
                )

                if not closest_dates:
                    self.stdout.write(
                        f"{ticker} (ticker_id {expiration_data['ticker_id']}): No option "
                        "expiration date within the next 31 days."
                    )
                else:
                    formatted = ", ".join(d.isoformat() for d in closest_dates)
                    self.stdout.write(
                        f"{ticker} (ticker_id {expiration_data['ticker_id']}): {formatted}; "
                        f"furthest: {furthest_option_date.isoformat() if furthest_option_date else 'N/A'}"
                    )

                chosen_option_exp = self._select_option_expiration(
                    expiration_data["dates"],
                    today,
                    target_weeks=TARGET_OPTION_WEEKS,
                )
                ticker_id_value = self._coerce_ticker_id(expiration_data.get("ticker_id"))
            defaults: dict[str, Any] = {"category": "stock"}
            if ticker_id_value is not None:
                defaults["id"] = ticker_id_value

            investment, created = Investment.objects.get_or_create(
                ticker=ticker, defaults=defaults
            )

            if not created and ticker_id_value is not None and investment.id != ticker_id_value:
                try:
                    with transaction.atomic():
                        Investment.objects.filter(pk=investment.pk).update(id=ticker_id_value)
                        investment.id = ticker_id_value
                except IntegrityError:
                    self.stderr.write(
                        f"Unable to update id for {ticker} to {ticker_id_value}: value already in use."
                    )

            investment.option_exp = chosen_option_exp

            if created:
                investment.save()
            else:
                update_fields = ["option_exp", "updated_at"]
                investment.save(update_fields=update_fields)

            action = "Created" if created else "Updated"
            self.stdout.write(
                f"{action} investment {ticker} with option expiration={chosen_option_exp}"
            )
            updated_tickers.append(ticker)

        if not updated_tickers:
            raise CommandError("No investments were updated with option expiration data.")

        return ", ".join(updated_tickers)

    # -------------------------
    # Networking + extraction
    # -------------------------

    def _fetch_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            response = requests.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:  # pragma: no cover
            raise CommandError(f"Failed to call '{url}': {exc}") from exc
        if headers and "x-rapidapi-key" in headers:
            log_rapidapi_fetch(self)

        if response.status_code != 200:
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type:
                raise CommandError(
                    "Received an HTML error response from "
                    f"'{url}'. Check the server logs for details."
                )
            raise CommandError(
                f"Received unexpected status code {response.status_code} from '{url}': {response.text}"
            )

        content_type = response.headers.get("Content-Type", "")
        if "text/html" in content_type:
            raise CommandError(
                f"Received HTML from '{url}' when JSON was expected. "
                "Check that the endpoint is healthy and returning JSON."
            )

        try:
            return response.json()
        except ValueError as exc:
            raise CommandError(f"Response from '{url}' did not contain valid JSON.") from exc

    def _extract_investments(self, payload: Any) -> list[dict[str, Any]]:
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
                    "Investments payload did not contain a list under 'results' or 'data'."
                )
        else:
            raise CommandError("Investments payload had an unexpected structure.")

        investments: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ticker = entry.get("ticker")
            if ticker:
                investments.append(
                    {
                        "ticker": str(ticker),
                        "weekly_options": entry.get("weekly_options"),
                    }
                )
        return investments

    def _fetch_option_expirations(self, ticker: str) -> dict:
        payload = self._fetch_json(
            OPTION_EXPIRATIONS_ENDPOINT,
            params={"symbol": ticker},
            headers=API_HEADERS,
        )
        ticker_id = self._extract_ticker_id(payload)
        dates = self._extract_option_dates(payload)
        if ticker_id is None:
            raise CommandError("Missing expected data in Seeking Alpha response")
        return {"ticker_id": ticker_id, "dates": dates}

    def _extract_option_dates(self, payload: Any) -> list[str]:
        data_section = payload
        if isinstance(payload, dict) and "data" in payload:
            data_section = payload.get("data")

        if isinstance(data_section, dict):
            attributes = data_section.get("attributes")
            source = attributes if isinstance(attributes, dict) else data_section
            dates = source.get("dates")
            if isinstance(dates, list):
                return [str(v) for v in dates if v is not None]
        elif isinstance(data_section, list):
            for entry in data_section:
                if not isinstance(entry, dict):
                    continue
                attributes = entry.get("attributes")
                source = attributes if isinstance(attributes, dict) else entry
                dates = source.get("dates")
                if isinstance(dates, list):
                    return [str(v) for v in dates if v is not None]
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

    # -------------------------
    # Date logic
    # -------------------------

    def _parse_expiration_dates(self, dates: Iterable[str]) -> list[date]:
        """Parse dates, drop malformed, de-duplicate, return sorted ascending."""
        parsed: set[date] = set()
        for s in dates:
            try:
                parsed.add(datetime.strptime(str(s), "%m/%d/%Y").date())
            except ValueError:
                continue
        return sorted(parsed)

    def _select_closest_dates(
        self, dates: Iterable[str], today: date, upper_bound: date
    ) -> list[date]:
        valid_dates: list[date] = []
        for date_string in dates:
            try:
                parsed_date = datetime.strptime(date_string, "%m/%d/%Y").date()
            except ValueError:  # pragma: no cover
                continue
            if today <= parsed_date <= upper_bound:
                valid_dates.append(parsed_date)

        return sorted(valid_dates, key=lambda d: (upper_bound - d).days)[:2]

    def _select_furthest_date(self, dates: Iterable[str]) -> date | None:
        furthest: date | None = None
        for date_string in dates:
            try:
                parsed_date = datetime.strptime(str(date_string), "%m/%d/%Y").date()
            except ValueError:  # pragma: no cover
                continue
            if furthest is None or parsed_date > furthest:
                furthest = parsed_date
        return furthest

    def _select_option_expiration(
        self, dates: list[str], today: date, target_weeks: int
    ) -> date | None:
        """Return the first expiration date on/after the target Friday."""
        parsed = self._parse_expiration_dates(dates)
        upcoming = [d for d in parsed if d >= today]

        if not upcoming:
            return None

        target_date = self._target_friday(today, target_weeks)
        for expiration_date in upcoming:
            if expiration_date >= target_date:
                return expiration_date
        return None

    def _target_friday(self, today: date, target_weeks: int) -> date:
        if target_weeks < 1:
            raise CommandError("TARGET_OPTION_WEEKS must be at least 1.")
        days_until_friday = (4 - today.weekday()) % 7
        next_friday = today + timedelta(days=days_until_friday)
        return next_friday + timedelta(weeks=target_weeks - 1)

    # -------------------------
    # Id coercion
    # -------------------------
    def _coerce_ticker_id(self, ticker_id: Any) -> int | None:
        try:
            return int(ticker_id)
        except (TypeError, ValueError):
            return None
