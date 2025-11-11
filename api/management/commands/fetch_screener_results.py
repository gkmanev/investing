from __future__ import annotations

import json
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
        parser.add_argument(
            "--market-cap",
            dest="market_cap",
            help=(
                "Optional minimum market cap filter. Accepts raw numbers or values with "
                "suffixes such as K, M, B, or T (e.g. 500M, 1.2B)."
            ),
        )
        parser.add_argument(
            "--min-price",
            dest="min_price",
            help="Optional minimum last price filter (e.g. 10, 15.5).",
        )
        parser.add_argument(
            "--max-price",
            dest="max_price",
            help="Optional maximum last price filter (e.g. 250, 499.99).",
        )
        parser.add_argument(
            "--only-filter-keys",
            nargs="+",
            dest="only_filter_keys",
            help=(
                "Limit the outgoing payload to these top-level filter keys. "
                "Useful when a screener stores additional filters that should be ignored."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> str:
        screener_name: str = options["screener_name"]
        page: int = options["page"]
        per_page: int = options["per_page"]
        asset_type: str = options["asset_type"]
        market_cap_raw: str | None = options.get("market_cap")
        min_price_raw: str | None = options.get("min_price")
        max_price_raw: str | None = options.get("max_price")
        only_filter_keys: Iterable[str] | None = options.get("only_filter_keys")

        screener = self._get_screener(screener_name)
        payload = self._build_payload(screener.filters.all())
        if only_filter_keys:
            payload = self._limit_payload_to_keys(payload, only_filter_keys)
        if market_cap_raw:
            market_cap_value = self._parse_market_cap(market_cap_raw)
            payload = self._apply_market_cap_filter(payload, market_cap_value)
        price_bounds: dict[str, float] = {}
        if min_price_raw is not None:
            price_bounds["gte"] = self._parse_price(min_price_raw, "minimum")
        if max_price_raw is not None:
            price_bounds["lte"] = self._parse_price(max_price_raw, "maximum")
        if price_bounds:
            if ("gte" in price_bounds and "lte" in price_bounds) and (
                price_bounds["gte"] > price_bounds["lte"]
            ):
                raise CommandError(
                    "Minimum price cannot be greater than maximum price."
                )
            payload = self._apply_price_filter(payload, price_bounds)
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
            formatted_payload = json.dumps(payload, indent=2, sort_keys=True)
            self.stderr.write("POST payload:\n" + formatted_payload)

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

            payload = self._merge_payload_dicts(payload, filter_payload)

        return payload

    def _limit_payload_to_keys(
        self, payload: dict[str, Any], allowed_keys: Iterable[str]
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CommandError(
                "Screener filters payload has an unexpected structure."
            )

        allowed_key_set = {key for key in allowed_keys if key}
        if not allowed_key_set:
            raise CommandError("At least one filter key must be specified.")

        filtered_payload = {
            key: value for key, value in payload.items() if key in allowed_key_set
        }

        if not filtered_payload:
            available = ", ".join(sorted(payload.keys())) or "<none>"
            raise CommandError(
                "Requested filter keys were not found in the screener payload. "
                f"Available keys: {available}."
            )

        return filtered_payload

    def _merge_payload_dicts(
        self, base: dict[str, Any], incoming: dict[str, Any], path: str = "root"
    ) -> dict[str, Any]:
        merged: dict[str, Any] = dict(base)

        for key, value in incoming.items():
            current_path = f"{path}.{key}"
            if key not in merged:
                merged[key] = value
                continue

            existing_value = merged[key]

            if existing_value is value or existing_value == value:
                continue

            if isinstance(existing_value, dict) and isinstance(value, dict):
                merged[key] = self._merge_payload_dicts(
                    existing_value, value, current_path
                )
                continue

            if isinstance(existing_value, list) and isinstance(value, list):
                combined_list = list(existing_value)
                for item in value:
                    if item not in combined_list:
                        combined_list.append(item)
                merged[key] = combined_list
                continue

            raise CommandError(
                "Conflicting values encountered while building payload at "
                f"'{current_path}'."
            )

        return merged

    def _apply_market_cap_filter(
        self, payload: dict[str, Any], market_cap_value: int
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CommandError(
                "Screener filters payload has an unexpected structure."
            )

        filter_section = payload.get("filter")
        containers: list[dict[str, Any]] = []

        if isinstance(filter_section, dict):
            containers.append(filter_section)

        containers.append(payload)

        for container in containers:
            if "marketcap_display" in container:
                self._merge_market_cap_filter(container, market_cap_value)
                return payload

        if isinstance(filter_section, dict):
            self._merge_market_cap_filter(filter_section, market_cap_value)
            return payload

        self._merge_market_cap_filter(payload, market_cap_value)
        return payload

    def _merge_market_cap_filter(
        self, container: dict[str, Any], market_cap_value: int
    ) -> None:
        existing_filter = container.get("marketcap_display")

        if existing_filter is None:
            container["marketcap_display"] = {"gte": market_cap_value}
            return

        if not isinstance(existing_filter, dict):
            raise CommandError(
                "Existing marketcap_display filter has an unexpected structure."
            )

        updated_filter = dict(existing_filter)
        updated_filter["gte"] = market_cap_value
        container["marketcap_display"] = updated_filter

    def _parse_market_cap(self, market_cap: str) -> int:
        cleaned_value = market_cap.strip().upper().replace(",", "")
        if not cleaned_value:
            raise CommandError("Market cap value cannot be empty.")

        suffix_multipliers = {
            "K": 1_000,
            "M": 1_000_000,
            "B": 1_000_000_000,
            "T": 1_000_000_000_000,
        }

        multiplier = 1
        number_part = cleaned_value

        if cleaned_value[-1] in suffix_multipliers:
            multiplier = suffix_multipliers[cleaned_value[-1]]
            number_part = cleaned_value[:-1]

        try:
            numeric_value = float(number_part)
        except ValueError as exc:
            raise CommandError(
                "Market cap value must be a number optionally followed by K, M, B, or T."
            ) from exc

        if numeric_value < 0:
            raise CommandError("Market cap value cannot be negative.")

        return int(numeric_value * multiplier)

    def _apply_price_filter(
        self, payload: dict[str, Any], bounds: dict[str, float]
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise CommandError(
                "Screener filters payload has an unexpected structure."
            )

        filter_section = payload.get("filter")
        containers: list[dict[str, Any]] = []

        if isinstance(filter_section, dict):
            containers.append(filter_section)

        containers.append(payload)

        for container in containers:
            if "last" in container:
                self._merge_price_filter(container, bounds)
                return payload

        if isinstance(filter_section, dict):
            self._merge_price_filter(filter_section, bounds)
            return payload

        self._merge_price_filter(payload, bounds)
        return payload

    def _merge_price_filter(
        self, container: dict[str, Any], bounds: dict[str, float]
    ) -> None:
        existing_filter = container.get("last")

        if existing_filter is None:
            container["last"] = dict(bounds)
            return

        if not isinstance(existing_filter, dict):
            raise CommandError(
                "Existing last price filter has an unexpected structure."
            )

        updated_filter = dict(existing_filter)
        updated_filter.update(bounds)
        container["last"] = updated_filter

    def _parse_price(self, price: str, descriptor: str) -> float:
        if price is None:
            raise CommandError(f"{descriptor.title()} price value cannot be empty.")

        cleaned_value = price.strip()
        if not cleaned_value:
            raise CommandError(f"{descriptor.title()} price value cannot be empty.")

        try:
            numeric_value = float(cleaned_value)
        except ValueError as exc:
            raise CommandError(
                "Price filters must be numeric values."
            ) from exc

        if numeric_value < 0:
            raise CommandError("Price filters cannot be negative.")

        return float(numeric_value)

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

