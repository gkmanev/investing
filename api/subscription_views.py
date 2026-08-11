import datetime
import logging

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .entitlements import serialize_plan_context
from .billing_email import send_paid_invoice_notification
from .models import BillingNotification, PremiumSubscription

User = get_user_model()
logger = logging.getLogger(__name__)


class PremiumSubscriptionSerializer(serializers.ModelSerializer):
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = PremiumSubscription
        fields = [
            "status",
            "is_active",
            "start_date",
            "current_period_start",
            "current_period_end",
            "cancel_at_period_end",
            "cancel_at",
            "canceled_at",
            "ended_at",
            "created_at",
        ]
        read_only_fields = fields


class PremiumSubscriptionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscription_payload = None
        try:
            sub = request.user.premium_subscription
        except PremiumSubscription.DoesNotExist:
            sub = None
        if sub is not None:
            subscription_payload = PremiumSubscriptionSerializer(sub).data
        else:
            subscription_payload = {
                "status": None,
                "is_active": False,
                "start_date": None,
                "current_period_start": None,
                "current_period_end": None,
                "cancel_at_period_end": False,
                "cancel_at": None,
                "canceled_at": None,
                "ended_at": None,
                "created_at": None,
            }
        return Response(
            {
                "subscription": subscription_payload,
                **serialize_plan_context(request.user, subscription=sub),
            }
        )


class CreateStripeCheckoutSessionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        price_id = request.data.get("price_id")
        success_url = request.data.get("success_url")
        cancel_url = request.data.get("cancel_url")
        customer_email = request.data.get("customer_email")
        metadata = dict(request.data.get("metadata") or {})

        if not price_id or not success_url or not cancel_url:
            return Response(
                {"detail": "price_id, success_url, and cancel_url are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        metadata["user_id"] = str(request.user.pk)

        stripe.api_key = settings.STRIPE_SECRET_KEY

        try:
            session_kwargs = {
                "mode": "subscription",
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": metadata,
                "subscription_data": {"metadata": metadata},
            }
            if customer_email:
                session_kwargs["customer_email"] = customer_email

            session = stripe.checkout.Session.create(**session_kwargs)
        except stripe.StripeError as exc:
            logger.error("Stripe error creating checkout session: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response({"checkout_url": session.url})


class CreateStripeCustomerPortalSessionView(APIView):
    """Create a short-lived Stripe-hosted subscription-management URL."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            subscription = request.user.premium_subscription
        except PremiumSubscription.DoesNotExist:
            return Response(
                {"detail": "No subscription is available to manage."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not subscription.stripe_customer_id:
            logger.warning(
                "Cannot create Stripe portal session for user_id=%s: missing customer ID",
                request.user.id,
            )
            return Response(
                {"detail": "Subscription billing details are not available."},
                status=status.HTTP_409_CONFLICT,
            )

        stripe.api_key = settings.STRIPE_SECRET_KEY
        return_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/profile"
        try:
            session = stripe.billing_portal.Session.create(
                customer=subscription.stripe_customer_id,
                return_url=return_url,
            )
        except stripe.StripeError as exc:
            logger.error("Stripe error creating customer portal session: %s", exc)
            return Response(
                {"detail": "Unable to open subscription management."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response({"portal_url": session.url})


class StripeWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE", "")
        webhook_secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", "")

        if not webhook_secret:
            logger.error("STRIPE_WEBHOOK_SECRET is not configured")
            return Response(
                {"detail": "Webhook secret not configured."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        stripe.api_key = settings.STRIPE_SECRET_KEY

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
        except stripe.errors.SignatureVerificationError:
            logger.warning("Stripe webhook signature verification failed")
            return Response({"detail": "Invalid signature."}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response({"detail": "Invalid payload."}, status=status.HTTP_400_BAD_REQUEST)

        if event["type"] == "checkout.session.completed":
            try:
                self._handle_checkout_completed(event["data"]["object"])
            except Exception:
                logger.exception("Error processing checkout.session.completed")
                return Response(
                    {"detail": "Internal error."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        elif event["type"] in {
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        }:
            try:
                self._handle_subscription_event(event["data"]["object"])
            except Exception:
                logger.exception("Error processing %s", event["type"])
                return Response(
                    {"detail": "Internal error."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
        elif event["type"] == "invoice.paid":
            try:
                self._handle_paid_invoice(event["data"]["object"])
            except Exception:
                logger.exception("Error processing invoice.paid")
                return Response(
                    {"detail": "Internal error."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

        return Response({"detail": "OK."}, status=status.HTTP_200_OK)

    def _handle_checkout_completed(self, session: dict) -> None:
        metadata = session.get("metadata") or {}
        user_id = metadata.get("user_id")
        if not user_id:
            logger.warning("checkout.session.completed missing metadata.user_id")
            return

        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            logger.warning("Stripe webhook: user_id=%s not found", user_id)
            return

        stripe_subscription_id = session.get("subscription") or ""
        stripe_customer_id = session.get("customer") or ""
        subscription_synced = False

        if stripe_subscription_id:
            try:
                stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)
                self._sync_subscription_object(
                    stripe_sub,
                    fallback_user=user,
                    fallback_customer_id=stripe_customer_id,
                )
                subscription_synced = True
            except Exception:
                logger.exception(
                    "Failed to retrieve Stripe subscription %s", stripe_subscription_id
                )

        if not subscription_synced:
            PremiumSubscription.objects.update_or_create(
                user=user,
                defaults={
                    "stripe_subscription_id": stripe_subscription_id or f"checkout-{session.get('id')}",
                    "stripe_customer_id": stripe_customer_id,
                    "status": PremiumSubscription.Status.ACTIVE,
                    "current_period_end": None,
                },
            )

        # Stripe includes the first subscription invoice on a completed Checkout
        # Session. Sending from here means the customer gets their receipt even
        # if the invoice.paid webhook was not selected in the Stripe dashboard.
        # The invoice record below makes this safe when that webhook arrives too.
        invoice_id = session.get("invoice") or ""
        if invoice_id and session.get("payment_status") == "paid":
            invoice = stripe.Invoice.retrieve(invoice_id)
            if invoice.get("paid") or invoice.get("status") == "paid":
                self._handle_paid_invoice(invoice, fallback_user=user)

    def _handle_subscription_event(self, subscription: dict) -> None:
        # Recent Stripe API versions omit current_period_end from subscription
        # event payloads. Fetch the canonical subscription so the local billing
        # record retains the renewal/end date used by the profile UI.
        if not subscription.get("current_period_end") and subscription.get("id"):
            try:
                subscription = stripe.Subscription.retrieve(subscription["id"])
            except stripe.StripeError:
                logger.exception(
                    "Failed to retrieve Stripe subscription %s during webhook sync",
                    subscription.get("id"),
                )
        self._sync_subscription_object(subscription)

    def _handle_paid_invoice(self, invoice: dict, *, fallback_user=None) -> None:
        invoice_id = invoice.get("id")
        if not invoice_id:
            logger.warning("Stripe invoice.paid event missing invoice ID")
            return

        user = self._resolve_user_for_invoice(invoice) or fallback_user
        if user is None:
            logger.warning("Paid Stripe invoice notification skipped; could not resolve user for invoice=%s", invoice_id)
            return

        # Reserve the invoice before sending. The unique constraint makes webhook
        # redelivery harmless, including concurrent deliveries.
        try:
            with transaction.atomic():
                BillingNotification.objects.create(stripe_invoice_id=invoice_id, user=user)
        except IntegrityError:
            return

        try:
            send_paid_invoice_notification(user=user, invoice=invoice)
        except Exception:
            BillingNotification.objects.filter(stripe_invoice_id=invoice_id).delete()
            raise

    def _resolve_user_for_invoice(self, invoice: dict):
        parent_subscription = (
            (invoice.get("parent") or {}).get("subscription_details") or {}
        )
        subscription_id = (
            invoice.get("subscription")
            or parent_subscription.get("subscription")
            or ""
        )
        customer_id = invoice.get("customer") or ""
        subscription_details = invoice.get("subscription_details") or {}
        metadata = (
            subscription_details.get("metadata")
            or parent_subscription.get("metadata")
            or invoice.get("metadata")
            or {}
        )
        user_id = metadata.get("user_id")
        if user_id:
            try:
                return User.objects.get(pk=user_id)
            except User.DoesNotExist:
                logger.warning("Stripe invoice metadata user_id=%s not found", user_id)

        filters = {}
        if subscription_id:
            filters["stripe_subscription_id"] = subscription_id
        elif customer_id:
            filters["stripe_customer_id"] = customer_id
        if filters:
            existing = PremiumSubscription.objects.filter(**filters).select_related("user").first()
            if existing is not None:
                return existing.user
        return None

    def _sync_subscription_object(
        self,
        subscription: dict,
        *,
        fallback_user=None,
        fallback_customer_id: str = "",
    ) -> None:
        user = self._resolve_user_for_subscription(
            subscription,
            fallback_user=fallback_user,
        )
        if user is None:
            logger.warning(
                "Stripe subscription sync skipped; could not resolve user for subscription=%s customer=%s",
                subscription.get("id"),
                subscription.get("customer"),
            )
            return

        PremiumSubscription.objects.update_or_create(
            user=user,
            defaults={
                "stripe_subscription_id": subscription.get("id") or "",
                "stripe_customer_id": subscription.get("customer")
                or fallback_customer_id
                or "",
                "status": self._map_subscription_status(subscription.get("status")),
                "start_date": self._stripe_timestamp_to_datetime(subscription.get("start_date")),
                "current_period_start": self._stripe_timestamp_to_datetime(
                    subscription.get("current_period_start")
                ),
                "current_period_end": self._stripe_timestamp_to_datetime(
                    subscription.get("current_period_end")
                ),
                "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
                "cancel_at": self._stripe_timestamp_to_datetime(subscription.get("cancel_at")),
                "canceled_at": self._stripe_timestamp_to_datetime(
                    subscription.get("canceled_at")
                ),
                "ended_at": self._stripe_timestamp_to_datetime(subscription.get("ended_at")),
            },
        )

    def _resolve_user_for_subscription(self, subscription: dict, *, fallback_user=None):
        metadata = subscription.get("metadata") or {}
        user_id = metadata.get("user_id")
        if user_id:
            try:
                return User.objects.get(pk=user_id)
            except User.DoesNotExist:
                logger.warning("Stripe subscription metadata user_id=%s not found", user_id)

        subscription_id = subscription.get("id") or ""
        customer_id = subscription.get("customer") or ""

        existing = None
        if subscription_id:
            existing = PremiumSubscription.objects.filter(
                stripe_subscription_id=subscription_id
            ).select_related("user").first()
        if existing is None and customer_id:
            existing = PremiumSubscription.objects.filter(
                stripe_customer_id=customer_id
            ).select_related("user").first()
        if existing is not None:
            return existing.user

        return fallback_user

    def _stripe_timestamp_to_datetime(self, timestamp_value):
        if not timestamp_value:
            return None
        return datetime.datetime.fromtimestamp(timestamp_value, tz=datetime.timezone.utc)

    def _map_subscription_status(self, raw_status: str | None) -> str:
        normalized = str(raw_status or "").strip().lower()
        if normalized == "trialing":
            return PremiumSubscription.Status.TRIALING
        if normalized in {"past_due", "unpaid", "incomplete", "incomplete_expired"}:
            return PremiumSubscription.Status.PAST_DUE
        if normalized == "paused":
            return PremiumSubscription.Status.PAUSED
        if normalized in {"canceled", "cancelled"}:
            return PremiumSubscription.Status.CANCELLED
        return PremiumSubscription.Status.ACTIVE
