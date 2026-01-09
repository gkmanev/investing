from rest_framework import serializers

from .models import (
    DueDiligenceReport,
    FinancialStatement,
    Investment,
    ScreenerFilter,
    ScreenerType,
)


class InvestmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Investment
        fields = [
            "id",
            "ticker",
            "category",
            "screener_type",
            "price",
            "volume",
            "market_cap",
            "options_suitability",
            "option_exp",
            "description",
            "opt_val",
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


class FinancialStatementSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialStatement
        fields = [
            "id",
            "symbol",
            "target_currency",
            "period_type",
            "statement_type",
            "payload",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_symbol(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Symbol cannot be empty.")
        return cleaned.upper()


class DueDiligenceReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = DueDiligenceReport
        fields = [
            "id",
            "symbol",
            "rating",
            "confidence",
            "model_name",
            "report",
            "financial_data",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_symbol(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise serializers.ValidationError("Symbol cannot be empty.")
        return cleaned.upper()
