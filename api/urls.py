from django.urls import include, path
from rest_framework.routers import DefaultRouter

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

urlpatterns = [path("", include(router.urls))]
