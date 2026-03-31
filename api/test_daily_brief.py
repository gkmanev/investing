from datetime import date, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, PeriodicTask
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .daily_brief_services import subscribe_user
from .models import DailyBrief, DailyBriefEdition, DailyBriefSubscription, EmailVerificationToken, Symbol


User = get_user_model()


class DailyBriefScheduleCommandTestCase(TestCase):
    @override_settings(
        DAILY_BRIEF_SEND_HOUR_UTC=16,
        DAILY_BRIEF_SEND_MINUTE_UTC=0,
        CELERY_TIMEZONE="UTC",
    )
    def test_sync_daily_brief_schedule_creates_periodic_task(self) -> None:
        call_command("sync_daily_brief_schedule")

        task = PeriodicTask.objects.get(name="send-daily-top-3-edition")
        self.assertEqual(task.task, "api.tasks.send_daily_top_3_edition")
        self.assertTrue(task.enabled)
        self.assertEqual(task.crontab.hour, "16")
        self.assertEqual(task.crontab.minute, "0")
        self.assertEqual(str(task.crontab.timezone), "UTC")

    @override_settings(
        DAILY_BRIEF_SEND_HOUR_UTC=18,
        DAILY_BRIEF_SEND_MINUTE_UTC=45,
        CELERY_TIMEZONE="UTC",
    )
    def test_sync_daily_brief_schedule_updates_existing_periodic_task(self) -> None:
        stale_schedule = CrontabSchedule.objects.create(
            minute="0",
            hour="16",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone="UTC",
        )
        PeriodicTask.objects.create(
            name="send-daily-top-3-edition",
            task="api.tasks.send_daily_top_3_edition",
            crontab=stale_schedule,
            enabled=False,
        )

        call_command("sync_daily_brief_schedule")

        task = PeriodicTask.objects.get(name="send-daily-top-3-edition")
        self.assertTrue(task.enabled)
        self.assertEqual(task.crontab.hour, "18")
        self.assertEqual(task.crontab.minute, "45")
        self.assertEqual(str(task.crontab.timezone), "UTC")


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


class DailyBriefPopulationTestCase(TestCase):
    def create_symbol(
        self,
        *,
        ticker: str,
        score: int,
        rsi: str,
        roi: str,
        delta: float,
        alternatives: list[dict] | None = None,
    ) -> Symbol:
        option_data = {
            "option_symbol": f"{ticker}_MAIN",
            "strike_price": 95.0,
            "bid": 3.4,
            "ask": 3.6,
            "mid": 3.5,
            "delta": delta,
            "roi": float(roi),
        }
        if alternatives is not None:
            option_data["alternatives"] = alternatives

        return Symbol.objects.create(
            ticker=ticker,
            exchange="NASDAQ",
            score=score,
            rsi=rsi,
            roi=roi,
            option_data=option_data,
        )

    def test_populate_daily_brief_ranks_symbols_using_alternatives(self) -> None:
        target_date = date(2026, 3, 31)
        self.create_symbol(
            ticker="NFLX",
            score=94,
            rsi="55.00",
            roi="7.10",
            delta=-0.30,
        )
        self.create_symbol(
            ticker="AAPL",
            score=80,
            rsi="69.99",
            roi="2.80",
            delta=-0.31,
            alternatives=[
                {
                    "option_symbol": "AAPL_ALT_BEST",
                    "strike_price": 94.0,
                    "bid": 2.8,
                    "ask": 3.0,
                    "mid": 2.9,
                    "delta": -0.18,
                    "roi": 6.2,
                }
            ],
        )
        self.create_symbol(
            ticker="MSFT",
            score=92,
            rsi="64.00",
            roi="6.20",
            delta=-0.25,
        )
        self.create_symbol(
            ticker="NVDA",
            score=91,
            rsi="61.00",
            roi="5.90",
            delta=-0.19,
        )
        self.create_symbol(
            ticker="AMZN",
            score=88,
            rsi="70.00",
            roi="9.10",
            delta=-0.22,
        )
        self.create_symbol(
            ticker="META",
            score=79,
            rsi="40.00",
            roi="8.50",
            delta=-0.20,
        )

        call_command("populate_daily_brief", edition_date=target_date.isoformat())
        call_command("populate_daily_brief", edition_date=target_date.isoformat())

        briefs = list(DailyBrief.objects.filter(edition_date=target_date).order_by("rank"))
        self.assertEqual(len(briefs), 3)
        self.assertEqual([brief.ticker for brief in briefs], ["NFLX", "AAPL", "MSFT"])
        self.assertEqual([brief.rank for brief in briefs], [1, 2, 3])
        self.assertFalse(briefs[0].is_alternative)
        self.assertTrue(briefs[1].is_alternative)
        self.assertEqual(briefs[1].option_data["option_symbol"], "AAPL_ALT_BEST")
        self.assertEqual(float(briefs[1].roi), 6.2)
        self.assertEqual(float(briefs[1].delta), -0.18)
        self.assertFalse(DailyBrief.objects.filter(edition_date=target_date, ticker="AMZN").exists())
        self.assertFalse(DailyBrief.objects.filter(edition_date=target_date, ticker="META").exists())


class DailyBriefTableAPITestCase(APITestCase):
    def test_daily_brief_list_filters_by_date(self) -> None:
        symbol = Symbol.objects.create(
            ticker="AAPL",
            exchange="NASDAQ",
            score=90,
            rsi="55.00",
            roi="6.20",
            option_data={"option_symbol": "AAPL_MAIN", "delta": -0.25, "roi": 6.2},
        )
        DailyBrief.objects.create(
            edition_date=date(2026, 3, 31),
            rank=1,
            symbol=symbol,
            ticker=symbol.ticker,
            score=symbol.score,
            rsi=symbol.rsi,
            roi="6.20",
            delta="-0.25",
            is_alternative=False,
            option_data={"option_symbol": "AAPL_MAIN", "delta": -0.25, "roi": 6.2},
        )
        DailyBrief.objects.create(
            edition_date=date(2026, 3, 30),
            rank=1,
            symbol=symbol,
            ticker=symbol.ticker,
            score=symbol.score,
            rsi=symbol.rsi,
            roi="5.80",
            delta="-0.30",
            is_alternative=True,
            option_data={"option_symbol": "AAPL_ALT", "delta": -0.30, "roi": 5.8},
        )

        response = self.client.get(
            reverse("dailybrief-list"),
            {"edition_date": "2026-03-31"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["edition_date"], "2026-03-31")
        self.assertEqual(response.data[0]["ticker"], "AAPL")
        self.assertFalse(response.data[0]["is_alternative"])
        self.assertEqual(response.data[0]["option_data"]["option_symbol"], "AAPL_MAIN")


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

        Symbol.objects.create(
            ticker="NVDA",
            score=96,
            classification="Great",
            rsi="55.00",
            roi="6.236",
            price="120.00",
            technical_score="88.00",
            option_data={
                "tvTechnicals": "Strong Buy",
                "spreadValue": 0.8,
                "rawStrike": 110.004,
                "rawPrice": 120.005,
                "delta": -0.3051,
                "roi": 6.236,
            },
        )
        Symbol.objects.create(
            ticker="MSFT",
            score=93,
            classification="Great",
            rsi="48.00",
            roi="7.105",
            price="330.00",
            technical_score="81.00",
            option_data={
                "tvTechnicals": "Buy",
                "spreadValue": 0.5,
                "rawStrike": 300.124,
                "rawPrice": 330.126,
                "delta": -0.286,
                "roi": 7.105,
            },
        )
        Symbol.objects.create(
            ticker="META",
            score=91,
            classification="Strong",
            rsi="61.00",
            roi="5.40",
            price="510.00",
            technical_score="74.00",
            option_data={
                "tvTechnicals": "Buy",
                "spreadValue": 1.1,
                "rawStrike": 470,
                "rawPrice": 510,
                "delta": -0.30,
                "roi": 5.4,
            },
        )
        Symbol.objects.create(
            ticker="AAPL",
            score=88,
            classification="Strong",
            rsi="52.00",
            roi="4.10",
            price="210.00",
            technical_score="70.00",
            option_data={
                "tvTechnicals": "Strong Buy",
                "spreadValue": 0.9,
                "rawStrike": 190,
                "rawPrice": 210,
                "delta": -0.31,
                "roi": 4.1,
            },
        )
        Symbol.objects.create(
            ticker="AMZN",
            score=99,
            classification="Great",
            rsi="49.00",
            roi="9.90",
            price="200.00",
            technical_score="95.00",
            option_data={
                "tvTechnicals": "Hold",
                "spreadValue": 0.4,
                "rawStrike": 180,
                "rawPrice": 200,
                "delta": -0.20,
                "roi": 9.9,
            },
        )

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
        self.assertIn("1. MSFT", mail.outbox[0].body)
        self.assertIn("2. NVDA", mail.outbox[0].body)
        self.assertIn("MSFT", mail.outbox[0].body)
        self.assertIn("3. META", mail.outbox[0].body)
        self.assertIn("NVDA", mail.outbox[0].body)
        self.assertIn("META", mail.outbox[0].body)
        self.assertIn("Score: 93", mail.outbox[0].body)
        self.assertIn("Technicals: Buy", mail.outbox[0].body)
        self.assertIn("Price/Strike: 330.13/300.12", mail.outbox[0].body)
        self.assertIn("Chance of profit: 71.40%", mail.outbox[0].body)
        self.assertIn("ROI: 7.11", mail.outbox[0].body)
        self.assertIn("Technicals: Strong Buy", mail.outbox[0].body)
        self.assertIn("Price/Strike: 120.01/110.00", mail.outbox[0].body)
        self.assertIn("Chance of profit: 69.49%", mail.outbox[0].body)
        self.assertIn("ROI: 6.24", mail.outbox[0].body)
        self.assertNotIn("AAPL", mail.outbox[0].body)
        self.assertNotIn("AMZN", mail.outbox[0].body)
        self.assertNotIn(settings.DEFAULT_FROM_EMAIL, set(mail.outbox[0].bcc))
