from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from api.models import BillingNotification, PremiumSubscription


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

    @override_settings(BILLING_NOTIFICATION_EMAIL="", RESEND_API_KEY=None)
    @patch("api.subscription_views.stripe.Invoice.retrieve")
    @patch("api.subscription_views.stripe.Subscription.retrieve")
    @patch("api.subscription_views.stripe.Webhook.construct_event")
    def test_paid_checkout_sends_customer_invoice_receipt(
        self,
        mock_construct_event,
        mock_retrieve_subscription,
        mock_retrieve_invoice,
    ) -> None:
        mock_construct_event.return_value = {
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_paid_123",
                "subscription": "sub_paid_123",
                "customer": "cus_paid_123",
                "invoice": "in_checkout_123",
                "payment_status": "paid",
                "metadata": {"user_id": str(self.user.id)},
            }},
        }
        mock_retrieve_subscription.return_value = {
            "id": "sub_paid_123",
            "customer": "cus_paid_123",
            "status": "active",
            "metadata": {"user_id": str(self.user.id)},
        }
        mock_retrieve_invoice.return_value = {
            "id": "in_checkout_123",
            "number": "ABC-0003",
            "paid": True,
            "amount_paid": 2900,
            "currency": "usd",
            "invoice_pdf": "https://invoice.example/pdf",
            "lines": {"data": [{"description": "PutPulse Pro monthly"}]},
        }

        response = self.client.post(
            reverse("webhooks-stripe"),
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_retrieve_invoice.assert_called_once_with("in_checkout_123")
        self.assertEqual(BillingNotification.objects.filter(stripe_invoice_id="in_checkout_123").count(), 1)
        self.assertEqual(mail.outbox[0].to, ["sub@example.com"])
        self.assertIn("Your PutPulse Pro invoice", mail.outbox[0].subject)

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

    @patch("api.subscription_views.stripe.Subscription.retrieve")
    @patch("api.subscription_views.stripe.Webhook.construct_event")
    def test_subscription_update_fetches_missing_period_end_from_stripe(
        self,
        mock_construct_event,
        mock_retrieve_subscription,
    ) -> None:
        PremiumSubscription.objects.create(
            user=self.user,
            stripe_subscription_id="sub_existing",
            stripe_customer_id="cus_existing",
            status=PremiumSubscription.Status.ACTIVE,
        )
        mock_construct_event.return_value = {
            "type": "customer.subscription.updated",
            "data": {
                "object": {
                    "id": "sub_existing",
                    "customer": "cus_existing",
                    "status": "active",
                    "metadata": {},
                }
            },
        }
        mock_retrieve_subscription.return_value = {
            "id": "sub_existing",
            "customer": "cus_existing",
            "status": "active",
            "metadata": {},
            "start_date": 1791475200,
            "current_period_start": 1791475200,
            "current_period_end": 1794067200,
            "cancel_at_period_end": True,
            "cancel_at": 1794067200,
            "canceled_at": None,
            "ended_at": None,
        }

        response = self.client.post(
            reverse("webhooks-stripe"),
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        subscription = PremiumSubscription.objects.get(user=self.user)
        self.assertIsNotNone(subscription.current_period_end)
        self.assertIsNotNone(subscription.start_date)
        self.assertIsNotNone(subscription.current_period_start)
        self.assertTrue(subscription.cancel_at_period_end)
        self.assertIsNotNone(subscription.cancel_at)
        mock_retrieve_subscription.assert_called_once_with("sub_existing")

    @override_settings(BILLING_NOTIFICATION_EMAIL="owner@example.com", RESEND_API_KEY=None)
    @patch("api.subscription_views.stripe.Webhook.construct_event")
    def test_paid_invoice_webhook_sends_customer_receipt_and_owner_notification_once(self, mock_construct_event) -> None:
        PremiumSubscription.objects.create(
            user=self.user,
            stripe_subscription_id="sub_invoice",
            stripe_customer_id="cus_invoice",
            status=PremiumSubscription.Status.ACTIVE,
        )
        mock_construct_event.return_value = {
            "type": "invoice.paid",
            "data": {"object": {
                "id": "in_paid_123", "number": "ABC-0001", "subscription": "sub_invoice",
                "amount_paid": 2900, "currency": "usd",
                "hosted_invoice_url": "https://invoice.example/hosted",
                "invoice_pdf": "https://invoice.example/pdf",
                "lines": {"data": [{"description": "PutPulse Pro monthly"}]},
            }},
        }

        response = self.client.post(reverse("webhooks-stripe"), data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="sig_test")
        duplicate_response = self.client.post(reverse("webhooks-stripe"), data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="sig_test")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(duplicate_response.status_code, status.HTTP_200_OK)
        self.assertEqual(BillingNotification.objects.filter(stripe_invoice_id="in_paid_123").count(), 1)
        self.assertEqual(len(mail.outbox), 2)
        customer_receipt = next(email for email in mail.outbox if email.to == ["sub@example.com"])
        owner_notification = next(email for email in mail.outbox if email.to == ["owner@example.com"])
        self.assertIn("Your PutPulse Pro invoice", customer_receipt.subject)
        self.assertIn("29.00 USD", customer_receipt.body)
        self.assertIn("https://invoice.example/pdf", customer_receipt.body)
        self.assertIn("29.00 USD", owner_notification.body)

    @override_settings(
        BILLING_NOTIFICATION_EMAIL="",
        RESEND_API_KEY="test-resend-key",
        RESEND_API_URL="https://api.resend.com/emails",
        RESEND_FROM_EMAIL="admin@putpulse.com",
        EMAIL_TIMEOUT=20,
    )
    @patch("api.billing_email.requests.post")
    @patch("api.subscription_views.stripe.Webhook.construct_event")
    def test_paid_invoice_webhook_sends_customer_receipt_via_resend(
        self, mock_construct_event, mock_post
    ) -> None:
        mock_construct_event.return_value = {
            "type": "invoice.paid",
            "data": {"object": {
                "id": "in_resend_123", "number": "ABC-0002", "subscription": "sub_resend",
                "amount_paid": 2900, "currency": "usd",
                "hosted_invoice_url": "https://invoice.example/hosted",
                "invoice_pdf": "https://invoice.example/pdf",
                "lines": {"data": [{"description": "PutPulse Pro monthly"}]},
            }},
        }
        PremiumSubscription.objects.create(
            user=self.user,
            stripe_subscription_id="sub_resend",
            stripe_customer_id="cus_resend",
            status=PremiumSubscription.Status.ACTIVE,
        )

        response = self.client.post(reverse("webhooks-stripe"), data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="sig_test")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-resend-key")
        self.assertEqual(kwargs["json"]["from"], "admin@putpulse.com")
        self.assertEqual(kwargs["json"]["to"], ["sub@example.com"])
        self.assertIn("Your PutPulse Pro invoice", kwargs["json"]["subject"])
        self.assertIn("https://invoice.example/pdf", kwargs["json"]["text"])
        self.assertEqual(kwargs["timeout"], 20)
        mock_post.return_value.raise_for_status.assert_called_once_with()
