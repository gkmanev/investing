from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import (
    InvestmentViewSet,
    ScreenerFilterViewSet,
    ScreenerTypeViewSet,
)

router = DefaultRouter()
router.register("investments", InvestmentViewSet)
router.register("screener-types", ScreenerTypeViewSet)
router.register("screener-filters", ScreenerFilterViewSet)

urlpatterns = [path("", include(router.urls))]
