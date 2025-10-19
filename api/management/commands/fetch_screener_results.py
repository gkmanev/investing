from __future__ import annotations

from typing import Any, Iterable, List

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
            response_payload = response.json()
        except ValueError as exc:
            raise CommandError("Received invalid JSON from Seeking Alpha API") from exc

        ticker_names = self._extract_ticker_names(response_payload)
        if not ticker_names:
            raise CommandError(
                "Seeking Alpha API response did not include any ticker names."
            )

        formatted_payload = "\n".join(ticker_names)
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

    def _extract_ticker_names(self, payload: Any) -> List[str]:
        if not isinstance(payload, dict):
            raise CommandError(
                "Seeking Alpha API returned an unexpected payload structure."
            )

        data = payload.get("data", [])
        if not isinstance(data, list):
            raise CommandError(
                "Seeking Alpha API returned an unexpected payload structure."
            )

        names: List[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue

            attributes = item.get("attributes", {})
            if not isinstance(attributes, dict):
                continue

            direct_name = attributes.get("name")
            if isinstance(direct_name, str) and direct_name:
                names.append(direct_name)
                continue

            direct_names = attributes.get("names")
            if isinstance(direct_names, list):
                names.extend(
                    str(value)
                    for value in direct_names
                    if isinstance(value, str) and value
                )
                continue

            profile = attributes.get("p", {})
            if isinstance(profile, dict):
                profile_name = profile.get("name")
                if isinstance(profile_name, str) and profile_name:
                    names.append(profile_name)
                    continue

                profile_names = profile.get("names")
                if isinstance(profile_names, list):
                    names.extend(
                        str(value)
                        for value in profile_names
                        if isinstance(value, str) and value
                    )

        return names

