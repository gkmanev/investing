from __future__ import annotations

import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Any, Callable

import certifi
import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections
from django.utils import timezone

from api.models import Symbol


DEFAULT_EXCHANGE = "NASDAQ"
MIN_DTE = 25
MAX_DTE = 55
PUT_DELTA_MIN = Decimal("-0.37")
PUT_DELTA_MAX = Decimal("-0.24")
ROI_THRESHOLD = Decimal("2")
MAX_CALL_SPREAD_PCT = Decimal("25")
MAX_WORKERS = 1
DEFAULT_MAX_REQUESTS_PER_SECOND = 2.0
MAX_RETRIES = 6
BACKOFF_SECONDS = 1.5
EXCHANGE_ALIASES = {
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
    "NAS": "NASDAQ",
    "NASDAQGS": "NASDAQ",
    "NASDAQGM": "NASDAQ",
    "NASDAQCM": "NASDAQ",
    "NYQ": "NYSE",
    "NYS": "NYSE",
    "ASE": "AMEX",
    "AMEX": "AMEX",
    "PCX": "NYSEARCA",
    "ARCX": "NYSEARCA",
    "BTS": "BATS",
    "BATS": "BATS",
}


class RequestRateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.interval = 1 / requests_per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._cooldown_until = 0.0

    def wait(self) -> None:
        while True:
            delay = 0.0
            with self._lock:
                now = time.monotonic()
                next_allowed = max(self._next_allowed, self._cooldown_until)
                if now >= next_allowed:
                    self._next_allowed = now + self.interval
                    return
                delay = next_allowed - now
            time.sleep(delay)

    def backoff(self, delay_seconds: float) -> None:
        if delay_seconds <= 0:
            return
        with self._lock:
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + delay_seconds,
            )


class TradingViewOptions:
    OPTIONS_URL = (
        "https://scanner.tradingview.com/options/scan2?label-product=options-builder"
    )
    GLOBAL_URL = (
        "https://scanner.tradingview.com/global/scan2?label-product=options-builder"
    )
    SYMBOL_URL = "https://scanner.tradingview.com/symbol"
    HEADERS = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://www.tradingview.com/",
    }

    def __init__(
        self,
        session: requests.Session | None = None,
        rate_limiter: RequestRateLimiter | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.rate_limiter = rate_limiter

    def close(self) -> None:
        self.session.close()

    @staticmethod
    def _retry_delay(response: requests.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(float(retry_after), BACKOFF_SECONDS)
                except ValueError:
                    pass
        return BACKOFF_SECONDS * (2 ** (attempt - 1))

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        response: requests.Response | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            if self.rate_limiter is not None:
                self.rate_limiter.wait()
            response = self.session.request(method, url, **kwargs)
            if response.status_code != 429:
                response.raise_for_status()
                return response
            if attempt == MAX_RETRIES:
                response.raise_for_status()
            retry_delay = self._retry_delay(response, attempt)
            if self.rate_limiter is not None:
                self.rate_limiter.backoff(retry_delay)
            time.sleep(retry_delay)

        assert response is not None
        response.raise_for_status()
        return response

    def post_options(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST",
            self.OPTIONS_URL,
            json=payload,
            headers=self.HEADERS,
            timeout=30,
        )
        return response.json()

    def post_global(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            "POST",
            self.GLOBAL_URL,
            json=payload,
            headers=self.HEADERS,
            timeout=30,
        )
        return response.json()

    def get_expirations(self, exchange: str, ticker: str) -> list[int]:
        payload = {
            "columns": ["expiration"],
            "filter": [
                {"left": "type", "operation": "equal", "right": "option"},
                {"left": "root", "operation": "equal", "right": ticker},
            ],
            "ignore_unknown_fields": False,
            "index_filters": [
                {"name": "underlying_symbol", "values": [f"{exchange}:{ticker}"]}
            ],
        }
        data = self.post_options(payload)
        return sorted(
            {
                int(item["f"][0])
                for item in data.get("symbols", [])
                if item.get("f") and item["f"][0] is not None
            }
        )

    def pick_expiration(
        self,
        expirations: list[int],
        *,
        min_dte: int,
        max_dte: int,
    ) -> int | None:
        today = date.today()
        candidates: list[tuple[int, int]] = []

        for expiration in expirations:
            expiration_date = datetime.strptime(str(expiration), "%Y%m%d").date()
            dte = (expiration_date - today).days
            if min_dte <= dte <= max_dte:
                candidates.append((dte, expiration))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def get_chain(
        self,
        exchange: str,
        ticker: str,
        expiration: int,
    ) -> list[dict[str, Any]]:
        payload = {
            "columns": [
                "ask",
                "bid",
                "currency",
                "delta",
                "expiration",
                "gamma",
                "iv",
                "option-type",
                "pricescale",
                "rho",
                "root",
                "strike",
                "theoPrice",
                "theta",
                "vega",
                "volume",
                "open_interest",
                "bid_iv",
                "ask_iv",
            ],
            "filter": [
                {"left": "type", "operation": "equal", "right": "option"},
                {"left": "expiration", "operation": "equal", "right": int(expiration)},
                {"left": "root", "operation": "equal", "right": ticker},
            ],
            "ignore_unknown_fields": False,
            "index_filters": [
                {"name": "underlying_symbol", "values": [f"{exchange}:{ticker}"]}
            ],
        }
        data = self.post_options(payload)
        fields = data["fields"]
        rows: list[dict[str, Any]] = []

        for item in data.get("symbols", []):
            row = {"option_symbol": item["s"]}
            row.update(dict(zip(fields, item["f"])))
            row["option_type"] = row.pop("option-type")
            row["strike_price"] = row.get("strike")
            rows.append(row)

        return rows

class FinancialModelingPrepClient:
    BASE_URL = "https://financialmodelingprep.com/stable"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = (
            (api_key if api_key is not None else getattr(settings, "FINANCIAL_MODELING_API_KEY", ""))
            or ""
        ).strip()
        if not self.api_key:
            raise ValueError("FINANCIAL_MODELING_API_KEY is missing in Django settings")
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())

    def _fetch_json(self, url: str) -> Any:
        max_attempts = 4
        wait = 15

        for attempt in range(1, max_attempts + 1):
            try:
                with urllib.request.urlopen(
                    url,
                    context=self._ssl_context,
                    timeout=30,
                ) as response:
                    body = response.read()
                return json.loads(body.decode("utf-8")) if body else None
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                sleep_secs = int(retry_after) if retry_after else wait
                if attempt == max_attempts:
                    raise
                time.sleep(sleep_secs)
                wait *= 2

    def _resolve_quote(self, ticker: str) -> dict[str, Any]:
        normalized_ticker = ticker.upper()
        query = urllib.parse.urlencode(
            {"symbol": normalized_ticker, "apikey": self.api_key}
        )
        url = f"{self.BASE_URL}/quote-short?{query}"
        payload = self._fetch_json(url)
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"No quote data returned for {normalized_ticker}")

        quote: dict[str, Any] | None = None
        for candidate in payload:
            if not isinstance(candidate, dict):
                continue
            returned_symbol = str(candidate.get("symbol", "")).strip().upper()
            if returned_symbol == normalized_ticker:
                quote = candidate
                break

        if quote is None:
            if len(payload) == 1 and isinstance(payload[0], dict) and "symbol" not in payload[0]:
                quote = payload[0]
            else:
                raise ValueError(f"No exact quote match returned for {normalized_ticker}")

        if not isinstance(quote, dict) or "price" not in quote:
            raise ValueError(f"Missing price in quote response for {normalized_ticker}")
        return quote

    def get_underlying_price(self, ticker: str) -> Any:
        return self._resolve_quote(ticker)["price"]

    def get_price_and_volume(self, ticker: str) -> tuple[Any, Any]:
        quote = self._resolve_quote(ticker)
        return quote["price"], quote.get("volume")

    def get_rsi(self, ticker: str, period_length: int = 14, timeframe: str = "1hour") -> Any:
        normalized_ticker = ticker.upper()
        query = urllib.parse.urlencode({
            "symbol": normalized_ticker,
            "periodLength": period_length,
            "timeframe": timeframe,
            "apikey": self.api_key,
        })
        url = f"{self.BASE_URL}/technical-indicators/rsi?{query}"
        payload = self._fetch_json(url)
        if not isinstance(payload, list) or not payload:
            return None
        last = payload[-1]
        if not isinstance(last, dict):
            return None
        return last.get("rsi")


class Command(BaseCommand):
    help = "Fetch TradingView put data, calculate ROI, and save it on Symbol rows."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "tickers",
            nargs="*",
            help="Optional ticker symbols to limit processing.",
        )
        parser.add_argument(
            "--exchange",
            default=DEFAULT_EXCHANGE,
            help="Fallback exchange when Symbol.exchange is blank.",
        )
        parser.add_argument(
            "--min-dte",
            type=int,
            default=MIN_DTE,
            dest="min_dte",
            help="Minimum days to expiration.",
        )
        parser.add_argument(
            "--max-dte",
            type=int,
            default=MAX_DTE,
            dest="max_dte",
            help="Maximum days to expiration.",
        )
        parser.add_argument(
            "--delta-min",
            type=str,
            default=str(PUT_DELTA_MIN),
            dest="delta_min",
            help="Minimum put delta filter.",
        )
        parser.add_argument(
            "--delta-max",
            type=str,
            default=str(PUT_DELTA_MAX),
            dest="delta_max",
            help="Maximum put delta filter.",
        )
        parser.add_argument(
            "--rsi",
            action="store_true",
            help="Only fetch options for symbols with RSI between 30 and 70.",
        )
        parser.add_argument(
            "--skip-rsi",
            action="store_true",
            dest="skip_rsi",
            help="Do not fetch or update RSI from Financial Modeling Prep.",
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
            "--roi-threshold",
            type=str,
            default=str(ROI_THRESHOLD),
            dest="roi_threshold",
            help="Minimum ROI threshold for saving a candidate.",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=MAX_WORKERS,
            help="Number of tickers to process concurrently.",
        )
        parser.add_argument(
            "--max-rps",
            type=float,
            default=DEFAULT_MAX_REQUESTS_PER_SECOND,
            dest="max_rps",
            help="Maximum TradingView requests per second across all workers.",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        min_dte = options["min_dte"]
        max_dte = options["max_dte"]
        delta_min = self._to_decimal(options["delta_min"])
        delta_max = self._to_decimal(options["delta_max"])
        roi_threshold = self._to_decimal(options["roi_threshold"])
        fallback_exchange = str(options["exchange"]).upper()

        if min_dte > max_dte:
            raise CommandError("--min-dte cannot be greater than --max-dte.")
        if delta_min is None or delta_max is None or roi_threshold is None:
            raise CommandError("Delta bounds and ROI threshold must be valid numbers.")
        if delta_min > delta_max:
            raise CommandError("--delta-min cannot be greater than --delta-max.")
        if roi_threshold < 0:
            raise CommandError("--roi-threshold cannot be negative.")
        if options["workers"] < 1:
            raise CommandError("--workers must be at least 1.")
        if options["max_rps"] <= 0:
            raise CommandError("--max-rps must be greater than 0.")
        fmp_api_key = (getattr(settings, "FINANCIAL_MODELING_API_KEY", "") or "").strip()
        price_client = FinancialModelingPrepClient(api_key=fmp_api_key) if fmp_api_key else None
        if price_client is None:
            self.stderr.write(
                "FINANCIAL_MODELING_API_KEY is not configured; using stored Symbol.price values."
            )

        requested_tickers = [ticker.upper() for ticker in options["tickers"]]
        if requested_tickers:
            queryset = Symbol.objects.filter(ticker__in=requested_tickers)
        else:
            roi_above = options.get("roi_above")
            if roi_above is not None:
                queryset = Symbol.objects.filter(roi__gt=roi_above)
            else:
                queryset = Symbol.objects.filter(score__gte=75)

        if options.get("rsi"):
            queryset = queryset.filter(
                rsi__gte=Decimal("30"),
                rsi__lte=Decimal("70"),
            )

        stale_hours = options.get("stale_hours")
        if stale_hours is not None:
            cutoff = timezone.now() - timedelta(hours=stale_hours)
            queryset = queryset.filter(updated_at__lt=cutoff)

        queryset = queryset.order_by("ticker")

        symbols = list(
            queryset.only(
                "ticker",
                "exchange",
                "price",
                "rsi",
                "option_exp",
                "option_volume",
                "option_iv",
                "option_data",
                "roi",
                "updated_at",
            )
        )
        if not symbols:
            self.stdout.write("No symbols found.")
            return ""

        processed = 0
        updated = 0
        cleared = 0
        errors = 0
        print_lock = threading.Lock()
        client_lock = threading.Lock()
        clients: list[TradingViewOptions] = []
        thread_state = threading.local()
        rate_limiter = RequestRateLimiter(options["max_rps"])

        def reporter(message: str, *, error: bool = False) -> None:
            with print_lock:
                (self.stderr if error else self.stdout).write(message)

        def process_symbol(symbol: Symbol) -> tuple[bool, bool]:
            close_old_connections()
            try:
                client = getattr(thread_state, "client", None)
                if client is None:
                    client = TradingViewOptions(rate_limiter=rate_limiter)
                    thread_state.client = client
                    with client_lock:
                        clients.append(client)
                exchange = self._normalize_exchange(
                    symbol.exchange or fallback_exchange or DEFAULT_EXCHANGE
                )
                return self._process_symbol(
                    client=client,
                    price_client=price_client,
                    symbol=symbol,
                    exchange=exchange,
                    min_dte=min_dte,
                    max_dte=max_dte,
                    delta_min=delta_min,
                    delta_max=delta_max,
                    roi_threshold=roi_threshold,
                    fetch_rsi=not options["skip_rsi"],
                    reporter=reporter,
                )
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=options["workers"]) as executor:
            futures = {executor.submit(process_symbol, symbol): symbol for symbol in symbols}
            for future in as_completed(futures):
                symbol = futures[future]
                processed += 1
                try:
                    changed, was_cleared = future.result()
                except CommandError as exc:
                    errors += 1
                    reporter(f"{symbol.ticker}: {exc}", error=True)
                    continue
                except Exception as exc:  # pragma: no cover - defensive guard
                    errors += 1
                    reporter(
                        f"{symbol.ticker}: unexpected error while processing - {exc}",
                        error=True,
                    )
                    continue

                if changed:
                    updated += 1
                if was_cleared:
                    cleared += 1
        for client in clients:
            client.close()

        self.stdout.write(
            f"Processed {processed} symbols | updated {updated} | "
            f"cleared {cleared} | errors {errors}"
        )
        return ""

    @staticmethod
    def _normalize_exchange(exchange: str | None) -> str:
        if not exchange:
            return DEFAULT_EXCHANGE

        normalized = str(exchange).strip().upper()
        return EXCHANGE_ALIASES.get(normalized, normalized)

    def _process_symbol(
        self,
        *,
        client: TradingViewOptions,
        price_client: FinancialModelingPrepClient | None,
        symbol: Symbol,
        exchange: str,
        min_dte: int,
        max_dte: int,
        delta_min: Decimal,
        delta_max: Decimal,
        roi_threshold: Decimal,
        fetch_rsi: bool,
        reporter: Callable[[str], None] | None = None,
    ) -> tuple[bool, bool]:
        if price_client is None:
            underlying_price = self._to_decimal(symbol.price)
            monthly_rsi = symbol.rsi
            fmp_volume = None
        else:
            try:
                underlying_price_raw, fmp_volume = price_client.get_price_and_volume(symbol.ticker)
                underlying_price = self._to_decimal(underlying_price_raw)
                monthly_rsi = (
                    self._to_decimal(price_client.get_rsi(symbol.ticker))
                    if fetch_rsi
                    else symbol.rsi
                )
            except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                raise CommandError(
                    self._format_fmp_request_error(ticker=symbol.ticker, exc=exc)
                ) from exc
            except (KeyError, TypeError, ValueError) as exc:
                raise CommandError(
                    f"Financial Modeling Prep returned invalid data for {symbol.ticker}: {exc}"
                ) from exc

        try:
            expirations = client.get_expirations(exchange=exchange, ticker=symbol.ticker)
            selected = client.pick_expiration(
                expirations,
                min_dte=min_dte,
                max_dte=max_dte,
            )
        except requests.RequestException as exc:
            raise CommandError(
                self._format_tradingview_request_error(
                    exchange=exchange,
                    ticker=symbol.ticker,
                    exc=exc,
                )
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(
                f"TradingView returned invalid data for {exchange}:{symbol.ticker}."
            ) from exc

        if underlying_price is None:
            if price_client is None:
                raise CommandError(
                    "Missing or invalid underlying price and "
                    "FINANCIAL_MODELING_API_KEY is not configured."
                )
            raise CommandError("Missing or invalid underlying price.")

        if selected is None:
            changed = self._update_symbol_snapshot(
                symbol,
                price=underlying_price,
                rsi=monthly_rsi,
                option_exp=None,
                option_volume=None,
                option_iv=None,
                option_data=None,
                call_data=None,
                roi=None,
            )
            self._write_status(
                f"{symbol.ticker}: no expiration in {min_dte}-{max_dte} DTE window.",
                reporter=reporter,
            )
            return changed, changed

        selected_date = datetime.strptime(str(selected), "%Y%m%d").date()

        try:
            chain = client.get_chain(
                exchange=exchange,
                ticker=symbol.ticker,
                expiration=selected,
            )
        except requests.RequestException as exc:
            raise CommandError(
                self._format_tradingview_request_error(
                    exchange=exchange,
                    ticker=symbol.ticker,
                    exc=exc,
                    context=f"chain {selected}",
                )
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(
                f"TradingView returned invalid chain data for {exchange}:{symbol.ticker} {selected}."
            ) from exc

        roi_candidates = self._build_roi_candidates(
            chain=chain,
            delta_min=delta_min,
            delta_max=delta_max,
            roi_threshold=roi_threshold,
            option_type="put",
        )
        best_candidate = self._select_roi_candidate(roi_candidates)

        call_candidates = self._collect_call_contracts(chain=chain)

        had_option_metrics = any(
            value is not None
            for value in (
                symbol.option_data,
                symbol.roi,
                symbol.option_volume,
                symbol.option_iv,
            )
        )

        option_data = (
            self._build_option_snapshot(best_candidate, roi_candidates)
            if best_candidate is not None
            else None
        )
        call_data = (
            [self._serialize_option_data(c) for c in call_candidates]
            if call_candidates
            else None
        )
        roi_value = (
            self._to_decimal(best_candidate.get("roi"))
            if best_candidate is not None
            else None
        )
        option_volume = self._to_int(fmp_volume) if best_candidate else None
        option_iv = self._to_decimal(best_candidate.get("iv")) if best_candidate else None
        self._validate_snapshot_consistency(
            ticker=symbol.ticker,
            underlying_price=underlying_price,
            option_data=option_data,
        )
        changed = self._update_symbol_snapshot(
            symbol,
            price=underlying_price,
            rsi=monthly_rsi,
            option_exp=selected_date,
            option_volume=option_volume,
            option_iv=option_iv,
            option_data=option_data,
            call_data=call_data,
            roi=roi_value,
        )

        if best_candidate is None:
            call_status = (
                f"saved {len(call_candidates)} calls"
                if call_candidates
                else "no qualifying calls"
            )
            self._write_status(
                f"{symbol.ticker}: no puts in delta range for {selected_date}; "
                f"price/expiration updated, option data cleared; {call_status}.",
                reporter=reporter,
            )
            return changed, had_option_metrics and changed

        self._write_status(
            f"{symbol.ticker}: exp {selected_date} price {underlying_price} "
            f"strike {best_candidate['strike_price']} bid {best_candidate['bid']} "
            f"ask {best_candidate['ask']} delta {best_candidate['delta']} "
            f"vol {best_candidate.get('volume')} iv {best_candidate.get('iv')} "
            f"roi {best_candidate['roi']}",
            reporter=reporter,
        )
        return changed, False

    def _write_status(
        self,
        message: str,
        *,
        reporter: Callable[[str], None] | None = None,
    ) -> None:
        if reporter is not None:
            reporter(message)
            return
        self.stdout.write(message)

    @staticmethod
    def _validate_snapshot_consistency(
        *,
        ticker: str,
        underlying_price: Decimal,
        option_data: dict[str, Any] | None,
    ) -> None:
        if option_data:
            option_type = str(option_data.get("option_type", "")).strip().lower()
            strike_price = Command._to_decimal(option_data.get("strike_price"))
            if option_type == "put" and strike_price is not None:
                if strike_price >= underlying_price:
                    raise CommandError(
                        f"Inconsistent market snapshot for {ticker}: underlying price "
                        f"{underlying_price} is not above selected put strike {strike_price}."
                    )

    @staticmethod
    def _format_tradingview_request_error(
        *,
        exchange: str,
        ticker: str,
        exc: requests.RequestException,
        context: str | None = None,
    ) -> str:
        prefix = f"TradingView request failed for {exchange}:{ticker}"
        if context:
            prefix = f"{prefix} ({context})"

        response = getattr(exc, "response", None)
        if response is not None and response.status_code == 429:
            return (
                f"{prefix}: rate limited by TradingView (HTTP 429). "
                "Try lowering --max-rps or using --skip-rsi."
            )
        return f"{prefix}: {exc}"

    @staticmethod
    def _format_fmp_request_error(
        *,
        ticker: str,
        exc: urllib.error.HTTPError | urllib.error.URLError,
    ) -> str:
        prefix = f"Financial Modeling Prep request failed for {ticker}"
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code == 429:
                return (
                    f"{prefix}: rate limited by Financial Modeling Prep (HTTP 429). "
                    "Check your API quota or retry later."
                )
            return f"{prefix} ({exc.code}): {exc.reason}"

        reason = getattr(exc, "reason", exc)
        return f"{prefix}: {reason}"

    def _build_roi_candidates(
        self,
        *,
        chain: list[dict[str, Any]],
        delta_min: Decimal,
        delta_max: Decimal,
        roi_threshold: Decimal,
        option_type: str = "put",
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        for row in chain:
            if str(row.get("option_type", "")).lower() != option_type:
                continue

            delta_value = self._to_decimal(row.get("delta"))
            if delta_value is None or not (delta_min <= delta_value <= delta_max):
                continue

            strike_price = self._to_decimal(row.get("strike_price"))
            if strike_price is None:
                continue

            roi = self._calculate_roi_value(
                bid_price=row.get("bid"),
                ask_price=row.get("ask"),
                strike_price=strike_price,
            )
            if roi is None or roi < roi_threshold:
                continue

            option = dict(row)
            option["delta"] = delta_value
            option["strike_price"] = strike_price
            option["mid"] = self._calculate_mid_price(
                bid_price=row.get("bid"),
                ask_price=row.get("ask"),
            )
            option["roi"] = roi
            candidates.append(option)

        return candidates

    def _collect_call_contracts(
        self,
        *,
        chain: list[dict[str, Any]],
        delta_min: Decimal = Decimal("0.10"),
        delta_max: Decimal = Decimal("0.85"),
    ) -> list[dict[str, Any]]:
        contracts: list[dict[str, Any]] = []

        for row in chain:
            if str(row.get("option_type", "")).lower() != "call":
                continue

            delta_value = self._to_decimal(row.get("delta"))
            if delta_value is None or not (delta_min <= delta_value <= delta_max):
                continue

            strike_price = self._to_decimal(row.get("strike_price"))
            if strike_price is None:
                continue

            bid_value = self._to_decimal(row.get("bid"))
            if bid_value is None or bid_value <= 0:
                continue

            volume = self._to_int(row.get("volume"))
            open_interest = self._to_int(row.get("open_interest"))
            if volume is None and open_interest is None:
                continue

            spread_pct = self._calculate_spread_percentage(
                bid_price=row.get("bid"),
                ask_price=row.get("ask"),
            )
            if spread_pct is None or spread_pct > MAX_CALL_SPREAD_PCT:
                continue

            option = dict(row)
            option["delta"] = delta_value
            option["strike_price"] = strike_price
            option["volume"] = volume
            option["open_interest"] = open_interest
            option["mid"] = self._calculate_mid_price(
                bid_price=row.get("bid"),
                ask_price=row.get("ask"),
            )
            contracts.append(option)

        contracts.sort(key=lambda item: item["strike_price"])
        return contracts

    def _select_roi_candidate(
        self, roi_candidates: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        best_option: dict[str, Any] | None = None
        best_roi: Decimal | None = None
        best_delta: Decimal | None = None

        for option in roi_candidates:
            roi_value = self._to_decimal(option.get("roi"))
            delta_value = self._to_decimal(option.get("delta"))
            if roi_value is None or delta_value is None:
                continue

            if best_roi is None or roi_value > best_roi:
                best_option = option
                best_roi = roi_value
                best_delta = delta_value
                continue

            if roi_value == best_roi and best_delta is not None:
                if abs(delta_value) < abs(best_delta):
                    best_option = option
                    best_delta = delta_value

        return best_option

    @staticmethod
    def _calculate_roi_value(
        *, bid_price: Any, ask_price: Any, strike_price: Decimal
    ) -> Decimal | None:
        bid_decimal = Command._to_decimal(bid_price)
        ask_decimal = Command._to_decimal(ask_price)
        if bid_decimal is None or ask_decimal is None or strike_price == 0:
            return None

        try:
            mid_price = (bid_decimal + ask_decimal) / Decimal("2")
            percentage = (mid_price / strike_price) * Decimal("100")
        except (InvalidOperation, DivisionByZero):
            return None

        return percentage.quantize(Decimal("0.01"))

    @staticmethod
    def _calculate_mid_price(*, bid_price: Any, ask_price: Any) -> Decimal | None:
        bid_decimal = Command._to_decimal(bid_price)
        ask_decimal = Command._to_decimal(ask_price)
        if bid_decimal is None or ask_decimal is None:
            return None

        try:
            return ((bid_decimal + ask_decimal) / Decimal("2")).quantize(
                Decimal("0.01")
            )
        except (InvalidOperation, DivisionByZero):
            return None

    @staticmethod
    def _calculate_spread_percentage(
        *, bid_price: Any, ask_price: Any
    ) -> Decimal | None:
        bid_decimal = Command._to_decimal(bid_price)
        ask_decimal = Command._to_decimal(ask_price)
        if bid_decimal is None or ask_decimal is None or ask_decimal <= 0:
            return None

        try:
            spread_pct = ((ask_decimal - bid_decimal) / ask_decimal) * Decimal("100")
        except (InvalidOperation, DivisionByZero):
            return None

        if spread_pct < 0:
            return None
        return spread_pct.quantize(Decimal("0.01"))

    @staticmethod
    def _to_decimal(value: Any) -> Decimal | None:
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            return None

    def _build_option_snapshot(
        self,
        option: dict[str, Any],
        roi_candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        serialized = self._serialize_option_data(option)
        primary_delta = self._to_decimal(option.get("delta"))
        if primary_delta is None:
            return serialized

        alternatives: list[dict[str, Any]] = []
        for candidate in roi_candidates:
            if candidate is option:
                continue

            candidate_delta = self._to_decimal(candidate.get("delta"))
            if candidate_delta is None or abs(candidate_delta) >= abs(primary_delta):
                continue

            alternatives.append(candidate)

        alternatives.sort(
            key=lambda item: abs(self._to_decimal(item.get("delta")) or Decimal("999")),
            reverse=True,
        )
        serialized["alternatives"] = [
            self._serialize_option_data(candidate) for candidate in alternatives
        ]
        return serialized

    def _serialize_option_data(self, option: dict[str, Any]) -> dict[str, Any]:
        serialized: dict[str, Any] = {}

        for key, value in option.items():
            if isinstance(value, Decimal):
                serialized[key] = float(value)
            else:
                serialized[key] = value

        if "strike_price" in serialized:
            serialized["strike"] = serialized["strike_price"]

        return serialized

    def _update_symbol_snapshot(
        self,
        symbol: Symbol,
        *,
        price: Decimal | None,
        rsi: Decimal | None,
        option_exp: date | None,
        option_volume: int | None,
        option_iv: Decimal | None,
        option_data: dict[str, Any] | None,
        call_data: dict[str, Any] | None,
        roi: Decimal | None,
    ) -> bool:
        changed_fields: list[str] = []

        if price != symbol.price:
            symbol.price = price
            changed_fields.append("price")
        if rsi != symbol.rsi:
            symbol.rsi = rsi
            changed_fields.append("rsi")
        if option_exp != symbol.option_exp:
            symbol.option_exp = option_exp
            changed_fields.append("option_exp")
        if option_volume != symbol.option_volume:
            symbol.option_volume = option_volume
            changed_fields.append("option_volume")
        if option_iv != symbol.option_iv:
            symbol.option_iv = option_iv
            changed_fields.append("option_iv")
        if option_data != symbol.option_data:
            symbol.option_data = option_data
            changed_fields.append("option_data")
        if call_data != symbol.call_data:
            symbol.call_data = call_data
            changed_fields.append("call_data")
        if roi != symbol.roi:
            symbol.roi = roi
            changed_fields.append("roi")

        if not changed_fields:
            return False

        symbol.save(update_fields=[*changed_fields, "updated_at"])
        return True
