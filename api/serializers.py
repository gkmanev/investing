from rest_framework import serializers

from .models import Investment, ScreenerFilter, ScreenerType


class InvestmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Investment
        fields = [
            "id",
            "ticker",
            "category",
            "price",
            "volume",
            "market_cap",
            "description",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_ticker(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Ticker cannot be empty.")
        return value.upper()


class ScreenerFilterSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScreenerFilter
        fields = [
            "id",
            "screener_type",
            "label",
            "payload",
            "display_order",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_label(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Label cannot be empty.")
        return value


class ScreenerTypeSerializer(serializers.ModelSerializer):
    filters = ScreenerFilterSerializer(many=True, read_only=True)

    class Meta:
        model = ScreenerType
        fields = [
            "id",
            "name",
            "description",
            "created_at",
            "updated_at",
            "filters",
        ]
        read_only_fields = ["id", "created_at", "updated_at", "filters"]

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Name cannot be empty.")
        return value
