from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import quote_plus

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import models

from api.models import Investment

PROFILE_CHUNK_SIZE = 3

BASE_URL = "http://127.0.0.1:8000"
INVESTMENTS_ENDPOINT = f"{BASE_URL}/api/investments/"
# RapidAPI proxy for Seeking Alpha profile data.
PROFILE_ENDPOINT = "https://seeking-alpha.p.rapidapi.com/symbols/get-profile"
OPTION_EXPIRATIONS_ENDPOINT = (
    "https://seeking-alpha.p.rapidapi.com/symbols/get-option-expirations"
)
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}


class Command(BaseCommand):
    """Fetch profile data for investments and update stored values."""

    help = "Fetches local investments and updates their price and market cap using profile data."

    def add_arguments(self, parser) -> None:  # pragma: no cover - argparse wiring
        parser.add_argument(
            "--skip-priced",
            action="store_true",
            help="Skip investments that already have a stored price.",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        investments_payload = self._fetch_json(INVESTMENTS_ENDPOINT)
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
        missing_tickers: list[str] = []
        for chunk in self._chunked(tickers, PROFILE_CHUNK_SIZE):
            profiles = self._fetch_profiles_for_chunk(chunk)
            if not profiles:
                missing_tickers.extend(chunk)
                continue

            updated_chunk = self._update_investments(chunk, profiles)
            self._assert_profiles_persisted(updated_chunk)
            updated_tickers.extend(updated_chunk.keys())
            missing_tickers.extend([ticker for ticker in chunk if ticker not in updated_chunk])

        if not updated_tickers:
            raise CommandError(
                "No matching investments were updated with the returned profile data."
            )
        if missing_tickers:
            self.stdout.write(
                "No profile data returned for: " + ", ".join(sorted(set(missing_tickers)))
            )

        missing_tickers = [ticker for ticker in tickers if ticker not in updated_tickers]
        if missing_tickers:
            self.stdout.write(
                "No profile data returned for: " + ", ".join(sorted(set(missing_tickers)))
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

    def _build_profile_url(self, tickers: Iterable[str]) -> str:
        ticker_string = ",".join(tickers)
        encoded = quote_plus(ticker_string)
        return f"{PROFILE_ENDPOINT}?symbols={encoded}"

    def _fetch_profiles_for_chunk(self, chunk: list[str]) -> dict[str, dict[str, Any]]:
        profile_url = self._build_profile_url(chunk)
        self.stdout.write(
            f"Requesting profile data for {', '.join(chunk)} at {profile_url}"
        )
        profile_payload = self._fetch_json(profile_url, headers=API_HEADERS)
        profiles = self._build_profile_map(profile_payload)

        missing = [ticker for ticker in chunk if ticker.upper() not in profiles]
        if not missing or len(chunk) == 1:
            return profiles

        fallback_profiles: dict[str, dict[str, Any]] = {}
        fallback_size = min(max((len(chunk) + 1) // 2, 1), PROFILE_CHUNK_SIZE)
        for fallback_chunk in self._chunked(missing, fallback_size):
            fallback_profiles.update(self._fetch_profiles_for_chunk(fallback_chunk))

        profiles.update(fallback_profiles)
        return profiles

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

    def _chunked(self, values: Iterable[str], size: int) -> Iterable[list[str]]:
        chunk: list[str] = []
        for value in values:
            chunk.append(value)
            if len(chunk) == size:
                yield chunk
                chunk = []

        if chunk:
            yield chunk

    def _build_profile_map(self, payload: Any) -> dict[str, dict[str, Any]]:
        data_section = payload
        if isinstance(payload, dict) and "data" in payload:
            data_section = payload["data"]

        profiles: dict[str, dict[str, Any]] = {}

        if isinstance(data_section, dict):
            iterator = (
                (symbol, profile)
                for symbol, profile in data_section.items()
                if isinstance(profile, dict)
            )
        elif isinstance(data_section, list):
            iterator = []
            for profile in data_section:
                if not isinstance(profile, dict):
                    continue
                symbol = (
                    profile.get("id")
                    or profile.get("symbol")
                    or profile.get("ticker")
                )
                if not symbol:
                    continue
                iterator.append((str(symbol), profile))
        else:
            raise CommandError("Profile payload had an unexpected structure.")

        for symbol, profile in iterator:
            normalized = self._normalize_profile(profile)
            profiles[str(symbol).upper()] = normalized

        return profiles

    def _normalize_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        attributes = profile.get("attributes")
        if isinstance(attributes, dict):
            source = attributes
        else:
            source = profile

        last_daily = source.get("lastDaily")
        if isinstance(last_daily, dict) and "last" in last_daily:
            last_value = last_daily.get("last")
        else:
            last_value = source.get("last")

        return {
            "last": last_value,
            "marketCap": source.get("marketCap"),
        }

    def _update_investments(
        self, tickers: Iterable[str], profiles: dict[str, dict[str, Any]]
    ) -> dict[str, tuple[Decimal | None, Decimal | None, int]]:
        updated: dict[str, tuple[Decimal | None, Decimal | None, int]] = {}

        for ticker in tickers:
            profile = profiles.get(ticker.upper())
            if not profile:
                continue

            price = self._quantize_for_field(
                self._parse_decimal(profile.get("last")),
                Investment._meta.get_field("price"),
            )
            market_cap = self._quantize_for_field(
                self._parse_decimal(profile.get("marketCap")),
                Investment._meta.get_field("market_cap"),
            )
            expirations = self._fetch_option_expirations(ticker)
            options_suitability = self._calculate_options_suitability(expirations)
            investment, created = Investment.objects.get_or_create(
                ticker=ticker, defaults={"category": "stock"}
            )

            investment.price = price
            investment.market_cap = market_cap
            investment.options_suitability = options_suitability
            if created:
                investment.save()
            else:
                investment.save(
                    update_fields=[
                        "price",
                        "market_cap",
                        "options_suitability",
                        "updated_at",
                    ]
                )

            action = "Created" if created else "Updated"
            self.stdout.write(
                f"{action} investment {ticker} with price={price} and market cap={market_cap}"
            )
            updated[ticker] = (price, market_cap, options_suitability)

        return updated

    def _assert_profiles_persisted(
        self, updated: dict[str, tuple[Decimal | None, Decimal | None, int]]
    ) -> None:
        if not updated:
            return

        failed: list[str] = []
        for ticker, (price, market_cap, options_suitability) in updated.items():
            try:
                investment = Investment.objects.get(ticker=ticker)
            except Investment.DoesNotExist:
                failed.append(ticker)
                continue

            if (
                investment.price != price
                or investment.market_cap != market_cap
                or investment.options_suitability != options_suitability
            ):
                failed.append(ticker)

        if failed:
            raise CommandError(
                "Failed to persist profile data for: " + ", ".join(sorted(set(failed)))
            )

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

        return 1 if expirations_next_month >= 4 else 0

    def _parse_decimal(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:
            raise CommandError(f"Unable to parse '{value}' as a decimal number.") from exc

    def _quantize_for_field(self, value: Decimal | None, field: models.Field) -> Decimal | None:
        if value is None:
            return None
        if not isinstance(field, models.DecimalField):
            return value

        quantize_exp = Decimal("1").scaleb(-field.decimal_places)
        return value.quantize(quantize_exp, rounding=ROUND_HALF_UP)
