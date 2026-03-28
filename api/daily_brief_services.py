from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import DailyBriefEdition, DailyBriefSubscription, Symbol


logger = logging.getLogger(__name__)
DAILY_BRIEF_ALLOWED_TECHNICALS = {"buy", "strong_buy"}


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
    return str(value)


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


def _extract_roi_value(symbol: Symbol) -> Decimal | None:
    roi_value = _to_decimal(symbol.roi)
    if roi_value is not None:
        return roi_value

    option_data = symbol.option_data or {}
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


def _build_top_symbols_payload(limit: int = 3) -> list[dict[str, str | int | None]]:
    candidates = list(
        Symbol.objects.filter(
            score__gt=80,
            rsi__gte=30,
            rsi__lte=70,
            option_data__isnull=False,
        )
    )

    filtered_symbols: list[Symbol] = []
    seen_tickers: set[str] = set()
    for symbol in candidates:
        if symbol.ticker in seen_tickers:
            continue
        if not _is_daily_brief_candidate(symbol):
            continue
        seen_tickers.add(symbol.ticker)
        filtered_symbols.append(symbol)

    filtered_symbols.sort(
        key=lambda symbol: (
            -(_extract_roi_value(symbol) or Decimal("0")),
            -(symbol.score or 0),
            0
            if _extract_technical_signal(symbol) == "strong_buy"
            else 1
            if _extract_technical_signal(symbol) == "buy"
            else 2,
            symbol.ticker,
        )
    )
    symbols = filtered_symbols[:limit]
    return [
        {
            "ticker": symbol.ticker,
            "score": symbol.score,
            "classification": symbol.classification,
            "price": _format_decimal(symbol.price),
            "technical_score": _format_decimal(symbol.technical_score),
            "market_cap": _format_decimal(symbol.market_cap),
            "roi": _format_decimal(symbol.roi),
        }
        for symbol in symbols
    ]


def _build_daily_brief_defaults(*, edition_date) -> dict:
    top_symbols = _build_top_symbols_payload()
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
