from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping

from rest_framework import viewsets
from rest_framework.exceptions import ValidationError

from .models import Investment, ScreenerFilter, ScreenerType
from .serializers import (
    InvestmentSerializer,
    ScreenerFilterSerializer,
    ScreenerTypeSerializer,
)


class InvestmentViewSet(viewsets.ModelViewSet):
    """CRUD viewset that also supports lightweight filtering."""

    queryset = Investment.objects.all()
    serializer_class = InvestmentSerializer

    def get_queryset(self):  # type: ignore[override]
        queryset = super().get_queryset()
        params = self.request.query_params

        category = params.get("category")
        if category:
            queryset = queryset.filter(category__iexact=category)

        screenter_type = params.get("screenter_type")
        if screenter_type:
            queryset = queryset.filter(screenter_type__iexact=screenter_type)

        ticker_query = params.get("ticker")
        if ticker_query:
            queryset = queryset.filter(ticker__icontains=ticker_query)

        queryset = self._apply_decimal_range_filter(
            queryset, params, field_name="price", min_param="min_price", max_param="max_price"
        )
        queryset = self._apply_decimal_range_filter(
            queryset,
            params,
            field_name="market_cap",
            min_param="min_market_cap",
            max_param="max_market_cap",
        )
        queryset = self._apply_integer_min_filter(
            queryset, params, field_name="volume", param_name="min_volume"
        )
        queryset = self._apply_integer_exact_filter(
            queryset,
            params,
            field_name="options_suitability",
            param_name="options_suitability",
        )

        return queryset

    def perform_create(self, serializer: InvestmentSerializer) -> None:
        serializer.save()

    def _apply_decimal_range_filter(
        self,
        queryset,
        params: Mapping[str, str],
        *,
        field_name: str,
        min_param: str,
        max_param: str,
    ):
        min_value = self._parse_decimal(params.get(min_param), min_param)
        max_value = self._parse_decimal(params.get(max_param), max_param)

        if min_value is not None and max_value is not None and min_value > max_value:
            raise ValidationError(
                {max_param: "Maximum value must be greater than or equal to minimum value."}
            )

        if min_value is not None:
            queryset = queryset.filter(**{f"{field_name}__gte": min_value})
        if max_value is not None:
            queryset = queryset.filter(**{f"{field_name}__lte": max_value})
        return queryset

    def _apply_integer_min_filter(
        self, queryset, params: Mapping[str, str], *, field_name: str, param_name: str
    ):
        raw_value = params.get(param_name)
        if raw_value is None:
            return queryset

        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            raise ValidationError({param_name: "Enter a valid integer."})

        return queryset.filter(**{f"{field_name}__gte": value})

    def _apply_integer_exact_filter(
        self, queryset, params: Mapping[str, str], *, field_name: str, param_name: str
    ):
        raw_value = params.get(param_name)
        if raw_value is None:
            return queryset

        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            raise ValidationError({param_name: "Enter a valid integer."})

        return queryset.filter(**{field_name: value})

    def _parse_decimal(self, raw_value: str | None, field: str) -> Decimal | None:
        if raw_value is None:
            return None
        try:
            value = Decimal(raw_value)
        except (InvalidOperation, TypeError):
            raise ValidationError({field: "Enter a valid number."})
        return value


class ScreenerTypeViewSet(viewsets.ModelViewSet):
    queryset = ScreenerType.objects.prefetch_related("filters").all()
    serializer_class = ScreenerTypeSerializer


class ScreenerFilterViewSet(viewsets.ModelViewSet):
    queryset = ScreenerFilter.objects.select_related("screener_type").all()
    serializer_class = ScreenerFilterSerializer
