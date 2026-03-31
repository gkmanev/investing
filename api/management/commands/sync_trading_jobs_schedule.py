from __future__ import annotations

import json
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
        parser.add_argument(
            "--trading-start",
            help="TradingView window start in HH:MM. Defaults to TRADING_VIEW_SCRAPE_START_TIME_UTC.",
        )
        parser.add_argument(
            "--trading-end",
            help="TradingView window end in HH:MM. Defaults to TRADING_VIEW_SCRAPE_END_TIME_UTC.",
        )
        parser.add_argument(
            "--interval-minutes",
            type=int,
            help="TradingView run interval in minutes. Defaults to TRADING_VIEW_SCRAPE_INTERVAL_MINUTES.",
        )
        parser.add_argument(
            "--initial-screener-time",
            help="Initial screener daily run time in HH:MM. Defaults to INITIAL_SCREENER_TIME_UTC.",
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
        trading_start = options["trading_start"] or getattr(
            settings,
            "TRADING_VIEW_SCRAPE_START_TIME_UTC",
            "13:00",
        )
        trading_end = options["trading_end"] or getattr(
            settings,
            "TRADING_VIEW_SCRAPE_END_TIME_UTC",
            "20:00",
        )
        interval_minutes = options["interval_minutes"]
        if interval_minutes is None:
            interval_minutes = int(
                getattr(settings, "TRADING_VIEW_SCRAPE_INTERVAL_MINUTES", 45)
            )
        initial_screener_time = options["initial_screener_time"] or getattr(
            settings,
            "INITIAL_SCREENER_TIME_UTC",
            "12:30",
        )

        trading_start_hour, trading_start_minute = self._parse_time_value(
            trading_start,
            option_name="--trading-start",
        )
        trading_end_hour, trading_end_minute = self._parse_time_value(
            trading_end,
            option_name="--trading-end",
        )
        initial_screener_hour, initial_screener_minute = self._parse_time_value(
            initial_screener_time,
            option_name="--initial-screener-time",
        )

        trading_slots = self._build_trading_view_slots(
            start_hour=trading_start_hour,
            start_minute=trading_start_minute,
            end_hour=trading_end_hour,
            end_minute=trading_end_minute,
            interval_minutes=interval_minutes,
        )
        trading_names: set[str] = set()
        created_trading = 0
        updated_trading = 0

        for index, (hour, minute) in enumerate(trading_slots):
            task_name = f"{TRADING_VIEW_TASK_NAME_PREFIX}{hour:02d}{minute:02d}-utc"
            trading_names.add(task_name)
            _, _, task_created = self._upsert_crontab_task(
                task_name=task_name,
                task_path=TRADING_VIEW_TASK_PATH,
                hour=hour,
                minute=minute,
                timezone_name=timezone_name,
                kwargs={"skip_rsi": index > 0},
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
            hour=initial_screener_hour,
            minute=initial_screener_minute,
            timezone_name=timezone_name,
            kwargs=None,
        )

        initial_action = "Created" if initial_screener_created else "Updated"
        trading_action = (
            f"created={created_trading}, updated={updated_trading}, removed_stale={stale_trading_count}"
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Synced periodic tasks for trading jobs "
                f"(trading_view_scrape: {trading_action}; "
                f"initial_screener: {initial_action.lower()} at "
                f"{initial_screener_hour:02d}:{initial_screener_minute:02d} {timezone_name})."
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
        kwargs: dict[str, object] | None,
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
                "kwargs": json.dumps(kwargs or {}),
            },
        )
        return schedule, task, task_created

    def _validate_time(self, *, hour: int, minute: int) -> None:
        if not 0 <= hour <= 23:
            raise CommandError("--hour must be between 0 and 23.")
        if not 0 <= minute <= 59:
            raise CommandError("--minute must be between 0 and 59.")

    def _parse_time_value(self, value: str, *, option_name: str) -> tuple[int, int]:
        try:
            hour_text, minute_text = str(value).split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except (AttributeError, TypeError, ValueError) as exc:
            raise CommandError(f"{option_name} must be in HH:MM format.") from exc

        self._validate_time(hour=hour, minute=minute)
        return hour, minute

    def _validate_timezone(self, timezone_name: str) -> None:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise CommandError(f"Unknown timezone '{timezone_name}'.") from exc
