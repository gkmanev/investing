import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .auth_email import create_email_verification_token, send_verification_email
from .auth_serializers import (
    LoginSerializer,
    RegisterSerializer,
    ResendVerificationSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)
from .daily_brief_services import (
    activate_pending_subscription_after_verification,
    subscribe_user,
)
from .models import EmailVerificationToken, PremiumSubscription
from .subscription_views import PremiumSubscriptionSerializer


User = get_user_model()
logger = logging.getLogger(__name__)


def _premium_data(user) -> dict | None:
    try:
        return PremiumSubscriptionSerializer(user.premium_subscription).data
    except PremiumSubscription.DoesNotExist:
        return None


def _build_auth_payload(user) -> tuple[dict, str]:
    refresh = RefreshToken.for_user(user)
    return {
        "access": str(refresh.access_token),
        "user": UserSerializer(user).data,
        "premium_subscription": _premium_data(user),
    }, str(refresh)


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        refresh_token,
        httponly=True,
        secure=settings.AUTH_REFRESH_COOKIE_SECURE,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        domain=settings.AUTH_REFRESH_COOKIE_DOMAIN,
        max_age=settings.AUTH_REFRESH_COOKIE_MAX_AGE,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.AUTH_REFRESH_COOKIE_NAME,
        path=settings.AUTH_REFRESH_COOKIE_PATH,
        domain=settings.AUTH_REFRESH_COOKIE_DOMAIN,
        samesite=settings.AUTH_REFRESH_COOKIE_SAMESITE,
    )


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.AUTH_ALLOW_PUBLIC_REGISTRATION:
            return Response(
                {"detail": "Public registration is disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        daily_brief_opt_in = serializer.validated_data.get("daily_brief_opt_in", False)
        try:
            with transaction.atomic():
                user = serializer.save()
                if daily_brief_opt_in:
                    subscribe_user(user, source="signup")
                token_obj = create_email_verification_token(user=user)
                send_verification_email(user=user, token_obj=token_obj)
        except Exception:
            logger.exception(
                "Registration email delivery failed for username=%s",
                serializer.validated_data.get("username"),
            )
            return Response(
                {
                    "detail": (
                        "We could not send the verification email right now. "
                        "Please try again shortly."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "detail": "Verification email sent. Please confirm your email before logging in.",
                "requires_verification": True,
                "user": UserSerializer(user).data,
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        payload, refresh_token = _build_auth_payload(serializer.validated_data["user"])
        response = Response(payload, status=status.HTTP_200_OK)
        _set_refresh_cookie(response, refresh_token)
        return response


class VerifyEmailView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data, context={})
        serializer.is_valid(raise_exception=True)

        token_obj = serializer.context["token_obj"]
        if token_obj.expires_at <= timezone.now():
            token_obj.delete()
            raise serializers.ValidationError({"token": "Verification token has expired."})

        user = token_obj.user
        user.is_active = True
        user.save(update_fields=["is_active"])
        activate_pending_subscription_after_verification(user)
        EmailVerificationToken.objects.filter(user=user).delete()

        payload, refresh_token = _build_auth_payload(user)
        response = Response(
            {
                "detail": "Email verified successfully.",
                **payload,
            },
            status=status.HTTP_200_OK,
        )
        _set_refresh_cookie(response, refresh_token)
        return response


class ResendVerificationView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ResendVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        identifier = serializer.validated_data["identifier"]
        user = User.objects.filter(
            Q(username__iexact=identifier) | Q(email__iexact=identifier)
        ).first()
        generic_message = (
            "If an unverified account exists for that identifier, a verification email has "
            "been sent."
        )
        if user is None or user.is_active:
            return Response({"detail": generic_message}, status=status.HTTP_200_OK)

        latest_token = user.email_verification_tokens.order_by("-created_at").first()
        if (
            latest_token is not None
            and (timezone.now() - latest_token.created_at).total_seconds()
            < settings.AUTH_RESEND_COOLDOWN_SECONDS
        ):
            return Response(
                {
                    "detail": (
                        "A verification email was sent recently. Please wait before requesting "
                        "another one."
                    )
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            with transaction.atomic():
                token_obj = create_email_verification_token(user=user)
                send_verification_email(user=user, token_obj=token_obj)
        except Exception:
            logger.exception(
                "Verification email resend failed for user_id=%s",
                user.id,
            )
            return Response(
                {
                    "detail": (
                        "We could not send the verification email right now. "
                        "Please try again shortly."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({"detail": generic_message}, status=status.HTTP_200_OK)


class RefreshView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        refresh_token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME)
        if not refresh_token:
            raise serializers.ValidationError({"detail": "Refresh token cookie is missing."})

        serializer = TokenRefreshSerializer(data={"refresh": refresh_token})
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            raise InvalidToken(str(exc)) from exc

        # Resolve user to attach current premium_subscription state
        premium = None
        try:
            token = RefreshToken(refresh_token)
            user_id = token.payload.get("user_id")
            if user_id:
                user = User.objects.select_related("premium_subscription").get(pk=user_id)
                premium = _premium_data(user)
        except Exception:
            pass

        response = Response(
            {"access": serializer.validated_data["access"], "premium_subscription": premium},
            status=status.HTTP_200_OK,
        )

        rotated_refresh = serializer.validated_data.get("refresh")
        if rotated_refresh:
            _set_refresh_cookie(response, rotated_refresh)
        return response


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        refresh_token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME)
        if refresh_token:
            try:
                token = RefreshToken(refresh_token)
                token.blacklist()
            except TokenError:
                pass

        response = Response(status=status.HTTP_204_NO_CONTENT)
        _clear_refresh_cookie(response)
        return response


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data, status=status.HTTP_200_OK)
