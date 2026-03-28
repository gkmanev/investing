from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from api.daily_brief_services import (
    get_active_daily_brief_recipient_list,
    get_or_create_daily_brief_edition,
    send_daily_brief_to_active_subscribers,
)


class Command(BaseCommand):
    help = "Send or preview the Daily Top 3 email edition."

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
            help="Optional cap on active recipients, useful for controlled testing.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview the edition and recipients without sending email.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Send even if this edition date was already sent.",
        )

    def handle(self, *args, **options) -> None:
        edition_date = self._parse_date(options.get("edition_date"))
        limit = options.get("limit")
        dry_run = options["dry_run"]
        force = options["force"]

        if limit is not None and limit <= 0:
            raise CommandError("--limit must be a positive integer.")

        if dry_run:
            edition = get_or_create_daily_brief_edition(target_date=edition_date)
            recipients = get_active_daily_brief_recipient_list(limit=limit)
            self.stdout.write(self.style.SUCCESS("Daily brief dry run"))
            self.stdout.write(f"Edition date: {edition.edition_date.isoformat()}")
            self.stdout.write(f"Subject: {edition.subject}")
            self.stdout.write(f"Recipient count: {len(recipients)}")
            for recipient in recipients:
                self.stdout.write(f" - {recipient}")
            return

        result = send_daily_brief_to_active_subscribers(
            target_date=edition_date,
            limit=limit,
            force=force,
        )
        if result["already_sent"] and not force:
            self.stdout.write(
                self.style.WARNING(
                    f"Edition {result['edition_date']} was already sent to "
                    f"{result['recipient_count']} recipients."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Sent edition {result['edition_date']} to {result['recipient_count']} recipients."
            )
        )

    def _parse_date(self, raw_value: str | None) -> date | None:
        if not raw_value:
            return None
        try:
            return date.fromisoformat(raw_value)
        except ValueError as exc:
            raise CommandError("--date must be in YYYY-MM-DD format.") from exc
