from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping

from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from .models import (
    DueDiligenceReport,
    FinancialStatement,
    Investment,
    ScreenerFilter,
    ScreenerType,
)
from .serializers import (
    DueDiligenceReportSerializer,
    FinancialStatementSerializer,
    InvestmentSerializer,
    ScreenerFilterSerializer,
    ScreenerTypeSerializer,
)


class InvestmentViewSet(viewsets.ModelViewSet):
    """CRUD viewset that also supports lightweight filtering."""

    queryset = Investment.objects.all()
    serializer_class = InvestmentSerializer

    def list(self, request, *args, **kwargs):  # type: ignore[override]
        screener_type = request.query_params.get("screener_type") or request.query_params.get(
            "screenter_type"
        )
        if screener_type and screener_type.strip().lower() == "custom screener filter":
            queryset = self.filter_queryset(self.get_queryset())
            serializer = self.get_serializer(queryset, many=True)
            return Response({"count": len(serializer.data), "results": serializer.data})

        return super().list(request, *args, **kwargs)

    def get_queryset(self):  # type: ignore[override]
        queryset = super().get_queryset()
        params = self.request.query_params

        category = params.get("category")
        if category:
            queryset = queryset.filter(category__iexact=category)

        screener_type = params.get("screener_type") or params.get("screenter_type")
        if screener_type:
            queryset = queryset.filter(screener_type__iexact=screener_type)

        ticker_query = params.get("ticker")
        if ticker_query:
            queryset = queryset.filter(ticker__icontains=ticker_query)

        queryset = self._apply_decimal_filter(
            queryset,
            params,
            field_name="price",
            exact_param="price",
            min_param="min_price",
            max_param="max_price",
        )
        queryset = self._apply_decimal_filter(
            queryset,
            params,
            field_name="roi",
            exact_param="roi",
            min_param="min_roi",
            max_param="max_roi",
        )
        queryset = self._apply_decimal_filter(
            queryset,
            params,
            field_name="delta",
            exact_param="delta",
            min_param="min_delta",
            max_param="max_delta",
        )
        queryset = self._apply_decimal_filter(
            queryset,
            params,
            field_name="market_cap",
            min_param="min_market_cap",
            max_param="max_market_cap",
        )
        queryset = self._apply_decimal_filter(
            queryset,
            params,
            field_name="rsi",
            min_param="min_rsi",
            max_param="max_rsi",
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
        queryset = self._apply_boolean_filter(
            queryset,
            params,
            field_name="weekly_options",
            param_name="weekly_options",
        )

        return queryset

    def perform_create(self, serializer: InvestmentSerializer) -> None:
        serializer.save()

    def _apply_decimal_filter(
        self,
        queryset,
        params: Mapping[str, str],
        *,
        field_name: str,
        exact_param: str | None = None,
        min_param: str | None = None,
        max_param: str | None = None,
    ):
        exact_value = (
            self._parse_decimal(params.get(exact_param), exact_param) if exact_param else None
        )
        min_value = self._parse_decimal(params.get(min_param), min_param) if min_param else None
        max_value = self._parse_decimal(params.get(max_param), max_param) if max_param else None

        if min_value is not None and max_value is not None and min_value > max_value:
            raise ValidationError(
                {max_param: "Maximum value must be greater than or equal to minimum value."}
            )

        if exact_value is not None:
            queryset = queryset.filter(**{field_name: exact_value})
        if min_value is not None:
            queryset = queryset.filter(**{f"{field_name}__gte": min_value})
        if max_value is not None:
            queryset = queryset.filter(**{f"{field_name}__lte": max_value})
        return queryset

    def _apply_integer_min_filter(
        self, queryset, params: Mapping[str, str], *, field_name: str, param_name: str
    ):
        value = self._parse_integer(params.get(param_name), param_name)
        if value is None:
            return queryset

        return queryset.filter(**{f"{field_name}__gte": value})

    def _apply_integer_exact_filter(
        self, queryset, params: Mapping[str, str], *, field_name: str, param_name: str
    ):
        value = self._parse_integer(params.get(param_name), param_name)
        if value is None:
            return queryset

        return queryset.filter(**{field_name: value})

    def _apply_boolean_filter(
        self, queryset, params: Mapping[str, str], *, field_name: str, param_name: str
    ):
        value = self._parse_boolean(params.get(param_name), param_name)
        if value is None:
            return queryset

        return queryset.filter(**{field_name: value})

    def _parse_decimal(self, raw_value: str | None, field: str) -> Decimal | None:
        if raw_value is None:
            return None
        try:
            value = Decimal(raw_value)
        except (InvalidOperation, TypeError):
            raise ValidationError({field: "Enter a valid number."})
        return value

    def _parse_integer(self, raw_value: str | None, field: str) -> int | None:
        if raw_value is None:
            return None

        try:
            return int(raw_value)
        except (TypeError, ValueError):
            raise ValidationError({field: "Enter a valid integer."})

    def _parse_boolean(self, raw_value: str | None, field: str) -> bool | None:
        if raw_value is None:
            return None

        normalized_value = raw_value.strip().lower()
        if normalized_value in {"true", "1", "yes", "y"}:
            return True
        if normalized_value in {"false", "0", "no", "n"}:
            return False

        raise ValidationError({field: "Enter a valid boolean."})


class ScreenerTypeViewSet(viewsets.ModelViewSet):
    queryset = ScreenerType.objects.prefetch_related("filters").all()
    serializer_class = ScreenerTypeSerializer


class ScreenerFilterViewSet(viewsets.ModelViewSet):
    queryset = ScreenerFilter.objects.select_related("screener_type").all()
    serializer_class = ScreenerFilterSerializer


class FinancialStatementViewSet(viewsets.ModelViewSet):
    queryset = FinancialStatement.objects.all()
    serializer_class = FinancialStatementSerializer

    def get_queryset(self):  # type: ignore[override]
        queryset = super().get_queryset()
        params = self.request.query_params

        symbol = params.get("symbol")
        if symbol:
            queryset = queryset.filter(symbol__iexact=symbol)

        target_currency = params.get("target_currency")
        if target_currency:
            queryset = queryset.filter(target_currency__iexact=target_currency)

        period_type = params.get("period_type")
        if period_type:
            queryset = queryset.filter(period_type__iexact=period_type)

        statement_type = params.get("statement_type")
        if statement_type:
            queryset = queryset.filter(statement_type__iexact=statement_type)

        return queryset


class DueDiligenceReportViewSet(viewsets.ModelViewSet):
    queryset = DueDiligenceReport.objects.all()
    serializer_class = DueDiligenceReportSerializer

    def get_queryset(self):  # type: ignore[override]
        queryset = super().get_queryset()
        params = self.request.query_params

        symbol = params.get("symbol")
        if symbol:
            queryset = queryset.filter(symbol__iexact=symbol)

        rating = params.get("rating")
        if rating:
            queryset = queryset.filter(rating__iexact=rating)

        model_name = params.get("model_name")
        if model_name:
            queryset = queryset.filter(model_name__iexact=model_name)

        return queryset
