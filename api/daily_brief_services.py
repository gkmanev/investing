from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import DailyBriefEdition, DailyBriefSubscription, Symbol


logger = logging.getLogger(__name__)


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


def _build_top_symbols_payload(limit: int = 3) -> list[dict[str, str | int | None]]:
    symbols = list(
        Symbol.objects.filter(score__isnull=False).order_by(
            "-score",
            "-technical_score",
            "-market_cap",
            "ticker",
        )[:limit]
    )
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


def send_daily_brief_to_active_subscribers(*, target_date=None) -> dict[str, int | str | bool]:
    edition_date = target_date or timezone.now().date()

    with transaction.atomic():
        edition = get_or_create_daily_brief_edition(target_date=edition_date)
        edition = DailyBriefEdition.objects.select_for_update().get(pk=edition.pk)

        if edition.sent_at is not None:
            logger.info("Daily brief edition %s already sent", edition.edition_date)
            return {
                "edition_date": edition.edition_date.isoformat(),
                "recipient_count": edition.recipient_count,
                "already_sent": True,
            }

        recipient_list = list(
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
