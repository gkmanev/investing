from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone

from .models import EmailVerificationToken


def create_email_verification_token(*, user):
    EmailVerificationToken.objects.filter(user=user).delete()
    return EmailVerificationToken.objects.create(
        user=user,
        expires_at=timezone.now() + timedelta(hours=settings.AUTH_EMAIL_VERIFICATION_HOURS),
    )


def build_verification_url(*, token: str) -> str:
    query = urlencode({"token": token})
    base_url = settings.FRONTEND_BASE_URL.rstrip("/")
    path = settings.AUTH_VERIFY_EMAIL_PATH
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base_url}{path}?{query}"


def send_verification_email(*, user, token_obj: EmailVerificationToken) -> None:
    verification_url = build_verification_url(token=str(token_obj.token))
    subject = "Verify your PutPulse account"
    message = render_to_string(
        "api/email/verify_email.txt",
        {
            "username": user.username,
            "verification_url": verification_url,
            "expiry_hours": settings.AUTH_EMAIL_VERIFICATION_HOURS,
        },
    )
    send_mail(
        subject=subject,
        message=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
        fail_silently=False,
    )
