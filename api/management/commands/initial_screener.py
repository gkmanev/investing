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


#TEST


MAX_WORKERS = 8  # concurrent tickers processed at once

SCORE_MIN = 75  # minimum score to qualify for earnings screening
TV_SCANNER_URL = "https://scanner.tradingview.com/america/scan"
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}
TV_SCAN_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}
DEFAULT_TV_EXCHANGE = "NASDAQ"
TV_EXCHANGE_ALIASES = {
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
logger = logging.getLogger(__name__)
TECHNICAL_SCORE_MISSING = object()


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


def _normalize_tv_exchange(exchange: str | None) -> str:
    normalized = (exchange or "").strip().upper()
    if not normalized:
        return DEFAULT_TV_EXCHANGE
    return TV_EXCHANGE_ALIASES.get(normalized, normalized)


def _build_tv_symbol(ticker: str, exchange: str | None = None) -> str:
    return f"{_normalize_tv_exchange(exchange)}:{ticker.upper()}"


def _coerce_date_like(value: Any) -> Optional[date]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric /= 1000
            return datetime.fromtimestamp(numeric).date()
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue

    return None


def _fetch_next_earnings_date(
    ticker: str, exchange: str = ""
) -> tuple[Optional[date], dict[str, Any]]:
    today = date.today()
    requested_symbol = _build_tv_symbol(ticker, exchange)
    payload = {
        "symbols": {
            "tickers": [requested_symbol],
            "query": {"types": []},
        },
        "columns": ["earnings_release_next_date"],
    }

    try:
        response = requests.post(
            TV_SCANNER_URL,
            json=payload,
            headers=TV_SCAN_HEADERS,
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("TradingView earnings lookup failed for %s: %s", requested_symbol, exc)
        return None, {
            "source": "tradingview",
            "status": f"request_exception:{exc.__class__.__name__}",
            "requested_symbol": requested_symbol,
            "returned_symbol": None,
            "candidate_dates": [],
        }

    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            "TradingView earnings lookup returned invalid JSON for %s",
            requested_symbol,
        )
        return None, {
            "source": "tradingview",
            "status": "invalid_json",
            "requested_symbol": requested_symbol,
            "returned_symbol": None,
            "candidate_dates": [],
        }

    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        return None, {
            "source": "tradingview",
            "status": "empty_result",
            "requested_symbol": requested_symbol,
            "returned_symbol": None,
            "candidate_dates": [],
        }

    returned_symbol = None
    candidate_dates: list[date] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if returned_symbol is None and isinstance(row.get("s"), str):
            returned_symbol = row["s"]
        values = row.get("d")
        if not isinstance(values, list) or not values:
            continue
        parsed = _coerce_date_like(values[0])
        if parsed is not None:
            candidate_dates.append(parsed)

    status = "ok" if candidate_dates else "no_earnings_field"

    upcoming = sorted({v for v in candidate_dates if v >= today})
    if not upcoming and candidate_dates:
        logger.info(
            "TradingView earnings dates for %s were present but not upcoming: %s (today=%s)",
            requested_symbol,
            ", ".join(sorted({v.isoformat() for v in candidate_dates})),
            today.isoformat(),
        )

    if not upcoming:
        logger.info(
            "No upcoming TradingView earnings date for %s (status=%s)",
            requested_symbol,
            status,
        )

    return upcoming[0] if upcoming else None, {
        "source": "tradingview",
        "status": status,
        "requested_symbol": requested_symbol,
        "returned_symbol": returned_symbol,
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


def map_tv_score(score: Any) -> str | None:
    numeric_score = _coerce_decimal(score)
    if numeric_score is None:
        return None
    if numeric_score >= Decimal("0.5"):
        return Symbol.TechnicalScore.STRONG_BUY
    if numeric_score >= Decimal("0.1"):
        return Symbol.TechnicalScore.BUY
    if numeric_score > Decimal("-0.1"):
        return Symbol.TechnicalScore.NEUTRAL
    if numeric_score > Decimal("-0.5"):
        return Symbol.TechnicalScore.SELL
    return Symbol.TechnicalScore.STRONG_SELL


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def fetch_technical_scores(tickers: list[str]) -> dict[str, Decimal]:
    if not tickers:
        return {}

    payload = {
        "filter": [
            {
                "left": "name",
                "operation": "in_range",
                "right": tickers,
            }
        ],
        "columns": ["name", "Recommend.All|1M"],
        "range": [0, len(tickers) * 4],
    }

    response = requests.post(
        TV_SCANNER_URL,
        headers={"Content-Type": "text/plain", **REQUEST_HEADERS},
        json=payload,
        timeout=20,
    )
    response.raise_for_status()

    data = response.json()
    scores: dict[str, Decimal] = {}

    for item in data.get("data", []):
        values = item.get("d", [])
        if len(values) < 2:
            continue

        name, score = values[0], values[1]
        parsed_score = _coerce_decimal(score)
        if name and parsed_score is not None and name not in scores:
            scores[str(name).upper()] = parsed_score

    return scores


def fetch_tv_technicals(tickers: list[str]) -> dict[str, str | None]:
    raw_scores = fetch_technical_scores(tickers)
    return {
        ticker: map_tv_score(score)
        for ticker, score in raw_scores.items()
    }


def _fetch_all_tv_technicals(tickers: list[str], *, batch_size: int = 200) -> dict[str, str | None]:
    technicals: dict[str, str | None] = {}
    for start in range(0, len(tickers), batch_size):
        batch = tickers[start : start + batch_size]
        if not batch:
            continue
        try:
            technicals.update(fetch_tv_technicals(batch))
        except requests.RequestException as exc:
            logger.warning(
                "TradingView technicals lookup failed for batch starting at %s: %s",
                batch[0],
                exc,
            )
    return technicals


def _process_symbol(
    symbol: Symbol,
    fmp_api_key: str,
    force: bool,
    technical_rating: object,
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
    if technical_rating is not TECHNICAL_SCORE_MISSING and technical_rating != symbol.technical_score:
        symbol.technical_score = technical_rating
        market_data_fields.append("technical_score")
    if market_data_fields:
        symbol.save(update_fields=market_data_fields + ["updated_at"])

    next_date, earnings_debug = _fetch_next_earnings_date(symbol.ticker, symbol.exchange)
    earnings_updated = False
    if next_date is not None and next_date != symbol.next_earnings_date:
        Symbol.objects.filter(ticker=symbol.ticker).update(next_earnings_date=next_date)
        earnings_updated = True

    return {
        "ticker": symbol.ticker,
        "market_data_updated": bool(market_data_fields),
        "technical_rating": (
            technical_rating
            if technical_rating is not TECHNICAL_SCORE_MISSING
            else None
        ),
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
        technicals_by_ticker = _fetch_all_tv_technicals([symbol.ticker for symbol in symbols])
        if not fmp_api_key:
            self.stderr.write(
                "FINANCIAL_MODELING_API_KEY is not configured; DCF skipped."
            )
        else:
            self.stdout.write(
                "FINANCIAL_MODELING_API_KEY detected "
                f"(len={len(fmp_api_key)}, masked={_mask_secret(fmp_api_key)})."
            )

        self.stdout.write(
            "Screening "
            f"{total} symbols (score >= {SCORE_MIN}) for earnings date, DCF, price/RSI, and TradingView technicals..."
        )

        earnings_updated = 0
        market_data_updated = 0
        print_lock = threading.Lock()

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {
                executor.submit(
                    _process_symbol,
                    sym,
                    fmp_api_key,
                    options["force"],
                    technicals_by_ticker.get(sym.ticker, TECHNICAL_SCORE_MISSING),
                ): sym
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
                        f"status={debug['status']}, "
                        f"source={debug['source']}, "
                        f"requested={debug['requested_symbol']}"
                    )
                    returned_symbol = debug["returned_symbol"]
                    if returned_symbol:
                        detail += f", returned={returned_symbol}"
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
                elif result["technical_rating"] is not None:
                    with print_lock:
                        self.stdout.write(
                            f"  {ticker}: technical_score={result['technical_rating']}"
                        )

        self.stdout.write(
            self.style.SUCCESS(
                "\nDone. Updated "
                f"{earnings_updated}/{total} symbols with a next earnings date; "
                "updated market data (price/DCF/RSI/technicals) "
                f"for {market_data_updated} symbols."
            )
        )
