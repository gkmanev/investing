from __future__ import annotations

from decimal import Decimal, DivisionByZero, InvalidOperation, ROUND_HALF_UP
from typing import Any

import requests
from django.core.management.base import BaseCommand, CommandError

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

            put_options = self._filter_put_options(options_payload, investment.price)

            target_strike = self._target_strike(investment.price)
            matching_option = self._find_option_by_strike(put_options, target_strike)

            if matching_option is None:
                self.stdout.write(
                    f"{investment.ticker}: no put option at strike {target_strike} "
                    f"below price {investment.price}."
                )
                continue

            formatted_option = (
                f"{matching_option['symbol']} (strike {matching_option['strike_price']}, "
                f"last {matching_option.get('last', 'N/A')}, "
                f"opt_val {matching_option.get('opt_val', 'N/A')}%)"
            )
            summary = (
                f"{investment.ticker}: put option at strike {target_strike} below "
                f"{investment.price}: {formatted_option}"
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
        self, payload: Any, max_price: Decimal, *args: Any
    ) -> list[dict[str, Any]]:
        """Return put options below the max price.

        Accepts extra positional arguments for backward compatibility with
        older call sites that passed additional parameters, ensuring the
        command does not fail with a positional argument error.
        """
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

            option_with_value = dict(option)
            option_with_value["opt_val"] = self._calculate_option_value(
                last_price=option.get("last"), strike_price=strike_price
            )
            filtered.append((strike_price, option_with_value))

        filtered.sort(key=lambda item: item[0], reverse=True)
        return [option for _, option in filtered]

    @staticmethod
    def _target_strike(max_price: Decimal) -> Decimal:
        """Round the current price to the nearest integer and subtract two."""

        max_price_decimal = Decimal(str(max_price))
        rounded_price = max_price_decimal.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return rounded_price - Decimal(2)

    @staticmethod
    def _find_option_by_strike(
        options: list[dict[str, Any]], target_strike: Decimal
    ) -> dict[str, Any] | None:
        """Return the first option whose strike matches the target value."""

        for option in options:
            try:
                strike_price = Decimal(str(option.get("strike_price")))
            except (InvalidOperation, TypeError):
                continue

            if strike_price == target_strike:
                return option

        return None

    @staticmethod
    def _calculate_option_value(
        *, last_price: Any, strike_price: Decimal
    ) -> str:
        """Return (last / strike) * 100 as a percentage string.

        Returns "N/A" when prices are missing or invalid.
        """

        try:
            last_decimal = Decimal(str(last_price))
        except (InvalidOperation, TypeError):
            return "N/A"

        if strike_price == 0:
            return "N/A"

        try:
            opt_value = (last_decimal / strike_price) * Decimal("100")
        except (InvalidOperation, DivisionByZero):
            return "N/A"

        return f"{opt_value.quantize(Decimal('0.01'))}"

    def _extract_options(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if isinstance(payload, dict):
            options = payload.get("options")
            if isinstance(options, list):
                return [item for item in options if isinstance(item, dict)]

        raise CommandError("Options data was not found in the API response.")
