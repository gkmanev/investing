from rest_framework import serializers

from .models import Investment


class InvestmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Investment
        fields = [
            "id",
            "name",
            "ticker",
            "category",
            "risk_level",
            "description",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_name(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Name cannot be empty.")
        return value

    def validate_ticker(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("Ticker cannot be empty.")
        return value.upper()
