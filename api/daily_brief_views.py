from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .daily_brief_serializers import (
    DailyBriefSerializer,
    DailyBriefSubscribeSerializer,
    DailyBriefSubscriptionSerializer,
)
from .daily_brief_services import (
    get_or_create_subscription,
    subscribe_user,
    unsubscribe_user,
)
from .models import DailyBrief
from .permissions import IsStaffOrReadOnly


class DailyBriefViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DailyBrief.objects.select_related("symbol")
    serializer_class = DailyBriefSerializer
    permission_classes = [IsStaffOrReadOnly]

    def get_queryset(self):  # type: ignore[override]
        queryset = super().get_queryset()
        params = self.request.query_params

        edition_date = params.get("edition_date")
        if edition_date:
            queryset = queryset.filter(edition_date=edition_date)

        ticker = params.get("ticker")
        if ticker:
            queryset = queryset.filter(ticker__icontains=ticker)

        is_alternative = params.get("is_alternative")
        if is_alternative is not None:
            normalized = is_alternative.strip().lower()
            if normalized in {"true", "1", "yes"}:
                queryset = queryset.filter(is_alternative=True)
            elif normalized in {"false", "0", "no"}:
                queryset = queryset.filter(is_alternative=False)

        return queryset


class DailyBriefSubscriptionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        subscription = get_or_create_subscription(request.user)
        return Response(DailyBriefSubscriptionSerializer(subscription).data)


class DailyBriefSubscribeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DailyBriefSubscribeSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        subscription = subscribe_user(
            request.user,
            source=serializer.validated_data["source"],
        )
        return Response(DailyBriefSubscriptionSerializer(subscription).data)


class DailyBriefUnsubscribeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        subscription = unsubscribe_user(request.user)
        return Response(DailyBriefSubscriptionSerializer(subscription).data)
