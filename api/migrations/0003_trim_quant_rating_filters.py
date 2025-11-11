import json

from django.db import migrations


def _normalise_value(value: str) -> str:
    return value.strip().lower().replace("_", " ")


def _filter_quant_rating_entries(value, allowed):
    if isinstance(value, list):
        filtered = [
            item
            for item in value
            if isinstance(item, str) and _normalise_value(item) in allowed
        ]
        if filtered != value:
            return filtered, True
        return value, False

    if isinstance(value, dict):
        changed = False
        updated = {}
        for key, entry in value.items():
            new_entry, entry_changed = _filter_quant_rating_entries(entry, allowed)
            updated[key] = new_entry
            if entry_changed or new_entry != entry:
                changed = True
        if changed:
            return updated, True
        return value, False

    return value, False


def _trim_quant_rating(payload, allowed):
    if isinstance(payload, dict):
        changed = False
        updated = {}
        for key, value in payload.items():
            if key == "quant_rating":
                new_value, value_changed = _filter_quant_rating_entries(value, allowed)
            else:
                new_value, value_changed = _trim_quant_rating(value, allowed)
            updated[key] = new_value
            if value_changed or new_value != value:
                changed = True
        if changed:
            return updated, True
        return payload, False

    if isinstance(payload, list):
        changed = False
        updated_list = []
        for item in payload:
            new_item, item_changed = _trim_quant_rating(item, allowed)
            updated_list.append(new_item)
            if item_changed or new_item != item:
                changed = True
        if changed:
            return updated_list, True
        return payload, False

    return payload, False


def _format_label_from_payload(payload):
    if isinstance(payload, dict):
        if not payload:
            return ""
        parts = []
        for key in sorted(payload):
            value = payload[key]
            if isinstance(value, (dict, list)):
                value_repr = json.dumps(value, sort_keys=True)
            else:
                value_repr = str(value)
            parts.append(f"{key}={value_repr}")
        return ", ".join(parts)

    if payload in (None, ""):
        return ""

    return str(payload)


def forwards(apps, schema_editor):
    ScreenerType = apps.get_model("api", "ScreenerType")
    ScreenerFilter = apps.get_model("api", "ScreenerFilter")

    allowed = {"strong buy", "buy"}

    try:
        screener = ScreenerType.objects.get(name="Stocks by Quant")
    except ScreenerType.DoesNotExist:
        return

    filters = ScreenerFilter.objects.filter(screener_type=screener)
    for filter_obj in filters:
        payload = filter_obj.payload
        if payload is None:
            continue

        trimmed_payload, changed = _trim_quant_rating(payload, allowed)
        if not changed:
            continue

        filter_obj.payload = trimmed_payload

        new_label = _format_label_from_payload(trimmed_payload)
        if new_label:
            filter_obj.label = new_label

        filter_obj.save(update_fields=["payload", "label", "updated_at"])


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0002_screenertype_screenerfilter"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]

