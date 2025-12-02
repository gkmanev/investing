from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Tuple
import copy
import json

import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.custom_filters import CUSTOM_FILTER_PAYLOAD
from api.models import ScreenerFilter, ScreenerType

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
        with transaction.atomic():
            for index, item in enumerate(data):
                attributes = _extract_attributes(item, index)
                name = attributes["name"]
                description = _extract_description(attributes)
                filter_specs = _extract_filters(attributes, index)
                filter_specs.append(_build_custom_filter(index, len(filter_specs)))

                screener_type, _ = ScreenerType.objects.update_or_create(
                    name=name,
                    defaults={"description": description},
                )
                _synchronise_filters(screener_type, filter_specs)

                formatted_entries.append(
                    _format_entry(name, [spec.label for spec in filter_specs])
                )

        formatted_payload = "\n".join(formatted_entries)
        self.stdout.write(formatted_payload)
        return formatted_payload


@dataclass
class FilterSpec:
    label: str
    payload: Any


def _build_custom_filter(screener_index: int, filter_count: int) -> FilterSpec:
    label = _format_filter_label(
        CUSTOM_FILTER_PAYLOAD, screener_index, filter_count
    )
    return FilterSpec(label=label, payload=copy.deepcopy(CUSTOM_FILTER_PAYLOAD))


def _extract_attributes(item: Any, index: int) -> dict[str, Any]:
    attributes = item.get("attributes") if isinstance(item, dict) else None
    if not isinstance(attributes, dict) or "name" not in attributes:
        raise CommandError(
            f"Unexpected payload structure: missing 'attributes.name' at index {index}."
        )
    return attributes


def _extract_description(attributes: dict[str, Any]) -> str:
    description_fields = (
        attributes.get("description"),
        attributes.get("shortDescription"),
        attributes.get("summary"),
    )
    for value in description_fields:
        if isinstance(value, str) and value.strip():
            return value
        if value not in (None, "") and not isinstance(value, (list, dict)):
            return str(value)
    return ""


def _extract_filters(attributes: dict[str, Any], index: int) -> List[FilterSpec]:
    raw_filters = attributes.get("filters")

    # Treat None and {} as "no filters"
    if raw_filters is None or raw_filters == {}:
        return []

    # Normalize to a list of items
    if isinstance(raw_filters, dict):
        items = [raw_filters]
    elif isinstance(raw_filters, Iterable) and not isinstance(raw_filters, (str, bytes)):
        items = list(raw_filters)
    else:
        raise CommandError(
            "Unexpected payload structure: 'attributes.filters' must be a list or dict "
            f"at index {index}."
        )

    formatted_filters: List[FilterSpec] = []
    for filter_index, filter_item in enumerate(items):
        spec = _normalise_filter(filter_item, index, filter_index)
        if spec:  # skip empty specs returned by _normalise_filter
            formatted_filters.append(spec)

    return formatted_filters


def _normalise_filter(
    filter_item: Any, screener_index: int, filter_index: int
) -> FilterSpec | None:
    if isinstance(filter_item, dict):
        sanitised_filter = _sanitise_filter_dict(filter_item)
        if not sanitised_filter:
            return None
        label_source: Any = sanitised_filter
        payload: Any = sanitised_filter
    elif isinstance(filter_item, Iterable) and not isinstance(filter_item, (str, bytes)):
        label_source = filter_item
        payload = list(filter_item)
    else:
        label_source = filter_item
        payload = filter_item

    label = _format_filter_label(label_source, screener_index, filter_index)
    if not label:
        return None

    return FilterSpec(label=label, payload=payload)


def _format_filter_label(
    filter_item: Any, screener_index: int, filter_index: int
) -> str:
    # Dict filter: render key=value pairs; skip if empty
    if isinstance(filter_item, dict):
        if not filter_item:
            return ""  # silently skip empty dict filters
        parts = []
        for key in sorted(filter_item):
            value = filter_item[key]
            if isinstance(value, (dict, list)):
                value_repr = json.dumps(value, sort_keys=True)
            else:
                value_repr = str(value)
            parts.append(f"{key}={value_repr}")
        return ", ".join(parts)

    # Primitive filter: ensure it's not empty/null-ish
    if filter_item in (None, ""):
        return ""  # skip empty values instead of raising

    return str(filter_item)


def _format_entry(name: str, filters: List[str]) -> str:
    # Drop any empty strings that slipped through
    filters = [f for f in filters if f]
    if not filters:
        return name

    formatted_filters = "\n".join(f"  - {filter_value}" for filter_value in filters)
    return f"{name}\n{formatted_filters}"


def _synchronise_filters(
    screener_type: ScreenerType, filter_specs: List[FilterSpec]
) -> None:
    existing_filters = {
        f.label: f for f in screener_type.filters.all()
    }

    seen_labels: list[str] = []
    for order, spec in enumerate(filter_specs, start=1):
        payload, payload_changed = _apply_payload_rules(
            screener_type, spec.payload
        )

        label = spec.label
        if payload_changed:
            new_label = _format_filter_label(payload, 0, 0)
            if new_label:
                label = new_label

        spec.payload = payload
        spec.label = label

        seen_labels.append(label)
        filter_obj = existing_filters.get(label)
        if filter_obj is None:
            ScreenerFilter.objects.create(
                screener_type=screener_type,
                label=label,
                payload=payload,
                display_order=order,
            )
            continue

        filter_obj.payload = payload
        filter_obj.display_order = order
        filter_obj.save(update_fields=["payload", "display_order", "updated_at"])

    if seen_labels:
        screener_type.filters.exclude(label__in=seen_labels).delete()
    else:
        # No filters left; remove all existing ones for consistency
        screener_type.filters.all().delete()


def _sanitise_filter_dict(filter_dict: dict[str, Any]) -> dict[str, Any]:
    """Normalise known filter keys while keeping identifier data available."""

    sanitised_items: dict[str, Any] = {}
    for key, value in filter_dict.items():
        if isinstance(key, str):
            normalised_key = key.lower().replace("_", "")
            if normalised_key == "industryid":
                sanitised_items["industry_id"] = value
                continue

        sanitised_items[key] = value

    return sanitised_items


def _apply_payload_rules(
    screener_type: ScreenerType, payload: Any
) -> Tuple[Any, bool]:
    if payload is None:
        return payload, False

    if screener_type.name.strip().lower() == "stocks by quant":
        return _trim_quant_rating_values(payload)

    return payload, False


def _trim_quant_rating_values(payload: Any) -> Tuple[Any, bool]:
    allowed_values = {"strong buy", "buy"}

    if isinstance(payload, dict):
        changed = False
        updated_dict: dict[str, Any] = {}
        for key, value in payload.items():
            if key == "quant_rating":
                new_value, value_changed = _filter_quant_rating_entries(value, allowed_values)
            else:
                new_value, value_changed = _trim_quant_rating_values(value)
            updated_dict[key] = new_value
            if value_changed or new_value != value:
                changed = True
        if changed:
            return updated_dict, True
        return payload, False

    if isinstance(payload, list):
        changed = False
        updated_list: list[Any] = []
        for item in payload:
            new_item, item_changed = _trim_quant_rating_values(item)
            updated_list.append(new_item)
            if item_changed or new_item != item:
                changed = True
        if changed:
            return updated_list, True
        return payload, False

    return payload, False


def _filter_quant_rating_entries(
    value: Any, allowed_values: set[str]
) -> Tuple[Any, bool]:
    if isinstance(value, list):
        filtered = [
            item
            for item in value
            if isinstance(item, str)
            and _normalise_quant_rating_value(item) in allowed_values
        ]
        if filtered != value:
            return filtered, True
        return value, False

    if isinstance(value, dict):
        changed = False
        updated_dict: dict[str, Any] = {}
        for key, entry in value.items():
            new_entry, entry_changed = _filter_quant_rating_entries(entry, allowed_values)
            updated_dict[key] = new_entry
            if entry_changed or new_entry != entry:
                changed = True
        if changed:
            return updated_dict, True
        return value, False

    return value, False


def _normalise_quant_rating_value(value: str) -> str:
    return value.strip().lower().replace("_", " ")

