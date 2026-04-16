"""
Django management command for stock research via OpenAI agents.

Usage:
    python manage.py ai_agents_potential NVDA --score 74
    python manage.py ai_agents_potential MSFT --score 81 --format json --save-to-db
"""

import concurrent.futures
import json
import sys
import time
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from openai import OpenAI


AGENT_MODEL = "gpt-4o-mini"
SYNTHESIS_MODEL = "gpt-4o-mini"
DEFAULT_SEARCH_TOOL = "web_search"

MODEL_PRICING_PER_1M_TOKENS = {
    "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
}
WEB_SEARCH_COST_PER_CALL = {
    "web_search": 0.01,
    "web_search_preview": 0.025,
}
WEB_SEARCH_FIXED_INPUT_TOKENS = {
    ("web_search", "gpt-4o-mini"): 8_000,
    ("web_search", "gpt-4.1-mini"): 8_000,
}

ALIGNMENT_CHOICES = {"supports", "contradicts", "nuances", "mixed", "not_applicable"}
STANCE_CHOICES = {"bullish", "neutral", "bearish"}
TONE_CHOICES = {"bull", "neutral", "risk"}
DOT_COLORS = {"green", "blue", "teal", "red", "amber"}

PANEL_INSIGHT_SPECS = [
    {
        "key": "business_moat",
        "title": "Business & moat",
        "dot_color": "green",
        "tone": "bull",
        "tag": "Competitive moat",
    },
    {
        "key": "revenue_growth",
        "title": "Revenue growth",
        "dot_color": "blue",
        "tone": "bull",
        "tag": "Growth driver",
    },
    {
        "key": "ai_disruption_risk",
        "title": "AI disruption risk",
        "dot_color": "amber",
        "tone": "risk",
        "tag": "Disruption watch",
    },
    {
        "key": "key_risks",
        "title": "Key risks",
        "dot_color": "red",
        "tone": "risk",
        "tag": "Monitor closely",
    },
]


def score_label(score: int) -> str:
    if score < 40:
        return "Weak"
    if score < 55:
        return "Fair"
    if score < 70:
        return "Good"
    if score < 85:
        return "Strong"
    return "Excellent"


def score_context(ticker: str, score: int | None) -> str:
    if score is None:
        return "No fundamental score provided. Conduct a standalone qualitative analysis."
    return (
        f"Context: The investor has already analyzed {ticker}'s financial statements "
        f"over the last 5 years using a proprietary fundamental scoring system (scale 0-100). "
        f"The stock received a score of {score}/100 ({score_label(score)} fundamentals). "
        "Your qualitative research should complement this quantitative assessment."
    )


AGENTS = [
    {
        "id": 0,
        "key": "business_potential",
        "label": "Agent 01 - Business potential",
        "prompt": lambda ticker, score, ctx: (
            f"You are a senior equity analyst. Research the stock {ticker} "
            f"and analyze its BUSINESS POTENTIAL.\n\n{ctx}\n\n"
            "Focus on:\n"
            "1. Core business model and competitive moat\n"
            "2. Revenue growth trajectory and key drivers\n"
            "3. Management quality and capital allocation\n"
            "4. Major risks to the bull case\n\n"
            + (
                f"Comment on whether your findings support, challenge, "
                f"or add nuance to the fundamental score of {score}/100.\n\n"
                if score is not None
                else ""
            )
            + "Return valid JSON only with this schema:\n"
            "{\n"
            '  "summary": "1-2 short sentences, max 220 chars",\n'
            '  "bull_points": ["short point", "short point"],\n'
            '  "risk_points": ["short point", "short point"],\n'
            '  "score_alignment": "supports|contradicts|nuances|mixed|not_applicable"\n'
            "}\n"
            "Keep each bullet under 110 characters. No markdown. No extra keys."
        ),
    },
    {
        "id": 1,
        "key": "sector_growth",
        "label": "Agent 02 - Sector growth",
        "prompt": lambda ticker, score, ctx: (
            f"You are a senior equity analyst. Research the stock {ticker} "
            f"and analyze its SECTOR AND INDUSTRY GROWTH POTENTIAL.\n\n{ctx}\n\n"
            "Focus on:\n"
            "1. Current market size and projected CAGR\n"
            "2. Key structural tailwinds driving the sector\n"
            f"3. Competitive landscape and {ticker}'s positioning\n"
            "4. Regulatory or macro risks to the sector\n\n"
            + (
                f"Comment on whether sector dynamics support or contradict "
                f"a fundamental score of {score}/100.\n\n"
                if score is not None
                else ""
            )
            + "Return valid JSON only with this schema:\n"
            "{\n"
            '  "summary": "1-2 short sentences, max 220 chars",\n'
            '  "bull_points": ["short point", "short point"],\n'
            '  "risk_points": ["short point", "short point"],\n'
            '  "score_alignment": "supports|contradicts|nuances|mixed|not_applicable"\n'
            "}\n"
            "Keep each bullet under 110 characters. No markdown. No extra keys."
        ),
    },
    {
        "id": 2,
        "key": "ai_disruption_risk",
        "label": "Agent 03 - AI disruption risk",
        "prompt": lambda ticker, score, ctx: (
            f"You are a senior technology and equity analyst. Research the stock {ticker} "
            f"and analyze its DISRUPTION RISK FROM AI AND AI AGENTS.\n\n{ctx}\n\n"
            "Focus on:\n"
            "1. How AI or AI agents could reduce demand for the company's core products or seats\n"
            "2. Whether AI lowers barriers to entry or weakens pricing power and differentiation\n"
            "3. Whether customer budgets may shift toward AI infrastructure or automation instead\n"
            "4. What defenses or offsets the company has against AI-native or agentic disruption\n\n"
            + (
                f"Assess whether AI disruption risk might weaken or reinforce "
                f"a fundamental score of {score}/100.\n\n"
                if score is not None
                else ""
            )
            + "Return valid JSON only with this schema:\n"
            "{\n"
            '  "summary": "1-2 short sentences, max 220 chars",\n'
            '  "bull_points": ["short point", "short point"],\n'
            '  "risk_points": ["short point", "short point"],\n'
            '  "score_alignment": "supports|contradicts|nuances|mixed|not_applicable"\n'
            "}\n"
            "Keep each bullet under 110 characters. No markdown. No extra keys."
        ),
    },
]


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"


def print_divider(
    char: str = "-",
    width: int = 72,
    color: str = DIM,
    *,
    stream: Any = None,
) -> None:
    print(f"{color}{char * width}{RESET}", file=stream or sys.stdout)


def print_header(ticker: str, score: int | None, *, stream: Any = None) -> None:
    output = stream or sys.stdout
    print(file=output)
    print_divider("=", color=CYAN, stream=output)
    score_str = (
        f"  |  Fundamental score: {BOLD}{score}/100 - {score_label(score)}{RESET}"
        if score is not None
        else ""
    )
    print(
        f"{BOLD}{CYAN}  Stock Research Agents  {RESET}{DIM}|  {ticker}{RESET}{score_str}",
        file=output,
    )
    print_divider("=", color=CYAN, stream=output)
    print(file=output)


def print_agent_result(result: dict[str, Any], elapsed: float, *, stream: Any = None) -> None:
    output = stream or sys.stdout
    print(
        f"{BOLD}{GREEN}OK {result['label']}{RESET}  {DIM}({elapsed:.1f}s){RESET}",
        file=output,
    )
    print_divider(stream=output)
    print(f"  Summary: {result['summary']}", file=output)
    if result["bull_points"]:
        print(f"  Upside: {', '.join(result['bull_points'])}", file=output)
    if result["risk_points"]:
        print(f"  Risks: {', '.join(result['risk_points'])}", file=output)
    print(f"  Score alignment: {result['score_alignment']}", file=output)
    print(file=output)


def print_synthesis(panel: dict[str, Any], model: str, *, stream: Any = None) -> None:
    output = stream or sys.stdout
    print_divider("=", color=YELLOW, stream=output)
    print(
        f"{BOLD}{YELLOW}  Frontend Summary{RESET}  {DIM}(via {model}){RESET}",
        file=output,
    )
    print_divider("=", color=YELLOW, stream=output)
    print(file=output)
    print(f"  Executive summary: {panel['executive_summary']}", file=output)
    print(file=output)
    for insight in panel["insights"]:
        tag = f" [{insight['tag']}]" if insight.get("tag") else ""
        print(f"  {BOLD}{insight['title']}{RESET}{DIM}{tag}{RESET}", file=output)
        print(f"    {insight['summary']}", file=output)
    print(file=output)
    print(
        f"  Overall stance: {panel['overall_stance']}  |  "
        f"Score alignment: {panel['score_alignment']}  |  "
        f"Confidence: {panel['confidence']:.2f}",
        file=output,
    )
    print(file=output)
    print_divider("=", color=YELLOW, stream=output)
    print(file=output)


def collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").split())


def truncate_text(value: Any, limit: int, fallback: str = "") -> str:
    text = collapse_whitespace(value)
    if not text:
        return fallback
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip(" ,;:.") + "..."


def coerce_string_list(value: Any, *, limit: int = 2, item_limit: int = 110) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []

    cleaned: list[str] = []
    for item in items:
        text = truncate_text(item, item_limit)
        if text:
            cleaned.append(text)
        if len(cleaned) >= limit:
            break
    return cleaned


def normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    normalized = collapse_whitespace(value).lower().replace(" ", "_")
    return normalized if normalized in allowed else fallback


def clamp_confidence(value: Any, fallback: float) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(1.0, round(confidence, 2)))


def extract_json_payload(text: str) -> Any:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(cleaned):
        if char not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(cleaned[index:])
            return payload
        except ValueError:
            continue

    raise ValueError("Model response did not contain valid JSON.")


def extract_text_from_response(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text.strip()

    texts: list[str] = []

    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", None)
            if text:
                texts.append(text)

    if texts:
        return "\n".join(texts).strip()

    choices = getattr(response, "choices", None)
    if choices:
        message = choices[0].message
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            content_texts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    content_texts.append(part.get("text", ""))
                else:
                    text = getattr(part, "text", None)
                    if text:
                        content_texts.append(text)
            if content_texts:
                return "\n".join(content_texts).strip()

    raise ValueError("Model response did not contain any text output.")


def usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return usage
    if hasattr(usage, "model_dump"):
        dumped = usage.model_dump()
        if isinstance(dumped, dict):
            return dumped

    data: dict[str, Any] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "input_tokens_details",
        "output_tokens_details",
    ):
        value = getattr(usage, key, None)
        if value is None:
            continue
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        data[key] = value
    return data


def safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def count_web_search_calls(response: Any) -> int:
    output_items = getattr(response, "output", None)
    if output_items is None and hasattr(response, "model_dump"):
        output_items = response.model_dump().get("output")
    if not output_items:
        return 0

    total = 0
    for item in output_items:
        item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        if isinstance(item_type, str) and "web_search" in item_type:
            total += 1
    return total


def extract_usage_metrics(response: Any, model: str, use_web_search: bool) -> dict[str, Any]:
    usage = usage_to_dict(getattr(response, "usage", None))
    input_details = usage_to_dict(usage.get("input_tokens_details"))
    output_details = usage_to_dict(usage.get("output_tokens_details"))
    web_search_calls = count_web_search_calls(response) if use_web_search else 0

    return {
        "model": model,
        "input_tokens": safe_int(usage.get("input_tokens") or usage.get("prompt_tokens")),
        "output_tokens": safe_int(usage.get("output_tokens") or usage.get("completion_tokens")),
        "cached_input_tokens": safe_int(
            input_details.get("cached_tokens") or usage.get("cached_input_tokens")
        ),
        "reasoning_tokens": safe_int(output_details.get("reasoning_tokens")),
        "web_search_calls": web_search_calls,
    }


def normalize_generation_result(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        return {"text": result, "usage": None}
    if isinstance(result, dict):
        return {"text": str(result.get("text", "")).strip(), "usage": result.get("usage")}
    raise ValueError("Unsupported generation result format.")


def calculate_usage_cost(usage_items: list[dict[str, Any]]) -> dict[str, float]:
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "web_search_calls": 0,
        "billed_search_input_tokens": 0,
    }
    total_cost = 0.0

    for usage in usage_items:
        pricing = MODEL_PRICING_PER_1M_TOKENS.get(usage.get("model"))
        if pricing:
            input_tokens = safe_int(usage.get("input_tokens"))
            cached_input_tokens = min(input_tokens, safe_int(usage.get("cached_input_tokens")))
            uncached_input_tokens = max(input_tokens - cached_input_tokens, 0)
            output_tokens = safe_int(usage.get("output_tokens"))

            totals["input_tokens"] += uncached_input_tokens
            totals["cached_input_tokens"] += cached_input_tokens
            totals["output_tokens"] += output_tokens

            total_cost += (uncached_input_tokens / 1_000_000) * pricing["input"]
            total_cost += (cached_input_tokens / 1_000_000) * pricing["cached_input"]
            total_cost += (output_tokens / 1_000_000) * pricing["output"]

        web_search_calls = safe_int(usage.get("web_search_calls"))
        search_tool_type = str(usage.get("search_tool_type") or "")
        totals["web_search_calls"] += web_search_calls
        total_cost += web_search_calls * WEB_SEARCH_COST_PER_CALL.get(search_tool_type, 0.0)

        billed_search_input_tokens = (
            WEB_SEARCH_FIXED_INPUT_TOKENS.get((search_tool_type, str(usage.get("model"))), 0)
            * web_search_calls
        )
        totals["billed_search_input_tokens"] += billed_search_input_tokens
        if pricing and billed_search_input_tokens:
            total_cost += (billed_search_input_tokens / 1_000_000) * pricing["input"]

    return {
        "input_tokens": float(totals["input_tokens"]),
        "cached_input_tokens": float(totals["cached_input_tokens"]),
        "output_tokens": float(totals["output_tokens"]),
        "web_search_calls": float(totals["web_search_calls"]),
        "billed_search_input_tokens": float(totals["billed_search_input_tokens"]),
        "estimated_cost_usd": round(total_cost, 6),
    }


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


def generate_text(
    client: OpenAI,
    prompt: str,
    model: str,
    max_output_tokens: int,
    use_web_search: bool = False,
    search_tool_type: str = DEFAULT_SEARCH_TOOL,
) -> dict[str, Any]:
    if hasattr(client, "responses"):
        request: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
        }
        if use_web_search:
            request["tools"] = [{"type": search_tool_type}]

        response = client.responses.create(**request)
        usage = extract_usage_metrics(response, model, use_web_search)
        if use_web_search:
            usage["search_tool_type"] = search_tool_type
        return {
            "text": extract_text_from_response(response),
            "usage": usage,
        }

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_output_tokens,
    )
    usage = usage_to_dict(getattr(response, "usage", None))
    return {
        "text": extract_text_from_response(response),
        "usage": {
            "model": model,
            "input_tokens": safe_int(usage.get("prompt_tokens")),
            "output_tokens": safe_int(usage.get("completion_tokens")),
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "web_search_calls": 0,
        },
    }


def infer_score_alignment(results: list[dict[str, Any]]) -> str:
    alignments = [result["score_alignment"] for result in results if result.get("score_alignment")]
    meaningful = [value for value in alignments if value != "not_applicable"]
    if not meaningful:
        return "not_applicable"
    unique = set(meaningful)
    if len(unique) == 1:
        return meaningful[0]
    if "supports" in unique and "contradicts" in unique:
        return "mixed"
    if "nuances" in unique:
        return "nuances"
    return "mixed"


def infer_overall_stance(results: list[dict[str, Any]]) -> str:
    bull_signals = sum(len(result["bull_points"]) for result in results)
    risk_signals = sum(len(result["risk_points"]) for result in results)
    alignment = infer_score_alignment(results)
    if alignment == "contradicts" or risk_signals > bull_signals + 1:
        return "bearish"
    if bull_signals > risk_signals:
        return "bullish"
    return "neutral"


def normalize_agent_payload(agent: dict[str, Any], payload: Any, raw_text: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Agent payload must be a JSON object.")

    return {
        "id": agent["id"],
        "key": agent["key"],
        "label": agent["label"],
        "summary": truncate_text(payload.get("summary"), 220, truncate_text(raw_text, 220, "No summary available.")),
        "bull_points": coerce_string_list(payload.get("bull_points")),
        "risk_points": coerce_string_list(payload.get("risk_points")),
        "score_alignment": normalize_choice(
            payload.get("score_alignment"),
            ALIGNMENT_CHOICES,
            "not_applicable",
        ),
        "raw_text": raw_text,
    }


def build_fallback_panel(
    ticker: str,
    score: int | None,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    by_key = {result["key"]: result for result in results}
    business = by_key.get("business_potential")
    sector = by_key.get("sector_growth")
    ai = by_key.get("ai_disruption_risk")

    def first_available(*values: str) -> str:
        for value in values:
            text = truncate_text(value, 220)
            if text:
                return text
        return "No summary available."

    risk_fragments: list[str] = []
    for result in results:
        risk_fragments.extend(result.get("risk_points", []))

    insights = [
        {
            "key": "business_moat",
            "title": "Business & moat",
            "summary": first_available(
                business["summary"] if business else "",
                *(business["bull_points"] if business else []),
            ),
            "tone": "bull",
            "dot_color": "green",
            "tag": "Competitive moat",
        },
        {
            "key": "revenue_growth",
            "title": "Revenue growth",
            "summary": first_available(
                *(business["bull_points"] if business else []),
                sector["summary"] if sector else "",
            ),
            "tone": "bull",
            "dot_color": "blue",
            "tag": "Growth driver",
        },
        {
            "key": "ai_disruption_risk",
            "title": "AI disruption risk",
            "summary": first_available(
                ai["summary"] if ai else "",
                *(ai["risk_points"] if ai else []),
                *(ai["bull_points"] if ai else []),
            ),
            "tone": "risk",
            "dot_color": "amber",
            "tag": "Disruption watch",
        },
        {
            "key": "key_risks",
            "title": "Key risks",
            "summary": first_available(
                " ".join(risk_fragments),
                sector["summary"] if sector else "",
                ai["summary"] if ai else "",
            ),
            "tone": "risk",
            "dot_color": "red",
            "tag": "Monitor closely",
        },
    ]

    executive_summary = truncate_text(
        " ".join(
            part
            for part in [
                business["summary"] if business else "",
                sector["summary"] if sector else "",
                ai["summary"] if ai else "",
            ]
            if part
        ),
        320,
        f"{ticker} research generated from {len(results)} agent result(s).",
    )

    return {
        "report_kind": "frontend_analysis_panel",
        "version": 1,
        "ticker": ticker,
        "fundamental_score": score,
        "fundamental_label": score_label(score) if score is not None else None,
        "executive_summary": executive_summary,
        "overall_stance": infer_overall_stance(results),
        "score_alignment": infer_score_alignment(results),
        "confidence": round(min(0.55 + (0.1 * len(results)), 0.85), 2),
        "insights": insights,
        "agents": [
            {
                "key": result["key"],
                "label": result["label"],
                "summary": result["summary"],
                "bull_points": result["bull_points"],
                "risk_points": result["risk_points"],
                "score_alignment": result["score_alignment"],
                "elapsed_seconds": round(float(result.get("elapsed", 0.0)), 2),
            }
            for result in results
        ],
        "generated_at": timezone.now().isoformat(),
    }


def normalize_panel_payload(
    ticker: str,
    score: int | None,
    payload: Any,
    results: list[dict[str, Any]],
    fallback_panel: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return fallback_panel

    fallback_insights = {
        item["key"]: item for item in fallback_panel["insights"] if isinstance(item, dict)
    }
    incoming_insights = {
        item.get("key"): item
        for item in payload.get("insights", [])
        if isinstance(item, dict) and item.get("key")
    }

    normalized_insights: list[dict[str, Any]] = []
    for spec in PANEL_INSIGHT_SPECS:
        incoming = incoming_insights.get(spec["key"], {})
        fallback = fallback_insights.get(spec["key"], {})
        normalized_insights.append(
            {
                "key": spec["key"],
                "title": spec["title"],
                "summary": truncate_text(
                    incoming.get("summary"),
                    220,
                    fallback.get("summary", "No summary available."),
                ),
                "tone": normalize_choice(incoming.get("tone"), TONE_CHOICES, spec["tone"]),
                "dot_color": normalize_choice(
                    incoming.get("dot_color"),
                    DOT_COLORS,
                    spec["dot_color"],
                ),
                "tag": truncate_text(
                    incoming.get("tag"),
                    40,
                    fallback.get("tag") or spec["tag"] or "",
                )
                or None,
            }
        )

    return {
        "report_kind": "frontend_analysis_panel",
        "version": 1,
        "ticker": ticker,
        "fundamental_score": score,
        "fundamental_label": score_label(score) if score is not None else None,
        "executive_summary": truncate_text(
            payload.get("executive_summary"),
            320,
            fallback_panel["executive_summary"],
        ),
        "overall_stance": normalize_choice(
            payload.get("overall_stance"),
            STANCE_CHOICES,
            fallback_panel["overall_stance"],
        ),
        "score_alignment": normalize_choice(
            payload.get("score_alignment"),
            ALIGNMENT_CHOICES,
            fallback_panel["score_alignment"],
        ),
        "confidence": clamp_confidence(payload.get("confidence"), fallback_panel["confidence"]),
        "insights": normalized_insights,
        "agents": fallback_panel["agents"],
        "generated_at": timezone.now().isoformat(),
    }


def run_agent(
    client: OpenAI,
    agent: dict[str, Any],
    ticker: str,
    score: int | None,
    ctx: str,
    search_tool_type: str,
) -> dict[str, Any]:
    prompt = agent["prompt"](ticker, score, ctx)
    generation = normalize_generation_result(
        generate_text(
            client=client,
            prompt=prompt,
            model=AGENT_MODEL,
            max_output_tokens=512,
            use_web_search=True,
            search_tool_type=search_tool_type,
        )
    )
    text = generation["text"]
    payload = extract_json_payload(text)
    normalized = normalize_agent_payload(agent, payload, text)
    if generation["usage"]:
        normalized["usage"] = generation["usage"]
    return normalized


def run_synthesis(
    client: OpenAI,
    ticker: str,
    score: int | None,
    results: list[dict[str, Any]],
    synthesis_model: str,
) -> dict[str, Any]:
    fallback_panel = build_fallback_panel(ticker, score, results)
    combined = json.dumps(
        [
            {
                "label": result["label"],
                "summary": result["summary"],
                "bull_points": result["bull_points"],
                "risk_points": result["risk_points"],
                "score_alignment": result["score_alignment"],
            }
            for result in results
        ],
        indent=2,
    )
    score_line = (
        f"The investor's fundamental scoring system rated {ticker} "
        f"{score}/100 ({score_label(score)}).\n\n"
        if score is not None
        else ""
    )
    prompt = (
        f"{score_line}"
        f"Three AI agents produced the following compact research on {ticker}:\n\n"
        f"{combined}\n\n"
        "Prepare a compact frontend JSON payload for a stock analysis panel. "
        "Return valid JSON only with this schema:\n"
        "{\n"
        '  "executive_summary": "3-4 concise sentences, max 320 chars",\n'
        '  "overall_stance": "bullish|neutral|bearish",\n'
        '  "score_alignment": "supports|contradicts|nuances|mixed|not_applicable",\n'
        '  "confidence": 0.0,\n'
        '  "insights": [\n'
        "    {\n"
        '      "key": "business_moat",\n'
        '      "summary": "max 220 chars",\n'
        '      "tone": "bull|neutral|risk",\n'
        '      "dot_color": "green|blue|teal|red|amber",\n'
        '      "tag": "short label or null"\n'
        "    },\n"
        "    {\n"
        '      "key": "revenue_growth",\n'
        '      "summary": "max 220 chars",\n'
        '      "tone": "bull|neutral|risk",\n'
        '      "dot_color": "green|blue|teal|red|amber",\n'
        '      "tag": "short label or null"\n'
        "    },\n"
        "    {\n"
        '      "key": "ai_disruption_risk",\n'
        '      "summary": "max 220 chars",\n'
        '      "tone": "bull|neutral|risk",\n'
        '      "dot_color": "green|blue|teal|red|amber",\n'
        '      "tag": "short label or null"\n'
        "    },\n"
        "    {\n"
        '      "key": "key_risks",\n'
        '      "summary": "max 220 chars",\n'
        '      "tone": "bull|neutral|risk",\n'
        '      "dot_color": "green|blue|teal|red|amber",\n'
        '      "tag": "short label or null"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Requirements:\n"
        "- Exactly four insights with those keys in that order.\n"
        "- Make the wording dense, specific, and ready to render in a small UI card.\n"
        "- The AI insight must assess disruption risk from AI and AI agents, not generic tailwinds.\n"
        "- No markdown and no extra keys."
    )
    generation = normalize_generation_result(
        generate_text(
            client=client,
            prompt=prompt,
            model=synthesis_model,
            max_output_tokens=700,
        )
    )
    text = generation["text"]
    payload = extract_json_payload(text)
    panel = normalize_panel_payload(ticker, score, payload, results, fallback_panel)
    if generation["usage"]:
        panel["_usage"] = generation["usage"]
    return panel


def save_due_diligence_report(panel: dict[str, Any], model_name: str) -> None:
    from api.models import DueDiligenceReport

    rating_map = {
        "bullish": "BUY",
        "neutral": "HOLD",
        "bearish": "SELL",
    }

    DueDiligenceReport.objects.create(
        symbol=panel["ticker"],
        rating=rating_map.get(panel["overall_stance"], "HOLD"),
        confidence=panel["confidence"],
        model_name=model_name,
        report=panel,
    )


class Command(BaseCommand):
    help = (
        "Run parallel AI research agents on a stock ticker. "
        "Analyzes business potential, sector growth, and AI disruption risk, "
        "factoring in your existing fundamental score."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "ticker",
            type=str,
            help="Stock ticker symbol (e.g. NVDA, MSFT, AAPL)",
        )
        parser.add_argument(
            "--score",
            type=int,
            default=None,
            metavar="0-100",
            help="Your fundamental score from the financial statement analysis (0-100).",
        )
        parser.add_argument(
            "--synthesis-model",
            type=str,
            default=SYNTHESIS_MODEL,
            metavar="MODEL",
            help=(
                "Model used for the final executive synthesis. "
                f"Default: {SYNTHESIS_MODEL}"
            ),
        )
        parser.add_argument(
            "--no-synthesis",
            action="store_true",
            help="Skip the model-based synthesis step and build the panel locally.",
        )
        parser.add_argument(
            "--workers",
            type=int,
            default=3,
            help="Number of parallel threads for agent calls (default: 3).",
        )
        parser.add_argument(
            "--format",
            choices=["text", "json"],
            default="text",
            help="Output format. Use json for frontend consumption.",
        )
        parser.add_argument(
            "--save-to-db",
            action="store_true",
            help="Save the summarized frontend payload into DueDiligenceReport.report.",
        )
        parser.add_argument(
            "--search-tool",
            choices=["web_search", "web_search_preview"],
            default=DEFAULT_SEARCH_TOOL,
            help=(
                "Responses API web search tool to use for the research agents. "
                f"Default: {DEFAULT_SEARCH_TOOL}"
            ),
        )

    def handle(self, *args, **options):
        ticker = options["ticker"].upper().strip()
        score = options["score"]
        synthesis_model = options["synthesis_model"]
        no_synthesis = options["no_synthesis"]
        workers = options["workers"]
        output_format = options["format"]
        save_to_db = options["save_to_db"]
        search_tool_type = options["search_tool"]
        status_stream = self.stderr if output_format == "json" else self.stdout
        terminal_stream = self.stderr if output_format == "json" else self.stdout

        if score is not None and not (0 <= score <= 100):
            raise CommandError("--score must be between 0 and 100.")
        if workers < 1:
            raise CommandError("--workers must be at least 1.")

        try:
            api_key = getattr(settings, "OPENAI_API_KEY", None)
            client = OpenAI(api_key=api_key) if api_key else OpenAI()
        except Exception as exc:
            raise CommandError(f"Failed to initialize OpenAI client: {exc}")

        ctx = score_context(ticker, score)

        print_header(ticker, score, stream=terminal_stream)

        status_stream.write(
            f"{DIM}  Running {len(AGENTS)} agents in parallel "
            f"(model: {AGENT_MODEL}, search: {search_tool_type}) ...{RESET}\n"
        )

        results: list[dict[str, Any]] = []
        errors: list[str] = []
        usage_items: list[dict[str, Any]] = []

        def _run(agent: dict[str, Any]) -> dict[str, Any]:
            start = time.time()
            try:
                result = run_agent(client, agent, ticker, score, ctx, search_tool_type)
                result["elapsed"] = time.time() - start
                return result
            except Exception as exc:
                return {
                    "id": agent["id"],
                    "key": agent["key"],
                    "label": agent["label"],
                    "result": None,
                    "error": str(exc),
                    "elapsed": time.time() - start,
                }

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run, agent): agent for agent in AGENTS}
            for future in concurrent.futures.as_completed(futures):
                outcome = future.result()
                if outcome.get("error"):
                    errors.append(f"{outcome['label']}: {outcome['error']}")
                    status_stream.write(
                        f"{RED}ERR {outcome['label']} failed: {outcome['error']}{RESET}\n"
                    )
                else:
                    results.append(outcome)
                    if outcome.get("usage"):
                        usage_items.append(outcome["usage"])

        results.sort(key=lambda result: result["id"])

        if not results:
            raise CommandError("All agents failed. No report could be generated.")

        print(file=terminal_stream)
        for result in results:
            print_agent_result(result, result.get("elapsed", 0), stream=terminal_stream)

        if errors:
            status_stream.write(
                f"{YELLOW}  {len(errors)} agent(s) failed; "
                f"continuing with available results only.{RESET}\n"
            )

        panel = build_fallback_panel(ticker, score, results)
        panel_model_name = "local-fallback"

        if no_synthesis:
            status_stream.write(
                f"{DIM}  Synthesis skipped (--no-synthesis); using local panel builder.{RESET}\n"
            )
        elif len(results) >= 2:
            status_stream.write(
                f"{DIM}  Running frontend synthesis "
                f"(model: {synthesis_model}) ...{RESET}\n"
            )
            try:
                panel = run_synthesis(client, ticker, score, results, synthesis_model)
                panel_model_name = synthesis_model
                if panel.get("_usage"):
                    usage_items.append(panel["_usage"])
                    del panel["_usage"]
            except Exception as exc:
                status_stream.write(
                    f"{YELLOW}  Synthesis failed; using local fallback panel ({exc}).{RESET}\n"
                )
        else:
            status_stream.write(
                f"{YELLOW}  Not enough results for model synthesis "
                f"({len(results)}/3 agents succeeded); using local fallback panel.{RESET}\n"
            )

        if save_to_db:
            save_due_diligence_report(panel, f"{AGENT_MODEL} + {panel_model_name}")
            status_stream.write(f"{GREEN}  Saved summarized report to DueDiligenceReport.{RESET}\n")

        usage_summary = format_usage_summary(usage_items)
        if usage_summary:
            status_stream.write(f"{DIM}{usage_summary}{RESET}\n")

        if output_format == "json":
            print_synthesis(panel, panel_model_name, stream=terminal_stream)
            self.stdout.write(json.dumps(panel, indent=2))
        else:
            print_synthesis(panel, panel_model_name, stream=terminal_stream)
            self.stdout.write(f"{GREEN}  Done.{RESET}\n")
