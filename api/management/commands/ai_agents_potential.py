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
        "key": "ai_sector_tailwinds",
        "title": "AI & sector tailwinds",
        "dot_color": "teal",
        "tone": "neutral",
        "tag": None,
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
        "key": "ai_tailwinds",
        "label": "Agent 03 - AI context and tailwinds",
        "prompt": lambda ticker, score, ctx: (
            f"You are a senior technology and equity analyst. Research the stock {ticker} "
            f"and analyze its position in the context of ARTIFICIAL INTELLIGENCE.\n\n{ctx}\n\n"
            "Focus on:\n"
            "1. How AI is affecting this company's core business\n"
            "2. The company's own AI products, investments, or strategy\n"
            "3. AI-driven demand opportunities for this sector\n"
            "4. Competitive threats from AI-native players\n\n"
            + (
                f"Assess how AI dynamics might shift or reinforce "
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


def generate_text(
    client: OpenAI,
    prompt: str,
    model: str,
    max_output_tokens: int,
    use_web_search: bool = False,
) -> str:
    if hasattr(client, "responses"):
        request: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
        }
        if use_web_search:
            request["tools"] = [{"type": "web_search_preview"}]

        response = client.responses.create(**request)
        return extract_text_from_response(response)

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_output_tokens,
    )
    return extract_text_from_response(response)


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
    ai = by_key.get("ai_tailwinds")

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
            "key": "ai_sector_tailwinds",
            "title": "AI & sector tailwinds",
            "summary": first_available(
                ai["summary"] if ai else "",
                *(sector["bull_points"] if sector else []),
                sector["summary"] if sector else "",
            ),
            "tone": "neutral",
            "dot_color": "teal",
            "tag": None,
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
) -> dict[str, Any]:
    prompt = agent["prompt"](ticker, score, ctx)
    text = generate_text(
        client=client,
        prompt=prompt,
        model=AGENT_MODEL,
        max_output_tokens=512,
        use_web_search=True,
    )
    payload = extract_json_payload(text)
    return normalize_agent_payload(agent, payload, text)


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
        '      "key": "ai_sector_tailwinds",\n'
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
        "- No markdown and no extra keys."
    )
    text = generate_text(
        client=client,
        prompt=prompt,
        model=synthesis_model,
        max_output_tokens=700,
    )
    payload = extract_json_payload(text)
    return normalize_panel_payload(ticker, score, payload, results, fallback_panel)


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
        "Analyzes business potential, sector growth, and AI tailwinds, "
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

    def handle(self, *args, **options):
        ticker = options["ticker"].upper().strip()
        score = options["score"]
        synthesis_model = options["synthesis_model"]
        no_synthesis = options["no_synthesis"]
        workers = options["workers"]
        output_format = options["format"]
        save_to_db = options["save_to_db"]
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
            f"(model: {AGENT_MODEL}) ...{RESET}\n"
        )

        results: list[dict[str, Any]] = []
        errors: list[str] = []

        def _run(agent: dict[str, Any]) -> dict[str, Any]:
            start = time.time()
            try:
                result = run_agent(client, agent, ticker, score, ctx)
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

        if output_format == "json":
            print_synthesis(panel, panel_model_name, stream=terminal_stream)
            self.stdout.write(json.dumps(panel, indent=2))
        else:
            print_synthesis(panel, panel_model_name, stream=terminal_stream)
            self.stdout.write(f"{GREEN}  Done.{RESET}\n")
