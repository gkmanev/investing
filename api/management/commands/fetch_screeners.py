from typing import Any, Iterable, List
import json

import requests
from django.core.management.base import BaseCommand, CommandError

API_URL = "https://seeking-alpha.p.rapidapi.com/screeners/list"
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
}


class Command(BaseCommand):
    """Fetch the list of screeners from the Seeking Alpha API."""

    help = "Fetch screeners list from Seeking Alpha via RapidAPI"

    def handle(self, *args: Any, **options: Any) -> str:
        try:
            response = requests.get(API_URL, headers=API_HEADERS, timeout=30)
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise CommandError(f"Failed to call Seeking Alpha API: {exc}") from exc

        if response.status_code != 200:
            raise CommandError(
                f"Received unexpected status code {response.status_code}: {response.text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CommandError("Received invalid JSON from Seeking Alpha API") from exc

        data = payload.get("data", [])
        if not isinstance(data, list):
            raise CommandError("Unexpected payload structure: 'data' is not a list.")

        formatted_entries: List[str] = []
        for index, item in enumerate(data):
            attributes = _extract_attributes(item, index)
            name = attributes["name"]
            filters = _extract_filters(attributes, index)
            formatted_entries.append(_format_entry(name, filters))

        formatted_payload = "\n".join(formatted_entries)
        self.stdout.write(formatted_payload)
        return formatted_payload


def _extract_attributes(item: Any, index: int) -> dict[str, Any]:
    attributes = item.get("attributes") if isinstance(item, dict) else None
    if not isinstance(attributes, dict) or "name" not in attributes:
        raise CommandError(
            "Unexpected payload structure: missing 'attributes.name' at index "
            f"{index}."
        )
    return attributes


def _extract_filters(attributes: dict[str, Any], index: int) -> List[str]:
    raw_filters = attributes.get("filters")
    if raw_filters is None:
        return []
    if isinstance(raw_filters, dict):
        raw_filters = [raw_filters]
    if not isinstance(raw_filters, Iterable) or isinstance(raw_filters, (str, bytes)):
        raise CommandError(
            "Unexpected payload structure: 'attributes.filters' must be a list or dict at index "
            f"{index}."
        )

    formatted_filters: List[str] = []
    for filter_index, filter_item in enumerate(raw_filters):
        formatted_filters.append(_format_filter(filter_item, index, filter_index))

    return formatted_filters


def _format_filter(filter_item: Any, screener_index: int, filter_index: int) -> str:
    if isinstance(filter_item, dict):
        if not filter_item:
            raise CommandError(
                "Unexpected payload structure: empty filter definition at screener index "
                f"{screener_index}, filter index {filter_index}."
            )
        parts = []
        for key in sorted(filter_item):
            value = filter_item[key]
            if isinstance(value, (dict, list)):
                value_repr = json.dumps(value, sort_keys=True)
            else:
                value_repr = str(value)
            parts.append(f"{key}={value_repr}")
        return ", ".join(parts)

    if filter_item in (None, ""):
        raise CommandError(
            "Unexpected payload structure: empty filter definition at screener index "
            f"{screener_index}, filter index {filter_index}."
        )

    return str(filter_item)


def _format_entry(name: str, filters: List[str]) -> str:
    if not filters:
        return name

    formatted_filters = "\n".join(f"  - {filter_value}" for filter_value in filters)
    return f"{name}\n{formatted_filters}"
