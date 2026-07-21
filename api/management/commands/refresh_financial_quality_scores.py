"""Refresh persisted fundamental-quality scores from FMP annual statements."""

from __future__ import annotations

import time
from typing import Any

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from api.helper import FinancialMetricsCalculator
from api.models import Symbol


FMP_BASE_URL = "https://financialmodelingprep.com/stable"
STATEMENT_ENDPOINTS = {
    "balance_sheet": "balance-sheet-statement",
    "income_statement": "income-statement",
    "cash_flow": "cash-flow-statement",
}


class Command(BaseCommand):
    help = (
        "Fetch fresh annual FMP statements, calculate fundamental quality, and "
        "update Symbol.score and Symbol.classification."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--symbols",
            help="Comma-separated tickers to refresh. Defaults to every Symbol.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Process at most this many selected symbols.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Fetch and calculate scores without writing database changes.",
        )
        parser.add_argument(
            "--delay",
            type=float,
            default=0.25,
            help="Seconds to wait between FMP requests (default: 0.25).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        api_key = (getattr(settings, "FINANCIAL_MODELING_API_KEY", "") or "").strip()
        if not api_key:
            raise CommandError("FINANCIAL_MODELING_API_KEY is not configured.")

        delay = options["delay"]
        if delay < 0:
            raise CommandError("--delay cannot be negative.")

        symbols = self._select_symbols(options.get("symbols"), options.get("limit"))
        if not symbols:
            self.stdout.write("No symbols selected.")
            return

        dry_run = bool(options["dry_run"])
        updated = unchanged = failed = 0
        action = "Would update" if dry_run else "Refreshing"
        self.stdout.write(f"{action} fundamental quality for {len(symbols)} symbol(s).")

        session = requests.Session()
        for index, symbol in enumerate(symbols):
            try:
                financial_data = self._fetch_financial_data(
                    session=session,
                    ticker=symbol.ticker,
                    api_key=api_key,
                    delay=delay,
                )
                report = FinancialMetricsCalculator(financial_data).process()
                score = report["long_term_quality_score"]
                classification = report["classification"]
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                failed += 1
                self.stderr.write(f"  {symbol.ticker}: skipped ({exc})")
                continue

            changed_fields: list[str] = []
            if symbol.score != score:
                symbol.score = score
                changed_fields.append("score")
            if symbol.classification != classification:
                symbol.classification = classification
                changed_fields.append("classification")

            if changed_fields:
                if not dry_run:
                    symbol.save(update_fields=changed_fields + ["updated_at"])
                updated += 1
                self.stdout.write(
                    f"  {symbol.ticker}: score={score}, classification={classification}"
                )
            else:
                unchanged += 1

            if delay and index < len(symbols) - 1:
                time.sleep(delay)

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {'Would update' if dry_run else 'Updated'}: {updated}; "
                f"unchanged: {unchanged}; skipped: {failed}."
            )
        )

    def _select_symbols(self, raw_symbols: str | None, limit: int | None) -> list[Symbol]:
        if limit is not None and limit < 1:
            raise CommandError("--limit must be at least 1.")

        queryset = Symbol.objects.all().order_by("ticker")
        if raw_symbols:
            tickers = [ticker.strip().upper() for ticker in raw_symbols.split(",") if ticker.strip()]
            if not tickers:
                raise CommandError("--symbols must include at least one ticker.")
            queryset = queryset.filter(ticker__in=tickers)

        if limit is not None:
            queryset = queryset[:limit]
        return list(queryset)

    def _fetch_financial_data(
        self,
        *,
        session: requests.Session,
        ticker: str,
        api_key: str,
        delay: float,
    ) -> dict[str, list[dict[str, Any]]]:
        statements: dict[str, list[dict[str, Any]]] = {}
        for position, (key, endpoint) in enumerate(STATEMENT_ENDPOINTS.items()):
            response = session.get(
                f"{FMP_BASE_URL}/{endpoint}",
                params={"symbol": ticker, "period": "annual", "limit": 5, "apikey": api_key},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                raise ValueError(f"FMP returned no {key} data")
            if not all(isinstance(item, dict) for item in payload):
                raise ValueError(f"FMP returned invalid {key} data")
            statements[key] = payload
            if delay and position < len(STATEMENT_ENDPOINTS) - 1:
                time.sleep(delay)
        return statements
