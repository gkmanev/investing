from django.contrib import admin

from .models import Investment, ScreenerFilter, ScreenerType


@admin.register(Investment)
class InvestmentAdmin(admin.ModelAdmin):
    list_display = ("name", "ticker", "category", "risk_level", "created_at")
    search_fields = ("name", "ticker", "category")
    list_filter = ("risk_level", "category")


@admin.register(ScreenerType)
class ScreenerTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at", "updated_at")
    search_fields = ("name",)


@admin.register(ScreenerFilter)
class ScreenerFilterAdmin(admin.ModelAdmin):
    list_display = ("label", "screener_type", "display_order", "created_at")
    list_filter = ("screener_type",)
    search_fields = ("label", "screener_type__name")
