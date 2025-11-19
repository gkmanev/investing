from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import quote_plus

import requests
from django.core.management.base import BaseCommand, CommandError

from api.models import Investment

BASE_URL = "http://127.0.0.1:8000"
INVESTMENTS_ENDPOINT = f"{BASE_URL}/api/investments/"
PROFILE_ENDPOINT = f"{BASE_URL}/symbols/get-profile"


class Command(BaseCommand):
    """Fetch profile data for the first few investments and update stored values."""

    help = "Fetches local investments and updates their price and market cap using profile data."

    def handle(self, *args: Any, **options: Any) -> str:
        investments_payload = self._fetch_json(INVESTMENTS_ENDPOINT)
        tickers = self._extract_tickers(investments_payload, limit=3)
        if not tickers:
            raise CommandError(
                "Investments endpoint did not return any entries with ticker information."
            )

        profile_url = self._build_profile_url(tickers)
        profile_payload = self._fetch_json(profile_url)
        profiles = self._build_profile_map(profile_payload)
        if not profiles:
            raise CommandError("Profile endpoint did not return any usable data.")

        updated_tickers = self._update_investments(tickers, profiles)
        if not updated_tickers:
            raise CommandError(
                "No matching investments were updated with the returned profile data."
            )

        return ", ".join(updated_tickers)

    def _fetch_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        try:
            response = requests.get(url, params=params, timeout=30)
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

    def _extract_tickers(self, payload: Any, *, limit: int) -> list[str]:
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
            if len(tickers) == limit:
                break

        return tickers

    def _build_profile_map(self, payload: Any) -> dict[str, dict[str, Any]]:
        data_section = payload
        if isinstance(payload, dict) and "data" in payload:
            data_section = payload["data"]

        profiles: dict[str, dict[str, Any]] = {}

        if isinstance(data_section, dict):
            iterable = (
                (symbol, profile)
                for symbol, profile in data_section.items()
                if isinstance(profile, dict)
            )
            for symbol, profile in iterable:
                profiles[symbol.upper()] = profile
            return profiles

        if isinstance(data_section, list):
            for profile in data_section:
                if not isinstance(profile, dict):
                    continue
                symbol = profile.get("symbol") or profile.get("ticker")
                if not symbol:
                    continue
                profiles[str(symbol).upper()] = profile
            return profiles

        raise CommandError("Profile payload had an unexpected structure.")

    def _update_investments(
        self, tickers: Iterable[str], profiles: dict[str, dict[str, Any]]
    ) -> list[str]:
        updated: list[str] = []

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
            updated.append(ticker)

        return updated

    def _parse_decimal(self, value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:
            raise CommandError(f"Unable to parse '{value}' as a decimal number.") from exc
