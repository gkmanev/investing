from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django_celery_beat.models import CrontabSchedule, PeriodicTask


TRADING_VIEW_TASK_NAME_PREFIX = "trading-view-scrape-"
TRADING_VIEW_TASK_PATH = "api.tasks.run_trading_view_scrape"
INITIAL_SCREENER_TASK_NAME = "initial-screener-daily"
INITIAL_SCREENER_TASK_PATH = "api.tasks.run_initial_screener"


class Command(BaseCommand):
    help = (
        "Create or update django-celery-beat schedules for trading_view_scrape "
        "and initial_screener."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--timezone",
            help="Timezone name stored on the crontab. Defaults to CELERY_TIMEZONE/TIME_ZONE.",
        )

    def handle(self, *args, **options) -> None:
        timezone_name = options["timezone"]
        if timezone_name is None:
            timezone_name = str(
                getattr(
                    settings,
                    "CELERY_TIMEZONE",
                    getattr(settings, "TIME_ZONE", "UTC"),
                )
            )

        self._validate_timezone(timezone_name)

        trading_slots = self._build_trading_view_slots(
            start_hour=13,
            start_minute=0,
            end_hour=20,
            end_minute=0,
            interval_minutes=45,
        )
        trading_names: set[str] = set()
        created_trading = 0
        updated_trading = 0

        for hour, minute in trading_slots:
            task_name = f"{TRADING_VIEW_TASK_NAME_PREFIX}{hour:02d}{minute:02d}-utc"
            trading_names.add(task_name)
            _, _, task_created = self._upsert_crontab_task(
                task_name=task_name,
                task_path=TRADING_VIEW_TASK_PATH,
                hour=hour,
                minute=minute,
                timezone_name=timezone_name,
            )
            if task_created:
                created_trading += 1
            else:
                updated_trading += 1

        stale_trading_qs = PeriodicTask.objects.filter(
            name__startswith=TRADING_VIEW_TASK_NAME_PREFIX
        ).exclude(name__in=trading_names)
        stale_trading_count = stale_trading_qs.count()
        stale_trading_qs.delete()

        _, _, initial_screener_created = self._upsert_crontab_task(
            task_name=INITIAL_SCREENER_TASK_NAME,
            task_path=INITIAL_SCREENER_TASK_PATH,
            hour=12,
            minute=30,
            timezone_name=timezone_name,
        )

        initial_action = "Created" if initial_screener_created else "Updated"
        trading_action = (
            f"created={created_trading}, updated={updated_trading}, removed_stale={stale_trading_count}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Synced periodic tasks for trading jobs "
                f"(trading_view_scrape: {trading_action}; "
                f"initial_screener: {initial_action.lower()} at 12:30 {timezone_name})."
            )
        )

    def _build_trading_view_slots(
        self,
        *,
        start_hour: int,
        start_minute: int,
        end_hour: int,
        end_minute: int,
        interval_minutes: int,
    ) -> list[tuple[int, int]]:
        self._validate_time(hour=start_hour, minute=start_minute)
        self._validate_time(hour=end_hour, minute=end_minute)
        if interval_minutes <= 0:
            raise CommandError("interval_minutes must be positive.")

        start = datetime(2000, 1, 1, start_hour, start_minute)
        end = datetime(2000, 1, 1, end_hour, end_minute)
        if start >= end:
            raise CommandError("TradingView window start must be before its end.")

        slots: list[tuple[int, int]] = []
        current = start
        while current < end:
            slots.append((current.hour, current.minute))
            current += timedelta(minutes=interval_minutes)

        return slots

    def _upsert_crontab_task(
        self,
        *,
        task_name: str,
        task_path: str,
        hour: int,
        minute: int,
        timezone_name: str,
    ) -> tuple[CrontabSchedule, PeriodicTask, bool]:
        self._validate_time(hour=hour, minute=minute)
        schedule, _ = CrontabSchedule.objects.get_or_create(
            minute=str(minute),
            hour=str(hour),
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone=timezone_name,
        )
        task, task_created = PeriodicTask.objects.update_or_create(
            name=task_name,
            defaults={
                "task": task_path,
                "crontab": schedule,
                "interval": None,
                "solar": None,
                "clocked": None,
                "enabled": True,
                "one_off": False,
            },
        )
        return schedule, task, task_created

    def _validate_time(self, *, hour: int, minute: int) -> None:
        if not 0 <= hour <= 23:
            raise CommandError("--hour must be between 0 and 23.")
        if not 0 <= minute <= 59:
            raise CommandError("--minute must be between 0 and 59.")

    def _validate_timezone(self, timezone_name: str) -> None:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise CommandError(f"Unknown timezone '{timezone_name}'.") from exc
