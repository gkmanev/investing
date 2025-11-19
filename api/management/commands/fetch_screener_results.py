from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, List

import requests
from django.core.management.base import BaseCommand, CommandError

from api.models import Investment, ScreenerType

API_URL = "https://seeking-alpha.p.rapidapi.com/screeners/get-results"
PROFILE_API_URL = "https://seeking-alpha.p.rapidapi.com/symbols/get-profile"
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
            help="Optional minimum close price filter (e.g. 10, 15.5).",
        )
        parser.add_argument(
            "--max-price",
            dest="max_price",
            help="Optional maximum close price filter (e.g. 250, 499.99).",
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
            "per_page": str(per_page),
            "type": asset_type,
        }

        formatted_payload = json.dumps(payload, indent=2, sort_keys=True)
        self.stderr.write("POST payload:\n" + formatted_payload)

        ticker_symbols = self._collect_symbols_from_screener(
            payload,
            query_params,
            start_page=page,
            per_page=per_page,
        )

        updated_symbols = self._create_or_update_investments(
            screener, ticker_symbols, self._fetch_symbol_profiles(ticker_symbols)
        )

        formatted_payload = "\n".join(updated_symbols)
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
            if "last" in container and "close" not in container:
                container["close"] = container.pop("last")

            if "close" in container:
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
        existing_filter = container.get("close")

        if existing_filter is None:
            container["close"] = dict(bounds)
            return

        if not isinstance(existing_filter, dict):
            raise CommandError(
                "Existing close price filter has an unexpected structure."
            )

        updated_filter = dict(existing_filter)
        updated_filter.update(bounds)
        container["close"] = updated_filter

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

    def _collect_symbols_from_screener(
        self,
        payload: dict[str, Any],
        base_query_params: dict[str, str],
        start_page: int,
        per_page: int,
    ) -> List[str]:
        seen: set[str] = set()
        collected: List[str] = []
        current_page = start_page
        total_pages: int | None = None

        while True:
            params = dict(base_query_params)
            params["page"] = str(current_page)

            response_payload = self._call_screener_api(params, payload)
            page_symbols = self._extract_ticker_symbols(response_payload)
            for symbol in page_symbols:
                normalized = symbol.upper()
                if normalized not in seen:
                    seen.add(normalized)
                    collected.append(normalized)

            total_pages = total_pages or self._extract_total_pages(response_payload)
            data_section = response_payload.get("data", [])
            data_count = len(data_section) if isinstance(data_section, list) else 0

            if total_pages is not None and current_page >= total_pages:
                break

            if data_count < per_page or data_count == 0:
                break

            current_page += 1

        if not collected:
            raise CommandError(
                "Seeking Alpha API response did not include any ticker symbols."
            )

        return collected

    def _call_screener_api(
        self, params: dict[str, str], payload: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response = requests.post(
                API_URL,
                headers=API_HEADERS,
                params=params,
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
            return response.json()
        except ValueError as exc:
            raise CommandError("Received invalid JSON from Seeking Alpha API") from exc

    def _extract_total_pages(self, payload: Any) -> int | None:
        if not isinstance(payload, dict):
            return None

        meta = payload.get("meta")
        if not isinstance(meta, dict):
            return None

        page_info = meta.get("page") or meta.get("pagination")
        if not isinstance(page_info, dict):
            return None

        potential_keys = ("total_pages", "totalPages", "total")
        for key in potential_keys:
            raw_value = page_info.get(key)
            total_pages = self._coerce_int(raw_value)
            if total_pages and total_pages > 0:
                return total_pages

        return None

    def _extract_ticker_symbols(self, payload: Any) -> List[str]:
        if not isinstance(payload, dict):
            raise CommandError(
                "Seeking Alpha API returned an unexpected payload structure."
            )

        data = payload.get("data", [])
        if not isinstance(data, list):
            raise CommandError(
                "Seeking Alpha API returned an unexpected payload structure."
            )

        symbols: List[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue

            potential_symbol = item.get("id")
            if isinstance(potential_symbol, str) and potential_symbol:
                symbols.append(potential_symbol.upper())
                continue

            attributes = item.get("attributes", {})
            attribute_symbol = None
            if isinstance(attributes, dict):
                attribute_symbol = self._extract_symbol_from_attributes(attributes)

            if attribute_symbol:
                symbols.append(attribute_symbol)
                continue

            potential_symbol = item.get("id")
            if isinstance(potential_symbol, str) and potential_symbol:
                symbols.append(potential_symbol.upper())

        return symbols

    def _extract_symbol_from_attributes(self, attributes: dict[str, Any]) -> str | None:
        candidates = (
            attributes.get("symbol"),
            attributes.get("ticker"),
            attributes.get("name"),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip().upper()

        profile = attributes.get("p", {})
        if isinstance(profile, dict):
            profile_symbol = profile.get("symbol") or profile.get("ticker")
            if isinstance(profile_symbol, str) and profile_symbol.strip():
                return profile_symbol.strip().upper()

        return None

    def _fetch_symbol_profiles(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        symbol_list = [symbol for symbol in symbols if symbol]
        if not symbol_list:
            return {}

        params = {"symbols": ",".join(symbol_list)}

        try:
            response = requests.get(
                PROFILE_API_URL,
                headers=API_HEADERS,
                params=params,
                timeout=30,
            )
        except requests.RequestException as exc:  # pragma: no cover - network failure
            raise CommandError(f"Failed to call Seeking Alpha profile API: {exc}") from exc

        if response.status_code != 200:
            raise CommandError(
                "Received unexpected status code "
                f"{response.status_code} from profile API: {response.text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CommandError(
                "Received invalid JSON from Seeking Alpha profile API"
            ) from exc

        return self._parse_profile_payload(payload)

    def _parse_profile_payload(self, payload: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise CommandError(
                "Seeking Alpha profile API returned an unexpected payload structure."
            )

        data = payload.get("data", [])
        if not isinstance(data, list):
            raise CommandError(
                "Seeking Alpha profile API returned an unexpected payload structure."
            )

        profiles: dict[str, dict[str, Any]] = {}
        for item in data:
            symbol = self._extract_symbol_from_profile_item(item)
            if not symbol:
                continue

            attributes = item.get("attributes", {}) if isinstance(item, dict) else {}
            if not isinstance(attributes, dict):
                attributes = {}

            profiles[symbol] = attributes

        return profiles

    def _extract_symbol_from_profile_item(self, item: Any) -> str | None:
        if not isinstance(item, dict):
            return None

        attributes = item.get("attributes")
        if isinstance(attributes, dict):
            for key in ("symbol", "ticker"):
                candidate = attributes.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip().upper()

        raw_symbol = item.get("id")
        if isinstance(raw_symbol, str) and raw_symbol.strip():
            return raw_symbol.strip().upper()

        return None

    def _create_or_update_investments(
        self,
        screener: ScreenerType,
        symbols: Iterable[str],
        profiles: dict[str, dict[str, Any]],
    ) -> List[str]:
        updated: List[str] = []
        description = screener.description or ""
        category = screener.name

        for symbol in symbols:
            if not symbol:
                continue
            profile = profiles.get(symbol.upper(), {}) or {}

            last_price = self._coerce_decimal(profile.get("last"))
            volume = self._coerce_int(profile.get("volume"))
            market_cap_value = profile.get("marketCap")
            if market_cap_value is None:
                market_cap_value = profile.get("market_cap")
            market_cap = self._coerce_decimal(market_cap_value)

            Investment.objects.update_or_create(
                ticker=symbol.upper(),
                defaults={
                    "category": category,
                    "price": last_price,
                    "volume": volume,
                    "market_cap": market_cap,
                    "description": description,
                },
            )
            updated.append(symbol.upper())

        return updated

    def _coerce_decimal(self, value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError):
            return None

    def _coerce_int(self, value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

