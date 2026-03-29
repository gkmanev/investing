from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django_celery_beat.models import CrontabSchedule, PeriodicTask


TASK_NAME = "send-daily-top-3-edition"
TASK_PATH = "api.tasks.send_daily_top_3_edition"


class Command(BaseCommand):
    help = "Create or update the django-celery-beat schedule for the Daily Top 3 email."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--hour",
            type=int,
            help="UTC hour for the daily brief task. Defaults to DAILY_BRIEF_SEND_HOUR_UTC.",
        )
        parser.add_argument(
            "--minute",
            type=int,
            help="UTC minute for the daily brief task. Defaults to DAILY_BRIEF_SEND_MINUTE_UTC.",
        )
        parser.add_argument(
            "--timezone",
            help="Timezone name stored on the crontab. Defaults to CELERY_TIMEZONE/TIME_ZONE.",
        )

    def handle(self, *args, **options) -> None:
        hour = options["hour"]
        minute = options["minute"]
        timezone_name = options["timezone"]

        if hour is None:
            hour = int(getattr(settings, "DAILY_BRIEF_SEND_HOUR_UTC", 16))
        if minute is None:
            minute = int(getattr(settings, "DAILY_BRIEF_SEND_MINUTE_UTC", 0))
        if timezone_name is None:
            timezone_name = str(
                getattr(
                    settings,
                    "CELERY_TIMEZONE",
                    getattr(settings, "TIME_ZONE", "UTC"),
                )
            )

        self._validate_time(hour=hour, minute=minute)
        self._validate_timezone(timezone_name)

        schedule, schedule_created = CrontabSchedule.objects.get_or_create(
            minute=str(minute),
            hour=str(hour),
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone=timezone_name,
        )

        task, task_created = PeriodicTask.objects.update_or_create(
            name=TASK_NAME,
            defaults={
                "task": TASK_PATH,
                "crontab": schedule,
                "interval": None,
                "solar": None,
                "clocked": None,
                "enabled": True,
                "one_off": False,
            },
        )

        action = "Created" if task_created else "Updated"
        schedule_action = "created" if schedule_created else "reused"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} periodic task '{TASK_NAME}' for {hour:02d}:{minute:02d} "
                f"{timezone_name} (crontab {schedule_action}, enabled={task.enabled})."
            )
        )

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
