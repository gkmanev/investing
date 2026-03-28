from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .daily_brief_serializers import (
    DailyBriefSubscribeSerializer,
    DailyBriefSubscriptionSerializer,
)
from .daily_brief_services import (
    get_or_create_subscription,
    subscribe_user,
    unsubscribe_user,
)


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
