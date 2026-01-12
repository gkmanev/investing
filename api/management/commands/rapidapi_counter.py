from __future__ import annotations

from django.core.management.base import BaseCommand


def log_rapidapi_fetch(command: BaseCommand) -> int:
    count = getattr(command, "_rapidapi_fetch_count", 0) + 1
    setattr(command, "_rapidapi_fetch_count", count)
    command.stdout.write(f"RapidAPI fetch count: {count}")
    return count
