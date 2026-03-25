from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from yfinance.screener.query import EquityQuery
from yfinance.screener.screener import screen

from api.models import Symbol

# NYSE=NYQ, NASDAQ Global Select=NMS, NASDAQ Global Market=NGM
MARKET_CAP_THRESHOLD = 5_000_000_000

SCREENER_QUERY = EquityQuery('and', [
    EquityQuery('is-in', ['exchange', 'NYQ', 'NMS', 'NGM']),
    EquityQuery('gt', ['intradaymarketcap', MARKET_CAP_THRESHOLD]),
])


class Command(BaseCommand):
    """Fetch NYSE/NASDAQ symbols with market cap >= 5B via yfinance and populate Symbol table."""

    help = "Fetch NYSE/NASDAQ symbols (market cap >= 5B) from Yahoo Finance and populate Symbol table"

    def add_arguments(self, parser) -> None:  # pragma: no cover - argument wiring
        parser.add_argument(
            "--page-size",
            type=int,
            default=250,
            dest="page_size",
            help="How many results to fetch per page (default: 250)",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        page_size: int = options["page_size"]
        all_quotes: list[dict] = []
        offset = 0

        while True:
            try:
                result = screen(
                    SCREENER_QUERY,
                    sortField="intradaymarketcap",
                    sortAsc=False,
                    offset=offset,
                    size=page_size,
                )
            except Exception as exc:
                raise CommandError(f"Yahoo Finance screener request failed: {exc}") from exc

            quotes: list[dict] = result.get("quotes") or []
            if not quotes:
                break

            all_quotes.extend(quotes)
            self.stdout.write(f"Fetched {len(all_quotes)} symbols so far...")

            if len(quotes) < page_size:
                break

            offset += page_size

        if not all_quotes:
            raise CommandError("Yahoo Finance screener returned no results.")

        synced, skipped = self._sync_symbols(all_quotes)
        self.stdout.write(self.style.SUCCESS(f"Done. Synced {synced} symbols ({skipped} skipped below 5B or missing cap."))

    def _sync_symbols(self, quotes: list[dict]) -> tuple[int, int]:
        synced = 0
        skipped = 0
        for quote in quotes:
            ticker = quote.get("symbol", "").strip()
            if not ticker:
                continue

            exchange = quote.get("exchange") or quote.get("fullExchangeName") or ""
            raw_cap = quote.get("marketCap")
            market_cap = Decimal(str(raw_cap)) if raw_cap is not None else None

            if market_cap is None or market_cap < MARKET_CAP_THRESHOLD:
                skipped += 1
                continue

            Symbol.objects.update_or_create(
                ticker=ticker,
                defaults={
                    "exchange": exchange,
                    "market_cap": market_cap,
                },
            )
            synced += 1

        return synced, skipped
