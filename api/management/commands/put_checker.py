from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, DivisionByZero, InvalidOperation
import math
from typing import Any

import numpy as np
import pandas as pd
import requests
import talib
try:
    import yfinance as yf
except ImportError:  # pragma: no cover - optional dependency
    yf = None
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


from api.models import Symbol

API_URL = "https://seeking-alpha.p.rapidapi.com/symbols/v2/get-options"
OPTION_EXPIRATIONS_ENDPOINT = (
    "https://seeking-alpha.p.rapidapi.com/symbols/get-option-expirations"
)
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}
OPTION_EXP_WINDOW_START_DAYS = 15
OPTION_EXP_WINDOW_END_DAYS = 50
ROI_THRESHOLD = Decimal("2")
DELTA_LOWER = Decimal("-0.36")
DELTA_UPPER = Decimal("-0.25")


class Command(BaseCommand):
    """Fetch put options priced below stored investment prices."""

    help = "Check for put options below the stored investment price."

    def add_arguments(self, parser) -> None:  # pragma: no cover - argparse wiring
        parser.add_argument(
            "--rsi",
            action="store_true",
            help="Only fetch options for symbols with RSI between 30 and 70.",
        )
        parser.add_argument(
            "--roi-above",
            type=float,
            default=None,
            dest="roi_above",
            help="Filter symbols by roi > VALUE instead of score >= 75.",
        )
        parser.add_argument(
            "--stale-hours",
            type=int,
            default=None,
            dest="stale_hours",
            help="Only process symbols whose updated_at is older than N hours.",
        )
        parser.add_argument(
            "--delta-lower",
            type=float,
            default=None,
            dest="delta_lower",
            help="Override DELTA_LOWER (e.g. -0.40).",
        )
        parser.add_argument(
            "--delta-upper",
            type=float,
            default=None,
            dest="delta_upper",
            help="Override DELTA_UPPER (e.g. -0.20).",
        )
        parser.add_argument(
            "--roi-threshold",
            type=float,
            default=None,
            dest="roi_threshold",
            help="Override ROI_THRESHOLD (e.g. 1.5).",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        roi_above = options.get("roi_above")
        if roi_above is not None:
            symbols = Symbol.objects.filter(roi__gt=roi_above)
        else:
            symbols = Symbol.objects.filter(score__gte=75)

        if options.get("rsi"):
            symbols = symbols.filter(
                rsi__gte=Decimal("30"),
                rsi__lte=Decimal("70"),
            )

        stale_hours = options.get("stale_hours")
        if stale_hours is not None:
            cutoff = timezone.now() - timedelta(hours=stale_hours)
            symbols = symbols.filter(updated_at__lt=cutoff)

        effective_delta_lower = (
            Decimal(str(options["delta_lower"]))
            if options.get("delta_lower") is not None
            else DELTA_LOWER
        )
        effective_delta_upper = (
            Decimal(str(options["delta_upper"]))
            if options.get("delta_upper") is not None
            else DELTA_UPPER
        )
        effective_roi_threshold = (
            Decimal(str(options["roi_threshold"]))
            if options.get("roi_threshold") is not None
            else ROI_THRESHOLD
        )

        if not symbols.exists():
            return ""

        risk_free_rate = self._fetch_risk_free_rate()
        if risk_free_rate is None:
            self.stderr.write(
                "Risk-free rate unavailable; implied volatility will be skipped."
            )

        total = 0
        skipped_missing_exp = 0
        skipped_earnings = 0
        skipped_missing_price = 0
        skipped_fetch_error = 0
        skipped_no_roi = 0
        processed_with_options = 0
        updated_metrics = 0
        cleared_metrics = 0
        total_raw_options = 0
        total_skipped_strike_above_price = 0
        total_skipped_iv_delta_failed = 0
        total_skipped_delta_out_of_range = 0
        total_skipped_roi_threshold = 0

        summaries: list[str] = []
        for symbol in symbols:
            total += 1
            self._update_price_and_rsi(symbol)
            option_exp = self._ensure_option_expiration(symbol)
            if option_exp is None:
                skipped_missing_exp += 1
                self.stderr.write(
                    f"Skipping {symbol.ticker}: missing option expiration date."
                )
                continue

            if (
                symbol.next_earnings_date is not None
                and date.today() <= symbol.next_earnings_date <= option_exp
            ):
                skipped_earnings += 1
                self.stderr.write(
                    f"Skipping {symbol.ticker}: earnings on {symbol.next_earnings_date} "
                    f"fall on or before option expiration {option_exp}."
                )
                continue

            if symbol.price is None:
                skipped_missing_price += 1
                self.stderr.write(
                    f"Skipping {symbol.ticker}: missing stored price for comparison."
                )
                continue

            try:
                options_payload, options_source = self._fetch_options(
                    symbol, option_exp.isoformat()
                )
            except CommandError as exc:
                skipped_fetch_error += 1
                self.stderr.write(f"{symbol.ticker}: {exc}")
                continue

            time_to_expiration = self._time_to_expiration_years(option_exp)
            put_options, filter_counts = self._filter_put_options(
                options_payload,
                symbol.price,
                spot_price=symbol.price,
                time_to_expiration=time_to_expiration,
                risk_free_rate=risk_free_rate,
            )
            total_raw_options += filter_counts["total_raw"]
            total_skipped_strike_above_price += filter_counts["skipped_strike_above_price"]
            total_skipped_iv_delta_failed += filter_counts["skipped_iv_delta_failed"]
            roi_options, roi_counts = self._build_roi_options(
                put_options,
                delta_lower=effective_delta_lower,
                delta_upper=effective_delta_upper,
            )
            total_skipped_delta_out_of_range += roi_counts["skipped_delta_out_of_range"]
            roi_candidates = [
                option
                for option in roi_options
                if option.get("roi") is not None and option["roi"] > effective_roi_threshold
            ]
            total_skipped_roi_threshold += len(roi_options) - len(roi_candidates)
            if roi_options:
                processed_with_options += 1

            roi_target = self._select_roi_candidate(roi_candidates)
            roi_value = roi_target.get("roi") if roi_target is not None else None
            if roi_target is not None:
                self._update_symbol_metrics(
                    symbol,
                    option_data={
                        k: (
                            None
                            if isinstance(v, (float, np.floating))
                            and (math.isnan(float(v)) or math.isinf(float(v)))
                            else int(v)
                            if isinstance(v, np.integer)
                            else float(v)
                            if isinstance(v, (Decimal, np.floating))
                            else v
                        )
                        for k, v in roi_target.items()
                    },
                    roi=self._to_decimal(roi_value),
                )
                updated_metrics += 1
            elif symbol.roi is not None:
                symbol.roi = None
                symbol.option_data = None
                symbol.save(
                    update_fields=[
                        "roi", "option_data", "updated_at",
                    ]
                )
                cleared_metrics += 1
                self.stdout.write(
                    f"{symbol.ticker}: ROI dropped below threshold — option data cleared."
                )
            self.stdout.write(
                "DEBUG "
                f"{symbol.ticker} ROI {self._format_value(roi_value)} "
                f"strike {self._format_value(self._to_decimal(roi_target.get('strike_price')) if roi_target else None)} "
                f"source {options_source}"
            )
            if options_source == "yfinance":
                self._debug_yfinance_payload(
                    symbol.ticker, option_exp, options_payload
                )

            if not roi_candidates:
                skipped_no_roi += 1
                continue

            for option in roi_candidates:
                roi_display = self._format_value(option.get("roi"))
                mid_display = self._format_value(option.get("mid"))
                delta_display = self._format_delta(option.get("delta"))
                rsi_display = self._format_rsi(symbol.rsi)
                summary = (
                    f"{symbol.ticker}: ROI {roi_display}% at strike "
                    f"{option.get('strike_price', 'N/A')} bid {option.get('bid', 'N/A')} "
                    f"ask {option.get('ask', 'N/A')} mid {mid_display} "
                    f"delta {delta_display} rsi {rsi_display}"
                )
                self.stdout.write(summary)
                summaries.append(summary)

        if not summaries:
            raise CommandError(
                "No put options met the ROI and delta thresholds for the selected investments."
            )

        self.stdout.write(
            "DEBUG summary "
            f"total {total} "
            f"total_raw_options {total_raw_options} "
            f"skipped_missing_exp {skipped_missing_exp} "
            f"skipped_earnings {skipped_earnings} "
            f"skipped_missing_price {skipped_missing_price} "
            f"skipped_fetch_error {skipped_fetch_error} "
            f"skipped_strike_above_price {total_skipped_strike_above_price} "
            f"skipped_iv_delta_failed {total_skipped_iv_delta_failed} "
            f"skipped_delta_out_of_range {total_skipped_delta_out_of_range} "
            f"skipped_roi_threshold {total_skipped_roi_threshold} "
            f"skipped_no_roi {skipped_no_roi} "
            f"processed_with_options {processed_with_options} "
            f"updated_metrics {updated_metrics} "
            f"cleared_metrics {cleared_metrics}"
        )

        return ""

    def _fetch_options(
        self, symbol: Symbol, expiration_date: str
    ) -> tuple[Any, str]:
        # Try RapidAPI first (requires seeking_alpha_ticker_id)
        if symbol.seeking_alpha_ticker_id:
            params = {
                "ticker_id": str(symbol.seeking_alpha_ticker_id),
                "expiration_date": expiration_date,
            }
            try:
                response = requests.get(
                    API_URL, headers=API_HEADERS, params=params, timeout=30
                )
                if response.status_code == 200:
                    payload = response.json()
                    if self._extract_options(payload):
                        return payload, "rapidapi"
            except (requests.RequestException, ValueError):
                pass
            self.stderr.write(
                f"{symbol.ticker}: RapidAPI options empty or failed, falling back to yfinance."
            )

        # Fall back to yfinance
        yfinance_options = self._fetch_options_yfinance(symbol.ticker, expiration_date)
        if yfinance_options:
            return yfinance_options, "yfinance"

        raise CommandError(
            f"No options data available from RapidAPI or yfinance for {symbol.ticker}."
        )

    def _ensure_option_expiration(self, symbol: Symbol) -> date | None:
        today = date.today()
        window_start = today + timedelta(days=OPTION_EXP_WINDOW_START_DAYS)
        window_end = today + timedelta(days=OPTION_EXP_WINDOW_END_DAYS)

        current_exp = symbol.option_exp
        self.stdout.write(
            "DEBUG option_exp_check "
            f"{symbol.ticker} "
            f"current {current_exp} "
            f"window {window_start}..{window_end}"
        )
        if current_exp is not None and window_start <= current_exp <= window_end:
            self.stdout.write(
                f"DEBUG option_exp_check {symbol.ticker} using stored {current_exp}"
            )
            return current_exp

        yfinance_dates = self._fetch_option_expirations_yfinance(symbol.ticker)
        if yfinance_dates:
            self.stdout.write(
                f"DEBUG option_exp_check {symbol.ticker} fetched yfinance {len(yfinance_dates)} dates"
            )
            expirations_in_window = [
                expiration
                for expiration in yfinance_dates
                if window_start <= expiration <= window_end
            ]
            chosen_exp = max(expirations_in_window) if expirations_in_window else None
            if chosen_exp is None and current_exp is not None and current_exp >= today:
                chosen_exp = current_exp
            if chosen_exp is not None:
                self.stdout.write(
                    f"DEBUG option_exp_check {symbol.ticker} chosen {chosen_exp} (yfinance)"
                )
                if chosen_exp != symbol.option_exp:
                    symbol.option_exp = chosen_exp
                    symbol.save(update_fields=["option_exp", "updated_at"])
                return chosen_exp
            self.stdout.write(
                f"DEBUG option_exp_check {symbol.ticker} no yfinance exp in window"
            )

        self.stdout.write(
            f"DEBUG option_exp_check {symbol.ticker} fetching via rapidapi"
        )
        try:
            expiration_data = self._fetch_option_expirations(symbol.ticker)
        except CommandError as exc:
            self.stderr.write(
                f"Failed to fetch option expirations for {symbol.ticker}: {exc}"
            )
            if current_exp is not None and current_exp >= today:
                return current_exp
            return None

        parsed_dates = self._parse_expiration_dates(expiration_data["dates"])
        expirations_in_window = [
            expiration
            for expiration in parsed_dates
            if window_start <= expiration <= window_end
        ]
        chosen_exp = max(expirations_in_window) if expirations_in_window else None
        if chosen_exp is None and current_exp is not None and current_exp >= today:
            chosen_exp = current_exp
        if parsed_dates:
            self.stdout.write(
                f"DEBUG option_exp_check {symbol.ticker} fetched rapidapi {len(parsed_dates)} dates"
            )
        if chosen_exp is not None:
            self.stdout.write(
                f"DEBUG option_exp_check {symbol.ticker} chosen {chosen_exp} (rapidapi)"
            )

        ticker_id_value = self._coerce_ticker_id(expiration_data.get("ticker_id"))
        if ticker_id_value is not None and symbol.seeking_alpha_ticker_id != ticker_id_value:
            symbol.seeking_alpha_ticker_id = ticker_id_value
            symbol.save(update_fields=["seeking_alpha_ticker_id", "updated_at"])

        if chosen_exp != symbol.option_exp:
            symbol.option_exp = chosen_exp
            symbol.save(update_fields=["option_exp", "updated_at"])

        return chosen_exp

    def _fetch_option_expirations(self, ticker: str) -> dict[str, Any]:
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

    def _fetch_option_expirations_yfinance(self, ticker: str) -> list[date]:
        if yf is None:
            return []

        try:
            ticker_data = yf.Ticker(ticker)
            expirations = ticker_data.options
        except Exception as exc:  # pragma: no cover - network failure
            self.stderr.write(
                f"Failed to fetch option expirations via yfinance for {ticker}: {exc}"
            )
            return []

        if not expirations:
            return []

        parsed_dates: set[date] = set()
        for value in expirations:
            try:
                parsed_dates.add(datetime.strptime(str(value), "%Y-%m-%d").date())
            except ValueError:
                continue

        return sorted(parsed_dates)

    def _fetch_options_yfinance(
        self, ticker: str, expiration_date: str
    ) -> list[dict[str, Any]]:
        if yf is None:
            return []

        try:
            ticker_data = yf.Ticker(ticker)
            chain = ticker_data.option_chain(expiration_date)
        except Exception as exc:  # pragma: no cover - network failure
            self.stderr.write(
                f"Failed to fetch options via yfinance for {ticker} {expiration_date}: {exc}"
            )
            return []

        puts = getattr(chain, "puts", None)
        if puts is None:
            return []

        try:
            records = puts.to_dict(orient="records")
        except Exception:
            return []

        options: list[dict[str, Any]] = []
        for row in records:
            options.append(
                {
                    "option_type": "put",
                    "strike_price": row.get("strike"),
                    "bid": row.get("bid"),
                    "ask": row.get("ask"),
                    "last_price": row.get("lastPrice"),
                    "volume": row.get("volume"),
                    "open_interest": row.get("openInterest"),
                    "contract_symbol": row.get("contractSymbol"),
                    "implied_volatility": row.get("impliedVolatility"),
                }
            )

        if any(
            option.get("bid") is not None or option.get("ask") is not None
            for option in options
        ):
            return options

        return []

    def _fetch_price_and_rsi(
        self, ticker: str
    ) -> tuple[Decimal | None, Decimal | None]:
        try:
            df = yf.download(
                ticker,
                period="3mo",
                progress=False,
                actions=False,
                auto_adjust=False,
                group_by="column",
            )
        except Exception as exc:
            self.stderr.write(
                f"Failed to download price history for {ticker}: {exc}. "
                "Continuing without price/RSI data."
            )
            return None, None

        if df is None or df.empty:
            return None, None

        close = df.get("Close")
        if close is None:
            return None, None

        if isinstance(close, pd.DataFrame):
            if close.shape[1] == 1:
                close = close.iloc[:, 0]
            else:
                close = close[ticker] if ticker in close.columns else close.iloc[:, 0]

        close = close.dropna()
        if close.empty:
            return None, None

        price = None
        last_close = close.tail(1)
        if not last_close.empty:
            try:
                price = Decimal(str(last_close.iat[0]))
            except (InvalidOperation, ValueError, TypeError):
                price = None

        rsi = None
        closes = close.astype(float).to_numpy()
        if closes.size:
            rsi_values = talib.RSI(closes, timeperiod=14)
            valid_rsi = rsi_values[~np.isnan(rsi_values)]
            if valid_rsi.size:
                try:
                    rsi = Decimal(str(valid_rsi[-1]))
                except (InvalidOperation, ValueError, TypeError):
                    rsi = None

        return price, rsi

    def _update_price_and_rsi(self, symbol: Symbol) -> None:
        price, rsi = self._fetch_price_and_rsi(symbol.ticker)
        update_fields: list[str] = []
        if price is not None and price != symbol.price:
            symbol.price = price
            update_fields.append("price")
        if rsi is not None and rsi != symbol.rsi:
            symbol.rsi = rsi
            update_fields.append("rsi")
        if update_fields:
            update_fields.append("updated_at")
            symbol.save(update_fields=update_fields)

    def _debug_yfinance_payload(
        self, ticker: str, expiration_date: date, payload: Any
    ) -> None:
        try:
            options = self._extract_options(payload)
        except CommandError:
            self.stdout.write(
                f"DEBUG {ticker} yfinance exp {expiration_date.isoformat()} "
                "options missing"
            )
            return

        if not options:
            self.stdout.write(
                f"DEBUG {ticker} yfinance exp {expiration_date.isoformat()} "
                "options empty"
            )
            return

        sample = options[:5]
        formatted = []
        for option in sample:
            strike = option.get("strike_price")
            bid = option.get("bid")
            ask = option.get("ask")
            iv = option.get("implied_volatility")
            last = option.get("last_price")
            formatted.append(
                f"strike {strike} bid {bid} ask {ask} last {last} iv {iv}"
            )
        self.stdout.write(
            f"DEBUG {ticker} yfinance exp {expiration_date.isoformat()} "
            f"sample puts: {'; '.join(formatted)}"
        )

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

    def _filter_put_options(
        self,
        payload: Any,
        max_price: Decimal,
        *args: Any,
        spot_price: Decimal | None = None,
        time_to_expiration: Decimal | None = None,
        risk_free_rate: Decimal | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Return put options below the max price with filter counts.

        Accepts extra positional arguments for backward compatibility with
        older call sites that passed additional parameters, ensuring the
        command does not fail with a positional argument error.
        """
        options = self._extract_options(payload)
        filtered: list[tuple[Decimal, dict[str, Any]]] = []
        counts: dict[str, int] = {
            "total_raw": 0,
            "skipped_not_put": 0,
            "skipped_invalid_strike": 0,
            "skipped_strike_above_price": 0,
            "skipped_iv_delta_failed": 0,
        }

        for option in options:
            counts["total_raw"] += 1
            option_type = str(option.get("option_type", "")).lower()
            if option_type != "put":
                counts["skipped_not_put"] += 1
                continue

            strike_raw = option.get("strike_price")
            try:
                strike_price = Decimal(str(strike_raw))
            except (InvalidOperation, TypeError):
                counts["skipped_invalid_strike"] += 1
                continue

            if strike_price > max_price:
                counts["skipped_strike_above_price"] += 1
                continue

            option_with_value = dict(option)
            # Primary: Black-Scholes bisection from bid price.
            calculated_iv = self._calculate_implied_volatility(
                option_price=option.get("bid"),
                spot_price=spot_price,
                strike_price=strike_price,
                time_to_expiration=time_to_expiration,
                risk_free_rate=risk_free_rate,
            )
            if calculated_iv is None:
                # Fallback: market-provided IV (yfinance stores as fraction, e.g. 0.35 = 35%).
                api_iv = self._to_decimal(option.get("implied_volatility"))
                if api_iv is not None and api_iv > 0:
                    calculated_iv = (api_iv * Decimal("100")).quantize(Decimal("0.01"))
            option_with_value["implied_volatility"] = calculated_iv
            option_with_value["delta"] = self._calculate_delta(
                spot_price=spot_price,
                strike_price=strike_price,
                time_to_expiration=time_to_expiration,
                risk_free_rate=risk_free_rate,
                implied_volatility=calculated_iv,
            )
            if option_with_value["delta"] is None:
                counts["skipped_iv_delta_failed"] += 1
            filtered.append((strike_price, option_with_value))

        filtered.sort(key=lambda item: item[0], reverse=True)
        return [option for _, option in filtered], counts

    def _filter_roi_candidates(
        self,
        options: list[dict[str, Any]],
        *,
        roi_threshold: Decimal,
        delta_lower: Decimal,
        delta_upper: Decimal,
    ) -> list[dict[str, Any]]:
        """Return options whose delta range and ROI exceed the threshold."""

        roi_options, _ = self._build_roi_options(
            options, delta_lower=delta_lower, delta_upper=delta_upper
        )
        candidates = [
            option
            for option in roi_options
            if option.get("roi") is not None and option["roi"] > roi_threshold
        ]

        return candidates

    def _build_roi_options(
        self,
        options: list[dict[str, Any]],
        *,
        delta_lower: Decimal,
        delta_upper: Decimal,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Return options with calculated ROI for the delta range."""

        candidates: list[dict[str, Any]] = []
        counts: dict[str, int] = {
            "skipped_delta_none": 0,
            "skipped_delta_out_of_range": 0,
            "skipped_roi_calc_failed": 0,
        }
        for option in options:
            delta_raw = option.get("delta")
            if delta_raw is None:
                counts["skipped_delta_none"] += 1
                continue

            delta_decimal = self._to_decimal(delta_raw)
            if delta_decimal is None:
                counts["skipped_delta_none"] += 1
                continue

            if not (delta_lower <= delta_decimal <= delta_upper):
                counts["skipped_delta_out_of_range"] += 1
                continue

            try:
                strike_price = Decimal(str(option.get("strike_price")))
            except (InvalidOperation, TypeError):
                counts["skipped_roi_calc_failed"] += 1
                continue

            roi = self._calculate_roi_value(
                bid_price=option.get("bid"),
                ask_price=option.get("ask"),
                strike_price=strike_price,
            )
            if roi is None:
                counts["skipped_roi_calc_failed"] += 1
                continue

            option_with_roi = dict(option)
            option_with_roi["roi"] = roi
            option_with_roi["mid"] = self._calculate_mid_price(
                bid_price=option.get("bid"),
                ask_price=option.get("ask"),
            )
            candidates.append(option_with_roi)

        return candidates, counts

    def _select_roi_candidate(
        self, roi_candidates: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Return the highest ROI candidate, tie-breaking by lowest abs(delta)."""

        best_option: dict[str, Any] | None = None
        best_roi: Decimal | None = None
        best_delta: Decimal | None = None
        for option in roi_candidates:
            roi_value = self._to_decimal(option.get("roi"))
            delta_decimal = self._to_decimal(option.get("delta"))
            if roi_value is None or delta_decimal is None:
                continue

            if best_roi is None:
                best_roi = roi_value
                best_delta = delta_decimal
                best_option = option
                continue

            if roi_value > best_roi:
                best_roi = roi_value
                best_delta = delta_decimal
                best_option = option
                continue

            if roi_value == best_roi and best_delta is not None:
                if abs(delta_decimal) < abs(best_delta):
                    best_delta = delta_decimal
                    best_option = option

        return best_option

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
    def _calculate_bid_ask_spread(
        *, bid_price: Any, ask_price: Any
    ) -> Decimal | None:
        """Return (ask - bid) as a Decimal."""

        bid_decimal = Command._to_decimal(bid_price)
        ask_decimal = Command._to_decimal(ask_price)
        if bid_decimal is None or ask_decimal is None:
            return None

        try:
            spread = ask_decimal - bid_decimal
        except (InvalidOperation, DivisionByZero):
            return None

        if spread < 0:
            return None

        return spread.quantize(Decimal("0.0001"))

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

        try:
            if (
                option_decimal.is_nan()
                or spot_price.is_nan()
                or strike_price.is_nan()
                or option_decimal <= 0
                or spot_price <= 0
                or strike_price <= 0
            ):
                return None
        except (AttributeError, InvalidOperation):
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
    def _format_value(value: Decimal | None) -> str:
        """Convert an optional value to a printable string."""

        if value is None:
            return "N/A"

        return f"{value}"

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

    @staticmethod
    def _format_rsi(rsi: Decimal | None) -> str:
        """Convert an optional RSI to a printable string."""

        if rsi is None:
            return "N/A"

        return f"{rsi}"

    def _update_symbol_metrics(
        self,
        symbol: Symbol,
        *,
        option_data: dict | None,
        roi: Decimal | None,
    ) -> None:
        """Persist the best ROI option metrics on the symbol."""

        if option_data == symbol.option_data and roi == symbol.roi:
            return

        symbol.option_data = option_data
        symbol.roi = roi
        symbol.save(update_fields=["option_data", "roi", "updated_at"])

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

        self.stderr.write(
            "Options data was not found in the API response; skipping."
        )
        return []

    def _fetch_risk_free_rate(self) -> Decimal | None:
        """Fetch the 1-month Treasury rate from the FMP yield curve API."""

        api_key = getattr(settings, "FINANCIAL_MODELING_API_KEY", "")
        if not api_key:
            self.stderr.write("FINANCIAL_MODELING_API_KEY is not configured.")
            return None

        url = f"https://financialmodelingprep.com/stable/treasury-rates?apikey={api_key}"
        try:
            response = requests.get(url, timeout=30)
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
            data = response.json()
        except ValueError:
            self.stderr.write("Risk-free rate response was not valid JSON.")
            return None

        if not isinstance(data, list) or not data:
            self.stderr.write("Risk-free rate data was missing in the response.")
            return None

        entry = data[0] if isinstance(data[0], dict) else None
        if not entry:
            self.stderr.write("Risk-free rate entry was missing.")
            return None

        rate_raw = entry.get("month1")
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

    @staticmethod
    def _parse_expiration_dates(dates: list[str]) -> list[date]:
        """Parse dates, drop malformed, de-duplicate, return sorted ascending."""

        parsed: set[date] = set()
        for value in dates:
            try:
                parsed.add(datetime.strptime(str(value), "%m/%d/%Y").date())
            except ValueError:
                continue
        return sorted(parsed)

    @staticmethod
    def _coerce_ticker_id(ticker_id: Any) -> int | None:
        try:
            return int(ticker_id)
        except (TypeError, ValueError):
            return None
