from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from api.models import Investment

BASE_URL = "http://127.0.0.1:8000"
INVESTMENTS_ENDPOINT = f"{BASE_URL}/api/investments/"
OPTION_EXPIRATIONS_ENDPOINT = (
    "https://seeking-alpha.p.rapidapi.com/symbols/get-option-expirations"
)
PROFILE_ENDPOINT = "https://seeking-alpha.p.rapidapi.com/symbols/get-profile"
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}


class Command(BaseCommand):
    """Fetch option suitability data for investments."""

    help = "Fetches local investments and updates their options suitability."

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
        parser.add_argument(
            "--debug-suitability",
            action="store_true",
            help="Print debug information explaining suitability calculations.",
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
            tickers = [t for t in tickers if t.upper() not in priced_tickers]
            if not tickers:
                raise CommandError(
                    "No tickers remain to update after skipping priced investments."
                )

        updated_tickers: list[str] = []
        today = timezone.now().date()
        upper_bound = today + timedelta(days=31)
        debug_suitability: bool = bool(options.get("debug_suitability"))

        for ticker in tickers:
            try:
                expiration_data = self._fetch_option_expirations(ticker)
            except CommandError as exc:
                self.stderr.write(
                    f"Failed to fetch option expirations for {ticker}: {exc}. "
                    "Continuing without options data."
                )
                continue

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

            # NEW robust suitability: next 3 upcoming expirations must be weekly spaced
            (
                options_suitability,
                reason,
                next_three,
                chosen_option_exp,
            ) = self._weekly_suitability_details(expiration_data["dates"], today)

            if debug_suitability:
                shown = ", ".join(d.isoformat() for d in next_three) if next_three else "(none)"
                self.stdout.write(
                    f"{ticker}: suitability={options_suitability} | next_three={shown} | {reason}"
                )

            last_price = None
            rsi_value = None
            if options_suitability == 1:
                try:
                    last_price, rsi_value = self._fetch_profile_snapshot(ticker)
                except CommandError as exc:
                    self.stderr.write(
                        f"Failed to fetch profile for {ticker}: {exc}. "
                        "Continuing without price data."
                    )
                else:
                    if last_price is None:
                        self.stdout.write(
                            f"{ticker}: suitability met but profile returned no price; "
                            "leaving price unchanged."
                        )
                    else:
                        self.stdout.write(
                            f"{ticker}: suitability met with last price {last_price}."
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

            investment.options_suitability = options_suitability

            # NEW: if suitable, store the 3rd upcoming expiration (the last of the weekly chain)
            investment.option_exp = chosen_option_exp if options_suitability == 1 else None

            if last_price is not None:
                investment.price = last_price
            if rsi_value is not None:
                investment.rsi = rsi_value

            if created:
                investment.save()
            else:
                update_fields = ["options_suitability", "option_exp", "updated_at"]
                if last_price is not None:
                    update_fields.append("price")
                if rsi_value is not None:
                    update_fields.append("rsi")
                investment.save(update_fields=update_fields)

            action = "Created" if created else "Updated"
            self.stdout.write(
                f"{action} investment {ticker} with options suitability={options_suitability}"
            )
            updated_tickers.append(ticker)

        if not updated_tickers:
            raise CommandError("No investments were updated with options suitability data.")

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
                    "Investments payload did not contain a list under 'results' or 'data'."
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

    def _fetch_profile_snapshot(
        self, ticker: str
    ) -> tuple[Decimal | None, Decimal | None]:
        params = {"symbols": ticker}
        prepared = requests.Request("GET", PROFILE_ENDPOINT, params=params).prepare()
        self.stdout.write(f"Fetching profile data from {prepared.url}")

        payload = self._fetch_json(PROFILE_ENDPOINT, params=params, headers=API_HEADERS)
        last_price = self._extract_last_price(payload)
        rsi_value = self._extract_rsi(payload)
        print(f"Last Price for {ticker}:{last_price}")
        if rsi_value is not None:
            print(f"RSI for {ticker}:{rsi_value}")
        return last_price, rsi_value

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

    def _weekly_suitability_details(
        self, dates: list[str], today: date
    ) -> tuple[int, str, list[date], date | None]:
        """
        Rule:
          - take the next 3 expiration dates from today (>= today)
          - suitability = 1 if they are exactly 7 days apart consecutively
            (d2-d1 == 7 and d3-d2 == 7)
          - return chosen_option_exp = d3 when suitable else None
        """
        parsed = self._parse_expiration_dates(dates)
        upcoming = [d for d in parsed if d >= today]

        if len(upcoming) < 3:
            return 0, f"Not enough upcoming expirations (have {len(upcoming)}, need 3).", upcoming[:3], None

        next_three = upcoming[:3]
        gap1 = (next_three[1] - next_three[0]).days
        gap2 = (next_three[2] - next_three[1]).days

        if gap1 == 7 and gap2 == 7:
            return 1, f"Weekly chain OK (gaps: {gap1}, {gap2}).", next_three, next_three[2]

        return 0, f"Weekly chain FAIL (gaps: {gap1}, {gap2}; need 7,7).", next_three, None

    # -------------------------
    # Price parsing / id coercion
    # -------------------------

    def _extract_last_price(self, payload: Any) -> Decimal | None:
        data_section = payload
        if isinstance(payload, dict) and "data" in payload:
            data_section = payload.get("data")

        def _pluck_last(section: Any) -> Any | None:
            if not isinstance(section, dict):
                return None
            attributes = section.get("attributes")
            source = attributes if isinstance(attributes, dict) else section
            price_block = source.get("price") if isinstance(source, dict) else None
            if isinstance(price_block, dict) and "last" in price_block:
                return price_block.get("last")
            last_daily = source.get("lastDaily") if isinstance(source, dict) else None
            if isinstance(last_daily, dict) and "last" in last_daily:
                return last_daily.get("last")
            return source.get("last")

        last_value = None
        if isinstance(data_section, list):
            for entry in data_section:
                last_value = _pluck_last(entry)
                if last_value is not None:
                    break
        else:
            last_value = _pluck_last(data_section)

        if last_value is None:
            return None

        try:
            return Decimal(str(last_value))
        except (InvalidOperation, ValueError):
            return None

    def _extract_rsi(self, payload: Any) -> Decimal | None:
        value = self._find_payload_value(
            payload,
            {
                "rsi",
                "relative_strength_index",
                "relativeStrengthIndex",
                "relative_strength",
                "relativeStrength",
            },
        )
        if value is None:
            return None

        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None

    def _find_payload_value(self, payload: Any, keys: set[str]) -> Any | None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in keys:
                    return value
                nested_value = self._find_payload_value(value, keys)
                if nested_value is not None:
                    return nested_value
        elif isinstance(payload, list):
            for item in payload:
                nested_value = self._find_payload_value(item, keys)
                if nested_value is not None:
                    return nested_value
        return None

    def _coerce_ticker_id(self, ticker_id: Any) -> int | None:
        try:
            return int(ticker_id)
        except (TypeError, ValueError):
            return None
