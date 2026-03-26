from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from api.models import Symbol


class Command(BaseCommand):
    help = "Update Symbol scores from a scored-symbols CSV export."

    def add_arguments(self, parser) -> None:  # pragma: no cover - argparse wiring
        parser.add_argument(
            "--input",
            default="output/scored_symbols.csv",
            help="Source CSV path (default: output/scored_symbols.csv).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read and validate the CSV without writing any database changes.",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        input_path = Path(options["input"]).expanduser()
        dry_run = bool(options.get("dry_run"))

        if not input_path.exists():
            raise CommandError(f"CSV file not found: {input_path}")
        if input_path.is_dir():
            raise CommandError(f"Input path points to a directory: {input_path}")

        rows = self._read_rows(input_path)
        tickers = [row["ticker"] for row in rows]
        symbols_by_ticker = Symbol.objects.in_bulk(tickers, field_name="ticker")

        to_update: list[Symbol] = []
        unchanged = 0
        missing = 0

        for row in rows:
            symbol = symbols_by_ticker.get(row["ticker"])
            if symbol is None:
                missing += 1
                continue

            if symbol.score == row["score"]:
                unchanged += 1
                continue

            symbol.score = row["score"]
            to_update.append(symbol)

        if to_update and not dry_run:
            Symbol.objects.bulk_update(to_update, ["score"])

        action = "Would update" if dry_run else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {len(to_update)} symbols from {input_path}. "
                f"Unchanged: {unchanged}. Missing: {missing}."
            )
        )
        return str(input_path)

    def _read_rows(self, input_path: Path) -> list[dict[str, Any]]:
        parsed_rows: list[dict[str, Any]] = []

        with input_path.open("r", newline="", encoding="utf-8") as csv_file:
            reader = csv.DictReader(csv_file)
            fieldnames = set(reader.fieldnames or [])
            required_fields = {"ticker", "score"}
            missing_fields = required_fields - fieldnames
            if missing_fields:
                missing_display = ", ".join(sorted(missing_fields))
                raise CommandError(f"CSV is missing required columns: {missing_display}")

            for index, row in enumerate(reader, start=2):
                ticker = (row.get("ticker") or "").strip()
                raw_score = (row.get("score") or "").strip()

                if not ticker:
                    raise CommandError(f"Row {index}: ticker is blank.")
                if not raw_score:
                    raise CommandError(f"Row {index}: score is blank for {ticker}.")

                try:
                    score = int(raw_score)
                except ValueError as exc:
                    raise CommandError(
                        f"Row {index}: invalid score '{raw_score}' for {ticker}."
                    ) from exc

                parsed_rows.append({"ticker": ticker, "score": score})

        return parsed_rows
