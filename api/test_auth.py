import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .auth_email import send_verification_email
from .models import EmailVerificationToken, Investment, PremiumSubscription


User = get_user_model()


@override_settings(
    AUTH_ALLOW_PUBLIC_REGISTRATION=False,
    RESEND_API_KEY=None,
)
class AuthAPITestCase(APITestCase):
    def setUp(self) -> None:
        self.login_url = reverse("auth-login")
        self.logout_url = reverse("auth-logout")
        self.me_url = reverse("auth-me")
        self.refresh_url = reverse("auth-refresh")
        self.register_url = reverse("auth-register")
        self.verify_email_url = reverse("auth-verify-email")
        self.resend_verification_url = reverse("auth-resend-verification")

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

    def test_register_is_disabled_by_default(self) -> None:
        response = self.client.post(
            self.register_url,
            {
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "StrongPass123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(User.objects.filter(username="newuser").exists())

    @override_settings(
        AUTH_ALLOW_PUBLIC_REGISTRATION=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_register_creates_inactive_user_and_sends_verification_email(self) -> None:
        response = self.client.post(
            self.register_url,
            {
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "StrongPass123!",
                "first_name": "New",
                "last_name": "User",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["user"]["username"], "newuser")
        self.assertTrue(response.data["requires_verification"])
        self.assertNotIn("access", response.data)
        self.assertNotIn(settings.AUTH_REFRESH_COOKIE_NAME, response.cookies)
        user = User.objects.get(username="newuser")
        self.assertFalse(user.is_active)
        self.assertEqual(EmailVerificationToken.objects.filter(user=user).count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(
        AUTH_ALLOW_PUBLIC_REGISTRATION=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_register_ignores_existing_session_without_requiring_csrf(self) -> None:
        session_user, _ = self.create_user(username="session-user", email="session@example.com")
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(session_user)

        response = client.post(
            self.register_url,
            {
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "StrongPass123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.filter(username="newuser").exists())

    @override_settings(AUTH_ALLOW_PUBLIC_REGISTRATION=True)
    @patch("api.auth_views.send_verification_email", side_effect=RuntimeError("smtp down"))
    def test_register_returns_503_when_email_delivery_fails(self, _send_email) -> None:
        response = self.client.post(
            self.register_url,
            {
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "StrongPass123!",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(
            response.data["detail"],
            "We could not send the verification email right now. Please try again shortly.",
        )
        self.assertFalse(User.objects.filter(username="newuser").exists())
        self.assertEqual(EmailVerificationToken.objects.count(), 0)

    def test_login_accepts_email_and_returns_access_and_refresh_cookie(self) -> None:
        _, password = self.create_user()

        response = self.client.post(
            self.login_url,
            {"identifier": "alice@example.com", "password": password},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["username"], "alice")
        self.assertIn("access", response.data)
        self.assertIn(settings.AUTH_REFRESH_COOKIE_NAME, response.cookies)

    def test_login_rejects_unverified_user(self) -> None:
        _, password = self.create_user(is_active=False)

        response = self.client.post(
            self.login_url,
            {"identifier": "alice@example.com", "password": password},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("identifier", response.data)

    def test_me_requires_valid_access_token(self) -> None:
        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_returns_current_user(self) -> None:
        user, password = self.create_user()
        login_response = self.client.post(
            self.login_url,
            {"identifier": user.username, "password": password},
            format="json",
        )
        access_token = login_response.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["email"], user.email)
        self.assertFalse(response.data["has_full_access"])
        self.assertEqual(response.data["plan"], "free")
        self.assertIsNone(response.data["trial_days_left"])
        self.assertIn("premium_subscription", response.data)
        self.assertIsNone(response.data["premium_subscription"])

    def test_me_returns_full_access_for_staff_user(self) -> None:
        user, password = self.create_user(is_staff=True, is_superuser=True)
        login_response = self.client.post(
            self.login_url,
            {"identifier": user.username, "password": password},
            format="json",
        )
        access_token = login_response.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_staff"])
        self.assertTrue(response.data["is_superuser"])
        self.assertTrue(response.data["has_full_access"])

    def test_refresh_uses_cookie_and_rotates_refresh_token(self) -> None:
        user, password = self.create_user()
        login_response = self.client.post(
            self.login_url,
            {"identifier": user.username, "password": password},
            format="json",
        )
        old_refresh = login_response.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value

        response = self.client.post(self.refresh_url, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("user", response.data)
        self.assertEqual(response.data["user"]["username"], user.username)
        self.assertFalse(response.data["user"]["has_full_access"])
        self.assertEqual(response.data["plan"], "free")
        self.assertEqual(response.data["user"]["plan"], "free")
        self.assertIn("premium_subscription", response.data)
        self.assertIn(settings.AUTH_REFRESH_COOKIE_NAME, response.cookies)
        self.assertNotEqual(
            response.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value,
            old_refresh,
        )

    def test_authenticated_free_user_cannot_use_filtered_symbol_endpoint(self) -> None:
        user, password = self.create_user()
        login_response = self.client.post(
            self.login_url,
            {"identifier": user.username, "password": password},
            format="json",
        )
        access_token = login_response.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

        response = self.client.get(reverse("symbol-list"), {"min_price": "10"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_authenticated_paid_user_can_use_filtered_symbol_endpoint(self) -> None:
        user, password = self.create_user()
        PremiumSubscription.objects.create(
            user=user,
            stripe_subscription_id="sub_auth_paid_123",
            stripe_customer_id="cus_auth_paid_123",
            status=PremiumSubscription.Status.ACTIVE,
        )
        login_response = self.client.post(
            self.login_url,
            {"identifier": user.username, "password": password},
            format="json",
        )
        access_token = login_response.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

        response = self.client.get(reverse("symbol-list"), {"min_price": "10"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_me_returns_full_access_for_paid_user(self) -> None:
        user, password = self.create_user()
        PremiumSubscription.objects.create(
            user=user,
            stripe_subscription_id="sub_me_paid_123",
            stripe_customer_id="cus_me_paid_123",
            status=PremiumSubscription.Status.ACTIVE,
        )
        login_response = self.client.post(
            self.login_url,
            {"identifier": user.username, "password": password},
            format="json",
        )
        access_token = login_response.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")

        response = self.client.get(self.me_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["has_full_access"])
        self.assertEqual(response.data["plan"], "pro")

    def test_logout_blacklists_refresh_cookie(self) -> None:
        user, password = self.create_user()
        login_response = self.client.post(
            self.login_url,
            {"identifier": user.username, "password": password},
            format="json",
        )
        refresh_token = login_response.cookies[settings.AUTH_REFRESH_COOKIE_NAME].value

        response = self.client.post(self.logout_url, format="json")

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        another_client = APIClient()
        another_client.cookies[settings.AUTH_REFRESH_COOKIE_NAME] = refresh_token
        refresh_response = another_client.post(self.refresh_url, format="json")
        self.assertEqual(refresh_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_verify_email_ignores_existing_session_without_requiring_csrf(self) -> None:
        session_user, _ = self.create_user(username="session-user", email="session@example.com")
        user, _ = self.create_user(username="pending-user", email="pending@example.com", is_active=False)
        token_obj = EmailVerificationToken.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(hours=24),
        )
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(session_user)

        response = client.post(
            self.verify_email_url,
            {"token": str(token_obj.token)},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIn("access", response.data)
        self.assertIn(settings.AUTH_REFRESH_COOKIE_NAME, response.cookies)

    def test_verify_email_activates_user_and_returns_tokens(self) -> None:
        user, _ = self.create_user(is_active=False)
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
        self.assertEqual(response.data["user"]["username"], user.username)
        self.assertIn("access", response.data)
        self.assertIn(settings.AUTH_REFRESH_COOKIE_NAME, response.cookies)
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertFalse(EmailVerificationToken.objects.filter(user=user).exists())

    def test_verify_email_rejects_expired_token(self) -> None:
        user, _ = self.create_user(is_active=False)
        token_obj = EmailVerificationToken.objects.create(
            user=user,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        response = self.client.post(
            self.verify_email_url,
            {"token": str(token_obj.token)},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("token", response.data)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        AUTH_RESEND_COOLDOWN_SECONDS=0,
    )
    def test_resend_verification_sends_new_email(self) -> None:
        user, _ = self.create_user(is_active=False)
        old_token = EmailVerificationToken.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(hours=24),
        )

        response = self.client.post(
            self.resend_verification_url,
            {"identifier": user.email},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)
        new_token = EmailVerificationToken.objects.get(user=user)
        self.assertNotEqual(new_token.token, old_token.token)

    def test_resend_verification_rate_limits_recent_request(self) -> None:
        user, _ = self.create_user(is_active=False)
        EmailVerificationToken.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(hours=24),
        )

        response = self.client.post(
            self.resend_verification_url,
            {"identifier": user.username},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    @override_settings(AUTH_RESEND_COOLDOWN_SECONDS=0)
    @patch("api.auth_views.send_verification_email", side_effect=RuntimeError("smtp down"))
    def test_resend_verification_returns_503_when_email_delivery_fails(self, _send_email) -> None:
        user, _ = self.create_user(is_active=False)
        old_token = EmailVerificationToken.objects.create(
            user=user,
            expires_at=timezone.now() + timedelta(hours=24),
        )

        response = self.client.post(
            self.resend_verification_url,
            {"identifier": user.email},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(
            response.data["detail"],
            "We could not send the verification email right now. Please try again shortly.",
        )
        tokens = list(EmailVerificationToken.objects.filter(user=user))
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].token, old_token.token)


class StaffWritePermissionsAPITestCase(APITestCase):
    def setUp(self) -> None:
        self.investment_list_url = reverse("investment-list")
        self.screener_type_list_url = reverse("screenertype-list")

    def authenticate(self, *, is_staff: bool) -> User:
        user = User.objects.create_user(
            username="staff" if is_staff else "member",
            email="staff@example.com" if is_staff else "member@example.com",
            password="StrongPass123!",
            is_staff=is_staff,
        )
        access_token = str(RefreshToken.for_user(user).access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token}")
        return user

    def test_public_can_read_investment_list(self) -> None:
        Investment.objects.create(ticker="AAPL", category="Stock")

        response = self.client.get(self.investment_list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_anonymous_user_cannot_create_screener_type(self) -> None:
        response = self.client.post(
            self.screener_type_list_url,
            {"name": "Momentum", "description": "Fast movers"},
            format="json",
        )

        self.assertIn(
            response.status_code,
            {status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN},
        )

    def test_authenticated_non_staff_user_cannot_create_screener_type(self) -> None:
        self.authenticate(is_staff=False)

        response = self.client.post(
            self.screener_type_list_url,
            {"name": "Momentum", "description": "Fast movers"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_user_can_create_screener_type(self) -> None:
        self.authenticate(is_staff=True)

        response = self.client.post(
            self.screener_type_list_url,
            {"name": "Momentum", "description": "Fast movers"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["name"], "Momentum")


class AuthEmailTestCase(TestCase):
    @override_settings(
        RESEND_API_KEY="test-resend-key",
        RESEND_API_URL="https://api.resend.com/emails",
        RESEND_FROM_EMAIL="admin@putpulse.com",
        FRONTEND_BASE_URL="https://putpulse.com",
        AUTH_VERIFY_EMAIL_PATH="/verify-email",
        AUTH_EMAIL_VERIFICATION_HOURS=24,
        EMAIL_TIMEOUT=20,
    )
    @patch("api.auth_email.requests.post")
    def test_send_verification_email_uses_resend_when_configured(self, mock_post) -> None:
        mock_response = mock_post.return_value
        user = SimpleNamespace(username="alice", email="alice@example.com")
        token_obj = SimpleNamespace(token=uuid.UUID("11111111-1111-1111-1111-111111111111"))

        send_verification_email(user=user, token_obj=token_obj)

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-resend-key")
        self.assertEqual(kwargs["json"]["from"], "admin@putpulse.com")
        self.assertEqual(kwargs["json"]["to"], ["alice@example.com"])
        self.assertEqual(kwargs["json"]["subject"], "Verify your PutPulse account")
        self.assertIn(
            "https://putpulse.com/verify-email?token=11111111-1111-1111-1111-111111111111",
            kwargs["json"]["text"],
        )
        self.assertEqual(kwargs["timeout"], 20)
        mock_response.raise_for_status.assert_called_once_with()
