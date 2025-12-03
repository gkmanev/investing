from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from django.core.management.base import BaseCommand, CommandError
from math import erf, log, sqrt

from api.models import Investment

API_URL = "https://seeking-alpha.p.rapidapi.com/symbols/v2/get-options"
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}


class Command(BaseCommand):
    """Fetch put options priced below stored investment prices."""

    help = "Check for put options below the stored investment price."

    def add_arguments(self, parser) -> None:  # pragma: no cover - argparse wiring
        parser.add_argument(
            "--screener_type",
            help="Screener type to filter investments by.",
            required=True,
        )

    def handle(self, *args: Any, **options: Any) -> str:
        screener_type: str = options["screener_type"]
        investments = Investment.objects.filter(
            options_suitability=1, screener_type=screener_type
        )

        if not investments.exists():
            raise CommandError(
                "No investments found with options_suitability=1 for the provided screener type."
            )

        summaries: list[str] = []
        for investment in investments:
            if investment.option_exp is None:
                self.stderr.write(
                    f"Skipping {investment.ticker}: missing option expiration date."
                )
                continue

            if investment.price is None:
                self.stderr.write(
                    f"Skipping {investment.ticker}: missing stored price for comparison."
                )
                continue

            try:
                options_payload = self._fetch_options(
                    investment.id, investment.option_exp.isoformat()
                )
            except CommandError as exc:
                self.stderr.write(f"{investment.ticker}: {exc}")
                continue

            put_options = self._filter_put_options(
                options_payload, investment.price, investment.option_exp
            )
            top_puts = put_options[:5]
            if not top_puts:
                self.stdout.write(
                    f"{investment.ticker}: no put options below price {investment.price}."
                )
                continue

            formatted_options = ", ".join(
                (
                    f"{opt['symbol']} (strike {opt['strike_price']}, "
                    f"last {opt.get('last', 'N/A')}, "
                    f"delta {opt.get('option_delta', 'N/A')})"
                )
                for opt in top_puts
            )
            summary = (
                f"{investment.ticker}: top put options below {investment.price}: "
                f"{formatted_options}"
            )
            self.stdout.write(summary)
            summaries.append(summary)

        if not summaries:
            raise CommandError(
                "No put options found below stored prices for the selected investments."
            )

        return "\n".join(summaries)

    def _fetch_options(self, ticker_id: int, expiration_date: str) -> Any:
        params = {"ticker_id": str(ticker_id), "expiration_date": expiration_date}
        try:
            response = requests.get(
                API_URL, headers=API_HEADERS, params=params, timeout=30
            )
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise CommandError(f"Failed to call Seeking Alpha options API: {exc}") from exc

        if response.status_code != 200:
            raise CommandError(
                f"Unexpected status code {response.status_code}: {response.text}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise CommandError("Invalid JSON received from options API.") from exc

    def _filter_put_options(
        self, payload: Any, max_price: Decimal, expiration_date: date
    ) -> list[dict[str, Any]]:
        options = self._extract_options(payload)
        filtered: list[tuple[Decimal, dict[str, Any]]] = []

        for option in options:
            option_type = str(option.get("option_type", "")).lower()
            if option_type != "put":
                continue

            strike_raw = option.get("strike_price")
            try:
                strike_price = Decimal(str(strike_raw))
            except (InvalidOperation, TypeError):
                continue

            if strike_price > max_price:
                continue

            option_with_delta = dict(option)
            option_with_delta["option_delta"] = self._calculate_put_delta(
                underlying_price=float(max_price),
                strike_price=float(strike_price),
                expiration_date=expiration_date,
                implied_volatility=self._parse_implied_volatility(option.get("implied_volatility")),
                risk_free_rate=self._parse_risk_free_rate(option.get("risk_free_rate")),
            )
            filtered.append((strike_price, option_with_delta))

        filtered.sort(key=lambda item: item[0], reverse=True)
        return [option for _, option in filtered]

    def _calculate_put_delta(
        self,
        *,
        underlying_price: float,
        strike_price: float,
        expiration_date: date,
        implied_volatility: float | None,
        risk_free_rate: float,
    ) -> str:
        """Calculate Black-Scholes delta for a European put option.

        Returns "N/A" when inputs are insufficient for computation.
        """

        time_to_expiry_days = (expiration_date - date.today()).days
        time_to_expiry = time_to_expiry_days / 365.25

        if (
            implied_volatility is None
            or implied_volatility <= 0
            or time_to_expiry <= 0
            or underlying_price <= 0
            or strike_price <= 0
        ):
            return "N/A"

        sigma = implied_volatility
        try:
            d1 = (
                log(underlying_price / strike_price)
                + (risk_free_rate + 0.5 * sigma**2) * time_to_expiry
            ) / (sigma * sqrt(time_to_expiry))
        except (ValueError, ZeroDivisionError):
            return "N/A"

        delta = -self._standard_normal_cdf(-d1)
        return f"{delta:.4f}"

    @staticmethod
    def _standard_normal_cdf(x: float) -> float:
        return 0.5 * (1 + erf(x / sqrt(2)))

    @staticmethod
    def _parse_implied_volatility(raw_value: Any) -> float | None:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return None

        if value > 1:
            value /= 100
        return value if value > 0 else None

    @staticmethod
    def _parse_risk_free_rate(raw_value: Any) -> float:
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return 0.0

        if value > 1:
            value /= 100
        return value

    def _extract_options(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if isinstance(payload, dict):
            options = payload.get("options")
            if isinstance(options, list):
                return [item for item in options if isinstance(item, dict)]

        raise CommandError("Options data was not found in the API response.")
