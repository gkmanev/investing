from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from api.models import Symbol

EXPORT_FIELDS = (
    "ticker",
    "exchange",
    "market_cap",
    "initial_suitability",
    "score",
    "classification",
    "liquidity",
    "price",
    "dcf",
    "rsi",
    "technical_score",
    "option_exp",
    "option_volume",
    "option_iv",
    "next_earnings_date",
    "roi",
    "seeking_alpha_ticker_id",
    "created_at",
    "updated_at",
)


class Command(BaseCommand):
    help = "Export Symbol rows that already have a score to CSV."

    def add_arguments(self, parser) -> None:  # pragma: no cover - argparse wiring
        parser.add_argument(
            "--output",
            default="output/scored_symbols.csv",
            help="Destination CSV path (default: output/scored_symbols.csv).",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        output_path = Path(options["output"]).expanduser()
        if output_path.exists() and output_path.is_dir():
            raise CommandError(f"Output path points to a directory: {output_path}")

        output_path.parent.mkdir(parents=True, exist_ok=True)

        rows = Symbol.objects.filter(score__isnull=False).order_by("ticker").values(*EXPORT_FIELDS)
        exported_count = rows.count()

        with output_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            writer.writerows(rows.iterator())

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {exported_count} scored symbols to {output_path}"
            )
        )
        return str(output_path)
