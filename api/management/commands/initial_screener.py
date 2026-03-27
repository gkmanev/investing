from __future__ import annotations

import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any, Optional

import numpy as np
import pandas as pd
import requests
import talib
import yfinance as yf
from django.conf import settings
from django.core.management.base import BaseCommand

from api.models import Symbol

MAX_WORKERS = 8  # concurrent tickers processed at once

SCORE_MIN = 75  # minimum score to qualify for earnings screening
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
FMP_EARNINGS_URL = "https://financialmodelingprep.com/stable/earnings"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
logger = logging.getLogger(__name__)


def _configure_yfinance_cache() -> None:
    """Best-effort cache setup to avoid noisy tz-cache errors on ephemeral hosts."""
    if not hasattr(yf, "set_tz_cache_location"):
        return

    cache_dir = os.path.join(tempfile.gettempdir(), "py-yfinance")
    try:
        os.makedirs(cache_dir, exist_ok=True)
        yf.set_tz_cache_location(cache_dir)
    except Exception as exc:
        logger.warning("Failed to configure yfinance tz cache at %s: %s", cache_dir, exc)


def _mask_secret(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _coerce_unix_date(value: Any) -> Optional[date]:
    if value is None:
        return None

    try:
        return datetime.fromtimestamp(int(value)).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _fetch_quote_earnings_dates(ticker: str) -> tuple[list[date], str]:
    try:
        response = requests.get(
            YAHOO_QUOTE_URL,
            params={"symbols": ticker},
            headers=REQUEST_HEADERS,
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.warning("Yahoo earnings lookup failed for %s: %s", ticker, exc)
        return [], f"request_exception:{exc.__class__.__name__}"

    if response.status_code != 200:
        logger.warning(
            "Yahoo earnings lookup returned %s for %s", response.status_code, ticker
        )
        return [], f"http_{response.status_code}"

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Yahoo earnings lookup returned invalid JSON for %s", ticker)
        return [], "invalid_json"

    quote_response = payload.get("quoteResponse")
    if not isinstance(quote_response, dict):
        return [], "missing_quote_response"

    results = quote_response.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return [], "empty_result"

    quote = results[0]
    candidate_dates: list[date] = []
    for key in ("earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd"):
        parsed = _coerce_unix_date(quote.get(key))
        if parsed is not None:
            candidate_dates.append(parsed)

    if not candidate_dates:
        return [], "no_earnings_fields"

    return candidate_dates, "ok"


def _fetch_fmp_earnings_dates(ticker: str, api_key: str) -> tuple[list[date], str]:
    if not api_key:
        return [], "missing_api_key"

    try:
        response = requests.get(
            FMP_EARNINGS_URL,
            params={"symbol": ticker, "apikey": api_key},
            timeout=30,
        )
    except requests.RequestException as exc:
        logger.warning("FMP earnings lookup failed for %s: %s", ticker, exc)
        return [], f"request_exception:{exc.__class__.__name__}"

    if response.status_code != 200:
        logger.warning(
            "FMP earnings lookup returned %s for %s", response.status_code, ticker
        )
        return [], f"http_{response.status_code}"

    try:
        payload = response.json()
    except ValueError:
        logger.warning("FMP earnings lookup returned invalid JSON for %s", ticker)
        return [], "invalid_json"

    if isinstance(payload, dict):
        rows = payload.get("data")
        if isinstance(rows, dict):
            rows = [rows]
        elif not isinstance(rows, list):
            rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    candidate_dates: list[date] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _coerce_date_like(row.get("date"))
        if parsed is not None:
            candidate_dates.append(parsed)

    if not candidate_dates:
        return [], "no_date_rows"

    return candidate_dates, "ok"


def _coerce_date_like(value: Any) -> Optional[date]:
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue

    return None


def _fetch_next_earnings_date(
    ticker: str, fmp_api_key: str = ""
) -> tuple[Optional[date], dict[str, Any]]:
    today = date.today()
    quote_dates, quote_status = _fetch_quote_earnings_dates(ticker)
    candidate_dates = quote_dates
    source = "yahoo" if candidate_dates else "none"
    fmp_status = "not_needed"

    if not candidate_dates:
        candidate_dates, fmp_status = _fetch_fmp_earnings_dates(ticker, fmp_api_key)
        if candidate_dates:
            source = "fmp"

    upcoming = sorted({v for v in candidate_dates if v >= today})
    if not upcoming and candidate_dates:
        logger.info(
            "Earnings dates for %s were present but not upcoming: %s (today=%s)",
            ticker,
            ", ".join(sorted({v.isoformat() for v in candidate_dates})),
            today.isoformat(),
        )

    if not upcoming:
        logger.info(
            "No upcoming earnings date for %s (yahoo=%s, fmp=%s)",
            ticker,
            quote_status,
            fmp_status,
        )

    return upcoming[0] if upcoming else None, {
        "source": source,
        "quote_status": quote_status,
        "fmp_status": fmp_status,
        "candidate_dates": sorted({v.isoformat() for v in candidate_dates}),
    }


def _fetch_price_and_rsi(ticker: str) -> tuple[Decimal | None, Decimal | None]:
    try:
        df = yf.download(
            ticker,
            period="3mo",
            progress=False,
            actions=False,
            auto_adjust=False,
            group_by="column",
        )
    except Exception:
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


def _fetch_dcf(ticker: str, api_key: str) -> Decimal | None:
    if not api_key:
        return None

    url = "https://financialmodelingprep.com/stable/levered-discounted-cash-flow"
    params = {
        "symbol": ticker,
        "apikey": api_key,
    }
    try:
        response = requests.get(url, params=params, timeout=30)
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    try:
        payload = response.json()
    except ValueError:
        return None

    entry: dict[str, Any] | None = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        entry = payload[0]
    elif isinstance(payload, dict):
        if "dcf" in payload:
            entry = payload
        else:
            data = payload.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict):
                entry = data[0]

    if not entry:
        return None

    for key in ("dcf", "leveredDCF", "leveredDiscountedCashFlow"):
        value = entry.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            continue

    return None


def _process_symbol(
    symbol: Symbol,
    fmp_api_key: str,
    force: bool,
) -> dict[str, Any]:
    """Fetch all data for a single symbol and persist updates. Returns a result summary."""
    price, rsi = _fetch_price_and_rsi(symbol.ticker)
    dcf = _fetch_dcf(symbol.ticker, fmp_api_key) if fmp_api_key else None

    market_data_fields: list[str] = []
    if price is not None and price != symbol.price:
        symbol.price = price
        market_data_fields.append("price")
    if dcf is not None and dcf != symbol.dcf:
        symbol.dcf = dcf
        market_data_fields.append("dcf")
    if rsi is not None and rsi != symbol.rsi:
        symbol.rsi = rsi
        market_data_fields.append("rsi")
    if market_data_fields:
        symbol.save(update_fields=market_data_fields + ["updated_at"])

    next_date, earnings_debug = _fetch_next_earnings_date(symbol.ticker, fmp_api_key)
    earnings_updated = False
    if next_date is not None and next_date != symbol.next_earnings_date:
        Symbol.objects.filter(ticker=symbol.ticker).update(next_earnings_date=next_date)
        earnings_updated = True

    return {
        "ticker": symbol.ticker,
        "market_data_updated": bool(market_data_fields),
        "next_date": next_date,
        "earnings_updated": earnings_updated,
        "earnings_debug": earnings_debug,
    }


class Command(BaseCommand):
    help = (
        "Fetch next earnings date, DCF, and price/RSI for Symbol objects "
        f"with score >= {SCORE_MIN}."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            metavar="N",
            help="Process only the first N qualifying tickers.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Retained for compatibility; earnings dates are refreshed on every run.",
        )

    def handle(self, *args, **options) -> None:
        _configure_yfinance_cache()
        symbols = list(Symbol.objects.filter(score__gte=SCORE_MIN).order_by("ticker"))
        if not symbols:
            self.stdout.write("No symbols matched the initial screener criteria.")
            return

        limit = options["limit"]
        if limit is not None:
            symbols = symbols[:limit]
            self.stdout.write(f"--limit: processing first {len(symbols)} tickers.")

        total = len(symbols)
        fmp_api_key = (getattr(settings, "FINANCIAL_MODELING_API_KEY", "") or "").strip()
        if not fmp_api_key:
            self.stderr.write(
                "FINANCIAL_MODELING_API_KEY is not configured; DCF skipped and FMP earnings fallback disabled."
            )
        else:
            self.stdout.write(
                "FINANCIAL_MODELING_API_KEY detected "
                f"(len={len(fmp_api_key)}, masked={_mask_secret(fmp_api_key)})."
            )

        self.stdout.write(
            "Screening "
            f"{total} symbols (score >= {SCORE_MIN}) for earnings date, DCF, and price/RSI..."
        )

        earnings_updated = 0
        market_data_updated = 0
        print_lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(_process_symbol, sym, fmp_api_key, options["force"]): sym
                for sym in symbols
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as exc:
                    sym = futures[future]
                    with print_lock:
                        self.stderr.write(f"  {sym.ticker}: unexpected error - {exc}")
                    continue

                ticker = result["ticker"]
                if result["market_data_updated"]:
                    market_data_updated += 1
                if result["next_date"] is None:
                    debug = result["earnings_debug"]
                    candidate_dates = debug["candidate_dates"]
                    detail = (
                        f"yahoo={debug['quote_status']}, "
                        f"fmp={debug['fmp_status']}, "
                        f"source={debug['source']}"
                    )
                    if candidate_dates:
                        detail += f", candidates={','.join(candidate_dates)}"
                    with print_lock:
                        self.stderr.write(
                            f"  {ticker}: No upcoming earnings date found ({detail})."
                        )
                elif result["earnings_updated"]:
                    earnings_updated += 1
                    debug = result["earnings_debug"]
                    with print_lock:
                        self.stdout.write(
                            f"  {ticker}: next_earnings_date={result['next_date']} "
                            f"(source={debug['source']})"
                        )

        self.stdout.write(
            self.style.SUCCESS(
                "\nDone. Updated "
                f"{earnings_updated}/{total} symbols with a next earnings date; "
                "updated market data (price/DCF/RSI) "
                f"for {market_data_updated} symbols."
            )
        )
