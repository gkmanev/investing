from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import quote_plus

import requests
from django.core.management.base import BaseCommand, CommandError

from api.models import Investment

PROFILE_CHUNK_SIZE = 3

BASE_URL = "http://127.0.0.1:8000"
INVESTMENTS_ENDPOINT = f"{BASE_URL}/api/investments/"
# RapidAPI proxy for Seeking Alpha profile data.
PROFILE_ENDPOINT = "https://seeking-alpha.p.rapidapi.com/symbols/get-profile"
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}


class Command(BaseCommand):
    """Fetch profile data for investments and update stored values."""

    help = "Fetches local investments and updates their price and market cap using profile data."

    def handle(self, *args: Any, **options: Any) -> str:
        investments_payload = self._fetch_json(INVESTMENTS_ENDPOINT)
        tickers = self._extract_tickers(investments_payload)
        if not tickers:
            raise CommandError(
                "Investments endpoint did not return any entries with ticker information."
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
    ) -> dict[str, tuple[Decimal | None, Decimal | None]]:
        updated: dict[str, tuple[Decimal | None, Decimal | None]] = {}

        for ticker in tickers:
            profile = profiles.get(ticker.upper())
            if not profile:
                continue

            price = self._parse_decimal(profile.get("last"))
            market_cap = self._parse_decimal(profile.get("marketCap"))
            try:
                investment = Investment.objects.get(ticker=ticker)
            except Investment.DoesNotExist:
                continue

            investment.price = price
            investment.market_cap = market_cap
            investment.save(update_fields=["price", "market_cap", "updated_at"])
            updated[ticker] = (price, market_cap)

        return updated

    def _assert_profiles_persisted(
        self, updated: dict[str, tuple[Decimal | None, Decimal | None]]
    ) -> None:
        if not updated:
            return

        failed: list[str] = []
        for ticker, (price, market_cap) in updated.items():
            try:
                investment = Investment.objects.get(ticker=ticker)
            except Investment.DoesNotExist:
                failed.append(ticker)
                continue

            if investment.price != price or investment.market_cap != market_cap:
                failed.append(ticker)

        if failed:
            raise CommandError(
                "Failed to persist profile data for: " + ", ".join(sorted(set(failed)))
            )

    def _parse_decimal(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:
            raise CommandError(f"Unable to parse '{value}' as a decimal number.") from exc
