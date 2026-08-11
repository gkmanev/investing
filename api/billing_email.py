from __future__ import annotations

import datetime
import logging

import requests
from django.conf import settings
from django.core.mail import send_mail


logger = logging.getLogger(__name__)


def _send_email(*, recipients: list[str], subject: str, message: str) -> None:
    """Deliver billing email through Resend when it is configured."""
    if settings.RESEND_API_KEY:
        response = requests.post(
            settings.RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": settings.RESEND_FROM_EMAIL,
                "to": recipients,
                "subject": subject,
                "text": message,
            },
            timeout=settings.EMAIL_TIMEOUT,
        )
        response.raise_for_status()
        return

    send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)


def send_paid_invoice_notification(*, user, invoice: dict) -> None:
    """Send the Pro receipt to the customer and notify internal billing recipients."""
    billing_recipients = [
        address.strip()
        for address in settings.BILLING_NOTIFICATION_EMAIL.split(",")
        if address.strip()
    ]

    total = int(invoice.get("amount_paid") or invoice.get("total") or 0)
    currency = str(invoice.get("currency") or "").upper()
    amount = f"{total / 100:.2f} {currency}".strip()
    paid_at = invoice.get("status_transitions", {}).get("paid_at")
    paid_at_text = (
        datetime.datetime.fromtimestamp(paid_at, tz=datetime.timezone.utc).isoformat()
        if paid_at
        else "Not supplied by Stripe"
    )
    lines = invoice.get("lines", {}).get("data", [])
    descriptions = ", ".join(
        str(line.get("description") or "Pro subscription") for line in lines
    ) or "Pro subscription"
    invoice_number = invoice.get("number") or invoice.get("id") or "Unavailable"
    customer_subject = f"Your PutPulse Pro invoice: {amount} ({invoice_number})"
    customer_message = "\n".join(
        [
            f"Hi {user.get_full_name() or user.username},",
            "",
            "Thank you for subscribing to PutPulse Pro. Your payment has been received.",
            "",
            f"Invoice: {invoice_number}",
            f"Amount paid: {amount}",
            f"Items: {descriptions}",
            f"Paid at: {paid_at_text}",
            f"View invoice: {invoice.get('hosted_invoice_url') or 'Unavailable'}",
            f"Download invoice PDF: {invoice.get('invoice_pdf') or 'Unavailable'}",
        ]
    )
    _send_email(
        recipients=[user.email],
        subject=customer_subject,
        message=customer_message,
    )
    logger.info("Sent paid Pro invoice email via billing mailer to %s", user.email)

    # Preserve the existing internal billing alert without exposing internal
    # addresses to the customer as co-recipients.
    internal_recipients = [
        address for address in billing_recipients if address.lower() != user.email.lower()
    ]
    if not internal_recipients:
        return

    internal_subject = f"Paid Pro invoice: {amount} ({invoice_number})"
    internal_message = "\n".join(
        [
            "A PutPulse Pro invoice has been paid.",
            "",
            f"User: {user.get_full_name() or user.username} <{user.email}>",
            f"User ID: {user.pk}",
            f"Invoice: {invoice.get('number') or 'Unavailable'}",
            f"Stripe invoice ID: {invoice.get('id')}",
            f"Amount paid: {amount}",
            f"Items: {descriptions}",
            f"Paid at: {paid_at_text}",
            f"Hosted invoice: {invoice.get('hosted_invoice_url') or 'Unavailable'}",
            f"Invoice PDF: {invoice.get('invoice_pdf') or 'Unavailable'}",
        ]
    )
    _send_email(
        recipients=internal_recipients,
        subject=internal_subject,
        message=internal_message,
    )
