from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django_celery_beat.models import CrontabSchedule, PeriodicTask


POPULATE_TASK_NAME = "populate-daily-brief"
POPULATE_TASK_NAME_PREFIX = f"{POPULATE_TASK_NAME}-"
POPULATE_TASK_PATH = "api.tasks.run_populate_daily_brief"
SEND_TASK_NAME = "send-daily-top-3-edition"
SEND_TASK_PATH = "api.tasks.send_daily_top_3_edition"
WEEKDAY_CRONTAB = "1,2,3,4,5"


class Command(BaseCommand):
    help = (
        "Create or update django-celery-beat schedules for populating and "
        "sending the daily brief."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--hour",
            type=int,
            help="UTC hour for the daily brief send task. Defaults to DAILY_BRIEF_SEND_HOUR_UTC.",
        )
        parser.add_argument(
            "--minute",
            type=int,
            help="UTC minute for the daily brief send task. Defaults to DAILY_BRIEF_SEND_MINUTE_UTC.",
        )
        parser.add_argument(
            "--populate-time",
            help=(
                "UTC time in HH:MM, or comma-separated HH:MM values, for "
                "populate_daily_brief. "
                "Defaults to POPULATE_DAILY_BRIEF_TIME_UTC."
            ),
        )
        parser.add_argument(
            "--timezone",
            help="Timezone name stored on the crontab. Defaults to CELERY_TIMEZONE/TIME_ZONE.",
        )

    def handle(self, *args, **options) -> None:
        hour = options["hour"]
        minute = options["minute"]
        populate_time = options["populate_time"]
        timezone_name = options["timezone"]

        if hour is None:
            hour = int(getattr(settings, "DAILY_BRIEF_SEND_HOUR_UTC", 16))
        if minute is None:
            minute = int(getattr(settings, "DAILY_BRIEF_SEND_MINUTE_UTC", 0))
        if populate_time is None:
            populate_time = getattr(settings, "POPULATE_DAILY_BRIEF_TIME_UTC", "17:00")
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
        populate_slots = self._parse_populate_times_value(
            populate_time,
            option_name="--populate-time",
        )

        populate_names: set[str] = set()
        created_populate = 0
        updated_populate = 0
        for populate_hour, populate_minute in populate_slots:
            task_name = self._build_populate_task_name(
                hour=populate_hour,
                minute=populate_minute,
                slot_count=len(populate_slots),
            )
            populate_names.add(task_name)
            _, _, populate_task_created = self._upsert_crontab_task(
                task_name=task_name,
                task_path=POPULATE_TASK_PATH,
                hour=populate_hour,
                minute=populate_minute,
                timezone_name=timezone_name,
                day_of_week=WEEKDAY_CRONTAB,
            )
            if populate_task_created:
                created_populate += 1
            else:
                updated_populate += 1

        stale_populate_qs = PeriodicTask.objects.filter(
            Q(name=POPULATE_TASK_NAME) | Q(name__startswith=POPULATE_TASK_NAME_PREFIX)
        ).exclude(name__in=populate_names)
        stale_populate_count = stale_populate_qs.count()
        stale_populate_qs.delete()

        _, _, send_task_created = self._upsert_crontab_task(
            task_name=SEND_TASK_NAME,
            task_path=SEND_TASK_PATH,
            hour=hour,
            minute=minute,
            timezone_name=timezone_name,
            day_of_week="*",
        )

        populate_summary = ", ".join(
            f"{populate_hour:02d}:{populate_minute:02d}"
            for populate_hour, populate_minute in populate_slots
        )
        send_action = "Created" if send_task_created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                "Synced daily brief periodic tasks "
                f"(populate: created={created_populate}, updated={updated_populate}, "
                f"removed_stale={stale_populate_count} at {populate_summary} "
                f"weekdays "
                f"{timezone_name}; "
                f"send: {send_action.lower()} at {hour:02d}:{minute:02d} {timezone_name})."
            )
        )

    def _build_populate_task_name(self, *, hour: int, minute: int, slot_count: int) -> str:
        if slot_count == 1:
            return POPULATE_TASK_NAME
        return f"{POPULATE_TASK_NAME_PREFIX}{hour:02d}{minute:02d}-utc"

    def _upsert_crontab_task(
        self,
        *,
        task_name: str,
        task_path: str,
        hour: int,
        minute: int,
        timezone_name: str,
        day_of_week: str,
    ) -> tuple[CrontabSchedule, PeriodicTask, bool]:
        schedule, _ = CrontabSchedule.objects.get_or_create(
            minute=str(minute),
            hour=str(hour),
            day_of_week=day_of_week,
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

    def _parse_time_value(self, value: str, *, option_name: str) -> tuple[int, int]:
        try:
            hour_text, minute_text = str(value).split(":", 1)
            hour = int(hour_text)
            minute = int(minute_text)
        except (AttributeError, TypeError, ValueError) as exc:
            raise CommandError(f"{option_name} must be in HH:MM format.") from exc

        self._validate_time(hour=hour, minute=minute)
        return hour, minute

    def _parse_populate_times_value(
        self,
        value: str,
        *,
        option_name: str,
    ) -> list[tuple[int, int]]:
        try:
            candidates = [item.strip() for item in str(value).split(",") if item.strip()]
        except (AttributeError, TypeError) as exc:
            raise CommandError(
                f"{option_name} must be in HH:MM format or a comma-separated list of HH:MM values."
            ) from exc

        if not candidates:
            raise CommandError(
                f"{option_name} must be in HH:MM format or a comma-separated list of HH:MM values."
            )

        slots: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for candidate in candidates:
            slot = self._parse_time_value(candidate, option_name=option_name)
            if slot in seen:
                continue
            seen.add(slot)
            slots.append(slot)
        return slots

    def _validate_timezone(self, timezone_name: str) -> None:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise CommandError(f"Unknown timezone '{timezone_name}'.") from exc
