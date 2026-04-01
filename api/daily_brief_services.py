from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import DailyBrief, DailyBriefEdition, DailyBriefSubscription, Symbol


logger = logging.getLogger(__name__)
DAILY_BRIEF_ALLOWED_TECHNICALS = {"buy", "strong_buy"}
DAILY_BRIEF_MAX_ABS_DELTA = Decimal("0.3")


def get_or_create_subscription(user) -> DailyBriefSubscription:
    subscription, _ = DailyBriefSubscription.objects.get_or_create(user=user)
    return subscription


def subscribe_user(user, source: str) -> DailyBriefSubscription:
    subscription, created = DailyBriefSubscription.objects.get_or_create(user=user)
    previous_status = subscription.status
    previous_source = subscription.source
    target_status = (
        DailyBriefSubscription.Status.ACTIVE
        if user.is_active
        else DailyBriefSubscription.Status.PENDING_VERIFICATION
    )
    now = timezone.now()
    changed = created

    if created or previous_status == DailyBriefSubscription.Status.UNSUBSCRIBED:
        subscription.subscribed_at = now
        changed = True

    if subscription.unsubscribed_at is not None:
        subscription.unsubscribed_at = None
        changed = True

    if created or previous_status == DailyBriefSubscription.Status.UNSUBSCRIBED or not previous_source:
        if subscription.source != source:
            subscription.source = source
            changed = True

    target_is_active = target_status == DailyBriefSubscription.Status.ACTIVE
    if subscription.status != target_status:
        subscription.status = target_status
        changed = True
    if subscription.is_active != target_is_active:
        subscription.is_active = target_is_active
        changed = True

    if changed:
        subscription.save()

    logger.info(
        "Daily brief subscribe user_id=%s status=%s source=%s changed=%s",
        user.id,
        subscription.status,
        subscription.source or None,
        changed,
    )
    return subscription


def unsubscribe_user(user) -> DailyBriefSubscription:
    subscription, created = DailyBriefSubscription.objects.get_or_create(user=user)
    now = timezone.now()
    changed = created

    if subscription.status != DailyBriefSubscription.Status.UNSUBSCRIBED:
        subscription.status = DailyBriefSubscription.Status.UNSUBSCRIBED
        changed = True
    if subscription.is_active:
        subscription.is_active = False
        changed = True
    if subscription.unsubscribed_at is None:
        subscription.unsubscribed_at = now
        changed = True

    if changed:
        subscription.save()

    logger.info(
        "Daily brief unsubscribe user_id=%s changed=%s unsubscribed_at=%s",
        user.id,
        changed,
        subscription.unsubscribed_at,
    )
    return subscription


def activate_pending_subscription_after_verification(user) -> DailyBriefSubscription | None:
    subscription = DailyBriefSubscription.objects.filter(user=user).first()
    if subscription is None:
        return None
    if subscription.status != DailyBriefSubscription.Status.PENDING_VERIFICATION:
        return subscription

    subscription.status = DailyBriefSubscription.Status.ACTIVE
    subscription.is_active = True
    subscription.unsubscribed_at = None
    subscription.save()

    logger.info("Daily brief activation after verification user_id=%s", user.id)
    return subscription


def _format_decimal(value) -> str | None:
    if value is None:
        return None
    decimal_value = _to_decimal(value)
    if decimal_value is None:
        return str(value)
    rounded_value = decimal_value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(rounded_value, ".2f")


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalize_signal(value) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _get_option_data_value(option_data: dict, *keys: str):
    for key in keys:
        if key in option_data and option_data[key] is not None:
            return option_data[key]
    return None


def _extract_technical_signal(symbol: Symbol) -> str | None:
    option_data = symbol.option_data or {}
    signal = _normalize_signal(
        _get_option_data_value(
            option_data,
            "tvTechnicals",
            "tv_technicals",
            "technicals",
            "recommendation",
        )
    )
    if signal is not None:
        return signal
    return _normalize_signal(symbol.technical_score)


def _extract_spread_value(symbol: Symbol) -> Decimal | None:
    option_data = symbol.option_data or {}
    spread_value = _to_decimal(
        _get_option_data_value(
            option_data,
            "spreadValue",
            "spread_value",
            "bidAskSpread",
            "bid_ask_spread",
        )
    )
    if spread_value is not None:
        return abs(spread_value)

    bid_value = _to_decimal(_get_option_data_value(option_data, "bid"))
    ask_value = _to_decimal(_get_option_data_value(option_data, "ask"))
    if bid_value is None or ask_value is None:
        return None
    return abs(ask_value - bid_value)


def _extract_strike_value(symbol: Symbol) -> Decimal | None:
    option_data = symbol.option_data or {}
    return _to_decimal(
        _get_option_data_value(
            option_data,
            "rawStrike",
            "raw_strike",
            "strike_price",
            "strike",
        )
    )


def _extract_price_value(symbol: Symbol) -> Decimal | None:
    option_data = symbol.option_data or {}
    return _to_decimal(
        _get_option_data_value(
            option_data,
            "rawPrice",
            "raw_price",
            "stockPrice",
            "stock_price",
            "underlyingPrice",
            "underlying_price",
        )
    ) or _to_decimal(symbol.price)


def _extract_delta_percent(symbol: Symbol) -> Decimal | None:
    option_data = symbol.option_data or {}
    delta_value = _to_decimal(_get_option_data_value(option_data, "delta", "rawDelta"))
    if delta_value is None:
        return None
    delta_value = abs(delta_value)
    if delta_value <= Decimal("1"):
        return delta_value * Decimal("100")
    return delta_value


def _extract_chance_of_profit_percent(symbol: Symbol) -> Decimal | None:
    delta_percent = _extract_delta_percent(symbol)
    if delta_percent is None:
        return None
    return Decimal("100") - delta_percent


def _extract_roi_value(symbol: Symbol) -> Decimal | None:
    roi_value = _to_decimal(symbol.roi)
    if roi_value is not None:
        return roi_value

    option_data = symbol.option_data or {}
    return _to_decimal(_get_option_data_value(option_data, "roi"))


def _get_daily_brief_option_data(daily_brief: DailyBrief) -> dict:
    return daily_brief.option_data if isinstance(daily_brief.option_data, dict) else {}


def _extract_daily_brief_technical_signal(daily_brief: DailyBrief) -> str | None:
    option_data = _get_daily_brief_option_data(daily_brief)
    signal = _normalize_signal(
        _get_option_data_value(
            option_data,
            "tvTechnicals",
            "tv_technicals",
            "technicals",
            "recommendation",
        )
    )
    if signal is not None:
        return signal
    return _normalize_signal(daily_brief.symbol.technical_score)


def _extract_daily_brief_strike_value(daily_brief: DailyBrief) -> Decimal | None:
    option_data = _get_daily_brief_option_data(daily_brief)
    return _to_decimal(
        _get_option_data_value(
            option_data,
            "rawStrike",
            "raw_strike",
            "strike_price",
            "strike",
        )
    )


def _extract_daily_brief_price_value(daily_brief: DailyBrief) -> Decimal | None:
    option_data = _get_daily_brief_option_data(daily_brief)
    return _to_decimal(
        _get_option_data_value(
            option_data,
            "rawPrice",
            "raw_price",
            "stockPrice",
            "stock_price",
            "underlyingPrice",
            "underlying_price",
        )
    ) or _to_decimal(daily_brief.symbol.price)


def _extract_daily_brief_delta_percent(daily_brief: DailyBrief) -> Decimal | None:
    delta_value = _to_decimal(daily_brief.delta)
    if delta_value is None:
        option_data = _get_daily_brief_option_data(daily_brief)
        delta_value = _to_decimal(
            _get_option_data_value(option_data, "delta", "rawDelta", "raw_delta")
        )
    if delta_value is None:
        return None
    delta_value = abs(delta_value)
    if delta_value <= Decimal("1"):
        return delta_value * Decimal("100")
    return delta_value


def _extract_daily_brief_chance_of_profit_percent(daily_brief: DailyBrief) -> Decimal | None:
    delta_percent = _extract_daily_brief_delta_percent(daily_brief)
    if delta_percent is None:
        return None
    return Decimal("100") - delta_percent


def _extract_daily_brief_roi_value(daily_brief: DailyBrief) -> Decimal | None:
    roi_value = _to_decimal(daily_brief.roi)
    if roi_value is not None:
        return roi_value
    option_data = _get_daily_brief_option_data(daily_brief)
    return _to_decimal(_get_option_data_value(option_data, "roi"))


def _is_daily_brief_candidate(symbol: Symbol) -> bool:
    if symbol.score is None or symbol.score <= 80:
        return False
    if symbol.rsi is None or not (Decimal("30") <= symbol.rsi <= Decimal("70")):
        return False
    if _extract_technical_signal(symbol) not in DAILY_BRIEF_ALLOWED_TECHNICALS:
        return False

    spread_value = _extract_spread_value(symbol)
    if spread_value is None or spread_value >= Decimal("1.5"):
        return False

    strike_value = _extract_strike_value(symbol)
    price_value = _extract_price_value(symbol)
    if strike_value is None or price_value is None or strike_value >= price_value:
        return False

    delta_percent = _extract_delta_percent(symbol)
    if delta_percent is None or delta_percent >= Decimal("32"):
        return False

    return _extract_roi_value(symbol) is not None


def _get_daily_brief_rows(*, edition_date) -> list[DailyBrief]:
    daily_briefs = list(DailyBrief.edition_rows(edition_date=edition_date))
    if daily_briefs:
        return daily_briefs
    refresh_daily_brief(target_date=edition_date)
    return list(DailyBrief.edition_rows(edition_date=edition_date))


def _build_top_symbols_payload(*, edition_date) -> list[dict[str, str | int | None]]:
    daily_briefs = _get_daily_brief_rows(edition_date=edition_date)
    return [
        {
            "ticker": daily_brief.ticker,
            "score": daily_brief.score,
            "classification": daily_brief.symbol.classification,
            "technicals": (
                (_extract_daily_brief_technical_signal(daily_brief) or "")
                .replace("_", " ")
                .title()
            )
            or None,
            "price": _format_decimal(_extract_daily_brief_price_value(daily_brief)),
            "strike": _format_decimal(_extract_daily_brief_strike_value(daily_brief)),
            "chance_of_profit": _format_decimal(
                _extract_daily_brief_chance_of_profit_percent(daily_brief)
            ),
            "market_cap": _format_decimal(daily_brief.symbol.market_cap),
            "roi": _format_decimal(_extract_daily_brief_roi_value(daily_brief)),
        }
        for daily_brief in daily_briefs
    ]


def _build_daily_brief_defaults(*, edition_date) -> dict:
    top_symbols = _build_top_symbols_payload(edition_date=edition_date)
    subject = f"PutPulse Daily Top 3 | {edition_date:%Y-%m-%d}"
    body_text = render_to_string(
        "api/email/daily_brief.txt",
        {
            "edition_date": edition_date,
            "top_symbols": top_symbols,
            "frontend_base_url": settings.FRONTEND_BASE_URL.rstrip("/"),
        },
    )
    return {
        "subject": subject,
        "body_text": body_text,
        "top_symbols": top_symbols,
    }


def get_or_create_daily_brief_edition(*, target_date=None) -> DailyBriefEdition:
    edition_date = target_date or timezone.now().date()
    defaults = _build_daily_brief_defaults(edition_date=edition_date)
    try:
        edition, _ = DailyBriefEdition.objects.get_or_create(
            edition_date=edition_date,
            defaults=defaults,
        )
    except IntegrityError:
        edition = DailyBriefEdition.objects.get(edition_date=edition_date)
    updated_fields: list[str] = []
    for field_name, value in defaults.items():
        if getattr(edition, field_name) != value:
            setattr(edition, field_name, value)
            updated_fields.append(field_name)
    if updated_fields:
        edition.save(update_fields=[*updated_fields, "updated_at"])
    return edition


def get_active_daily_brief_recipient_list(*, limit: int | None = None) -> list[str]:
    queryset = (
        DailyBriefSubscription.objects.filter(
            status=DailyBriefSubscription.Status.ACTIVE,
            is_active=True,
            user__is_active=True,
            user__email__isnull=False,
        )
        .exclude(user__email="")
        .select_related("user")
        .order_by("user__email")
        .values_list("user__email", flat=True)
    )
    if limit is not None:
        queryset = queryset[:limit]
    return list(queryset)


def _send_daily_brief_email(*, edition: DailyBriefEdition, recipient_list: list[str]) -> int:
    if not recipient_list:
        return 0

    if settings.RESEND_API_KEY:
        for recipient in recipient_list:
            response = requests.post(
                settings.RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.RESEND_FROM_EMAIL,
                    "to": [recipient],
                    "subject": edition.subject,
                    "text": edition.body_text,
                },
                timeout=settings.EMAIL_TIMEOUT,
            )
            response.raise_for_status()
        logger.info("Sent daily brief via Resend to %s recipients", len(recipient_list))
        return len(recipient_list)

    message = EmailMessage(
        subject=edition.subject,
        body=edition.body_text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        bcc=recipient_list,
    )
    message.send(fail_silently=False)
    logger.info("Sent daily brief via Django email backend to %s recipients", len(recipient_list))
    return len(recipient_list)


def send_daily_brief_to_active_subscribers(
    *,
    target_date=None,
    limit: int | None = None,
    force: bool = False,
) -> dict[str, int | str | bool]:
    edition_date = target_date or timezone.now().date()

    with transaction.atomic():
        edition = get_or_create_daily_brief_edition(target_date=edition_date)
        edition = DailyBriefEdition.objects.select_for_update().get(pk=edition.pk)

        if edition.sent_at is not None and not force:
            logger.info("Daily brief edition %s already sent", edition.edition_date)
            return {
                "edition_date": edition.edition_date.isoformat(),
                "recipient_count": edition.recipient_count,
                "already_sent": True,
            }

        recipient_list = get_active_daily_brief_recipient_list(limit=limit)

        recipient_count = _send_daily_brief_email(
            edition=edition,
            recipient_list=recipient_list,
        )
        edition.sent_at = timezone.now()
        edition.recipient_count = recipient_count
        edition.save()

    logger.info(
        "Daily brief edition %s sent to %s recipients",
        edition_date,
        recipient_count,
    )
    return {
        "edition_date": edition_date.isoformat(),
        "recipient_count": recipient_count,
        "already_sent": False,
    }


def _extract_option_candidates(symbol: Symbol) -> list[dict[str, object]]:
    option_data = symbol.option_data if isinstance(symbol.option_data, dict) else {}
    candidates: list[dict[str, object]] = []

    if option_data:
        primary_snapshot = dict(option_data)
        if primary_snapshot.get("roi") is None and symbol.roi is not None:
            primary_snapshot["roi"] = symbol.roi
        candidates.append(
            {
                "option_data": primary_snapshot,
                "is_alternative": False,
            }
        )

    alternatives = option_data.get("alternatives")
    if isinstance(alternatives, list):
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                continue
            candidates.append(
                {
                    "option_data": dict(alternative),
                    "is_alternative": True,
                }
            )

    return candidates


def _extract_candidate_roi(*, symbol: Symbol, option_data: dict[str, object], is_alternative: bool) -> Decimal | None:
    roi_value = _to_decimal(option_data.get("roi"))
    if roi_value is not None:
        return roi_value
    if not is_alternative:
        return _to_decimal(symbol.roi)
    return None


def _extract_candidate_delta(option_data: dict[str, object]) -> Decimal | None:
    return _to_decimal(_get_option_data_value(option_data, "delta", "rawDelta", "raw_delta"))


def select_daily_brief_symbol_candidate(symbol: Symbol) -> dict[str, object] | None:
    if symbol.score is None or symbol.score < 80:
        return None
    if symbol.rsi is None or symbol.rsi >= Decimal("70"):
        return None
    if (
        symbol.option_exp is not None
        and symbol.next_earnings_date is not None
        and symbol.option_exp >= symbol.next_earnings_date
    ):
        return None

    best_candidate: dict[str, object] | None = None
    best_roi: Decimal | None = None
    best_delta: Decimal | None = None

    for raw_candidate in _extract_option_candidates(symbol):
        option_data = raw_candidate["option_data"]
        is_alternative = bool(raw_candidate["is_alternative"])
        if not isinstance(option_data, dict):
            continue

        roi_value = _extract_candidate_roi(
            symbol=symbol,
            option_data=option_data,
            is_alternative=is_alternative,
        )
        delta_value = _extract_candidate_delta(option_data)
        if (
            roi_value is None
            or roi_value < Decimal("3")
            or delta_value is None
            or abs(delta_value) > DAILY_BRIEF_MAX_ABS_DELTA
        ):
            continue

        if (
            best_candidate is None
            or best_roi is None
            or roi_value > best_roi
            or (roi_value == best_roi and best_delta is not None and abs(delta_value) < abs(best_delta))
        ):
            snapshot = dict(option_data)
            snapshot["roi"] = float(roi_value)
            snapshot["delta"] = float(delta_value)
            best_candidate = {
                "symbol": symbol,
                "ticker": symbol.ticker,
                "score": symbol.score,
                "rsi": symbol.rsi,
                "roi": roi_value,
                "delta": delta_value,
                "is_alternative": is_alternative,
                "option_data": snapshot,
            }
            best_roi = roi_value
            best_delta = delta_value

    return best_candidate


def build_daily_brief_rows(*, limit: int = 3) -> list[dict[str, object]]:
    selections: list[dict[str, object]] = []

    for symbol in Symbol.objects.filter(
        score__gte=80,
        rsi__lt=70,
        option_data__isnull=False,
    ).order_by("ticker"):
        selection = select_daily_brief_symbol_candidate(symbol)
        if selection is not None:
            selections.append(selection)

    selections.sort(
        key=lambda item: (
            -(item["roi"] or Decimal("0")),
            abs(item["delta"] or Decimal("999")),
            -(item["score"] or 0),
            str(item["ticker"]),
        )
    )

    ranked_rows: list[dict[str, object]] = []
    for rank, item in enumerate(selections[:limit], start=1):
        ranked_item = dict(item)
        ranked_item["rank"] = rank
        ranked_rows.append(ranked_item)

    return ranked_rows


def refresh_daily_brief(*, target_date=None, limit: int = 3) -> list[DailyBrief]:
    edition_date = target_date or timezone.now().date()
    rows = build_daily_brief_rows(limit=limit)

    with transaction.atomic():
        DailyBrief.objects.filter(edition_date=edition_date).delete()
        DailyBrief.objects.bulk_create(
            [
                DailyBrief(
                    edition_date=edition_date,
                    rank=int(row["rank"]),
                    symbol=row["symbol"],
                    ticker=str(row["ticker"]),
                    score=row["score"],
                    rsi=row["rsi"],
                    roi=row["roi"],
                    delta=row["delta"],
                    is_alternative=bool(row["is_alternative"]),
                    option_data=row["option_data"],
                )
                for row in rows
            ]
        )

    return list(DailyBrief.objects.filter(edition_date=edition_date).order_by("rank", "ticker"))
