from rest_framework import serializers

from .models import DailyBrief, DailyBriefSubscription


class DailyBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyBrief
        fields = [
            "id",
            "edition_date",
            "rank",
            "symbol",
            "ticker",
            "score",
            "rsi",
            "roi",
            "delta",
            "is_alternative",
            "option_data",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class DailyBriefSubscriptionSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    source = serializers.SerializerMethodField()

    class Meta:
        model = DailyBriefSubscription
        fields = ["status", "is_active", "email", "source", "subscribed_at"]
        read_only_fields = fields

    def get_source(self, obj: DailyBriefSubscription) -> str | None:
        return obj.source or None


class DailyBriefSubscribeSerializer(serializers.Serializer):
    source = serializers.ChoiceField(
        choices=[
            DailyBriefSubscription.Source.SIGNUP,
            DailyBriefSubscription.Source.DAILY_BRIEF_CTA,
            DailyBriefSubscription.Source.PROFILE,
        ],
        required=False,
        default=DailyBriefSubscription.Source.DAILY_BRIEF_CTA,
    )
