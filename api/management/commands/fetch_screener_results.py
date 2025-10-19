from __future__ import annotations

import json
from typing import Any, Iterable

import requests
from django.core.management.base import BaseCommand, CommandError

from api.models import ScreenerType

API_URL = "https://seeking-alpha.p.rapidapi.com/screeners/get-results"
API_HEADERS = {
    "x-rapidapi-key": "66dcbafb75msha536f3086b06788p1f5e7ajsnac1315877f0f",
    "x-rapidapi-host": "seeking-alpha.p.rapidapi.com",
    "Content-Type": "application/json",
}


class Command(BaseCommand):
    """Fetch screener results using previously stored filters."""

    help = "Fetch screener results from Seeking Alpha using stored filters"

    def add_arguments(self, parser) -> None:  # pragma: no cover - argument wiring
        parser.add_argument(
            "screener_name",
            help="Name of the screener type whose filters should be used.",
        )
        parser.add_argument(
            "--page",
            type=int,
            default=1,
            help="Results page to request (default: 1)",
        )
        parser.add_argument(
            "--per-page",
            type=int,
            default=100,
            dest="per_page",
            help="How many results to request per page (default: 100)",
        )
        parser.add_argument(
            "--type",
            default="stock",
            dest="asset_type",
            help="Asset type parameter for the upstream API (default: stock)",
        )

    def handle(self, *args: Any, **options: Any) -> str:
        screener_name: str = options["screener_name"]
        page: int = options["page"]
        per_page: int = options["per_page"]
        asset_type: str = options["asset_type"]

        screener = self._get_screener(screener_name)
        payload = self._build_payload(screener.filters.all())
        if not payload:
            raise CommandError(
                f"Screener '{screener_name}' does not have any stored filters."
            )

        query_params = {
            "page": str(page),
            "per_page": str(per_page),
            "type": asset_type,
        }

        try:
            response = requests.post(
                API_URL,
                headers=API_HEADERS,
                params=query_params,
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise CommandError(f"Failed to call Seeking Alpha API: {exc}") from exc

        if response.status_code != 200:
            raise CommandError(
                f"Received unexpected status code {response.status_code}: {response.text}"
            )

        try:
            formatted_payload = json.dumps(response.json(), indent=2, sort_keys=True)
        except ValueError as exc:
            raise CommandError("Received invalid JSON from Seeking Alpha API") from exc

        self.stdout.write(formatted_payload)
        return formatted_payload

    def _get_screener(self, screener_name: str) -> ScreenerType:
        try:
            return ScreenerType.objects.prefetch_related("filters").get(
                name=screener_name
            )
        except ScreenerType.DoesNotExist as exc:
            raise CommandError(
                f"Screener named '{screener_name}' does not exist in the database."
            ) from exc

    def _build_payload(self, filters: Iterable[Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for filter_obj in filters:
            filter_payload = filter_obj.payload
            if not filter_payload:
                continue

            if not isinstance(filter_payload, dict):
                raise CommandError(
                    "Unsupported filter payload structure encountered: "
                    f"{filter_obj.label!r}"
                )

            for key, value in filter_payload.items():
                if key in payload and payload[key] != value:
                    raise CommandError(
                        "Conflicting values for payload key "
                        f"'{key}' across filters."
                    )
                payload[key] = value

        return payload

