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
    DailyBriefSubscribeView,
    DailyBriefSubscriptionView,
    DailyBriefUnsubscribeView,
)
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
    path("", include(router.urls)),
]
