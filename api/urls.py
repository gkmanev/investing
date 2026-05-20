from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .auth_views import (
    LoginView,
    LogoutView,
    MeView,
    RefreshView,
    RegisterView,
    ResendVerificationView,
    VerifyEmailView,
)
from .daily_brief_views import (
    DailyBriefViewSet,
    DailyBriefSubscribeView,
    DailyBriefSubscriptionView,
    DailyBriefUnsubscribeView,
)
from .subscription_views import (
    CreateStripeCheckoutSessionView,
    PremiumSubscriptionView,
    StripeWebhookView,
)
from .agent_views import AgentView
from .views import (
    DueDiligenceReportViewSet,
    FinancialStatementViewSet,
    InvestmentViewSet,
    ScreenerFilterViewSet,
    ScreenerTypeViewSet,
    SymbolViewSet,
)

router = DefaultRouter()
router.register("investments", InvestmentViewSet)
router.register("symbols", SymbolViewSet)
router.register("screener-types", ScreenerTypeViewSet)
router.register("screener-filters", ScreenerFilterViewSet)
router.register("financial-statements", FinancialStatementViewSet)
router.register("due-diligence-reports", DueDiligenceReportViewSet)
router.register("daily-briefs", DailyBriefViewSet)

urlpatterns = [
    path("auth/register/", RegisterView.as_view(), name="auth-register"),
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/verify-email/", VerifyEmailView.as_view(), name="auth-verify-email"),
    path(
        "auth/resend-verification/",
        ResendVerificationView.as_view(),
        name="auth-resend-verification",
    ),
    path("auth/refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path(
        "daily-brief-subscription/",
        DailyBriefSubscriptionView.as_view(),
        name="daily-brief-subscription",
    ),
    path(
        "daily-brief-subscription/subscribe/",
        DailyBriefSubscribeView.as_view(),
        name="daily-brief-subscription-subscribe",
    ),
    path(
        "daily-brief-subscription/unsubscribe/",
        DailyBriefUnsubscribeView.as_view(),
        name="daily-brief-subscription-unsubscribe",
    ),
    path("premium-subscription/", PremiumSubscriptionView.as_view(), name="premium-subscription"),
    path(
        "create-stripe-checkout-session/",
        CreateStripeCheckoutSessionView.as_view(),
        name="create-stripe-checkout-session",
    ),
    path("webhooks/stripe/", StripeWebhookView.as_view(), name="webhooks-stripe"),
    path("agent/", AgentView.as_view(), name="agent"),
    path("", include(router.urls)),
]
