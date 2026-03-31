from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from api.daily_brief_services import refresh_daily_brief


class Command(BaseCommand):
    help = "Populate the DailyBrief table for a given day."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--date",
            dest="edition_date",
            help="Edition date in YYYY-MM-DD format. Defaults to today.",
        )
        parser.add_argument(
            "--limit",
            dest="limit",
            type=int,
            default=3,
            help="Maximum number of daily brief rows to persist. Defaults to 3.",
        )

    def handle(self, *args, **options) -> None:
        edition_date = self._parse_date(options.get("edition_date"))
        limit = options["limit"]

        if limit <= 0:
            raise CommandError("--limit must be a positive integer.")

        daily_briefs = refresh_daily_brief(target_date=edition_date, limit=limit)
        target_date = edition_date or (daily_briefs[0].edition_date if daily_briefs else date.today())

        self.stdout.write(
            self.style.SUCCESS(
                f"Populated {len(daily_briefs)} daily brief rows for {target_date.isoformat()}."
            )
        )
        for daily_brief in daily_briefs:
            self.stdout.write(
                f"#{daily_brief.rank} {daily_brief.ticker} "
                f"ROI {daily_brief.roi} delta {daily_brief.delta}"
            )

    def _parse_date(self, raw_value: str | None) -> date | None:
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError as exc:
            raise CommandError("--date must be in YYYY-MM-DD format.") from exc
