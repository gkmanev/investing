from rest_framework import viewsets

from .models import Investment, ScreenerFilter, ScreenerType
from .serializers import (
    InvestmentSerializer,
    ScreenerFilterSerializer,
    ScreenerTypeSerializer,
)


class InvestmentViewSet(viewsets.ModelViewSet):
    queryset = Investment.objects.all()
    serializer_class = InvestmentSerializer

    def perform_create(self, serializer: InvestmentSerializer) -> None:
        serializer.save()


class ScreenerTypeViewSet(viewsets.ModelViewSet):
    queryset = ScreenerType.objects.prefetch_related("filters").all()
    serializer_class = ScreenerTypeSerializer


class ScreenerFilterViewSet(viewsets.ModelViewSet):
    queryset = ScreenerFilter.objects.select_related("screener_type").all()
    serializer_class = ScreenerFilterSerializer
