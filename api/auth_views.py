import logging

from django.db import transaction
from django.db.models import Q
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
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
from .entitlements import serialize_plan_context
from .models import EmailVerificationToken, PremiumSubscription
from .subscription_views import PremiumSubscriptionSerializer


User = get_user_model()
logger = logging.getLogger(__name__)


def _premium_data(user) -> dict | None:
    try:
        return PremiumSubscriptionSerializer(user.premium_subscription).data
    except PremiumSubscription.DoesNotExist:
        return None


def _plan_data(user) -> dict:
    return serialize_plan_context(user)


def _build_auth_payload(user) -> tuple[dict, str]:
    refresh = RefreshToken.for_user(user)
    plan_data = _plan_data(user)
    return {
        "access": str(refresh.access_token),
        "user": UserSerializer(user).data,
        "premium_subscription": _premium_data(user),
        **plan_data,
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
    authentication_classes = []
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
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        payload, refresh_token = _build_auth_payload(serializer.validated_data["user"])
        response = Response(payload, status=status.HTTP_200_OK)
        _set_refresh_cookie(response, refresh_token)
        return response


class VerifyEmailView(APIView):
    authentication_classes = []
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
        user.date_joined = timezone.now()
        user.save(update_fields=["is_active", "date_joined"])
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
    authentication_classes = []
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
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        refresh_token = request.COOKIES.get(settings.AUTH_REFRESH_COOKIE_NAME)
        if not refresh_token:
            raise serializers.ValidationError({"detail": "Refresh token cookie is missing."})

        premium = None
        serialized_user = None
        plan_data = {
            "plan": "free",
            "trial_days_left": None,
            "has_full_access": False,
            "entitlements": {},
            "trial_expired": False,
        }
        try:
            token = RefreshToken(refresh_token)
            user_id = token.payload.get("user_id")
            if user_id:
                user = User.objects.filter(pk=user_id).first()
                if user is not None:
                    premium = _premium_data(user)
                    serialized_user = UserSerializer(user).data
                    plan_data = _plan_data(user)
        except Exception:
            pass

        serializer = TokenRefreshSerializer(data={"refresh": refresh_token})
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            return Response(
                {"detail": "Refresh token is invalid or expired."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        response = Response(
            {
                "access": serializer.validated_data["access"],
                "user": serialized_user,
                "premium_subscription": premium,
                **plan_data,
            },
            status=status.HTTP_200_OK,
        )

        rotated_refresh = serializer.validated_data.get("refresh")
        if rotated_refresh:
            _set_refresh_cookie(response, rotated_refresh)
        return response


class LogoutView(APIView):
    authentication_classes = []
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
    authentication_classes = [JWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request):
        plan_data = _plan_data(request.user)
        return Response(
            {
                **UserSerializer(request.user).data,
                "premium_subscription": _premium_data(request.user),
                **plan_data,
            },
            status=status.HTTP_200_OK,
        )


RegisterView = method_decorator(csrf_exempt, name="dispatch")(RegisterView)
LoginView = method_decorator(csrf_exempt, name="dispatch")(LoginView)
VerifyEmailView = method_decorator(csrf_exempt, name="dispatch")(VerifyEmailView)
ResendVerificationView = method_decorator(csrf_exempt, name="dispatch")(ResendVerificationView)
RefreshView = method_decorator(csrf_exempt, name="dispatch")(RefreshView)
LogoutView = method_decorator(csrf_exempt, name="dispatch")(LogoutView)
