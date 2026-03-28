from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .daily_brief_services import subscribe_user
from .models import DailyBriefEdition, DailyBriefSubscription, EmailVerificationToken, Symbol


User = get_user_model()


@override_settings(RESEND_API_KEY=None)
class DailyBriefAPITestCase(APITestCase):
    def setUp(self) -> None:
        self.register_url = reverse("auth-register")
        self.verify_email_url = reverse("auth-verify-email")
        self.status_url = reverse("daily-brief-subscription")
        self.subscribe_url = reverse("daily-brief-subscription-subscribe")
        self.unsubscribe_url = reverse("daily-brief-subscription-unsubscribe")

    def create_user(self, **overrides):
        defaults = {
            "username": "alice",
            "email": "alice@example.com",
            "password": "StrongPass123!",
            "is_active": True,
        }
        defaults.update(overrides)
        password = defaults.pop("password")
        user = User.objects.create_user(**defaults)
        user.set_password(password)
        user.save(update_fields=["password"])
        return user, password

    def authenticate(self, user) -> None:
        access_token = str(RefreshToken.for_user(user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

    @override_settings(
        AUTH_ALLOW_PUBLIC_REGISTRATION=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_register_opt_in_creates_pending_subscription(self) -> None:
        response = self.client.post(
            self.register_url,
            {
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "StrongPass123!",
                "daily_brief_opt_in": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="newuser")
        subscription = DailyBriefSubscription.objects.get(user=user)
        self.assertEqual(
            subscription.status,
            DailyBriefSubscription.Status.PENDING_VERIFICATION,
        )
        self.assertFalse(subscription.is_active)
        self.assertEqual(subscription.source, DailyBriefSubscription.Source.SIGNUP)
        self.assertIsNotNone(subscription.subscribed_at)

    def test_verify_email_activates_pending_subscription(self) -> None:
        user, _ = self.create_user(is_active=False)
        subscribe_user(user, source=DailyBriefSubscription.Source.SIGNUP)
        token_obj = EmailVerificationToken.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(hours=24),
        )

        response = self.client.post(
            self.verify_email_url,
            {"token": str(token_obj.token)},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        subscription = DailyBriefSubscription.objects.get(user=user)
        self.assertEqual(subscription.status, DailyBriefSubscription.Status.ACTIVE)
        self.assertTrue(subscription.is_active)

    def test_status_endpoint_returns_unsubscribed_default_state(self) -> None:
        user, _ = self.create_user()
        self.authenticate(user)

        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], DailyBriefSubscription.Status.UNSUBSCRIBED)
        self.assertFalse(response.data["is_active"])
        self.assertEqual(response.data["email"], user.email)
        self.assertIsNone(response.data["source"])
        self.assertTrue(DailyBriefSubscription.objects.filter(user=user).exists())

    def test_subscribe_endpoint_is_idempotent(self) -> None:
        user, _ = self.create_user()
        self.authenticate(user)

        first_response = self.client.post(
            self.subscribe_url,
            {"source": DailyBriefSubscription.Source.PROFILE},
            format="json",
        )
        second_response = self.client.post(
            self.subscribe_url,
            {"source": DailyBriefSubscription.Source.DAILY_BRIEF_CTA},
            format="json",
        )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        subscription = DailyBriefSubscription.objects.get(user=user)
        self.assertEqual(subscription.status, DailyBriefSubscription.Status.ACTIVE)
        self.assertTrue(subscription.is_active)
        self.assertEqual(subscription.source, DailyBriefSubscription.Source.PROFILE)
        self.assertEqual(
            first_response.data["subscribed_at"],
            second_response.data["subscribed_at"],
        )

    def test_unsubscribe_endpoint_is_idempotent(self) -> None:
        user, _ = self.create_user()
        subscribe_user(user, source=DailyBriefSubscription.Source.DAILY_BRIEF_CTA)
        self.authenticate(user)

        first_response = self.client.post(self.unsubscribe_url, format="json")
        second_response = self.client.post(self.unsubscribe_url, format="json")

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        subscription = DailyBriefSubscription.objects.get(user=user)
        self.assertEqual(subscription.status, DailyBriefSubscription.Status.UNSUBSCRIBED)
        self.assertFalse(subscription.is_active)
        self.assertIsNotNone(subscription.unsubscribed_at)
        self.assertEqual(
            first_response.data["status"],
            DailyBriefSubscription.Status.UNSUBSCRIBED,
        )
        self.assertEqual(
            first_response.data["subscribed_at"],
            second_response.data["subscribed_at"],
        )


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    RESEND_API_KEY=None,
)
class DailyBriefDeliveryTestCase(APITestCase):
    def create_user(self, *, username: str, email: str, is_active: bool = True):
        user = User.objects.create_user(
            username=username,
            email=email,
            password="StrongPass123!",
            is_active=is_active,
        )
        return user

    def test_daily_brief_delivery_generates_one_edition_and_skips_non_active_users(self) -> None:
        from .daily_brief_services import send_daily_brief_to_active_subscribers

        active_one = self.create_user(username="alpha", email="alpha@example.com")
        active_two = self.create_user(username="beta", email="beta@example.com")
        pending_user = self.create_user(
            username="gamma",
            email="gamma@example.com",
            is_active=False,
        )
        unsubscribed_user = self.create_user(
            username="delta",
            email="delta@example.com",
        )

        subscribe_user(active_one, source=DailyBriefSubscription.Source.SIGNUP)
        subscribe_user(active_two, source=DailyBriefSubscription.Source.PROFILE)
        subscribe_user(pending_user, source=DailyBriefSubscription.Source.SIGNUP)
        subscribe_user(unsubscribed_user, source=DailyBriefSubscription.Source.PROFILE)
        DailyBriefSubscription.objects.filter(user=unsubscribed_user).update(
            status=DailyBriefSubscription.Status.UNSUBSCRIBED,
            is_active=False,
            unsubscribed_at=timezone.now(),
        )

        Symbol.objects.create(ticker="NVDA", score=96, classification="Great")
        Symbol.objects.create(ticker="MSFT", score=93, classification="Great")
        Symbol.objects.create(ticker="META", score=91, classification="Strong")
        Symbol.objects.create(ticker="AAPL", score=88, classification="Strong")

        first_result = send_daily_brief_to_active_subscribers(
            target_date=date(2026, 3, 28),
        )
        second_result = send_daily_brief_to_active_subscribers(
            target_date=date(2026, 3, 28),
        )

        self.assertEqual(first_result["recipient_count"], 2)
        self.assertFalse(first_result["already_sent"])
        self.assertTrue(second_result["already_sent"])
        self.assertEqual(second_result["recipient_count"], 2)
        self.assertEqual(DailyBriefEdition.objects.count(), 1)
        edition = DailyBriefEdition.objects.get()
        self.assertEqual(edition.recipient_count, 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            set(mail.outbox[0].bcc),
            {active_one.email, active_two.email},
        )
        self.assertIn("NVDA", mail.outbox[0].body)
        self.assertIn("MSFT", mail.outbox[0].body)
        self.assertIn("META", mail.outbox[0].body)
        self.assertNotIn("AAPL", mail.outbox[0].body)
        self.assertNotIn(settings.DEFAULT_FROM_EMAIL, set(mail.outbox[0].bcc))
