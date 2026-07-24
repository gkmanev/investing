from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from api.models import PremiumSubscription


@override_settings(
    STRIPE_SECRET_KEY="test-stripe-key",
    STRIPE_WEBHOOK_SECRET="whsec_test",
)
class SubscriptionApiTests(APITestCase):
    def setUp(self) -> None:
        self.user = get_user_model().objects.create_user(
            username="sub-user",
            email="sub@example.com",
            password="test-pass-123",
        )
        self.client.force_authenticate(user=self.user)

    def test_premium_subscription_view_returns_free_plan_without_paid_subscription(self) -> None:
        response = self.client.get(reverse("premium-subscription"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["plan"], "free")
        self.assertIsNone(response.data["trial_days_left"])
        self.assertFalse(response.data["subscription"]["is_active"])

    @patch("api.subscription_views.stripe.billing_portal.Session.create")
    @override_settings(FRONTEND_BASE_URL="https://frontend.example")
    def test_customer_portal_session_returns_stripe_url(self, mock_create) -> None:
        PremiumSubscription.objects.create(
            user=self.user,
            stripe_subscription_id="sub_portal",
            stripe_customer_id="cus_portal",
            status=PremiumSubscription.Status.ACTIVE,
        )
        mock_create.return_value = type(
            "PortalSession", (), {"url": "https://billing.stripe.com/session/test"}
        )()

        response = self.client.post(reverse("create-stripe-customer-portal-session"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["portal_url"], "https://billing.stripe.com/session/test")
        mock_create.assert_called_once_with(
            customer="cus_portal",
            return_url="https://frontend.example/profile",
        )

    def test_customer_portal_session_requires_a_subscription(self) -> None:
        response = self.client.post(reverse("create-stripe-customer-portal-session"))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch("api.subscription_views.stripe.Subscription.retrieve")
    @patch("api.subscription_views.stripe.Webhook.construct_event")
    def test_checkout_completed_webhook_creates_active_subscription(
        self,
        mock_construct_event,
        mock_retrieve_subscription,
    ) -> None:
        mock_construct_event.return_value = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_123",
                    "subscription": "sub_test_123",
                    "customer": "cus_test_123",
                    "metadata": {"user_id": str(self.user.id)},
                }
            },
        }
        mock_retrieve_subscription.return_value = {
            "id": "sub_test_123",
            "customer": "cus_test_123",
            "status": "active",
            "metadata": {"user_id": str(self.user.id)},
            "current_period_end": 1794067200,
        }

        response = self.client.post(
            reverse("webhooks-stripe"),
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        subscription = PremiumSubscription.objects.get(user=self.user)
        self.assertEqual(subscription.stripe_subscription_id, "sub_test_123")
        self.assertEqual(subscription.stripe_customer_id, "cus_test_123")
        self.assertEqual(subscription.status, PremiumSubscription.Status.ACTIVE)
        self.assertIsNotNone(subscription.current_period_end)

    @patch("api.subscription_views.stripe.Webhook.construct_event")
    def test_subscription_deleted_webhook_marks_existing_subscription_cancelled(
        self,
        mock_construct_event,
    ) -> None:
        PremiumSubscription.objects.create(
            user=self.user,
            stripe_subscription_id="sub_existing",
            stripe_customer_id="cus_existing",
            status=PremiumSubscription.Status.ACTIVE,
        )
        mock_construct_event.return_value = {
            "type": "customer.subscription.deleted",
            "data": {
                "object": {
                    "id": "sub_existing",
                    "customer": "cus_existing",
                    "status": "canceled",
                    "metadata": {},
                    "current_period_end": 1794067200,
                }
            },
        }

        response = self.client.post(
            reverse("webhooks-stripe"),
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        subscription = PremiumSubscription.objects.get(user=self.user)
        self.assertEqual(subscription.status, PremiumSubscription.Status.CANCELLED)
