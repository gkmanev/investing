import datetime
import logging

import stripe
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

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
        try:
            sub = request.user.premium_subscription
            return Response(PremiumSubscriptionSerializer(sub).data)
        except PremiumSubscription.DoesNotExist:
            return Response(
                {"status": None, "is_active": False, "current_period_end": None, "created_at": None}
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

        current_period_end = None
        if stripe_subscription_id:
            try:
                stripe_sub = stripe.Subscription.retrieve(stripe_subscription_id)
                ts = stripe_sub.get("current_period_end")
                if ts:
                    current_period_end = datetime.datetime.fromtimestamp(
                        ts, tz=datetime.timezone.utc
                    )
            except Exception:
                logger.exception(
                    "Failed to retrieve Stripe subscription %s", stripe_subscription_id
                )

        PremiumSubscription.objects.update_or_create(
            user=user,
            defaults={
                "stripe_subscription_id": stripe_subscription_id,
                "stripe_customer_id": stripe_customer_id,
                "status": PremiumSubscription.Status.ACTIVE,
                "current_period_end": current_period_end,
            },
        )
