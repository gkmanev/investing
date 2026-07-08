import datetime
import logging

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .entitlements import serialize_plan_context
from .models import PremiumSubscription

User = get_user_model()
logger = logging.getLogger(__name__)


class PremiumSubscriptionSerializer(serializers.ModelSerializer):
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = PremiumSubscription
        fields = ["status", "is_active", "current_period_end", "created_at"]
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
                "current_period_end": None,
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

        if stripe_subscription_id:
            try:
                stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)
                self._sync_subscription_object(
                    stripe_sub,
                    fallback_user=user,
                    fallback_customer_id=stripe_customer_id,
                )
                return
            except Exception:
                logger.exception(
                    "Failed to retrieve Stripe subscription %s", stripe_subscription_id
                )

        PremiumSubscription.objects.update_or_create(
            user=user,
            defaults={
                "stripe_subscription_id": stripe_subscription_id or f"checkout-{session.get('id')}",
                "stripe_customer_id": stripe_customer_id,
                "status": PremiumSubscription.Status.ACTIVE,
                "current_period_end": None,
            },
        )

    def _handle_subscription_event(self, subscription: dict) -> None:
        self._sync_subscription_object(subscription)

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
                "current_period_end": self._stripe_timestamp_to_datetime(
                    subscription.get("current_period_end")
                ),
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
