from __future__ import annotations

from typing import Any

from django.conf import settings


DEFAULT_MODEL_PRICING_PER_1M_TOKENS = {
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
    "gpt-4.1-mini": {"input": 0.40, "cached_input": 0.10, "output": 1.60},
}

DEFAULT_WEB_SEARCH_COST_PER_CALL = {
    "web_search": 0.01,
    "web_search_preview": 0.025,
}

DEFAULT_WEB_SEARCH_FIXED_INPUT_TOKENS = {
    ("web_search", "gpt-4o-mini"): 8_000,
    ("web_search", "gpt-4.1-mini"): 8_000,
}


def _is_mock_like(value: Any) -> bool:
    return value.__class__.__module__.startswith("unittest.mock")


def usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None or _is_mock_like(usage):
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(exclude_none=True)
        if isinstance(dumped, dict):
            return dumped

    data: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "input_tokens_details",
        "output_tokens_details",
        "reasoning_tokens",
    ):
        value = getattr(usage, key, None)
        if value is None or _is_mock_like(value):
            continue
        nested_model_dump = getattr(value, "model_dump", None)
        if callable(nested_model_dump):
            dumped = nested_model_dump(exclude_none=True)
            if isinstance(dumped, dict):
                value = dumped
        data[key] = value
    return data


def safe_int(value: Any) -> int:
    if value is None or _is_mock_like(value):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def count_web_search_calls(response: Any) -> int:
    output_items = getattr(response, "output", None)
    if output_items is None or _is_mock_like(output_items):
        model_dump = getattr(response, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump(exclude_none=True)
            if isinstance(dumped, dict):
                output_items = dumped.get("output")
    if not output_items:
        return 0

    total = 0
    for item in output_items:
        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        if isinstance(item_type, str) and "web_search" in item_type:
            total += 1
    return total


def extract_openai_usage_metrics(
    response: Any,
    *,
    provider: str,
    model: str,
    prompt_key: str,
    iteration: int,
    use_web_search: bool = False,
    search_tool_type: str | None = None,
) -> dict[str, Any]:
    usage = usage_to_dict(getattr(response, "usage", None))
    input_details = usage_to_dict(usage.get("input_tokens_details"))
    output_details = usage_to_dict(usage.get("output_tokens_details"))
    web_search_calls = count_web_search_calls(response) if use_web_search else 0

    event = {
        "provider": provider,
        "model": model,
        "prompt_key": prompt_key,
        "iteration": iteration,
        "input_tokens": safe_int(usage.get("input_tokens") or usage.get("prompt_tokens")),
        "output_tokens": safe_int(usage.get("output_tokens") or usage.get("completion_tokens")),
        "cached_input_tokens": safe_int(
            input_details.get("cached_tokens") or usage.get("cached_input_tokens")
        ),
        "reasoning_tokens": safe_int(
            output_details.get("reasoning_tokens") or usage.get("reasoning_tokens")
        ),
        "web_search_calls": web_search_calls,
    }
    if search_tool_type:
        event["search_tool_type"] = search_tool_type
    event["estimated_cost_usd"] = calculate_usage_cost([event])["estimated_cost_usd"]
    return event


def extract_anthropic_usage_metrics(
    response: Any,
    *,
    model: str,
    prompt_key: str,
    iteration: int,
) -> dict[str, Any]:
    usage = usage_to_dict(getattr(response, "usage", None))
    event = {
        "provider": "anthropic",
        "model": model,
        "prompt_key": prompt_key,
        "iteration": iteration,
        "input_tokens": safe_int(usage.get("input_tokens")),
        "output_tokens": safe_int(usage.get("output_tokens")),
        "cached_input_tokens": safe_int(
            usage.get("cache_read_input_tokens") or usage.get("cached_input_tokens")
        ),
        "cache_creation_input_tokens": safe_int(usage.get("cache_creation_input_tokens")),
        "reasoning_tokens": 0,
        "web_search_calls": 0,
    }
    event["estimated_cost_usd"] = calculate_usage_cost([event])["estimated_cost_usd"]
    return event


def _pricing_table() -> dict[str, dict[str, float]]:
    configured = getattr(settings, "LLM_MODEL_PRICING_PER_1M_TOKENS", {}) or {}
    pricing = dict(DEFAULT_MODEL_PRICING_PER_1M_TOKENS)
    for key, value in configured.items():
        if isinstance(value, dict):
            pricing[str(key)] = value
    return pricing


def _web_search_costs() -> dict[str, float]:
    configured = getattr(settings, "LLM_WEB_SEARCH_COST_PER_CALL", {}) or {}
    costs = dict(DEFAULT_WEB_SEARCH_COST_PER_CALL)
    for key, value in configured.items():
        try:
            costs[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return costs


def _web_search_fixed_input_tokens() -> dict[tuple[str, str], int]:
    configured = getattr(settings, "LLM_WEB_SEARCH_FIXED_INPUT_TOKENS", {}) or {}
    fixed = dict(DEFAULT_WEB_SEARCH_FIXED_INPUT_TOKENS)
    for key, value in configured.items():
        if not isinstance(key, str) or ":" not in key:
            continue
        search_tool_type, model = key.split(":", 1)
        fixed[(search_tool_type, model)] = safe_int(value)
    return fixed


def _resolve_model_pricing(usage: dict[str, Any]) -> dict[str, float] | None:
    pricing = _pricing_table()
    provider = str(usage.get("provider") or "").strip()
    model = str(usage.get("model") or "").strip()
    if provider and model:
        provider_key = f"{provider}:{model}"
        if provider_key in pricing:
            return pricing[provider_key]
    return pricing.get(model)


def calculate_usage_cost(usage_items: list[dict[str, Any]]) -> dict[str, float]:
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "web_search_calls": 0,
        "billed_search_input_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    web_search_costs = _web_search_costs()
    fixed_search_input_tokens = _web_search_fixed_input_tokens()

    for usage in usage_items:
        input_tokens = safe_int(usage.get("input_tokens"))
        cached_input_tokens = min(input_tokens, safe_int(usage.get("cached_input_tokens")))
        uncached_input_tokens = max(input_tokens - cached_input_tokens, 0)
        output_tokens = safe_int(usage.get("output_tokens"))
        reasoning_tokens = safe_int(usage.get("reasoning_tokens"))
        web_search_calls = safe_int(usage.get("web_search_calls"))
        search_tool_type = str(usage.get("search_tool_type") or "")

        totals["input_tokens"] += uncached_input_tokens
        totals["cached_input_tokens"] += cached_input_tokens
        totals["output_tokens"] += output_tokens
        totals["reasoning_tokens"] += reasoning_tokens
        totals["web_search_calls"] += web_search_calls

        pricing = _resolve_model_pricing(usage)
        if pricing:
            totals["estimated_cost_usd"] += (uncached_input_tokens / 1_000_000) * float(
                pricing.get("input", 0.0)
            )
            totals["estimated_cost_usd"] += (cached_input_tokens / 1_000_000) * float(
                pricing.get("cached_input", pricing.get("input", 0.0))
            )
            totals["estimated_cost_usd"] += (output_tokens / 1_000_000) * float(
                pricing.get("output", 0.0)
            )

        totals["estimated_cost_usd"] += web_search_calls * web_search_costs.get(
            search_tool_type, 0.0
        )

        billed_search_input_tokens = fixed_search_input_tokens.get(
            (search_tool_type, str(usage.get("model") or "")),
            0,
        ) * web_search_calls
        totals["billed_search_input_tokens"] += billed_search_input_tokens
        if pricing and billed_search_input_tokens:
            totals["estimated_cost_usd"] += (billed_search_input_tokens / 1_000_000) * float(
                pricing.get("input", 0.0)
            )

    totals["estimated_cost_usd"] = round(float(totals["estimated_cost_usd"]), 6)
    return {key: float(value) for key, value in totals.items()}


def format_usage_summary(usage_items: list[dict[str, Any]]) -> str | None:
    if not usage_items:
        return None

    cost = calculate_usage_cost(usage_items)
    return (
        "  OpenAI usage: "
        f"{int(cost['input_tokens'])} input, "
        f"{int(cost['cached_input_tokens'])} cached input, "
        f"{int(cost['output_tokens'])} output, "
        f"{int(cost['web_search_calls'])} web search call(s)"
        + (
            f", {int(cost['billed_search_input_tokens'])} billed search input"
            if cost["billed_search_input_tokens"]
            else ""
        )
        + "  |  "
        f"Estimated cost: ${cost['estimated_cost_usd']:.4f}"
    )
