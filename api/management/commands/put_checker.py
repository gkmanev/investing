from __future__ import annotations

from datetime import date
from decimal import Decimal, DivisionByZero, InvalidOperation, ROUND_HALF_UP
import math
from typing import Any

import requests
from django.core.management.base import BaseCommand, CommandError

from api.models import Investment

API_URL = "https://seeking-alpha.p.rapidapi.com/symbols/v2/get-options"
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}
ROI_THRESHOLD = Decimal("2.5")
DELTA_LOWER = Decimal("-0.34")
DELTA_UPPER = Decimal("-0.25")


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

        risk_free_rate = self._fetch_risk_free_rate()
        if risk_free_rate is None:
            self.stderr.write(
                "Risk-free rate unavailable; implied volatility will be skipped."
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

            time_to_expiration = self._time_to_expiration_years(investment.option_exp)
            put_options = self._filter_put_options(
                options_payload,
                investment.price,
                spot_price=investment.price,
                time_to_expiration=time_to_expiration,
                risk_free_rate=risk_free_rate,
            )
            roi_candidates = self._filter_roi_candidates(
                put_options,
                roi_threshold=ROI_THRESHOLD,
                delta_lower=DELTA_LOWER,
                delta_upper=DELTA_UPPER,
            )

            target_strike = self._target_strike(investment.price)
            matching_option = self._find_option_by_strike(put_options, target_strike)
            if matching_option is not None:
                self._update_investment_opt_val(
                    investment, matching_option.get("opt_val")
                )

            if not roi_candidates:
                continue

            for option in roi_candidates:
                roi_display = self._format_opt_val(option.get("roi"))
                mid_display = self._format_opt_val(option.get("mid"))
                delta_display = self._format_delta(option.get("delta"))
                summary = (
                    f"{investment.ticker}: ROI {roi_display}% at strike "
                    f"{option.get('strike_price', 'N/A')} bid {option.get('bid', 'N/A')} "
                    f"ask {option.get('ask', 'N/A')} mid {mid_display} "
                    f"delta {delta_display}"
                )
                self.stdout.write(summary)
                summaries.append(summary)

        if not summaries:
            raise CommandError(
                "No put options met the ROI and delta thresholds for the selected investments."
            )

        return ""

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
        self,
        payload: Any,
        max_price: Decimal,
        *args: Any,
        spot_price: Decimal | None = None,
        time_to_expiration: Decimal | None = None,
        risk_free_rate: Decimal | None = None,
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
                bid_price=option.get("bid"), strike_price=strike_price
            )
            option_with_value["implied_volatility"] = self._calculate_implied_volatility(
                option_price=option.get("bid"),
                spot_price=spot_price,
                strike_price=strike_price,
                time_to_expiration=time_to_expiration,
                risk_free_rate=risk_free_rate,
            )
            option_with_value["delta"] = self._calculate_delta(
                spot_price=spot_price,
                strike_price=strike_price,
                time_to_expiration=time_to_expiration,
                risk_free_rate=risk_free_rate,
                implied_volatility=option_with_value["implied_volatility"],
            )
            filtered.append((strike_price, option_with_value))

        filtered.sort(key=lambda item: item[0], reverse=True)
        return [option for _, option in filtered]

    def _filter_roi_candidates(
        self,
        options: list[dict[str, Any]],
        *,
        roi_threshold: Decimal,
        delta_lower: Decimal,
        delta_upper: Decimal,
    ) -> list[dict[str, Any]]:
        """Return options whose delta range and ROI exceed the threshold."""

        candidates: list[dict[str, Any]] = []
        for option in options:
            delta_raw = option.get("delta")
            if delta_raw is None:
                continue

            delta_decimal = self._to_decimal(delta_raw)
            if delta_decimal is None:
                continue

            if not (delta_lower <= delta_decimal <= delta_upper):
                continue

            try:
                strike_price = Decimal(str(option.get("strike_price")))
            except (InvalidOperation, TypeError):
                continue

            roi = self._calculate_roi_value(
                bid_price=option.get("bid"),
                ask_price=option.get("ask"),
                strike_price=strike_price,
            )
            if roi is None or roi <= roi_threshold:
                continue

            option_with_roi = dict(option)
            option_with_roi["roi"] = roi
            option_with_roi["mid"] = self._calculate_mid_price(
                bid_price=option.get("bid"),
                ask_price=option.get("ask"),
            )
            candidates.append(option_with_roi)

        return candidates

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
        *, bid_price: Any, strike_price: Decimal
    ) -> Decimal | None:
        """Return (bid / strike) * 100 as a Decimal.

        Returns ``None`` when prices are missing or invalid.
        """

        bid_decimal = Command._to_decimal(bid_price)
        if bid_decimal is None:
            return None

        if strike_price == 0:
            return None

        try:
            percentage = (bid_decimal / strike_price) * Decimal("100")
        except (InvalidOperation, DivisionByZero):
            return None

        return percentage.quantize(Decimal("0.01"))

    @staticmethod
    def _calculate_roi_value(
        *, bid_price: Any, ask_price: Any, strike_price: Decimal
    ) -> Decimal | None:
        """Return ((bid + ask) / 2 / strike) * 100 as a Decimal.

        Returns ``None`` when prices are missing or invalid.
        """

        bid_decimal = Command._to_decimal(bid_price)
        ask_decimal = Command._to_decimal(ask_price)
        if bid_decimal is None or ask_decimal is None:
            return None

        if strike_price == 0:
            return None

        try:
            mid_price = (bid_decimal + ask_decimal) / Decimal("2")
            percentage = (mid_price / strike_price) * Decimal("100")
        except (InvalidOperation, DivisionByZero):
            return None

        return percentage.quantize(Decimal("0.01"))

    @staticmethod
    def _calculate_mid_price(*, bid_price: Any, ask_price: Any) -> Decimal | None:
        """Return (bid + ask) / 2 as a Decimal."""

        bid_decimal = Command._to_decimal(bid_price)
        ask_decimal = Command._to_decimal(ask_price)
        if bid_decimal is None or ask_decimal is None:
            return None

        try:
            mid_price = (bid_decimal + ask_decimal) / Decimal("2")
        except (InvalidOperation, DivisionByZero):
            return None

        return mid_price.quantize(Decimal("0.01"))

    @staticmethod
    def _calculate_implied_volatility(
        *,
        option_price: Any,
        spot_price: Decimal | None,
        strike_price: Decimal,
        time_to_expiration: Decimal | None,
        risk_free_rate: Decimal | None,
    ) -> Decimal | None:
        """Calculate implied volatility using a bisection method.

        Returns the implied volatility as a percentage, or ``None`` if inputs are
        missing or invalid.
        """

        option_decimal = Command._to_decimal(option_price)
        if (
            option_decimal is None
            or spot_price is None
            or time_to_expiration is None
            or risk_free_rate is None
        ):
            return None

        if option_decimal <= 0 or spot_price <= 0 or strike_price <= 0:
            return None

        if time_to_expiration <= 0:
            return None

        try:
            spot = float(spot_price)
            strike = float(strike_price)
            time_years = float(time_to_expiration)
            rate = float(risk_free_rate)
            target_price = float(option_decimal)
        except (TypeError, ValueError):
            return None

        def cdf(value: float) -> float:
            return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))

        def put_price(volatility: float) -> float:
            if volatility <= 0:
                return 0.0
            sqrt_time = math.sqrt(time_years)
            d1 = (
                math.log(spot / strike)
                + (rate + 0.5 * volatility**2) * time_years
            ) / (volatility * sqrt_time)
            d2 = d1 - volatility * sqrt_time
            return strike * math.exp(-rate * time_years) * cdf(-d2) - spot * cdf(-d1)

        low = 1e-6
        high = 5.0
        low_price = put_price(low)
        high_price = put_price(high)

        if target_price < low_price or target_price > high_price:
            return None

        for _ in range(100):
            mid = (low + high) / 2.0
            mid_price = put_price(mid)
            if abs(mid_price - target_price) < 1e-6:
                low = mid
                break
            if mid_price > target_price:
                high = mid
            else:
                low = mid

        return Decimal(str(low * 100)).quantize(Decimal("0.01"))

    @staticmethod
    def _calculate_delta(
        *,
        spot_price: Decimal | None,
        strike_price: Decimal,
        time_to_expiration: Decimal | None,
        risk_free_rate: Decimal | None,
        implied_volatility: Decimal | None,
    ) -> Decimal | None:
        """Calculate the Black-Scholes delta for a put option."""

        if (
            spot_price is None
            or time_to_expiration is None
            or risk_free_rate is None
            or implied_volatility is None
        ):
            return None

        if spot_price <= 0 or strike_price <= 0 or time_to_expiration <= 0:
            return None

        try:
            spot = float(spot_price)
            strike = float(strike_price)
            time_years = float(time_to_expiration)
            rate = float(risk_free_rate)
            volatility = float(implied_volatility) / 100.0
        except (TypeError, ValueError):
            return None

        if volatility <= 0:
            return None

        sqrt_time = math.sqrt(time_years)
        d1 = (
            math.log(spot / strike) + (rate + 0.5 * volatility**2) * time_years
        ) / (volatility * sqrt_time)
        delta = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0))) - 1.0
        return Decimal(str(delta)).quantize(Decimal("0.01"))

    @staticmethod
    def _to_decimal(value: Any) -> Decimal | None:
        """Convert a value to Decimal, returning None when conversion fails."""

        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError):
            return None

    @staticmethod
    def _format_opt_val(opt_val: Decimal | None) -> str:
        """Convert an optional opt_val to a printable string."""

        if opt_val is None:
            return "N/A"

        return f"{opt_val}"

    @staticmethod
    def _format_implied_volatility(implied_volatility: Decimal | None) -> str:
        """Convert an optional implied volatility to a printable string."""

        if implied_volatility is None:
            return "N/A"

        return f"{implied_volatility}"

    @staticmethod
    def _format_delta(delta: Decimal | None) -> str:
        """Convert an optional delta to a printable string."""

        if delta is None:
            return "N/A"

        return f"{delta}"

    def _update_investment_opt_val(
        self, investment: Investment, opt_val: Decimal | None
    ) -> None:
        """Persist the calculated opt_val on the investment if it changed."""

        if opt_val == investment.opt_val:
            return

        investment.opt_val = opt_val
        investment.save(update_fields=["opt_val"])

    @staticmethod
    def _format_recent_puts(
        put_options: list[dict[str, Any]], price: Decimal
    ) -> str:
        """Return a summary of the last three strike/bid values below price."""

        if not put_options:
            return f"No puts found below {price}."

        recent = put_options[:3]
        formatted = []
        for option in recent:
            strike = option.get("strike_price", "N/A")
            bid = option.get("bid", "N/A")
            delta_display = Command._format_delta(option.get("delta"))
            formatted.append(f"strike {strike} bid {bid} delta {delta_display}")

        return f"Last three strikes below {price}: {', '.join(formatted)}."

    def _extract_options(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]

        if isinstance(payload, dict):
            options = payload.get("options")
            if isinstance(options, list):
                return [item for item in options if isinstance(item, dict)]

        raise CommandError("Options data was not found in the API response.")

    def _fetch_risk_free_rate(self) -> Decimal | None:
        """Fetch the latest risk-free rate from the U.S. Treasury API."""

        url = (
            "https://api.fiscaldata.treasury.gov/services/api/"
            "fiscal_service/v2/accounting/od/avg_interest_rates"
        )
        params = {
            "filter": "security_desc:eq:Treasury Bills",
            "sort": "-record_date",
            "page[size]": "1",
        }
        try:
            response = requests.get(url, params=params, timeout=30)
        except requests.RequestException as exc:  # pragma: no cover - network failure
            self.stderr.write(f"Failed to fetch risk-free rate: {exc}")
            return None

        if response.status_code != 200:
            self.stderr.write(
                "Risk-free rate request failed with status "
                f"{response.status_code}: {response.text}"
            )
            return None

        try:
            payload = response.json()
        except ValueError:
            self.stderr.write("Risk-free rate response was not valid JSON.")
            return None

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or not data:
            self.stderr.write("Risk-free rate data was missing in the response.")
            return None

        entry = data[0] if isinstance(data[0], dict) else None
        if not entry:
            self.stderr.write("Risk-free rate entry was missing.")
            return None

        rate_raw = entry.get("avg_interest_rate_amt")
        rate_decimal = self._to_decimal(rate_raw)
        if rate_decimal is None:
            self.stderr.write("Risk-free rate value was invalid.")
            return None

        return (rate_decimal / Decimal("100")).quantize(Decimal("0.0001"))

    @staticmethod
    def _time_to_expiration_years(expiration_date: date | None) -> Decimal | None:
        """Return time to expiration in years."""

        if expiration_date is None:
            return None

        days = (expiration_date - date.today()).days
        if days <= 0:
            return None

        return (Decimal(days) / Decimal("365")).quantize(Decimal("0.0001"))
