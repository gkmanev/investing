from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping

from django.db.models import Q, Value
from django.db.models.functions import Replace
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .models import (
    CboeSecurity,
    DueDiligenceReport,
    FinancialStatement,
    Investment,
    PremiumSubscription,
    ScreenerFilter,
    ScreenerType,
    Symbol,
)
from .permissions import IsStaffOrReadOnly

SYMBOL_FILTER_PARAMS = {
    "ticker", "exchange", "classification", "liquidity",
    "technical_score", "technical_score_in", "initial_suitability",
    "price", "min_price", "max_price",
    "min_market_cap", "max_market_cap",
    "min_rsi", "max_rsi",
    "min_roi", "max_roi",
    "min_option_iv", "max_option_iv",
    "min_option_volume", "max_option_volume",
    "min_score", "max_score",
}


def _user_is_premium(user) -> bool:
    if not hasattr(user, "is_authenticated") or not user.is_authenticated:
        return False
    if user.is_staff:
        return True
    try:
        return user.premium_subscription.is_active
    except PremiumSubscription.DoesNotExist:
        return False
from .serializers import (
    DueDiligenceReportSerializer,
    FinancialStatementSerializer,
    InvestmentSerializer,
    ScreenerFilterSerializer,
    ScreenerTypeSerializer,
    SymbolSerializer,
)


class FilterMixin:
    """Reusable query-param filtering helpers for ModelViewSets."""

    def _apply_text_in_filter(
        self, queryset, params: Mapping[str, str], *, field_name: str, param_name: str
    ):
        raw_value = params.get(param_name)
        if raw_value is None:
            return queryset

        values = [value.strip() for value in raw_value.split(",") if value.strip()]
        if not values:
            return queryset

        query = Q()
        for value in values:
            query |= Q(**{f"{field_name}__iexact": value})
        return queryset.filter(query)

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


    def _apply_integer_range_filter(
        self,
        queryset,
        params: Mapping[str, str],
        *,
        field_name: str,
        min_param: str | None = None,
        max_param: str | None = None,
    ):
        min_value = self._parse_integer(params.get(min_param), min_param) if min_param else None
        max_value = self._parse_integer(params.get(max_param), max_param) if max_param else None

        if min_value is not None and max_value is not None and min_value > max_value:
            raise ValidationError(
                {max_param: "Maximum value must be greater than or equal to minimum value."}
            )

        if min_value is not None:
            queryset = queryset.filter(**{f"{field_name}__gte": min_value})
        if max_value is not None:
            queryset = queryset.filter(**{f"{field_name}__lte": max_value})
        return queryset

    def _apply_boolean_filter(
        self, queryset, params: Mapping[str, str], *, field_name: str, param_name: str
    ):
        value = self._parse_boolean(params.get(param_name), param_name)
        if value is None:
            return queryset
        return queryset.filter(**{field_name: value})

    def _apply_cboe_filter(self, queryset, params: Mapping[str, str], *, param_name: str):
        value = self._parse_boolean(params.get(param_name), param_name)
        if value is None:
            return queryset

        cboe_symbols = CboeSecurity.objects.values_list("symbol", flat=True)
        if value:
            return queryset.filter(ticker__in=cboe_symbols)
        return queryset.exclude(ticker__in=cboe_symbols)

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


class SymbolPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.page.paginator.count,
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "has_more": self.page.has_next(),
                "page": self.page.number,
                "page_size": self.get_page_size(self.request),
                "results": data,
            }
        )


class InvestmentViewSet(FilterMixin, viewsets.ModelViewSet):
    """CRUD viewset that also supports lightweight filtering."""

    queryset = Investment.objects.all()
    serializer_class = InvestmentSerializer
    permission_classes = [IsStaffOrReadOnly]

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
        queryset = super().get_queryset().prefetch_related("expiration_snapshots")
        params = self.request.query_params

        category = params.get("category")
        if category:
            queryset = queryset.filter(category__iexact=category)

        screener_type = params.get("screener_type") or params.get("screenter_type")
        if screener_type:
            normalized_screener_type = " ".join(screener_type.split())
            compact_screener_type = normalized_screener_type.replace(" ", "")
            queryset = queryset.annotate(
                screener_type_compact=Replace("screener_type", Value(" "), Value(""))
            ).filter(
                Q(screener_type__iexact=normalized_screener_type)
                | Q(screener_type_compact__iexact=compact_screener_type)
            )

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
        queryset = self._apply_cboe_filter(queryset, params, param_name="cboe")

        return queryset

    def perform_create(self, serializer: InvestmentSerializer) -> None:
        serializer.save()


class SymbolViewSet(FilterMixin, viewsets.ModelViewSet):
    """CRUD viewset for Symbol with query-param filtering."""

    queryset = Symbol.objects.all()
    serializer_class = SymbolSerializer
    permission_classes = [IsStaffOrReadOnly]
    pagination_class = SymbolPagination

    def list(self, request, *args, **kwargs):  # type: ignore[override]
        if set(request.query_params) & SYMBOL_FILTER_PARAMS and not _user_is_premium(request.user):
            raise PermissionDenied("Filters are available for premium subscribers only.")
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(
            {
                "count": len(serializer.data),
                "next": None,
                "previous": None,
                "has_more": False,
                "page": 1,
                "page_size": len(serializer.data),
                "results": serializer.data,
            }
        )

    def get_queryset(self):  # type: ignore[override]
        queryset = super().get_queryset()
        params = self.request.query_params

        ticker_query = params.get("ticker")
        if ticker_query:
            queryset = queryset.filter(ticker__icontains=ticker_query)

        exchange = params.get("exchange")
        if exchange:
            queryset = queryset.filter(exchange__iexact=exchange)

        classification = params.get("classification")
        if classification:
            queryset = queryset.filter(classification__iexact=classification)

        liquidity = params.get("liquidity")
        if liquidity:
            queryset = queryset.filter(liquidity__iexact=liquidity)

        technical_score = params.get("technical_score")
        if technical_score:
            queryset = queryset.filter(technical_score__iexact=technical_score)
        queryset = self._apply_text_in_filter(
            queryset,
            params,
            field_name="technical_score",
            param_name="technical_score_in",
        )

        queryset = self._apply_boolean_filter(
            queryset, params, field_name="initial_suitability", param_name="initial_suitability"
        )
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
        queryset = self._apply_decimal_filter(
            queryset,
            params,
            field_name="roi",
            min_param="min_roi",
            max_param="max_roi",
        )
        queryset = self._apply_decimal_filter(
            queryset,
            params,
            field_name="option_iv",
            min_param="min_option_iv",
            max_param="max_option_iv",
        )
        queryset = self._apply_integer_range_filter(
            queryset,
            params,
            field_name="option_volume",
            min_param="min_option_volume",
            max_param="max_option_volume",
        )
        queryset = self._apply_integer_range_filter(
            queryset,
            params,
            field_name="score",
            min_param="min_score",
            max_param="max_score",
        )

        return queryset


class ScreenerTypeViewSet(viewsets.ModelViewSet):
    queryset = ScreenerType.objects.prefetch_related("filters").all()
    serializer_class = ScreenerTypeSerializer
    permission_classes = [IsStaffOrReadOnly]


class ScreenerFilterViewSet(viewsets.ModelViewSet):
    queryset = ScreenerFilter.objects.select_related("screener_type").all()
    serializer_class = ScreenerFilterSerializer
    permission_classes = [IsStaffOrReadOnly]


class FinancialStatementViewSet(viewsets.ModelViewSet):
    queryset = FinancialStatement.objects.all()
    serializer_class = FinancialStatementSerializer
    permission_classes = [IsStaffOrReadOnly]

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
    permission_classes = [IsStaffOrReadOnly]

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
