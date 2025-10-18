from typing import Any

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

        screener_names = []
        for index, item in enumerate(data):
            attributes = item.get("attributes") if isinstance(item, dict) else None
            if not isinstance(attributes, dict) or "name" not in attributes:
                raise CommandError(
                    "Unexpected payload structure: missing 'attributes.name' at index "
                    f"{index}."
                )
            screener_names.append(attributes["name"])

        formatted_payload = "\n".join(screener_names)
        self.stdout.write(formatted_payload)
        return formatted_payload
